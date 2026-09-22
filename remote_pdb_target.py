#!/usr/bin/env python3
"""
Example Python program using the remote_pdb_lib bottom layer.

Run:
  python3 remote_pdb_target.py

Then open remote_pdb_control.html and connect to ws://127.0.0.1:8765.
"""

import time

from remote_pdb_lib import boot, checkpoint


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
        checkpoint("loop-start", "Before local variables change in this iteration")
        iteration += 1
        a += 1
        b += iteration
        checkpoint("after-update", "a, b, and iteration have changed")
        total = add(a, b)
        history.append(total)
        message = format_debug_line(iteration, a, b, total, history)
        checkpoint("before-print", "message is ready; inspect total/history/message here")
        print(message)

        # image.draw_string("[remote_pdb_target] loop={iteration:03d} a={a} b={b} a+b={total} recent=[{recent}]")
        
        if len(history) > 6:
            history.pop(0)
            checkpoint("history-trimmed", "history was longer than 6 and has been trimmed")
        time.sleep(0.6)


if __name__ == "__main__":
    boot(changing_values, host="127.0.0.1", port=8765, target_name="changing_values_demo")
