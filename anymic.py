#!/usr/bin/env python3
"""
AnyMic — turn any phone into a microphone, over the browser.

No phone app. The phone's browser captures the mic and streams raw PCM over a
WebSocket; this server feeds it into a virtual audio device that every app sees
as a normal microphone.

    python3 anymic.py

Then open the printed https:// URL on the phone and tap Start.

Linux creates the virtual microphone itself, through PipeWire or PulseAudio.
Windows and macOS have no user-mode way to invent a capture device — that takes
a driver — so there AnyMic plays into a virtual audio cable you install once
(VB-CABLE or BlackHole) and apps record from the other end of it.
"""

import argparse
import base64
import errno
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import signal
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

WINDOWS = sys.platform == "win32"
MACOS = sys.platform == "darwin"
LINUX = not WINDOWS and not MACOS

RATE = 48000
CHANNELS = 1
SAMPLE_BYTES = 2
BYTES_PER_SEC = RATE * CHANNELS * SAMPLE_BYTES
CHUNK_MS = 10
CHUNK_BYTES = BYTES_PER_SEC * CHUNK_MS // 1000

# The phone's clock and ours never agree exactly, and WiFi delivers in bursts.
# We hold a small buffer so brief jitter doesn't reach the audio graph. Too
# small and every hiccup punches a hole in the audio; too large and you hear
# yourself late. 120ms is comfortably above typical WiFi jitter.
PREBUFFER_MS = 120
MAX_BUFFER_MS = 400


QUIET = False       # --check reports for itself; the running commentary is noise


def log(msg):
    if not QUIET:
        print(f"[anymic] {msg}", flush=True)


# --------------------------------------------------------------------------
# Audio buffering
# --------------------------------------------------------------------------

class JitterBuffer:
    """Absorbs network jitter between the phone and the audio graph.

    Underruns yield silence rather than stalling the writer. A stalled writer
    is what produces the classic chopped "robot voice" — the consumer keeps
    pulling at 48kHz whether or not anything arrived, so a gap becomes a hole
    in the waveform. Silence at least keeps the stream phase-correct.
    """

    def __init__(self):
        self._buf = bytearray()
        self._lock = threading.Lock()
        self.underruns = 0
        self.dropped_bytes = 0
        self.total_in = 0
        self.primed = False

    def push(self, data):
        with self._lock:
            self._buf += data
            self.total_in += len(data)
            limit = MAX_BUFFER_MS * BYTES_PER_SEC // 1000
            if len(self._buf) > limit:
                # Latency has crept past what we're willing to carry (phone
                # clock running fast, or a burst after a stall). Drop the
                # oldest audio to snap back to live.
                excess = len(self._buf) - limit
                del self._buf[:excess]
                self.dropped_bytes += excess
            if not self.primed:
                if len(self._buf) >= PREBUFFER_MS * BYTES_PER_SEC // 1000:
                    self.primed = True

    def pop(self, n):
        with self._lock:
            if not self.primed:
                return b"\x00" * n
            have = len(self._buf)
            if have >= n:
                out = bytes(self._buf[:n])
                del self._buf[:n]
                return out
            # Short. Hand back what we have, pad the rest, and re-prime so we
            # rebuild a cushion instead of underrunning repeatedly.
            out = bytes(self._buf) + b"\x00" * (n - have)
            self._buf.clear()
            self.underruns += 1
            self.primed = False
            return out

    def reset(self):
        with self._lock:
            self._buf.clear()
            self.primed = False

    def depth_ms(self):
        with self._lock:
            return len(self._buf) * 1000 // BYTES_PER_SEC


# --------------------------------------------------------------------------
# Virtual microphone backends
# --------------------------------------------------------------------------

class FeedError(RuntimeError):
    """The audio path could not be set up.

    Carries an optional multi-line `hint` with what the user should do about
    it — on Windows and macOS that is usually "install the virtual cable",
    which deserves more than a one-line exception message.
    """

    def __init__(self, msg, hint=None):
        super().__init__(msg)
        self.hint = hint


class VirtualMic:
    """Base class: owns the jitter buffer, the pacing thread and teardown.

    Subclasses supply the platform-specific pieces — arranging for something
    the system believes is a microphone, and opening a sink we can write PCM
    into that feeds it. On Linux that device is created on the fly; on Windows
    and macOS it is a cable the user installed, which we only locate and open.
    """

    BACKEND = "?"

    def __init__(self, name="AnyMic", description="AnyMic (Phone)", device=None):
        self.name = name
        self.description = description
        self.device = device        # user's --device override, if any
        self.module_id = None
        self.proc = None
        self.buffer = JitterBuffer()
        self._stop = threading.Event()
        self._feeder = None

    # -- to be provided by the backend -------------------------------------
    def _create_device(self):
        """Bring a virtual microphone into existence. No-op where one already
        exists because the user installed a driver for it."""

    def _start_feed(self):
        """Open whatever we write PCM into. Must raise FeedError on failure."""
        raise NotImplementedError

    def _write(self, chunk):
        """Hand one CHUNK_MS block to the audio system. Raises on a dead feed."""
        raise NotImplementedError

    def _close_feed(self):
        """Tear the feed down. Called even if _start_feed failed part-way."""

    def _destroy_device(self):
        """Undo _create_device."""

    def _unload_stale(self):
        """Clear anything a previous run left behind."""

    @property
    def source_name(self):
        """The device id to record from — what the tests and tooling need."""
        return self.name

    @property
    def display_name(self):
        """What users see in an app's microphone list, which is not always the
        same string as source_name."""
        return self.description

    def ready_lines(self):
        """Extra startup guidance, if this backend needs any."""
        return []

    # -- shared ------------------------------------------------------------
    def _run(self, args, **kw):
        return subprocess.run(args, capture_output=True, text=True, **kw)

    def start(self):
        self._unload_stale()
        self._create_device()
        try:
            self._start_feed()
        except BaseException:
            self._close_feed()
            self._destroy_device()
            raise
        self._feeder = threading.Thread(target=self._feed_loop, daemon=True)
        self._feeder.start()
        log(f"audio path ready — apps will see it as '{self.display_name}'")

    def _feed_loop(self):
        """Write to the feed at exactly real time, gaps filled with silence."""
        next_t = time.monotonic()
        while not self._stop.is_set():
            next_t += CHUNK_MS / 1000.0
            delay = next_t - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            elif delay < -0.5:
                # Fell badly behind (system suspend, heavy load). Resync rather
                # than trying to write a backlog of stale audio.
                next_t = time.monotonic()
            try:
                self._write(self.buffer.pop(CHUNK_BYTES))
            except Exception:
                if not self._stop.is_set():
                    log("audio feed closed unexpectedly")
                return

    def stats(self):
        return {
            "buffer_ms": self.buffer.depth_ms(),
            "underruns": self.buffer.underruns,
            "dropped_ms": self.buffer.dropped_bytes * 1000 // BYTES_PER_SEC,
            "received_kb": self.buffer.total_in // 1024,
        }

    def stop(self):
        self._stop.set()
        if self._feeder:
            self._feeder.join(timeout=1.0)
        self._close_feed()
        self._destroy_device()


class SubprocessFeedMic(VirtualMic):
    """For backends whose sink is a child process we pipe raw PCM into."""

    def _popen_feed(self, argv, env=None):
        kw = {}
        if WINDOWS:
            # No console window for the child, and its own process group so a
            # console Ctrl-C doesn't reach it before we've quiesced the audio.
            kw["creationflags"] = (subprocess.CREATE_NO_WINDOW |
                                   subprocess.CREATE_NEW_PROCESS_GROUP)
        else:
            # Own session, so a Ctrl-C aimed at our process group doesn't kill
            # the feed process out from under the feeder thread. We want to
            # tear it down in order, after the audio path has been quiesced.
            kw["start_new_session"] = True
        return subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, env=env, bufsize=0, **kw)

    def _write(self, chunk):
        self.proc.stdin.write(chunk)

    def _close_feed(self):
        if not self.proc:
            return
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        self.proc.terminate()
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.proc = None


class PulseCtlMic(SubprocessFeedMic):
    """Linux backends: the virtual device is ours, made and unmade with pactl."""

    def _unload_stale(self):
        """Remove a virtual source left behind by a previous run."""
        r = self._run(["pactl", "list", "modules", "short"])
        for line in r.stdout.splitlines():
            if f"sink_name={self.name}" in line:
                mid = line.split("\t")[0]
                self._run(["pactl", "unload-module", mid])
                log(f"removed stale virtual source (module {mid})")

    def _created(self, module_id):
        self.module_id = module_id
        log(f"virtual source '{self.name}' created "
            f"(module {module_id}, {self.BACKEND} backend)")

    def _destroy_device(self):
        if self.module_id:
            self._run(["pactl", "unload-module", self.module_id])
            self.module_id = None
            log("virtual source removed")


