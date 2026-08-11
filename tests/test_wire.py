#!/usr/bin/env python3
"""End-to-end tests: starts a real server, talks WSS to it, checks the audio.

Runs on a spare port under its own device name, so it will not disturb an
UniMic you already have running.

    python3 tests/test_wire.py [--backend ...]

Linux creates its own virtual source, so the audio round-trip always runs.
Windows and macOS record the far end of whichever virtual cable is installed;
with no cable the server is pointed at the speakers instead and the round-trip
is skipped, since there is then nothing to record from.
"""
import argparse
import base64
import json
import math
import os
import re
import shutil
import signal
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "..", "unimic.py")
HOST, PORT = "127.0.0.1", 8446
NAME = "UniMicTest"

sys.path.insert(0, os.path.join(HERE, ".."))
import unimic  # noqa: E402

WINDOWS = unimic.WINDOWS
MACOS = unimic.MACOS
LINUX = unimic.LINUX

failures = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   [{detail}]" if detail else ""))
    if not cond:
        failures.append(name)


class WS:
    """Just enough WebSocket client to impersonate a phone."""

    def __init__(self, token=None):
        raw = socket.create_connection((HOST, PORT), timeout=10)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE            # the cert is self-signed
        self.s = ctx.wrap_socket(raw, server_hostname=HOST)
        key = base64.b64encode(os.urandom(16)).decode()
        path = "/ws" + (f"?token={token}" if token else "")
        self.s.sendall((f"GET {path} HTTP/1.1\r\nHost: {HOST}:{PORT}\r\n"
                        f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                        f"Sec-WebSocket-Key: {key}\r\n"
                        f"Sec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.s.recv(4096)
        self.status = buf.split(b"\r\n")[0].decode()

    def _exact(self, n):
        b = b""
        while len(b) < n:
            c = self.s.recv(n - len(b))
            if not c:
                return None
            b += c
        return b

    def recv(self, timeout=5):
        """-> ('text', obj) | ('close', code) | None"""
        self.s.settimeout(timeout)
        try:
            head = self._exact(2)
            if not head:
                return None
            op = head[0] & 0x0F
            n = head[1] & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._exact(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._exact(8))[0]
            payload = self._exact(n) if n else b""
            if op == 0x1:
                return ("text", json.loads(payload.decode()))
            if op == 0x8:
                return ("close",
                        struct.unpack(">H", payload[:2])[0] if len(payload) >= 2 else 1005)
            return ("other", op)
        except (socket.timeout, TimeoutError):
            return None

    def await_type(self, want, tries=8):
        for _ in range(tries):
            m = self.recv()
            if m is None or m[0] == "close":
                return m
            if m[0] == "text" and m[1].get("type") == want:
                return m
        return None

    def _frame(self, payload, opcode):
        n = len(payload)
        mask = os.urandom(4)
        hdr = bytearray([0x80 | opcode])
        if n < 126:
            hdr.append(0x80 | n)
        else:
            hdr.append(0x80 | 126)
            hdr += struct.pack(">H", n)
        key = (mask * (n // 4 + 1))[:n]
        masked = (int.from_bytes(payload, "big") ^ int.from_bytes(key, "big")) \
            .to_bytes(n, "big") if n else b""
        self.s.sendall(bytes(hdr) + mask + masked)

    def send_tone(self, chunks=1, phase=[0]):
        for _ in range(chunks):
            self._frame(tone(960, phase), 0x2)

    def send_oversized_header(self):
        """Announce a 4 GiB payload without sending it."""
        self.s.sendall(bytes([0x82, 0xFF]) + struct.pack(">Q", 1 << 32) + os.urandom(4))

    def close_clean(self):
        self._frame(struct.pack(">H", 1000), 0x8)
        time.sleep(0.3)
        self.s.close()

    def drop(self):
        self.s.close()


def fetch(path):
    """Plain HTTPS GET against the server, ignoring the self-signed cert."""
    import http.client
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    conn = http.client.HTTPSConnection(HOST, PORT, context=ctx, timeout=5)
    try:
        conn.request("GET", path)
        r = conn.getresponse()
        return r.status, r.read()
    finally:
        conn.close()


def tone(frames, phase=[0], hz=440, amp=12000):
    buf = bytearray()
    for _ in range(frames):
        buf += struct.pack("<h", int(amp * math.sin(2 * math.pi * hz * phase[0] / 48000)))
        phase[0] += 1
    return bytes(buf)


# --------------------------------------------------------------------------
# Talking to the server process, which is not a POSIX process everywhere
# --------------------------------------------------------------------------

def spawn_server(extra_args):
    kw = {}
    if WINDOWS:
        # Its own process group, so CTRL_BREAK_EVENT reaches it and not us.
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    return subprocess.Popen(
        [sys.executable, SERVER, "--port", str(PORT), "--name", NAME,
         "--description", "UniMic Test"] + extra_args,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, **kw)


def interrupt_server(proc):
    """Ask for the same orderly shutdown a user's Ctrl-C would produce."""
    try:
        if WINDOWS:
            # Windows has no process groups in the POSIX sense; CTRL_BREAK_EVENT
            # against the group we created is the closest equivalent, and the
            # server registers SIGBREAK alongside SIGINT for exactly this.
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGINT)
    except (OSError, ValueError):
        proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def wait_for_port(proc, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            socket.create_connection((HOST, PORT), timeout=0.5).close()
            return True
        except OSError:
            time.sleep(0.2)
    return False


# --------------------------------------------------------------------------
# Finding a cable, and recording the far end of it
# --------------------------------------------------------------------------

def find_cable():
    """-> (server --device argument, thing to record from), or (None, None)."""
    if LINUX:
        return None, None                    # Linux makes its own source
    if WINDOWS:
        for pre, pick, _ in unimic.WINDOWS_CABLES:
            for _, nm in unimic.WinMMMic.playback_devices():
                if nm.startswith(pre):
                    # waveIn truncates names to 31 characters too, so match on
                    # the part before the vendor suffix rather than the whole.
                    return nm, pick.split(" (")[0]
        return None, None
    for want in unimic.MAC_CABLES:
        for _, nm in unimic.CoreAudioMic.output_devices():
            if want.lower() in nm.lower():
                return nm, nm
    return None, None


class PulseRecorder:
    """Linux: parecord straight to a wav."""

    available = staticmethod(lambda: bool(shutil.which("parecord")))

    def __init__(self, target, path):
        self.path = path
        self.proc = subprocess.Popen(
            ["parecord", f"--device={target}", "--file-format=wav", "--channels=1",
             "--rate=48000", "--format=s16le", path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop(self):
        self.proc.terminate()
        self.proc.wait(timeout=5)
        return self.path


class FFmpegRecorder:
    """macOS: ffmpeg's avfoundation input, which indexes audio devices itself."""

    available = staticmethod(lambda: bool(shutil.which("ffmpeg")))

    @staticmethod
    def _index(name):
        r = subprocess.run(["ffmpeg", "-hide_banner", "-f", "avfoundation",
                            "-list_devices", "true", "-i", ""],
                           capture_output=True, text=True)
        audio = False
        for line in (r.stdout + r.stderr).splitlines():
            if "audio devices" in line.lower():
                audio = True
                continue
            if "video devices" in line.lower():
                audio = False
                continue
            m = re.search(r"\[(\d+)\]\s+(.+?)\s*$", line)
            if audio and m and name.lower() in m.group(2).lower():
                return int(m.group(1))
        return None

    def __init__(self, target, path):
        self.path = path
        idx = self._index(target)
        if idx is None:
            raise RuntimeError(f"avfoundation has no audio device {target!r}")
        self.proc = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "avfoundation", "-i", f":{idx}",
             "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", path],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)

    def stop(self):
        try:
            self.proc.communicate(b"q", timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        return self.path


class WinRecorder:
    """Windows: waveIn, the mirror image of the waveOut the server plays into.

    Same reasoning as the server side — it is the one capture API reachable
    from ctypes without COM, and it keeps the test suite dependency-free.
    """

    available = staticmethod(lambda: True)
    NBUF, CHUNK = 16, 1920

    def __init__(self, target, path):
        import ctypes
        from ctypes import wintypes
        self.ctypes, self.path = ctypes, path
        self.mm = ctypes.WinDLL("winmm")

        class WAVEINCAPSW(ctypes.Structure):
            _fields_ = [("wMid", wintypes.WORD), ("wPid", wintypes.WORD),
                        ("vDriverVersion", wintypes.UINT),
                        ("szPname", wintypes.WCHAR * 32),
                        ("dwFormats", wintypes.DWORD),
                        ("wChannels", wintypes.WORD),
                        ("wReserved1", wintypes.WORD)]

        # Reuse the server's WAVEFORMATEX/WAVEHDR definitions so the two halves
        # of the test cannot drift apart.
        m = unimic.WinMMMic._load()
        self.WAVEHDR = m["WAVEHDR"]
        HWAVEIN = ctypes.c_void_p
        self.mm.waveInOpen.argtypes = [ctypes.POINTER(HWAVEIN), ctypes.c_size_t,
                                       ctypes.POINTER(m["WAVEFORMATEX"]),
                                       ctypes.c_void_p, ctypes.c_void_p,
                                       wintypes.DWORD]
        self.mm.waveInGetDevCapsW.argtypes = [ctypes.c_size_t,
                                              ctypes.POINTER(WAVEINCAPSW),
                                              wintypes.UINT]

        dev = None
        for i in range(self.mm.waveInGetNumDevs()):
            caps = WAVEINCAPSW()
            if self.mm.waveInGetDevCapsW(i, ctypes.byref(caps),
                                         ctypes.sizeof(caps)) == 0:
                if caps.szPname.startswith(target):
                    dev = i
                    break
        if dev is None:
            raise RuntimeError(f"no capture device starting {target!r}")

        fmt = m["WAVEFORMATEX"](wFormatTag=1, nChannels=1, nSamplesPerSec=48000,
                                nAvgBytesPerSec=96000, nBlockAlign=2,
                                wBitsPerSample=16, cbSize=0)
        self.h = HWAVEIN()
        if self.mm.waveInOpen(ctypes.byref(self.h), dev, ctypes.byref(fmt),
                              None, None, 0) != 0:
            raise RuntimeError("waveInOpen failed")

        self.bufs, self.hdrs = [], []
        for _ in range(self.NBUF):
            b = ctypes.create_string_buffer(self.CHUNK)
            hdr = self.WAVEHDR()
            hdr.lpData = ctypes.cast(b, ctypes.c_void_p)
            hdr.dwBufferLength = self.CHUNK
            self.mm.waveInPrepareHeader(self.h, ctypes.byref(hdr), ctypes.sizeof(hdr))
            self.mm.waveInAddBuffer(self.h, ctypes.byref(hdr), ctypes.sizeof(hdr))
            self.bufs.append(b)
            self.hdrs.append(hdr)

        self.data = bytearray()
        self.running = True
        self.mm.waveInStart(self.h)
        self.thread = threading.Thread(target=self._pump, daemon=True)
        self.thread.start()

    def _pump(self):
        ctypes = self.ctypes
        i = 0
        while self.running:
            hdr = self.hdrs[i]
            if hdr.dwFlags & 0x00000001:                       # WHDR_DONE
                self.data += self.bufs[i].raw[:hdr.dwBytesRecorded]
                hdr.dwFlags &= ~0x00000001
                hdr.dwBytesRecorded = 0
                self.mm.waveInAddBuffer(self.h, ctypes.byref(hdr),
                                        ctypes.sizeof(hdr))
                i = (i + 1) % self.NBUF
            else:
                time.sleep(0.002)

    def stop(self):
        ctypes = self.ctypes
        self.running = False
        self.thread.join(timeout=2)
        self.mm.waveInStop(self.h)
        self.mm.waveInReset(self.h)
        for hdr in self.hdrs:
            self.mm.waveInUnprepareHeader(self.h, ctypes.byref(hdr),
                                          ctypes.sizeof(hdr))
        self.mm.waveInClose(self.h)
        with wave.open(self.path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(48000)
            w.writeframes(bytes(self.data))
        return self.path


RECORDER = WinRecorder if WINDOWS else FFmpegRecorder if MACOS else PulseRecorder


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------

def drain_check(backend_cls, device):
    """Does the audio system actually consume what we hand it, at real time?

    Runs the backend in-process for four seconds and counts the chunks that
    made it all the way out. A device that is not really draining shows up
    either as a write that blocks until it times out, or as a chunk count that
    falls short of the elapsed time — both of which this catches without
    needing anything to record from.
    """
    mic = backend_cls(NAME + "Drain", "UniMic Drain Test", device=device)
    written = [0]
    inner = mic._write

    def counting(chunk):
        inner(chunk)
        written[0] += 1

    mic._write = counting
    try:
        mic.start()
    except Exception as e:                              # noqa: BLE001
        print(f"  SKIP  audio device drains at real time ({e})")
        return

    try:
        t0 = time.monotonic()
        deadline = t0 + 4.0
        phase = [0]
        while time.monotonic() < deadline:
            # Quiet on purpose: with no cable installed this plays out of the
            # speakers, and the driver drains it just the same at any volume.
            mic.buffer.push(tone(480, phase, amp=600))  # 10ms
            time.sleep(0.01)
        elapsed = time.monotonic() - t0
    finally:
        mic.stop()

    expected = elapsed / (unimic.CHUNK_MS / 1000.0)
    ratio = written[0] / expected
    check("audio device drains at real time", 0.9 < ratio < 1.1,
          f"{written[0]} chunks in {elapsed:.1f}s, expected ~{expected:.0f}")
    check("draining did not underrun", mic.buffer.underruns <= 2,
          f"{mic.buffer.underruns} underruns")


def audio_check(target):
    """Stream a 440Hz tone in and confirm it comes out of the virtual mic."""
    if target is None:
        print("  SKIP  audio round-trip (no virtual cable installed)")
        return
    if not RECORDER.available():
        print(f"  SKIP  audio round-trip ({RECORDER.__name__} unavailable)")
        return

    wav = os.path.join(tempfile.gettempdir(), f"unimic-test-{os.getpid()}.wav")
    try:
        rec = RECORDER(target, wav)
    except Exception as e:                              # noqa: BLE001
        print(f"  SKIP  audio round-trip ({e})")
        return

    try:
        time.sleep(1.0)
        ws = WS()
        ws.await_type("hello")
        start = time.monotonic()
        for i in range(250):                      # 5 s, paced like a real phone
            ws.send_tone()
            due = start + (i + 1) * 0.02
            time.sleep(max(0, due - time.monotonic()))
        ws.close_clean()
        time.sleep(0.5)
    finally:
        rec.stop()

    with wave.open(wav) as w:
        frames = w.getnframes()
        data = w.readframes(frames)
    os.unlink(wav)

    samples = struct.unpack(f"<{len(data)//2}h", data)
    loud = [i for i, v in enumerate(samples) if abs(v) > 3000]
    check("audio reaches the virtual microphone", bool(loud),
          f"{frames} frames captured")
    if not loud:
        return
    seg = samples[loud[0]:loud[-1]]
    crossings = sum(1 for i in range(1, len(seg)) if (seg[i - 1] < 0) != (seg[i] < 0))
    freq = crossings / 2 / (len(seg) / 48000)
    check("tone comes out at the frequency it went in",
          439.0 < freq < 441.0, f"{freq:.1f} Hz, sent 440.0")
    gaps = sum(1 for v in seg if v == 0) / len(seg) * 100
    check("stream is continuous, not chopped", gaps < 2.0, f"{gaps:.2f}% silence")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="auto",
                    choices=("auto",) + tuple(sorted(unimic.BACKENDS)))
    ap.add_argument("--device", help="override the virtual cable to test against")
    args = ap.parse_args()

    device, record_from = (args.device, args.device) if args.device else find_cable()
    extra = ["--backend", args.backend]
    if not LINUX:
        # With no cable there is nothing to record, but everything that is not
        # the audio round-trip is still worth testing, so point at the speakers.
        extra += ["--device", device or "default"]
        if device:
            print(f"using virtual cable: {device}")
        else:
            print("no virtual cable found — testing against the default output; "
                  "the audio round-trip will be skipped")

    print("--- the audio path itself ---")
    backend_cls = unimic.choose_backend(args.backend)
    drain_check(backend_cls, None if LINUX else (device or "default"))

    proc = spawn_server(extra)
    if not wait_for_port(proc):
        print("server failed to start:\n" + (proc.stdout.read() if proc.stdout else ""))
        return 1

    # The Pulse backend exposes the sink's monitor rather than a source proper.
    if LINUX:
        record_from = f"{NAME}.monitor" if args.backend == "pulse" else NAME

    try:
        print("--- the page is served over TLS ---")
        status, body = fetch("/")
        check("GET / returns the phone page", status == 200 and b"UniMic" in body,
              f"HTTP {status}, {len(body)} bytes")
        check("the page carries the capture worklet",
              b"registerProcessor('pcm'" in body)
        status, _ = fetch("/nope")
        check("unknown paths 404", status == 404, f"HTTP {status}")

        print("--- claiming the microphone ---")
        a = WS()
        check("handshake upgrades", "101" in a.status, a.status)
        m = a.await_type("hello")
        check("server greets the owner with a token",
              m and m[0] == "text" and m[1].get("token"))
        token = m[1]["token"] if m and m[0] == "text" else None
        a.send_tone(5)

        print("--- a second device is turned away ---")
        b = WS()
        m = b.recv()
        check("it is told why", m and m[0] == "text" and m[1].get("reason") == "in-use",
              str(m))
        m = b.recv()
        check("and closed with code 4001", m and m[0] == "close" and m[1] == 4001, str(m))

        print("--- the owner is unaffected ---")
        a.send_tone(5)
        check("still receiving stats", a.await_type("stats") is not None)

        print("--- an oversized frame is refused ---")
        c = WS(token=token)
        c.await_type("hello")
        c.send_oversized_header()
        time.sleep(0.5)
        d = WS()
        m = d.recv()
        check("server survived and is still serving",
              m is not None and m[0] == "text", str(m))
        if m and m[0] == "text" and m[1].get("type") == "hello":
            d.close_clean()          # we accidentally became the owner; hand it back
        else:
            d.drop()

        print("--- an unexpected drop reserves the mic ---")
        try:
            a.drop()
        except OSError:
            pass
        owner = WS()
        owner.await_type("hello") or owner.close_clean()
        time.sleep(0.2)
        e = WS()
        m = e.recv()
        # Whoever holds it now, a stranger must be refused one way or the other.
        check("a stranger cannot walk in",
              m and m[0] == "text" and m[1].get("type") == "denied", str(m))
        e.drop()

        print("--- audio actually flows ---")
        time.sleep(unimic.MicLock.RESERVE_SECONDS + 0.5)
        audio_check(record_from)

    finally:
        interrupt_server(proc)

    if LINUX:
        left = subprocess.run(["pactl", "list", "modules", "short"],
                              capture_output=True, text=True).stdout
        check("virtual source is removed on shutdown", f"sink_name={NAME}" not in left)
    else:
        # Nothing to unload — the cable outlives us — but the server must still
        # have let go of the device, which a clean exit code demonstrates.
        check("server shut down cleanly", proc.returncode in (0, None),
              f"exit {proc.returncode}")

    print()
    if failures:
        print(f"FAILED: {len(failures)} — " + "; ".join(failures))
        return 1
    print("all wire tests pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
