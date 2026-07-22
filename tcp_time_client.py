#!/usr/bin/env python3
"""
Looping TCP client for remote_pdb_tcp_server.py.

Run while the debugged TCP server is active:
  python3 tcp_time_client.py
"""

import json
import socket
import time


HOST = "127.0.0.1"
PORT = 9099
INTERVAL_SECONDS = 1.0


def connect():
    sock = socket.create_connection((HOST, PORT), timeout=3)
    sock.settimeout(3)
    print(f"[tcp-client] connected local={sock.getsockname()} remote={sock.getpeername()}", flush=True)
    return sock


def main():
    sequence = 0
    sock = None

    while True:
        if sock is None:
            try:
                sock = connect()
            except OSError as exc:
                print(f"[tcp-client] connect failed: {exc}; retrying", flush=True)
                time.sleep(INTERVAL_SECONDS)
                continue

        sequence += 1
        payload = {
            "sequence": sequence,
            "client_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "epoch": time.time(),
        }

        try:
            wire = json.dumps(payload, ensure_ascii=False) + "\n"
            sock.sendall(wire.encode("utf-8"))
            print(f"[tcp-client] sent {payload}", flush=True)
            reply = sock.recv(8192)
            if not reply:
                print("[tcp-client] server closed connection", flush=True)
                sock.close()
                sock = None
            else:
                print(f"[tcp-client] recv {reply.decode('utf-8', errors='replace').strip()}", flush=True)
        except OSError as exc:
            print(f"[tcp-client] socket error: {exc}; reconnecting", flush=True)
            try:
                sock.close()
            except OSError:
                pass
            sock = None

        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[tcp-client] stopped", flush=True)