class PipeWireMic(PulseCtlMic):
    """A real virtual source, fed through pw-cat.

    WirePlumber's policy will not route a playback stream onto a node whose
    media.class is Audio/Source/Virtual — it classes the node as a source and
    silently connects the stream to the default sink instead, so the audio goes
    to the speakers while the microphone stays dead. pw-cat therefore runs with
    autoconnect disabled and gets linked into place by hand.
    """

    BACKEND = "pipewire"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.feed_node = "anymic-feed"

    def ready_lines(self):
        return [f"   Select '{self.description}' as the mic in your apps."]

    def _create_device(self):
        r = self._run([
            "pactl", "load-module", "module-null-sink",
            "media.class=Audio/Source/Virtual",
            f"sink_name={self.name}",
            "channel_map=mono",
            f'node.description="{self.description}"',
        ])
        if r.returncode != 0:
            raise FeedError(f"could not create virtual source: {r.stderr.strip()}")
        self._created(r.stdout.strip())

    def _start_feed(self):
        env = dict(os.environ)
        env["PIPEWIRE_PROPS"] = (
            "{ node.autoconnect=false node.name=%s }" % self.feed_node
        )
        self.proc = self._popen_feed(
            ["pw-cat", "--playback", "--format=s16", f"--rate={RATE}",
             f"--channels={CHANNELS}", "--raw", "-"], env=env)
        if not self._link():
            raise FeedError("could not link the feed into the virtual source")

    def _link(self, timeout=5.0):
        """Wait for pw-cat's port to appear, then wire it up."""
        src = f"{self.feed_node}:output_MONO"
        dst = f"{self.name}:input_MONO"
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                return False
            ports = self._run(["pw-link", "-o"]).stdout
            if src in ports:
                r = self._run(["pw-link", src, dst])
                if r.returncode == 0 or "exists" in r.stderr.lower():
                    return True
            # pw-cat only materialises its ports once it has data to play, so
            # prime the pipe while we wait for them to show up.
            try:
                self.proc.stdin.write(b"\x00" * CHUNK_BYTES)
            except (BrokenPipeError, ValueError):
                return False
            time.sleep(0.05)
        return False


