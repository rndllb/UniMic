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
# PipeWire virtual microphone
# --------------------------------------------------------------------------

class VirtualMic:
    """A PipeWire virtual source, fed through pw-cat.

    WirePlumber's policy will not route a playback stream onto a node whose
    media.class is Audio/Source/Virtual, so pw-cat gets auto-connected to the
    default sink (i.e. the speakers) instead. We therefore start pw-cat with
    autoconnect disabled and link it into the virtual source by hand.
    """

    def __init__(self, name="AnyMic", description="AnyMic (Phone)"):
        self.name = name
        self.description = description
        self.feed_node = "anymic-feed"
        self.module_id = None
        self.proc = None
        self.buffer = JitterBuffer()
        self._stop = threading.Event()
        self._feeder = None

    def _run(self, args, **kw):
        return subprocess.run(args, capture_output=True, text=True, **kw)

    def start(self):
        self._unload_stale()

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
        log(f"virtual source '{self.name}' created (module {self.module_id})")

        env = dict(os.environ)
        env["PIPEWIRE_PROPS"] = (
            "{ node.autoconnect=false node.name=%s }" % self.feed_node
        )
        self.proc = subprocess.Popen(
            ["pw-cat", "--playback", "--format=s16", f"--rate={RATE}",
             f"--channels={CHANNELS}", "--raw", "-"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, env=env, bufsize=0,
            # Own session, so a Ctrl-C aimed at our process group doesn't kill
            # pw-cat out from under the feeder thread. We want to tear it down
            # in order, after the audio path has been quiesced.
            start_new_session=True,
        )

        if not self._link():
            self.stop()
            raise RuntimeError("could not link the feed into the virtual source")

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

    def _feed_loop(self):
        """Write to pw-cat at exactly real time, filling gaps with silence."""
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


# --------------------------------------------------------------------------
# WebSocket
# --------------------------------------------------------------------------

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

    def _handle_ws(self):
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            self.send_error(400, "not a websocket request")
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
        sock = self.connection
        peer = self.client_address[0]
        log(f"phone connected from {peer}")
        mic.buffer.reset()

        stop = threading.Event()

        def stats_loop():
            while not stop.wait(1.0):
                try:
                    ws_send(sock, json.dumps(mic.stats()).encode())
                except OSError:
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
                    break
                elif opcode == 0x9:          # ping
                    ws_send(sock, payload, opcode=0xA)
        except (OSError, ssl.SSLError):
            pass
        finally:
            stop.set()
            mic.buffer.reset()
            log(f"phone disconnected ({peer})")


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
    args = ap.parse_args()

    for tool in ("pactl", "pw-cat", "pw-link", "openssl"):
        if not shutil.which(tool):
            sys.exit(f"missing required tool: {tool}")

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

    mic = VirtualMic(args.name, args.description)
    try:
        mic.start()
    except Exception:
        httpd.server_close()
        raise
    httpd.mic = mic

    url = f"https://{ip}:{args.port}/"
    print()
    print("  " + "=" * 52)
    print("   A N Y M I C")
    print(f"   Open this on your phone:   {url}")
    print("  " + "=" * 52)
    print("   The certificate is self-signed, so the browser will warn.")
    print("   Tap Advanced -> Proceed. Then tap Start.")
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
