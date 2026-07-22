#!/usr/bin/env python3
"""
Example Python program imported into remote_pdb_lib.

Run:
  python3 remote_pdb_target.py

Then open remote_pdb_control.html and connect to ws://127.0.0.1:8765.
"""

import time

from remote_pdb_lib import run_debugged


def add(a, b):
    total = a + b
    return total


def format_debug_line(iteration, a, b, total, history):
    recent = ", ".join(str(item) for item in history[-3:])
    return f"[target] loop={iteration:03d} a={a} b={b} a+b={total} recent=[{recent}]"


def changing_values():
    a = 1
    b = 10
    iteration = 0
    history = []

    while True:
        iteration += 1
        a += 1
        b += iteration
        total = add(a, b)
        history.append(total)
        message = format_debug_line(iteration, a, b, total, history)
        print(message)

        if len(history) > 6:
            history.pop(0)
        time.sleep(0.6)


if __name__ == "__main__":
    run_debugged(changing_values, host="127.0.0.1", port=8765, target_name="changing_values_demo")
