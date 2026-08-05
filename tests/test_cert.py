#!/usr/bin/env python3
"""Tests for the built-in certificate generator.

AnyMic emits its own self-signed certificate when openssl is not around, which
is the normal case on Windows. A certificate that OpenSSL will not parse means
a browser that will not connect, so this checks the DER is real: the pair
loads, a verifying client completes a handshake against it, and the SAN carries
the addresses the phone will actually use.

    python3 tests/test_cert.py
"""
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import anymic  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   [{detail}]" if detail else ""))
    if not cond:
        failures.append(name)


def handshake(cert, key, ip):
    """Serve once with the pair, connect with verification on, return the cert."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)

    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    err = []

    def serve():
        try:
            conn, _ = srv.accept()
            with ctx.wrap_socket(conn, server_side=True) as s:
                s.recv(16)
                s.sendall(b"ok")
        except Exception as e:                          # noqa: BLE001
            err.append(e)

    threading.Thread(target=serve, daemon=True).start()

    # Trust nothing but this certificate, and hold it to the 127.0.0.1 SAN
    # entry — exactly the check a browser makes, minus the trust store.
    cctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    cctx.load_verify_locations(cert)
    cctx.check_hostname = True
    with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
        with cctx.wrap_socket(raw, server_hostname="127.0.0.1") as s:
            s.sendall(b"ping")
            reply = s.recv(16)
            peer = s.getpeercert()
    srv.close()
    if err:
        raise err[0]
    return reply, peer


def main():
    ip = "192.168.1.190"
    tmp = tempfile.mkdtemp(prefix="anymic-cert-")
    cert = os.path.join(tmp, "cert.pem")
    key = os.path.join(tmp, "key.pem")

    print("--- the built-in generator ---")
    t0 = time.monotonic()
    cert_pem, key_pem = anymic._self_signed(ip)
    took = time.monotonic() - t0
    check("generates a certificate in reasonable time", took < 60, f"{took:.1f}s")
    check("emits PEM of the expected shape",
          cert_pem.startswith("-----BEGIN CERTIFICATE-----") and
          key_pem.startswith("-----BEGIN PRIVATE KEY-----"))

    with open(cert, "w") as f:
        f.write(cert_pem)
    with open(key, "w") as f:
        f.write(key_pem)

    try:
        reply, peer = handshake(cert, key, ip)
    except Exception as e:                              # noqa: BLE001
        check("a verifying client completes the handshake", False, str(e))
        print(f"\nFAILED: {len(failures)}")
        return 1

    check("a verifying client completes the handshake", reply == b"ok")
    san = peer.get("subjectAltName", ())
    check("the LAN address is in the SAN", ("IP Address", ip) in san, str(san))
    check("127.0.0.1 is in the SAN", ("IP Address", "127.0.0.1") in san)
    check("localhost is in the SAN", ("DNS", "localhost") in san)
    check("the subject is CN=anymic",
          ("commonName", "anymic") in [x for rdn in peer["subject"] for x in rdn])

    # An independent parser is worth having when we wrote the DER by hand.
    if shutil.which("openssl"):
        print("--- a second opinion from openssl ---")
        r = subprocess.run(["openssl", "x509", "-in", cert, "-noout", "-text"],
                           capture_output=True, text=True)
        check("openssl parses the certificate", r.returncode == 0,
              r.stderr.strip()[:80])
        check("it is an X.509 v3 certificate", "Version: 3 (0x2)" in r.stdout)
        check("it is signed with sha256WithRSAEncryption",
              "sha256WithRSAEncryption" in r.stdout)
        check("it is marked not-a-CA", "CA:FALSE" in r.stdout)
        check("key usage is set for a TLS server",
              "Digital Signature" in r.stdout and "TLS Web Server" in r.stdout)
        v = subprocess.run(["openssl", "verify", "-CAfile", cert, cert],
                           capture_output=True, text=True)
        check("openssl verifies it against itself", v.returncode == 0,
              (v.stdout + v.stderr).strip()[:80])
    else:
        print("  SKIP  second opinion from openssl (not installed)")

    print("--- ensure_cert on disk ---")
    d2 = tempfile.mkdtemp(prefix="anymic-ensure-")
    c1, k1 = anymic.ensure_cert(d2, ip)
    m1 = os.path.getmtime(c1)
    check("writes a cert and key", os.path.exists(c1) and os.path.exists(k1))
    time.sleep(1.1)
    c2, _ = anymic.ensure_cert(d2, ip)
    check("reuses the certificate for the same address",
          os.path.getmtime(c2) == m1)
    anymic.ensure_cert(d2, "10.0.0.7")
    check("regenerates when the LAN address changes",
          os.path.getmtime(c1) != m1)

    shutil.rmtree(tmp, ignore_errors=True)
    shutil.rmtree(d2, ignore_errors=True)

    print()
    if failures:
        print(f"FAILED: {len(failures)} — " + "; ".join(failures))
        return 1
    print("all certificate tests pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
