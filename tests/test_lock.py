#!/usr/bin/env python3
"""Unit tests for MicLock — who owns the microphone, and when.

    python3 tests/test_lock.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import anymic  # noqa: E402

failures = []


def check(name, cond):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond:
        failures.append(name)


def main():
    anymic.MicLock.RESERVE_SECONDS = 1.0     # keep the expiry test quick
    lock = anymic.MicLock()

    granted, tok_a, _ = lock.claim(None, "phoneA")
    check("first caller is granted", granted and tok_a)

    granted, _, why = lock.claim(None, "phoneB")
    check("second caller refused while the first is connected",
          not granted and why == "in-use")

    granted, _, _ = lock.claim("not-the-token", "phoneB")
    check("a wrong token is refused", not granted)

    granted, tok, _ = lock.claim(tok_a, "phoneA")
    check("the holder's own token still works (duplicate socket)",
          granted and tok == tok_a)

    # --- an unexpected drop reserves the mic for its owner ---
    lock.release(tok_a, deliberate=False)
    granted, _, why = lock.claim(None, "phoneB")
    check("a stranger is refused during the reservation",
          not granted and why == "reserved")

    granted, tok, _ = lock.claim(tok_a, "phoneA")
    check("the owner reclaims it with the token", granted and tok == tok_a)

    # --- the reservation eventually lapses ---
    lock.release(tok_a, deliberate=False)
    time.sleep(anymic.MicLock.RESERVE_SECONDS + 0.2)
    granted, tok_b, _ = lock.claim(None, "phoneB")
    check("a stranger is granted once the reservation lapses", granted)
    check("a lapsed reservation issues a fresh token", tok_b != tok_a)

    # --- pressing Stop hands over immediately ---
    lock.release(tok_b, deliberate=True)
    granted, tok_c, _ = lock.claim(None, "phoneC")
    check("a deliberate Stop frees the mic at once",
          granted and tok_c not in (tok_a, tok_b))

    # --- a stale token confers nothing ---
    lock.release(tok_c, deliberate=True)
    granted, tok_d, _ = lock.claim(tok_a, "phoneA")
    check("a stale token gets a fresh grant, not the old identity",
          granted and tok_d != tok_a)

    # --- only the holder may release ---
    lock.release("someone-elses-token", deliberate=True)
    granted, _, why = lock.claim(None, "phoneZ")
    check("release() from a non-holder is ignored",
          not granted and why == "in-use")

    print()
    if failures:
        print(f"FAILED: {len(failures)}")
        return 1
    print("all lock rules hold")
    return 0


if __name__ == "__main__":
    sys.exit(main())
