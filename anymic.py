#!/usr/bin/env python3
"""
AnyMic — turn any phone into a Linux microphone, over the browser.

No phone app, no kernel module, no root. The phone's browser captures the mic
and streams raw PCM over a WebSocket; this server feeds it into a PipeWire
virtual source that every app sees as a normal microphone.

    python3 anymic.py

Then open the printed https:// URL on the phone and tap Start.
"""

import argparse
import base64
import errno
import hashlib
import json
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


def log(msg):
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

class VirtualMic:
    """Base class: owns the jitter buffer, the pacing thread and teardown.

    Subclasses supply the two platform-specific pieces — creating a device the
    system believes is a microphone, and starting a process we can write PCM
    into that feeds it.
    """

    BACKEND = "?"

    def __init__(self, name="AnyMic", description="AnyMic (Phone)"):
        self.name = name
        self.description = description
        self.module_id = None
        self.proc = None
        self.buffer = JitterBuffer()
        self._stop = threading.Event()
        self._feeder = None

    # -- to be provided by the backend -------------------------------------
    def _create_device(self):
        raise NotImplementedError

    def _start_feed(self):
        raise NotImplementedError

    @property
    def source_name(self):
        """What users should select as their microphone."""
        return self.name

    # -- shared ------------------------------------------------------------
    def _run(self, args, **kw):
        return subprocess.run(args, capture_output=True, text=True, **kw)

    def _popen_feed(self, argv, env=None):
        return subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, env=env, bufsize=0,
            # Own session, so a Ctrl-C aimed at our process group doesn't kill
            # the feed process out from under the feeder thread. We want to
            # tear it down in order, after the audio path has been quiesced.
            start_new_session=True,
        )

    def start(self):
        self._unload_stale()
        self._create_device()
        log(f"virtual source '{self.name}' created "
            f"(module {self.module_id}, {self.BACKEND} backend)")
        self._start_feed()
        self._feeder = threading.Thread(target=self._feed_loop, daemon=True)
        self._feeder.start()
        log(f"audio path ready — apps will see it as '{self.description}'")

    def _unload_stale(self):
        """Remove a virtual source left behind by a previous run."""
        r = self._run(["pactl", "list", "modules", "short"])
        for line in r.stdout.splitlines():
            if f"sink_name={self.name}" in line:
                mid = line.split("\t")[0]
                self._run(["pactl", "unload-module", mid])
                log(f"removed stale virtual source (module {mid})")

    def _feed_loop(self):
        """Write to the feed process at exactly real time, gaps filled with silence."""
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
                self.proc.stdin.write(self.buffer.pop(CHUNK_BYTES))
            except (BrokenPipeError, ValueError):
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
        if self.proc:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.module_id:
            self._run(["pactl", "unload-module", self.module_id])
            log("virtual source removed")


class PipeWireMic(VirtualMic):
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

    def _create_device(self):
        r = self._run([
            "pactl", "load-module", "module-null-sink",
            "media.class=Audio/Source/Virtual",
            f"sink_name={self.name}",
            "channel_map=mono",
            f'node.description="{self.description}"',
        ])
        if r.returncode != 0:
            raise RuntimeError(f"could not create virtual source: {r.stderr.strip()}")
        self.module_id = r.stdout.strip()

    def _start_feed(self):
        env = dict(os.environ)
        env["PIPEWIRE_PROPS"] = (
            "{ node.autoconnect=false node.name=%s }" % self.feed_node
        )
        self.proc = self._popen_feed(
            ["pw-cat", "--playback", "--format=s16", f"--rate={RATE}",
             f"--channels={CHANNELS}", "--raw", "-"], env=env)
        if not self._link():
            self.stop()
            raise RuntimeError("could not link the feed into the virtual source")

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


class PulseMic(VirtualMic):
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

    def _create_device(self):
        r = self._run([
            "pactl", "load-module", "module-null-sink",
            f"sink_name={self.name}",
            "channel_map=mono",
            f'sink_properties=device.description="{self.description}"',
        ])
        if r.returncode != 0:
            raise RuntimeError(f"could not create null sink: {r.stderr.strip()}")
        self.module_id = r.stdout.strip()

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
            self.stop()
            raise RuntimeError("could not attach the feed to the null sink")

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


def choose_backend(preference="auto"):
    """Pick a backend. PipeWire is preferred; PulseAudio is the fallback."""
    have_pw = shutil.which("pw-cat") and shutil.which("pw-link")
    have_pa = shutil.which("pacat")

    if preference == "pipewire":
        if not have_pw:
            sys.exit("pipewire backend requested but pw-cat/pw-link are missing")
        return PipeWireMic
    if preference == "pulse":
        if not have_pa:
            sys.exit("pulse backend requested but pacat is missing")
        return PulseMic

    if have_pw:
        return PipeWireMic
    if have_pa:
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

def ensure_cert(certdir, ip):
    os.makedirs(certdir, exist_ok=True)
    cert = os.path.join(certdir, "cert.pem")
    keyf = os.path.join(certdir, "key.pem")
    if os.path.exists(cert) and os.path.exists(keyf):
        have = subprocess.run(["openssl", "x509", "-in", cert, "-noout", "-text"],
                              capture_output=True, text=True).stdout
        if f"IP Address:{ip}" in have:
            return cert, keyf
        log("LAN address changed — regenerating certificate")
    log("generating self-signed certificate...")
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", keyf, "-out", cert, "-days", "3650",
        "-subj", "/CN=anymic",
        "-addext", f"subjectAltName=IP:{ip},IP:127.0.0.1,DNS:localhost",
    ], check=True, capture_output=True)
    os.chmod(keyf, 0o600)
    return cert, keyf


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))   # no packets sent; just picks the route
        return s.getsockname()[0]
    finally:
        s.close()


def main():
    ap = argparse.ArgumentParser(
        prog="anymic", description="AnyMic — turn any phone into a Linux microphone.")
    ap.add_argument("--port", type=int, default=8443)
    ap.add_argument("--name", default="AnyMic", help="PipeWire node name")
    ap.add_argument("--description", default="AnyMic (Phone)",
                    help="name shown in app microphone lists")
    ap.add_argument("--certdir", default=os.path.join(HERE, "certs"))
    ap.add_argument("--backend", choices=("auto", "pipewire", "pulse"), default="auto",
                    help="audio backend (default: prefer PipeWire, fall back to PulseAudio)")
    args = ap.parse_args()

    for tool in ("pactl", "openssl"):
        if not shutil.which(tool):
            sys.exit(f"missing required tool: {tool}")
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

    mic = backend(args.name, args.description)
    try:
        mic.start()
    except Exception:
        httpd.server_close()
        raise
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
    if mic.BACKEND == "pulseaudio":
        print(f"   Select 'Monitor of {args.description}' as the mic in your apps.")
        print("   (PulseAudio has no virtual-source type, so it appears as a")
        print("    monitor. Some apps hide these behind a 'show monitors' option.)")
    else:
        print(f"   Select '{args.description}' as the mic in your apps.")
    print("   Ctrl-C to stop.")
    print()
    # Block-buffered whenever stdout isn't a terminal (a log file, systemd's
    # journal). Without this the URL — the one thing you need — sits in the
    # buffer until exit.
    sys.stdout.flush()

    def shutdown(*_):
        log("shutting down")
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        httpd.serve_forever()
    finally:
        mic.stop()
        httpd.server_close()


if __name__ == "__main__":
    main()
