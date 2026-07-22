#!/usr/bin/env python3
"""
Exception output capture target for remote_pdb_lib.

Run:
  python3 remote_pdb_lib.py remote_pdb_exception_target:exception_demo 127.0.0.1 8766

Then open remote_pdb_control.html, connect to ws://127.0.0.1:8766, and press Continue.
"""

import sys
import time
import traceback

from remote_pdb_lib import checkpoint


def nested_failure(value):
    checkpoint("exception-before-divide", "about to divide by zero inside nested_failure")
    return value / 0


def exception_demo():
    print("[exception-demo] stdout before handled exception", flush=True)
    print("[exception-demo] stderr before handled exception", file=sys.stderr, flush=True)
    checkpoint("exception-start", "stdout/stderr have already printed")

    try:
        nested_failure(42)
    except ZeroDivisionError:
        print("[exception-demo] caught ZeroDivisionError, printing traceback to stderr", flush=True)
        traceback.print_exc()
        checkpoint("exception-after-print-exc", "traceback.print_exc() has written to stderr")

    for index in range(3):
        print(f"[exception-demo] loop index={index}", flush=True)
        time.sleep(0.2)

    checkpoint("exception-before-uncaught", "next line raises an uncaught RuntimeError")
    raise RuntimeError("uncaught test exception for websocket stderr/output capture")


if __name__ == "__main__":
    from remote_pdb_lib import boot

    boot(exception_demo, host="127.0.0.1", port=8766, target_name="exception_demo")