class PulseMic(PulseCtlMic):
    """Fallback for systems running PulseAudio rather than PipeWire.

    PulseAudio has no equivalent of Audio/Source/Virtual, so instead we create
    an ordinary null sink and let apps record from its monitor. It works
    everywhere, at the cost of appearing as "Monitor of ..." in device lists,
    which some apps hide behind a "show monitors" toggle.
    """

    BACKEND = "pulseaudio"

    @property
    def source_name(self):
        return f"{self.name}.monitor"

    @property
    def display_name(self):
        return f"Monitor of {self.description}"

    def ready_lines(self):
        return [
            f"   Select 'Monitor of {self.description}' as the mic in your apps.",
            "   (PulseAudio has no virtual-source type, so it appears as a",
            "    monitor. Some apps hide these behind a 'show monitors' option.)"]

    def _create_device(self):
        r = self._run([
            "pactl", "load-module", "module-null-sink",
            f"sink_name={self.name}",
            "channel_map=mono",
            f'sink_properties=device.description="{self.description}"',
        ])
        if r.returncode != 0:
            raise FeedError(f"could not create null sink: {r.stderr.strip()}")
        self._created(r.stdout.strip())

    def _start_feed(self):
        stream = f"{self.name}-feed"
        self.proc = self._popen_feed([
            "pacat", "--playback", f"--device={self.name}",
            "--format=s16le", f"--rate={RATE}", f"--channels={CHANNELS}",
            f"--client-name={stream}", f"--stream-name={stream}", "--raw",
        ])
        # -d is the documented way to pick a sink and is honoured by a real
        # PulseAudio daemon. Under PipeWire's PulseAudio compatibility layer
        # it is silently overridden and the stream lands on the default sink
        # — i.e. the speakers — so move it into place explicitly afterwards.
        # The move is idempotent, so this is harmless where -d already worked.
        if not self._attach(stream):
            raise FeedError("could not attach the feed to the null sink")

    def _attach(self, stream, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                return False
            for block in self._run(["pactl", "list", "sink-inputs"]).stdout.split("Sink Input #")[1:]:
                if f'"{stream}"' not in block:
                    continue
                index = block.split("\n", 1)[0].strip()
                self._run(["pactl", "move-sink-input", index, self.name])
                return True
            # pacat may not register until it has something to play.
            try:
                self.proc.stdin.write(b"\x00" * CHUNK_BYTES)
            except (BrokenPipeError, ValueError):
                return False
            time.sleep(0.1)
        return False


# --------------------------------------------------------------------------
# Windows: play into a virtual audio cable with waveOut
# --------------------------------------------------------------------------

# Windows has no user-mode way to create a capture endpoint — that needs a
# driver — so the cable has to be installed once, by hand. These are the
# playback halves of the free ones, keyed by what waveOut reports. waveOut
# truncates device names to 31 characters, so match on a prefix.
#
#   playback device prefix -> (what to select as the mic in apps, product)
#
# Order is preference order, not just match order. VB-CABLE since driver pack
# 45 registers a 16-channel endpoint alongside the classic one; both show up
# under waveOut, and we want the plain mono-friendly "CABLE Input" of the two.
WINDOWS_CABLES = [
    ("CABLE Input",           "CABLE Output (VB-Audio Virtual Cable)",           "VB-CABLE"),
    ("VoiceMeeter Input",     "VoiceMeeter Output (VB-Audio VoiceMeeter VAIO)",  "VoiceMeeter"),
    ("VoiceMeeter Aux Input", "VoiceMeeter Aux Output (VB-Audio VoiceMeeter AUX VAIO)", "VoiceMeeter"),
    ("Line 1 (Virtual Audio", "Line 1 (Virtual Audio Cable)",                    "Virtual Audio Cable"),
    ("CABLE In 16ch",         "CABLE Output (VB-Audio Virtual Cable)",           "VB-CABLE"),
]

CABLE_HELP_WINDOWS = """\
No virtual audio cable was found.

Windows cannot invent a microphone without a driver, so AnyMic needs one
installed. VB-CABLE is free, about 2 MB, and the usual choice:

    https://vb-audio.com/Cable/

Download it, right-click VBCABLE_Setup_x64.exe, "Run as administrator",
then reboot. AnyMic will find it automatically on the next run.

VoiceMeeter (winget install VB-Audio.Voicemeeter) also works if you happen
to have it.

To try AnyMic without installing anything, run it with

    --device default

which plays the phone's audio out of your speakers instead. That is useful
for checking the phone half works, but it is not a microphone — no app will
be able to record from it."""


class WinMMMic(VirtualMic):
    """Feeds a virtual audio cable's playback end through the waveOut API.

    waveOut is the oldest of the three Windows audio APIs and the only one
    reachable from ctypes without a pile of COM boilerplate — WASAPI would mean
    hand-rolling IMMDeviceEnumerator vtables. Windows maps waveOut onto WASAPI
    shared mode internally, so the cost is a few extra milliseconds of latency
    and nothing else. At 48kHz mono that is a fair trade for staying inside the
    standard library.

    The driver is the clock here, not us. _write blocks until one of the queued
    buffers comes back, so the feed paces itself against the sound card rather
    than against time.monotonic() and the two never drift apart.
    """

    BACKEND = "winmm"
    NBUF = 8                       # 80ms of headroom inside the driver queue

    WHDR_DONE = 0x00000001
    WHDR_INQUEUE = 0x00000010
    WAVE_FORMAT_PCM = 1
    CALLBACK_NULL = 0x00000000

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._mm = None
        self._h = None             # HWAVEOUT
        self._hdrs = []
        self._bufs = []
        self._next = 0
        self._dev_id = None
        self._dev_name = None
        self._pick = None          # capture device the user should select
        self._product = None

    # -- ctypes plumbing ---------------------------------------------------
    @staticmethod
    def _load():
        import ctypes
        from ctypes import wintypes

        mm = ctypes.WinDLL("winmm")

        class WAVEFORMATEX(ctypes.Structure):
            _fields_ = [("wFormatTag", wintypes.WORD),
                        ("nChannels", wintypes.WORD),
                        ("nSamplesPerSec", wintypes.DWORD),
                        ("nAvgBytesPerSec", wintypes.DWORD),
                        ("nBlockAlign", wintypes.WORD),
                        ("wBitsPerSample", wintypes.WORD),
                        ("cbSize", wintypes.WORD)]

        class WAVEHDR(ctypes.Structure):
            pass

        # dwUser and reserved are DWORD_PTR, so they are 64-bit on a 64-bit
        # build. Declaring them as DWORD would shrink the struct and the driver
        # would scribble past the end of it.
        WAVEHDR._fields_ = [
            ("lpData", ctypes.c_void_p),
            ("dwBufferLength", wintypes.DWORD),
            ("dwBytesRecorded", wintypes.DWORD),
            ("dwUser", ctypes.c_void_p),
            ("dwFlags", wintypes.DWORD),
            ("dwLoops", wintypes.DWORD),
            ("lpNext", ctypes.POINTER(WAVEHDR)),
            ("reserved", ctypes.c_void_p),
        ]

        class WAVEOUTCAPSW(ctypes.Structure):
            _fields_ = [("wMid", wintypes.WORD),
                        ("wPid", wintypes.WORD),
                        ("vDriverVersion", wintypes.UINT),
                        ("szPname", wintypes.WCHAR * 32),
                        ("dwFormats", wintypes.DWORD),
                        ("wChannels", wintypes.WORD),
                        ("wReserved1", wintypes.WORD),
                        ("dwSupport", wintypes.DWORD)]

        HWAVEOUT = ctypes.c_void_p
        UINT_PTR = ctypes.c_size_t
        MMRESULT = wintypes.UINT

        mm.waveOutGetNumDevs.restype = wintypes.UINT
        mm.waveOutGetNumDevs.argtypes = []
        mm.waveOutGetDevCapsW.restype = MMRESULT
        mm.waveOutGetDevCapsW.argtypes = [UINT_PTR, ctypes.POINTER(WAVEOUTCAPSW),
                                          wintypes.UINT]
        mm.waveOutOpen.restype = MMRESULT
        mm.waveOutOpen.argtypes = [ctypes.POINTER(HWAVEOUT), UINT_PTR,
                                   ctypes.POINTER(WAVEFORMATEX),
                                   ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD]
        for fn in ("waveOutPrepareHeader", "waveOutUnprepareHeader", "waveOutWrite"):
            f = getattr(mm, fn)
            f.restype = MMRESULT
            f.argtypes = [HWAVEOUT, ctypes.POINTER(WAVEHDR), wintypes.UINT]
        mm.waveOutReset.restype = MMRESULT
        mm.waveOutReset.argtypes = [HWAVEOUT]
        mm.waveOutClose.restype = MMRESULT
        mm.waveOutClose.argtypes = [HWAVEOUT]
        mm.waveOutGetErrorTextW.restype = MMRESULT
        mm.waveOutGetErrorTextW.argtypes = [MMRESULT, wintypes.LPWSTR, wintypes.UINT]

        return {"ctypes": ctypes, "mm": mm, "WAVEFORMATEX": WAVEFORMATEX,
                "WAVEHDR": WAVEHDR, "WAVEOUTCAPSW": WAVEOUTCAPSW,
                "HWAVEOUT": HWAVEOUT}

    def _err(self, code):
        ctypes = self._mm["ctypes"]
        buf = ctypes.create_unicode_buffer(256)
        self._mm["mm"].waveOutGetErrorTextW(code, buf, 256)
        return buf.value or f"MMSYSERR {code}"

    def _check(self, code, what):
        if code != 0:
            raise FeedError(f"{what}: {self._err(code)}")

    @classmethod
    def playback_devices(cls):
        """[(id, name)] for every waveOut device, in Windows' own order."""
        mmc = cls._load()
        ctypes, mm = mmc["ctypes"], mmc["mm"]
        out = []
        for i in range(mm.waveOutGetNumDevs()):
            caps = mmc["WAVEOUTCAPSW"]()
            if mm.waveOutGetDevCapsW(i, ctypes.byref(caps),
                                     ctypes.sizeof(caps)) == 0:
                out.append((i, caps.szPname))
        return out

    # -- backend hooks -----------------------------------------------------
    def _create_device(self):
        self._mm = self._load()
        devices = self.playback_devices()

        if self.device and self.device.lower() == "default":
            # WAVE_MAPPER (-1) means "whatever the user's default output is".
            self._dev_id = self._mm["ctypes"].c_size_t(-1).value
            self._dev_name = "default output device"
            self._pick = None
            log("playing to the default output device — this is NOT a "
                "microphone, nothing can record from it")
            return

        if self.device:
            want = self.device.lower()
            for i, nm in devices:
                if want in nm.lower():
                    self._dev_id, self._dev_name = i, nm
                    for pre, pick, prod in WINDOWS_CABLES:
                        if nm.startswith(pre):
                            self._pick, self._product = pick, prod
                    return
            raise FeedError(
                f"no playback device matching {self.device!r}",
                "Devices Windows is offering:\n" +
                "\n".join(f"    [{i}] {nm}" for i, nm in devices))

        for pre, pick, prod in WINDOWS_CABLES:
            for i, nm in devices:
                if nm.startswith(pre):
                    self._dev_id, self._dev_name = i, nm
                    self._pick, self._product = pick, prod
                    log(f"found {prod} — feeding '{nm}'")
                    return

        raise FeedError("no virtual audio cable installed", CABLE_HELP_WINDOWS)

    def _start_feed(self):
        ctypes, mm = self._mm["ctypes"], self._mm["mm"]

        fmt = self._mm["WAVEFORMATEX"](
            wFormatTag=self.WAVE_FORMAT_PCM, nChannels=CHANNELS,
            nSamplesPerSec=RATE, nAvgBytesPerSec=BYTES_PER_SEC,
            nBlockAlign=CHANNELS * SAMPLE_BYTES,
            wBitsPerSample=SAMPLE_BYTES * 8, cbSize=0)

        h = self._mm["HWAVEOUT"]()
        self._check(mm.waveOutOpen(ctypes.byref(h), self._dev_id,
                                   ctypes.byref(fmt), None, None,
                                   self.CALLBACK_NULL),
                    f"could not open '{self._dev_name}'")
        self._h = h

        # Prepare every header once. The buffers stay put for the life of the
        # stream, so they only need unpreparing at the end.
        for _ in range(self.NBUF):
            buf = ctypes.create_string_buffer(CHUNK_BYTES)
            hdr = self._mm["WAVEHDR"]()
            hdr.lpData = ctypes.cast(buf, ctypes.c_void_p)
            hdr.dwBufferLength = CHUNK_BYTES
            self._check(mm.waveOutPrepareHeader(h, ctypes.byref(hdr),
                                                ctypes.sizeof(hdr)),
                        "could not prepare an audio buffer")
            self._bufs.append(buf)
            self._hdrs.append(hdr)

    def _write(self, chunk):
        ctypes, mm = self._mm["ctypes"], self._mm["mm"]
        hdr = self._hdrs[self._next]

        # Wait for the driver to give this one back. This is what paces us: the
        # sound card consumes at exactly 48kHz, so blocking here keeps us in
        # step with it instead of with the system clock.
        deadline = time.monotonic() + 2.0
        while hdr.dwFlags & self.WHDR_INQUEUE:
            if self._stop.is_set():
                return
            if time.monotonic() > deadline:
                raise FeedError("the audio device stopped accepting audio")
            time.sleep(0.001)

        ctypes.memmove(self._bufs[self._next], chunk, len(chunk))
        hdr.dwBufferLength = len(chunk)
        hdr.dwFlags &= ~self.WHDR_DONE
        self._check(mm.waveOutWrite(self._h, ctypes.byref(hdr),
                                    ctypes.sizeof(hdr)), "audio write failed")
        self._next = (self._next + 1) % self.NBUF

    def _close_feed(self):
        if not self._h:
            self._hdrs, self._bufs = [], []
            return
        ctypes, mm = self._mm["ctypes"], self._mm["mm"]
        # Reset first: it flushes the queue and marks every header done, which
        # is a precondition for unpreparing them.
        mm.waveOutReset(self._h)
        for hdr in self._hdrs:
            mm.waveOutUnprepareHeader(self._h, ctypes.byref(hdr),
                                      ctypes.sizeof(hdr))
        mm.waveOutClose(self._h)
        self._h = None
        self._hdrs, self._bufs = [], []

    @property
    def source_name(self):
        return self._pick or self._dev_name or self.name

    display_name = source_name

    def ready_lines(self):
        if not self._pick:
            return ["   Playing to your speakers. This is not a microphone —",
                    "   install a virtual cable to make it one."]
        return [f"   Select '{self._pick}' as the mic in your apps.",
                f"   ({self._product} is carrying the audio; AnyMic plays into",
                f"    '{self._dev_name}' and your apps record the other end.)"]


# --------------------------------------------------------------------------
# macOS: play into BlackHole, through CoreAudio or ffmpeg
# --------------------------------------------------------------------------

MAC_CABLES = ["BlackHole 2ch", "BlackHole 16ch", "BlackHole 64ch",
              "Soundflower (2ch)", "Loopback Audio", "Existential Audio"]

CABLE_HELP_MACOS = """\
No virtual audio cable was found.

macOS cannot invent a microphone without an audio driver, so AnyMic needs
one installed. BlackHole is free, open source, and the usual choice:

    brew install blackhole-2ch

(or download the installer from https://existential.audio/blackhole/)

You will be asked to allow the system extension in System Settings ->
Privacy & Security, then log out and back in. AnyMic finds it automatically
after that.

To try AnyMic without installing anything, run it with

    --device default

which plays the phone's audio out of your speakers instead. That is useful
for checking the phone half works, but it is not a microphone — no app will
be able to record from it."""


class CoreAudioMic(VirtualMic):
    """Feeds BlackHole through an AudioQueue, driven straight from ctypes.

    AudioQueue is the highest-level CoreAudio playback API and the only one
    that does not require building AudioUnit render callbacks by hand. We
    allocate a ring of buffers, enqueue them as audio arrives, and the queue's
    completion callback hands them back — so, as on Windows, the sound card
    ends up pacing the feed rather than the system clock.

    kAudioQueueProperty_CurrentDevice is what pins the queue to BlackHole
    rather than whatever the user's default output happens to be.
    """

    BACKEND = "coreaudio"
    NBUF = 8

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._ca = None
        self._aq = None
        self._free = []
        self._free_lock = threading.Condition()
        self._cb = None            # must outlive the queue, or it is GC'd
        self._uid = None
        self._dev_name = None

    @staticmethod
    def _fourcc(s):
        return struct.unpack(">I", s.encode())[0]

    @classmethod
    def _load(cls):
        import ctypes
        import ctypes.util

        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/"
                         "CoreFoundation")
        ca = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
        at = ctypes.CDLL("/System/Library/Frameworks/AudioToolbox.framework/"
                         "AudioToolbox")

        class AudioObjectPropertyAddress(ctypes.Structure):
            _fields_ = [("mSelector", ctypes.c_uint32),
                        ("mScope", ctypes.c_uint32),
                        ("mElement", ctypes.c_uint32)]

        class AudioStreamBasicDescription(ctypes.Structure):
            _fields_ = [("mSampleRate", ctypes.c_double),
                        ("mFormatID", ctypes.c_uint32),
                        ("mFormatFlags", ctypes.c_uint32),
                        ("mBytesPerPacket", ctypes.c_uint32),
                        ("mFramesPerPacket", ctypes.c_uint32),
                        ("mBytesPerFrame", ctypes.c_uint32),
                        ("mChannelsPerFrame", ctypes.c_uint32),
                        ("mBitsPerChannel", ctypes.c_uint32),
                        ("mReserved", ctypes.c_uint32)]

        class AudioQueueBuffer(ctypes.Structure):
            _fields_ = [("mAudioDataBytesCapacity", ctypes.c_uint32),
                        ("mAudioData", ctypes.c_void_p),
                        ("mAudioDataByteSize", ctypes.c_uint32),
                        ("mUserData", ctypes.c_void_p),
                        ("mPacketDescriptionCapacity", ctypes.c_uint32),
                        ("mPacketDescriptions", ctypes.c_void_p),
                        ("mPacketDescriptionCount", ctypes.c_uint32)]

        AudioQueueBufferRef = ctypes.POINTER(AudioQueueBuffer)
        AudioQueueRef = ctypes.c_void_p
        CALLBACK = ctypes.CFUNCTYPE(None, ctypes.c_void_p, AudioQueueRef,
                                    AudioQueueBufferRef)

        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                                 ctypes.c_uint32]
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                          ctypes.c_long, ctypes.c_uint32]
        cf.CFRelease.argtypes = [ctypes.c_void_p]

        ca.AudioObjectGetPropertyDataSize.restype = ctypes.c_int32
        ca.AudioObjectGetPropertyDataSize.argtypes = [
            ctypes.c_uint32, ctypes.POINTER(AudioObjectPropertyAddress),
            ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        ca.AudioObjectGetPropertyData.restype = ctypes.c_int32
        ca.AudioObjectGetPropertyData.argtypes = [
            ctypes.c_uint32, ctypes.POINTER(AudioObjectPropertyAddress),
            ctypes.c_uint32, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]

        at.AudioQueueNewOutput.restype = ctypes.c_int32
        at.AudioQueueNewOutput.argtypes = [
            ctypes.POINTER(AudioStreamBasicDescription), CALLBACK,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
            ctypes.POINTER(AudioQueueRef)]
        at.AudioQueueAllocateBuffer.restype = ctypes.c_int32
        at.AudioQueueAllocateBuffer.argtypes = [AudioQueueRef, ctypes.c_uint32,
                                                ctypes.POINTER(AudioQueueBufferRef)]
        at.AudioQueueEnqueueBuffer.restype = ctypes.c_int32
        at.AudioQueueEnqueueBuffer.argtypes = [AudioQueueRef, AudioQueueBufferRef,
                                               ctypes.c_uint32, ctypes.c_void_p]
        at.AudioQueueSetProperty.restype = ctypes.c_int32
        at.AudioQueueSetProperty.argtypes = [AudioQueueRef, ctypes.c_uint32,
                                             ctypes.c_void_p, ctypes.c_uint32]
        at.AudioQueueStart.restype = ctypes.c_int32
        at.AudioQueueStart.argtypes = [AudioQueueRef, ctypes.c_void_p]
        at.AudioQueueStop.restype = ctypes.c_int32
        at.AudioQueueStop.argtypes = [AudioQueueRef, ctypes.c_bool]
        at.AudioQueueDispose.restype = ctypes.c_int32
        at.AudioQueueDispose.argtypes = [AudioQueueRef, ctypes.c_bool]

        return {"ctypes": ctypes, "cf": cf, "ca": ca, "at": at,
                "Addr": AudioObjectPropertyAddress,
                "ASBD": AudioStreamBasicDescription,
                "BufRef": AudioQueueBufferRef, "QueueRef": AudioQueueRef,
                "CALLBACK": CALLBACK}

    @classmethod
    def _cfstr_to_py(cls, m, ref):
        ctypes = m["ctypes"]
        buf = ctypes.create_string_buffer(512)
        if m["cf"].CFStringGetCString(ref, buf, 512, 0x08000100):
            return buf.value.decode("utf-8", "replace")
        return ""

    @classmethod
    def output_devices(cls, m=None):
        """[(uid, name)] for every CoreAudio device with an output stream."""
        m = m or cls._load()
        ctypes = m["ctypes"]
        addr = m["Addr"](cls._fourcc("dev#"), cls._fourcc("glob"), 0)
        size = ctypes.c_uint32()
        if m["ca"].AudioObjectGetPropertyDataSize(1, ctypes.byref(addr), 0, None,
                                                  ctypes.byref(size)) != 0:
            return []
        n = size.value // ctypes.sizeof(ctypes.c_uint32)
        ids = (ctypes.c_uint32 * n)()
        if m["ca"].AudioObjectGetPropertyData(1, ctypes.byref(addr), 0, None,
                                              ctypes.byref(size), ids) != 0:
            return []

        out = []
        for dev in ids:
            # Skip anything with no output streams — input-only devices cannot
            # be played into. ('slay' really is the selector for
            # kAudioDevicePropertyStreamConfiguration.)
            cfg = m["Addr"](cls._fourcc("slay"), cls._fourcc("outp"), 0)
            csize = ctypes.c_uint32()
            if m["ca"].AudioObjectGetPropertyDataSize(
                    dev, ctypes.byref(cfg), 0, None, ctypes.byref(csize)) != 0:
                continue
            blob = ctypes.create_string_buffer(max(csize.value, 4))
            if m["ca"].AudioObjectGetPropertyData(dev, ctypes.byref(cfg), 0, None,
                                                  ctypes.byref(csize), blob) != 0:
                continue
            # AudioBufferList starts with mNumberBuffers; zero means no output.
            if struct.unpack("<I", blob.raw[:4])[0] == 0:
                continue

            name = uid = None
            for sel, slot in (("lnam", "name"), ("uid ", "uid")):
                a = m["Addr"](cls._fourcc(sel), cls._fourcc("glob"), 0)
                ref = ctypes.c_void_p()
                sz = ctypes.c_uint32(ctypes.sizeof(ref))
                if m["ca"].AudioObjectGetPropertyData(dev, ctypes.byref(a), 0, None,
                                                      ctypes.byref(sz),
                                                      ctypes.byref(ref)) != 0:
                    continue
                val = cls._cfstr_to_py(m, ref)
                m["cf"].CFRelease(ref)
                if slot == "name":
                    name = val
                else:
                    uid = val
            if name:
                out.append((uid, name))
        return out

    # -- backend hooks -----------------------------------------------------
    def _create_device(self):
        self._ca = self._load()
        devices = self.output_devices(self._ca)

        if self.device and self.device.lower() == "default":
            self._uid, self._dev_name = None, "default output device"
            log("playing to the default output device — this is NOT a "
                "microphone, nothing can record from it")
            return

        wanted = [self.device] if self.device else MAC_CABLES
        for want in wanted:
            for uid, nm in devices:
                if want.lower() in nm.lower():
                    self._uid, self._dev_name = uid, nm
                    log(f"found '{nm}'")
                    return

        if self.device:
            raise FeedError(
                f"no output device matching {self.device!r}",
                "Devices macOS is offering:\n" +
                "\n".join(f"    {nm}" for _, nm in devices))
        raise FeedError("no virtual audio cable installed", CABLE_HELP_MACOS)

    def _start_feed(self):
        m = self._ca
        ctypes = m["ctypes"]

        asbd = m["ASBD"](
            mSampleRate=float(RATE), mFormatID=self._fourcc("lpcm"),
            # signed integer | packed
            mFormatFlags=0x4 | 0x8,
            mBytesPerPacket=CHANNELS * SAMPLE_BYTES, mFramesPerPacket=1,
            mBytesPerFrame=CHANNELS * SAMPLE_BYTES, mChannelsPerFrame=CHANNELS,
            mBitsPerChannel=SAMPLE_BYTES * 8, mReserved=0)

        def on_done(_user, _aq, buf):
            with self._free_lock:
                self._free.append(buf)
                self._free_lock.notify()

        self._cb = m["CALLBACK"](on_done)
        aq = m["QueueRef"]()
        rc = m["at"].AudioQueueNewOutput(ctypes.byref(asbd), self._cb, None,
                                         None, None, 0, ctypes.byref(aq))
        if rc != 0:
            raise FeedError(f"could not create the audio queue (OSStatus {rc})")
        self._aq = aq

        if self._uid:
            ref = m["cf"].CFStringCreateWithCString(None, self._uid.encode(),
                                                    0x08000100)
            rc = m["at"].AudioQueueSetProperty(
                aq, self._fourcc("aqcd"), ctypes.byref(ctypes.c_void_p(ref)),
                ctypes.sizeof(ctypes.c_void_p))
            m["cf"].CFRelease(ref)
            if rc != 0:
                raise FeedError(
                    f"could not point the audio queue at '{self._dev_name}' "
                    f"(OSStatus {rc})")

        for _ in range(self.NBUF):
            buf = m["BufRef"]()
            rc = m["at"].AudioQueueAllocateBuffer(aq, CHUNK_BYTES,
                                                  ctypes.byref(buf))
            if rc != 0:
                raise FeedError(f"could not allocate audio buffers (OSStatus {rc})")
            self._free.append(buf)

        # Prime with silence before starting. A queue told to start with
        # nothing enqueued can decide it has run dry and stop itself, and the
        # first real audio then arrives to a queue that is no longer running.
        for _ in range(2):
            buf = self._free.pop(0)
            ctypes.memset(buf.contents.mAudioData, 0, CHUNK_BYTES)
            buf.contents.mAudioDataByteSize = CHUNK_BYTES
            m["at"].AudioQueueEnqueueBuffer(aq, buf, 0, None)

        rc = m["at"].AudioQueueStart(aq, None)
        if rc != 0:
            raise FeedError(f"could not start the audio queue (OSStatus {rc})")

    def _write(self, chunk):
        m = self._ca
        ctypes = m["ctypes"]
        with self._free_lock:
            # Same idea as the Windows path: block until the queue returns a
            # buffer, so playback rather than wall-clock time sets the pace.
            if not self._free:
                self._free_lock.wait(timeout=2.0)
            if not self._free:
                if self._stop.is_set():
                    return
                raise FeedError("the audio queue stopped accepting audio")
            buf = self._free.pop(0)
        ctypes.memmove(buf.contents.mAudioData, chunk, len(chunk))
        buf.contents.mAudioDataByteSize = len(chunk)
        rc = m["at"].AudioQueueEnqueueBuffer(self._aq, buf, 0, None)
        if rc != 0:
            raise FeedError(f"audio write failed (OSStatus {rc})")

    def _close_feed(self):
        if not self._aq:
            return
        self._ca["at"].AudioQueueStop(self._aq, True)
        self._ca["at"].AudioQueueDispose(self._aq, True)
        self._aq = None
        self._cb = None
        with self._free_lock:
            self._free = []

    @property
    def source_name(self):
        return self._dev_name or self.name

    display_name = source_name

    def ready_lines(self):
        if not getattr(self, "_uid", None):
            return ["   Playing to your speakers. This is not a microphone —",
                    "   install BlackHole to make it one."]
        return [f"   Select '{self._dev_name}' as the mic in your apps.",
                "   (AnyMic plays into it; apps record the same device.)"]


class FFmpegMic(SubprocessFeedMic):
    """macOS fallback: let ffmpeg do the CoreAudio talking.

    Used when the ctypes AudioQueue path cannot start — a macOS release that
    moved a symbol, a Python built against an odd runtime, anything of that
    shape. ffmpeg's audiotoolbox output does the same job, at the cost of a
    Homebrew install.
    """

    BACKEND = "ffmpeg"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._index = None
        self._dev_name = None

    @staticmethod
    def sinks():
        """[(index, name)] as ffmpeg's audiotoolbox output device sees them."""
        r = subprocess.run(["ffmpeg", "-hide_banner", "-sinks", "audiotoolbox"],
                           capture_output=True, text=True)
        found = []
        for line in (r.stdout + r.stderr).splitlines():
            m = re.search(r"\[(\d+)\]\s+(.+?)\s*$", line.strip())
            if m:
                found.append((int(m.group(1)), m.group(2)))
        return found

    def _create_device(self):
        if not shutil.which("ffmpeg"):
            raise FeedError("ffmpeg is not installed",
                            "Install it with:\n\n    brew install ffmpeg")
        devices = self.sinks()
        wanted = [self.device] if self.device else MAC_CABLES
        for want in wanted:
            for idx, nm in devices:
                if want.lower() in nm.lower():
                    self._index, self._dev_name = idx, nm
                    log(f"found '{nm}' (ffmpeg sink {idx})")
                    return
        if self.device:
            raise FeedError(
                f"no ffmpeg audio sink matching {self.device!r}",
                "Sinks ffmpeg is offering:\n" +
                "\n".join(f"    [{i}] {nm}" for i, nm in devices))
        raise FeedError("no virtual audio cable installed", CABLE_HELP_MACOS)

    def _start_feed(self):
        self.proc = self._popen_feed([
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "s16le", "-ar", str(RATE), "-ac", str(CHANNELS), "-i", "pipe:0",
            "-f", "audiotoolbox", "-audio_device_index", str(self._index), "-"])
        time.sleep(0.3)
        if self.proc.poll() is not None:
            raise FeedError("ffmpeg exited immediately")

    @property
    def source_name(self):
        return self._dev_name or self.name

    display_name = source_name

    def ready_lines(self):
        return [f"   Select '{self._dev_name}' as the mic in your apps."]


# --------------------------------------------------------------------------
# Backend selection
# --------------------------------------------------------------------------

BACKENDS = {"pipewire": PipeWireMic, "pulse": PulseMic,
            "winmm": WinMMMic, "coreaudio": CoreAudioMic, "ffmpeg": FFmpegMic}


def choose_backend(preference="auto"):
    """Pick a backend for this platform.

    Linux prefers PipeWire and falls back to PulseAudio; macOS prefers the
    in-process CoreAudio path and falls back to ffmpeg; Windows has one.
    """
    if preference != "auto":
        cls = BACKENDS[preference]
        if cls in (PipeWireMic, PulseMic) and not LINUX:
            sys.exit(f"the {preference} backend is Linux-only")
        if cls is WinMMMic and not WINDOWS:
            sys.exit("the winmm backend is Windows-only")
        if cls in (CoreAudioMic, FFmpegMic) and not MACOS:
            sys.exit(f"the {preference} backend is macOS-only")
        if cls is PipeWireMic and not (shutil.which("pw-cat") and
                                       shutil.which("pw-link")):
            sys.exit("pipewire backend requested but pw-cat/pw-link are missing")
        if cls is PulseMic and not shutil.which("pacat"):
            sys.exit("pulse backend requested but pacat is missing")
        return cls

    if WINDOWS:
        return WinMMMic
    if MACOS:
        return CoreAudioMic

    if shutil.which("pw-cat") and shutil.which("pw-link"):
        return PipeWireMic
    if shutil.which("pacat"):
        log("pw-cat/pw-link not found — falling back to the PulseAudio backend")
        return PulseMic
    sys.exit("no supported audio backend: install PipeWire (pw-cat, pw-link) "
             "or PulseAudio (pacat)")


# --------------------------------------------------------------------------
# WebSocket
# --------------------------------------------------------------------------

# Real audio frames are 1920 bytes. Anything remotely near this ceiling is a
# bug or an attempt to make us allocate on demand, so refuse rather than
# faithfully buffering whatever length a client claims to be sending.
MAX_FRAME_BYTES = 1 << 20


def ws_recv(sock):
    """Read one WebSocket frame. Returns (opcode, payload) or None on close."""

    def exact(n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    hdr = exact(2)
    if not hdr:
        return None
    opcode = hdr[0] & 0x0F
    masked = hdr[1] & 0x80
    length = hdr[1] & 0x7F
    if length == 126:
        ext = exact(2)
        if not ext:
            return None
        length = struct.unpack(">H", ext)[0]
    elif length == 127:
        ext = exact(8)
        if not ext:
            return None
        length = struct.unpack(">Q", ext)[0]
    if length > MAX_FRAME_BYTES:
        return None
    mask = exact(4) if masked else None
    payload = exact(length) if length else b""
    if payload is None:
        return None
    if mask and length:
        # Bulk XOR via one big-int operation — a per-byte Python loop here
        # costs real CPU at 50 frames/sec.
        key = (mask * (length // 4 + 1))[:length]
        payload = (int.from_bytes(payload, "big") ^
                   int.from_bytes(key, "big")).to_bytes(length, "big")
    return opcode, payload


def ws_send(sock, data, opcode=0x1):
    header = bytearray([0x80 | opcode])
    n = len(data)
    if n < 126:
        header.append(n)
    elif n < 65536:
        header.append(126)
        header += struct.pack(">H", n)
    else:
        header.append(127)
        header += struct.pack(">Q", n)
    sock.sendall(bytes(header) + data)


# Close codes we define ourselves. 4000-4999 is the application-private range,
# and unlike an HTTP status during the handshake these actually reach the
# browser, so the page can say *why* it was turned away.
CLOSE_IN_USE = 4001
CLOSE_RESERVED = 4002


class MicLock:
    """Grants the microphone to one device at a time, first come first served.

    Anyone can load the page — it is only the audio stream that is exclusive.
    The holder gets a secret token and is the only one who can reclaim the
    stream after a drop.

    A brief reservation after an unexpected disconnect is what makes this
    usable over WiFi: without it, a dropped connection would free the lock and
    the holder's own automatic reconnect could lose a race to some other device
    on the network. A deliberate Stop skips the reservation and frees the mic
    at once, so switching phones stays instant.
    """

    RESERVE_SECONDS = 30.0

    def __init__(self):
        self._lock = threading.Lock()
        self._token = None        # secret held by the current owner
        self._connected = False   # is the owner streaming right now?
        self._freed_at = None     # when an unexpected disconnect happened
        self.holder = None        # owner's address, for logging

    def _expire(self):
        """Drop a reservation that has outlived its welcome. Caller holds lock."""
        if (not self._connected and self._freed_at is not None
                and time.monotonic() - self._freed_at > self.RESERVE_SECONDS):
            self._token = None
            self._freed_at = None

    def claim(self, token, peer):
        """Try to take the mic. Returns (granted, token, refusal_reason)."""
        with self._lock:
            self._expire()

            if self._connected:
                # Same device reconnecting before we noticed the old socket
                # died — let it back in rather than locking out the owner.
                if token and secrets.compare_digest(token, self._token):
                    self.holder = peer
                    return True, self._token, None
                return False, None, "in-use"

            if self._token is not None:
                # Reserved. Only the previous owner may resume.
                if token and secrets.compare_digest(token, self._token):
                    self._connected = True
                    self._freed_at = None
                    self.holder = peer
                    return True, self._token, None
                return False, None, "reserved"

            self._token = secrets.token_urlsafe(18)
            self._connected = True
            self._freed_at = None
            self.holder = peer
            return True, self._token, None

    def release(self, token, deliberate):
        with self._lock:
            if not self._token or not token or not secrets.compare_digest(token, self._token):
                return
            self._connected = False
            if deliberate:
                self._token = None
                self._freed_at = None
            else:
                self._freed_at = time.monotonic()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "anymic"

    def log_message(self, fmt, *args):
        pass  # the interesting events are logged explicitly

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/ws":
            self._handle_ws()
        elif path in ("/", "/index.html"):
            self._serve_file("index.html", "text/html; charset=utf-8")
        else:
            self.send_error(404)

    def _serve_file(self, name, ctype):
        try:
            with open(os.path.join(HERE, name), "rb") as f:
                body = f.read()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _same_origin(self):
        """Reject WebSocket upgrades initiated by some other website.

        WebSockets are exempt from the same-origin policy, so once a browser
        has accepted our self-signed certificate, any page it later visits can
        open a socket to us. It could not hear anything — it would have to send
        audio, not receive it — but it could seize the lock and shut the owner
        out of their own microphone.

        Browsers always set Origin on a WebSocket handshake, so checking it
        closes that off. A missing Origin means a non-browser client (curl, the
        test suite), which is not the threat here and could forge any value
        anyway.
        """
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return urlparse(origin).netloc == self.headers.get("Host")

    def _handle_ws(self):
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            self.send_error(400, "not a websocket request")
            return
        if not self._same_origin():
            log(f"refused cross-origin websocket from {self.headers.get('Origin')}")
            self.send_error(403, "cross-origin websocket refused")
            return
        accept = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode()).digest()
        ).decode()
        self.wfile.write(
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: " + accept.encode() + b"\r\n\r\n"
        )
        self.wfile.flush()
        self.close_connection = True

        mic = self.server.mic
        lock = self.server.lock
        sock = self.connection
        peer = self.client_address[0]

        offered = parse_qs(urlparse(self.path).query).get("token", [None])[0]
        granted, token, refusal = lock.claim(offered, peer)
        if not granted:
            # Upgrade first, refuse second. An HTTP error during the handshake
            # is invisible to the browser's WebSocket API, but a close code is
            # not — so the page can explain itself instead of retry-looping.
            log(f"refused {peer}: microphone {refusal} by {lock.holder}")
            try:
                ws_send(sock, json.dumps({"type": "denied", "reason": refusal}).encode())
                ws_send(sock, struct.pack(
                    ">H", CLOSE_IN_USE if refusal == "in-use" else CLOSE_RESERVED),
                    opcode=0x8)
            except OSError:
                pass
            return

        log(f"phone connected from {peer}")
        mic.buffer.reset()

        stop = threading.Event()
        deliberate = False

        def stats_loop():
            while not stop.wait(1.0):
                try:
                    payload = mic.stats()
                    payload["type"] = "stats"
                    ws_send(sock, json.dumps(payload).encode())
                except OSError:
                    return

        try:
            ws_send(sock, json.dumps({"type": "hello", "token": token}).encode())
        except OSError:
            lock.release(token, deliberate=False)
            return

        reporter = threading.Thread(target=stats_loop, daemon=True)
        reporter.start()

        try:
            while True:
                frame = ws_recv(sock)
                if frame is None:
                    break
                opcode, payload = frame
                if opcode == 0x2:            # binary — PCM
                    mic.buffer.push(payload)
                elif opcode == 0x8:          # close
                    # 1000 means the user pressed Stop. Anything else is a drop,
                    # and the owner gets a window to come back.
                    code = struct.unpack(">H", payload[:2])[0] if len(payload) >= 2 else 1005
                    deliberate = (code == 1000)
                    break
                elif opcode == 0x9:          # ping
                    ws_send(sock, payload, opcode=0xA)
        except (OSError, ssl.SSLError):
            pass
        finally:
            stop.set()
            mic.buffer.reset()
            lock.release(token, deliberate)
            log(f"phone disconnected ({peer})"
                + ("" if deliberate else f" — reserved for {int(MicLock.RESERVE_SECONDS)}s"))


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# --------------------------------------------------------------------------
# TLS — getUserMedia only works in a secure context
# --------------------------------------------------------------------------

# ---- DER, just enough of it to write one certificate ---------------------
#
# openssl is standard on Linux but not on Windows, and macOS ships LibreSSL
# whose req(1) has drifted from OpenSSL's. Rather than make the user install a
# toolchain to get a self-signed certificate, we can emit one directly: X.509
# is DER, DER is trivially encodable, and hashlib plus pow() cover RSA. openssl
# is still used when it is there, because it is faster and better tested.

def _dlen(n):
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def _tlv(tag, body):
    return bytes([tag]) + _dlen(len(body)) + body


def _der_int(v):
    # Minimal big-endian, with a leading zero when the top bit would otherwise
    # make the value look negative.
    return _tlv(0x02, v.to_bytes(v.bit_length() // 8 + 1, "big"))


def _der_oid(dotted):
    parts = [int(x) for x in dotted.split(".")]
    body = bytes([parts[0] * 40 + parts[1]])
    for p in parts[2:]:
        chunk = []
        while True:
            chunk.insert(0, p & 0x7F)
            p >>= 7
            if not p:
                break
        body += bytes([c | 0x80 for c in chunk[:-1]] + [chunk[-1]])
    return _tlv(0x06, body)


_DER_NULL = b"\x05\x00"
_der_seq = lambda *p: _tlv(0x30, b"".join(p))
_der_set = lambda *p: _tlv(0x31, b"".join(p))
_der_octet = lambda d: _tlv(0x04, d)
_der_bits = lambda d, unused=0: _tlv(0x03, bytes([unused]) + d)
_der_utf8 = lambda s: _tlv(0x0C, s.encode())
_der_bool = lambda v: _tlv(0x01, b"\xff" if v else b"\x00")
_der_ctx = lambda n, body: _tlv(0xA0 | n, body)
_der_time = lambda t: _tlv(0x17, time.strftime("%y%m%d%H%M%SZ",
                                               time.gmtime(t)).encode())

_OID_RSA = "1.2.840.113549.1.1.1"
_OID_SHA256_RSA = "1.2.840.113549.1.1.11"
_OID_SHA256 = "2.16.840.1.101.3.4.2.1"
_OID_CN = "2.5.4.3"

_SMALL_PRIMES = [p for p in range(2, 512)
                 if all(p % q for q in range(2, int(p ** 0.5) + 1))]


def _probable_prime(n, rounds=40):
    for p in _SMALL_PRIMES:
        if n % p == 0:
            return n == p
    d, s = n - 1, 0
    while d % 2 == 0:
        d //= 2
        s += 1
    for _ in range(rounds):
        x = pow(secrets.randbelow(n - 3) + 2, d, n)
        if x == 1 or x == n - 1:
            continue
        for _ in range(s - 1):
            x = x * x % n
            if x == n - 1:
                break
        else:
            return False
    return True


def _gen_prime(bits):
    while True:
        # Top two bits set, so the product of two of these is exactly 2*bits
        # long and we don't have to keep retrying the multiply.
        c = secrets.randbits(bits) | (1 << (bits - 1)) | (1 << (bits - 2)) | 1
        if _probable_prime(c):
            return c


def _gen_rsa(bits=2048):
    e = 65537
    while True:
        p, q = _gen_prime(bits // 2), _gen_prime(bits // 2)
        if p == q:
            continue
        if p < q:                      # so qinv = q^-1 mod p is the CRT one
            p, q = q, p
        phi = (p - 1) * (q - 1)
        if math.gcd(e, phi) != 1:
            continue
        d = pow(e, -1, phi)
        return {"n": p * q, "e": e, "d": d, "p": p, "q": q,
                "dp": d % (p - 1), "dq": d % (q - 1), "qinv": pow(q, -1, p)}


def _rsa_sign_sha256(key, message):
    """EMSA-PKCS1-v1_5 over SHA-256, then the raw RSA operation."""
    digest_info = _der_seq(_der_seq(_der_oid(_OID_SHA256), _DER_NULL),
                           _der_octet(hashlib.sha256(message).digest()))
    k = (key["n"].bit_length() + 7) // 8
    em = b"\x00\x01" + b"\xff" * (k - len(digest_info) - 3) + b"\x00" + digest_info
    return pow(int.from_bytes(em, "big"), key["d"], key["n"]).to_bytes(k, "big")


def _pem(label, der):
    b64 = base64.b64encode(der).decode()
    body = "\n".join(b64[i:i + 64] for i in range(0, len(b64), 64))
    return f"-----BEGIN {label}-----\n{body}\n-----END {label}-----\n"


def _self_signed(ip, days=3650):
    """Return (cert_pem, key_pem) for a CN=anymic certificate covering `ip`."""
    key = _gen_rsa(2048)

    spki = _der_seq(
        _der_seq(_der_oid(_OID_RSA), _DER_NULL),
        _der_bits(_der_seq(_der_int(key["n"]), _der_int(key["e"]))))
    name = _der_seq(_der_set(_der_seq(_der_oid(_OID_CN), _der_utf8("anymic"))))
    sig_alg = _der_seq(_der_oid(_OID_SHA256_RSA), _DER_NULL)

    now = time.time()
    # GeneralName: dNSName is [2] IA5String, iPAddress is [7] OCTET STRING.
    san = _der_seq(
        _tlv(0x87, socket.inet_aton(ip)),
        _tlv(0x87, socket.inet_aton("127.0.0.1")),
        _tlv(0x82, b"localhost"))
    ext = lambda oid, critical, val: _der_seq(
        *( [_der_oid(oid)] + ([_der_bool(True)] if critical else []) +
           [_der_octet(val)] ))
    extensions = _der_ctx(3, _der_seq(
        ext("2.5.29.19", True, _der_seq()),                       # basicConstraints
        ext("2.5.29.15", True, _der_bits(b"\xa0", 5)),            # digitalSignature|keyEncipherment
        ext("2.5.29.37", False, _der_seq(_der_oid("1.3.6.1.5.5.7.3.1"))),  # serverAuth
        ext("2.5.29.17", False, san)))                            # subjectAltName

    tbs = _der_seq(
        _der_ctx(0, _der_int(2)),                                 # v3
        _der_int(secrets.randbits(63) | 1),
        sig_alg, name,
        _der_seq(_der_time(now - 3600), _der_time(now + days * 86400)),
        name, spki, extensions)

    cert = _der_seq(tbs, sig_alg, _der_bits(_rsa_sign_sha256(key, tbs)))
    pkcs1 = _der_seq(_der_int(0), _der_int(key["n"]), _der_int(key["e"]),
                     _der_int(key["d"]), _der_int(key["p"]), _der_int(key["q"]),
                     _der_int(key["dp"]), _der_int(key["dq"]),
                     _der_int(key["qinv"]))
    pkcs8 = _der_seq(_der_int(0), _der_seq(_der_oid(_OID_RSA), _DER_NULL),
                     _der_octet(pkcs1))
    return _pem("CERTIFICATE", cert), _pem("PRIVATE KEY", pkcs8)


def _lock_down(path):
    """Make the private key readable only by its owner, as best the OS allows."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    if WINDOWS:
        # chmod is close to meaningless on Windows; the ACL is the real control.
        subprocess.run(
            ["icacls", path, "/inheritance:r",
             "/grant:r", f"{os.environ.get('USERNAME', '')}:F"],
            capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)


def ensure_cert(certdir, ip):
    os.makedirs(certdir, exist_ok=True)
    cert = os.path.join(certdir, "cert.pem")
    keyf = os.path.join(certdir, "key.pem")
    stamp = os.path.join(certdir, "issued-for")

    # Which address the certificate covers is recorded alongside it rather than
    # read back out of the certificate: parsing a SAN needs either openssl or
    # another DER decoder, and this is one small file.
    if os.path.exists(cert) and os.path.exists(keyf) and os.path.exists(stamp):
        try:
            with open(stamp) as f:
                if f.read().strip() == ip:
                    return cert, keyf
        except OSError:
            pass
        log("LAN address changed — regenerating certificate")

    log("generating self-signed certificate...")
    if shutil.which("openssl"):
        r = subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", keyf, "-out", cert, "-days", "3650",
            "-subj", "/CN=anymic",
            "-addext", f"subjectAltName=IP:{ip},IP:127.0.0.1,DNS:localhost",
        ], capture_output=True, text=True)
        if r.returncode != 0:
            log("openssl could not generate it — using the built-in generator")
            log(r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "")
            _write_builtin_cert(cert, keyf, ip)
    else:
        _write_builtin_cert(cert, keyf, ip)

    _lock_down(keyf)
    with open(stamp, "w") as f:
        f.write(ip)
    return cert, keyf


def _write_builtin_cert(cert, keyf, ip):
    # A couple of seconds, almost all of it hunting for two 1024-bit primes.
    cert_pem, key_pem = _self_signed(ip)
    with open(cert, "w") as f:
        f.write(cert_pem)
    with open(keyf, "w") as f:
        f.write(key_pem)


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))   # no packets sent; just picks the route
        return s.getsockname()[0]
    except OSError:
        # No default route — an isolated LAN, or the phone's hotspot before it
        # has internet. Fall back to whatever the hostname resolves to.
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None,
                                           socket.AF_INET):
                addr = info[4][0]
                if not addr.startswith("127."):
                    return addr
        except OSError:
            pass
        return "127.0.0.1"
    finally:
        s.close()


def list_devices():
    """Print the playback devices this platform's backend can feed."""
    if WINDOWS:
        print("Playback devices (pass a distinctive part of one to --device):\n")
        for i, nm in WinMMMic.playback_devices():
            mark = "  <- virtual cable" if any(
                nm.startswith(p) for p, _, _ in WINDOWS_CABLES) else ""
            print(f"  [{i}] {nm}{mark}")
    elif MACOS:
        print("Output devices (pass a distinctive part of one to --device):\n")
        for _, nm in CoreAudioMic.output_devices():
            mark = "  <- virtual cable" if any(
                c.lower() in nm.lower() for c in MAC_CABLES) else ""
            print(f"  {nm}{mark}")
    else:
        print("On Linux AnyMic creates its own virtual source; there is no "
              "device to choose.")
    return 0


def check_setup(device=None):
    """Report whether this machine can run AnyMic. Exit status 0 means yes.

    Written for anymic.bat to call, but useful on its own — it answers "why is
    there no microphone" without starting a server or touching the audio graph.
    """
    global QUIET
    QUIET = True
    print("AnyMic setup check\n")
    print(f"  [ok]  Python {sys.version.split()[0]}")
    ready, hint = True, None

    if LINUX:
        if shutil.which("pactl"):
            print("  [ok]  pactl")
        else:
            print("  [--]  pactl is missing")
            ready = False
        if shutil.which("pw-cat") and shutil.which("pw-link"):
            print("  [ok]  PipeWire (pw-cat, pw-link)")
        elif shutil.which("pacat"):
            print("  [ok]  PulseAudio (pacat) — PipeWire not found, will fall back")
        else:
            print("  [--]  neither PipeWire (pw-cat, pw-link) nor PulseAudio (pacat)")
            ready = False
    else:
        # Resolving the device has no side effects on these platforms — the
        # cable exists or it does not — so this is safe to run at any time.
        attempts = [WinMMMic] if WINDOWS else [CoreAudioMic, FFmpegMic]
        for cls in attempts:
            probe = cls(device=device)
            try:
                probe._create_device()
                print(f"  [ok]  virtual audio cable: {probe.source_name}")
                print("        record from this in your apps")
                ready, hint = True, None
                break
            except FeedError as e:
                ready, hint = False, e.hint
                last = e
            except Exception as e:                       # noqa: BLE001
                ready, hint = False, None
                last = e
        if not ready:
            print(f"  [--]  {last}")

    print()
    if ready:
        print("Ready. Run: python anymic.py")
        return 0
    if hint:
        print(hint)
        print()
    print("Not ready.")
    return 1


def main():
    # These messages contain em-dashes. A Windows console, or any redirected
    # pipe, commonly defaults to a legacy code page that cannot encode them —
    # which would take the server down on its very first log line, in exactly
    # the unattended setups where the output matters most. UTF-8 with
    # replacement is readable everywhere and fatal nowhere.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass

    ap = argparse.ArgumentParser(
        prog="anymic", description="AnyMic — turn any phone into a microphone.")
    ap.add_argument("--port", type=int, default=8443)
    ap.add_argument("--name", default="AnyMic",
                    help="audio node name (Linux only)")
    ap.add_argument("--description", default="AnyMic (Phone)",
                    help="name shown in app microphone lists (Linux only)")
    ap.add_argument("--certdir", default=os.path.join(HERE, "certs"))
    ap.add_argument("--backend",
                    choices=("auto",) + tuple(sorted(BACKENDS)), default="auto",
                    help="audio backend (default: the right one for this OS)")
    ap.add_argument("--device", metavar="NAME",
                    help="Windows/macOS: which virtual cable to play into. "
                         "Part of the name is enough. 'default' plays to your "
                         "speakers instead, which is not a microphone but is "
                         "useful for testing.")
    ap.add_argument("--list-devices", action="store_true",
                    help="show the playback devices --device can pick from")
    ap.add_argument("--check", action="store_true",
                    help="report whether this machine is ready to run AnyMic, "
                         "and exit 0 if it is")
    args = ap.parse_args()

    if args.list_devices:
        return list_devices()
    if args.check:
        return check_setup(args.device)

    if LINUX:
        # Linux builds the virtual source itself, so it needs the tools to do
        # it. Windows and macOS play into a cable that already exists, and the
        # certificate no longer needs openssl on any platform.
        if not shutil.which("pactl"):
            sys.exit("missing required tool: pactl")
    elif args.name != "AnyMic" or args.description != "AnyMic (Phone)":
        log("--name/--description only apply on Linux, where AnyMic creates "
            "the device; elsewhere the cable's own name is what apps show")

    backend = choose_backend(args.backend)

    ip = lan_ip()
    cert, keyf = ensure_cert(args.certdir, ip)

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, keyf)

    # Claim the port before touching audio. A second instance must fail without
    # disturbing the virtual source the first one is already feeding — taking
    # the audio down and only then discovering the port is busy would break a
    # working microphone out from under whoever is using it.
    try:
        httpd = Server(("0.0.0.0", args.port), Handler)
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            sys.exit(f"port {args.port} is already in use — AnyMic may "
                     f"already be running. Use --port to pick another.")
        raise
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)

    mic = start_mic(backend, args)
    if mic is None:
        httpd.server_close()
        return 1
    httpd.mic = mic
    httpd.lock = MicLock()

    url = f"https://{ip}:{args.port}/"
    print()
    print("  " + "=" * 52)
    print("   A N Y M I C")
    print(f"   Open this on your phone:   {url}")
    print("  " + "=" * 52)
    print("   The certificate is self-signed, so the browser will warn.")
    print("   Tap Advanced -> Proceed. Then tap Start.")
    for line in mic.ready_lines():
        print(line)
    if WINDOWS:
        print("   If the phone cannot reach the page, allow Python through")
        print("   the firewall on your private network — Windows asks the")
        print("   first time, and denying it is silent afterwards.")
    print("   Ctrl-C to stop.")
    print()
    # Block-buffered whenever stdout isn't a terminal (a log file, systemd's
    # journal). Without this the URL — the one thing you need — sits in the
    # buffer until exit.
    sys.stdout.flush()

    def shutdown(*_):
        log("shutting down")
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    # SIGINT is Ctrl-C everywhere. SIGBREAK is Ctrl-Break, and is the only
    # signal a Windows service manager will actually send; SIGTERM exists on
    # Windows but nothing delivers it, so registering it there is harmless.
    for signame in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, signame, None)
        if sig is not None:
            try:
                signal.signal(sig, shutdown)
            except (ValueError, OSError):
                pass

    try:
        httpd.serve_forever()
    finally:
        mic.stop()
        httpd.server_close()
    return 0


def start_mic(backend, args):
    """Bring the audio path up, explaining itself if it cannot.

    On macOS a failure of the in-process CoreAudio path is worth retrying
    through ffmpeg before giving up — the cable is installed either way, and
    only the route to it differs.
    """
    attempts = [backend]
    if backend is CoreAudioMic and args.backend == "auto" and shutil.which("ffmpeg"):
        attempts.append(FFmpegMic)

    last = None
    for i, cls in enumerate(attempts):
        mic = cls(args.name, args.description, device=args.device)
        try:
            mic.start()
            return mic
        except Exception as e:                       # noqa: BLE001
            last = e
            if i + 1 < len(attempts):
                log(f"{cls.BACKEND} backend failed ({e}) — trying "
                    f"{attempts[i + 1].BACKEND}")

    print(f"\nanymic: {last}", file=sys.stderr)
    hint = getattr(last, "hint", None)
    if hint:
        print("\n" + hint, file=sys.stderr)
    print(file=sys.stderr)
    return None


if __name__ == "__main__":
    sys.exit(main() or 0)
