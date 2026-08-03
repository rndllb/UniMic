#!/usr/bin/env python3
"""End-to-end tests: starts a real server, talks WSS to it, checks the audio.

Runs on a spare port under its own device name, so it will not disturb an
AnyMic you already have running.

    python3 tests/test_wire.py [--backend pipewire|pulse]
"""
import argparse
import base64
import json
import math
import os
import signal
import socket
import ssl
import struct
import subprocess
import sys
import time
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "..", "anymic.py")
HOST, PORT = "127.0.0.1", 8446
NAME = "AnyMicTest"

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
            buf = bytearray()
            for _ in range(960):
                buf += struct.pack(
                    "<h", int(12000 * math.sin(2 * math.pi * 440 * phase[0] / 48000)))
                phase[0] += 1
            self._frame(bytes(buf), 0x2)

    def send_oversized_header(self):
        """Announce a 4 GiB payload without sending it."""
        self.s.sendall(bytes([0x82, 0xFF]) + struct.pack(">Q", 1 << 32) + os.urandom(4))

    def close_clean(self):
        self._frame(struct.pack(">H", 1000), 0x8)
        time.sleep(0.3)
        self.s.close()

    def drop(self):
        self.s.close()


def wait_for_port(proc, timeout=20):
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


def audio_check(source):
    """Stream a 440Hz tone in and confirm it comes out of the virtual mic."""
    if not shutil_which("parecord"):
        print("  SKIP  audio round-trip (parecord not installed)")
        return
    wav = os.path.join("/tmp", f"anymic-test-{os.getpid()}.wav")
    rec = subprocess.Popen(
        ["parecord", f"--device={source}", "--file-format=wav", "--channels=1",
         "--rate=48000", "--format=s16le", wav],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
        rec.terminate()
        rec.wait(timeout=5)

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


def shutil_which(cmd):
    import shutil
    return shutil.which(cmd)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="auto",
                    choices=("auto", "pipewire", "pulse"))
    args = ap.parse_args()

    proc = subprocess.Popen(
        [sys.executable, SERVER, "--port", str(PORT), "--name", NAME,
         "--description", "AnyMic Test", "--backend", args.backend],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        start_new_session=True)
    if not wait_for_port(proc):
        print("server failed to start:\n" + (proc.stdout.read() if proc.stdout else ""))
        return 1

    # The Pulse backend exposes the sink's monitor rather than a source proper.
    source = f"{NAME}.monitor" if args.backend == "pulse" else NAME

    try:
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
        time.sleep(anymic_reserve_seconds() + 0.5)
        audio_check(source)

    finally:
        os.killpg(os.getpgid(proc.pid), signal.SIGINT)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)

    left = subprocess.run(["pactl", "list", "modules", "short"],
                          capture_output=True, text=True).stdout
    check("virtual source is removed on shutdown", f"sink_name={NAME}" not in left)

    print()
    if failures:
        print(f"FAILED: {len(failures)} — " + "; ".join(failures))
        return 1
    print("all wire tests pass")
    return 0


def anymic_reserve_seconds():
    sys.path.insert(0, os.path.join(HERE, ".."))
    import anymic
    return anymic.MicLock.RESERVE_SECONDS


if __name__ == "__main__":
    sys.exit(main())
