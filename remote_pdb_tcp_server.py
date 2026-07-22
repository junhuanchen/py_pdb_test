#!/usr/bin/env python3
"""
TCP server target for remote_pdb_lib restart cleanup testing.

Run under the debugger with:
  python3 remote_pdb_lib.py remote_pdb_tcp_server:tcp_echo_server 127.0.0.1 8765

Then open remote_pdb_control.html and connect to ws://127.0.0.1:8765.
Use Restart in the HTML while tcp_time_client.py is sending data.
"""

import json
import socket
import time

from remote_pdb_lib import checkpoint


TCP_HOST = "127.0.0.1"
TCP_PORT = 9099


def describe_socket(sock):
    if sock is None:
        return {"exists": False}
    try:
        local = sock.getsockname()
    except OSError as exc:
        local = f"closed: {exc}"
    try:
        remote = sock.getpeername()
    except OSError:
        remote = None
    return {
        "exists": True,
        "fd": sock.fileno(),
        "local": local,
        "remote": remote,
        "timeout": sock.gettimeout(),
    }


def close_socket(name, sock):
    if sock is None:
        return
    before = describe_socket(sock)
    print(f"[tcp-server] closing {name}: {before}", flush=True)
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    finally:
        print(f"[tcp-server] closed {name}: fd={sock.fileno()}", flush=True)


def tcp_echo_server():
    run_id = int(time.time() * 1000)
    server_sock = None
    conn = None
    addr = None
    message_count = 0
    last_message = None

    try:
        checkpoint("tcp-before-create", "server_sock is still None before socket()")
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_sock.settimeout(0.5)
        checkpoint("tcp-after-create", "server_sock exists; inspect fd before bind")

        server_sock.bind((TCP_HOST, TCP_PORT))
        server_sock.listen(1)
        print(
            f"[tcp-server] run_id={run_id} listening on {TCP_HOST}:{TCP_PORT} "
            f"server={describe_socket(server_sock)}",
            flush=True,
        )
        checkpoint("tcp-listening", "server socket is bound and listening")

        while True:
            if conn is None:
                try:
                    conn, addr = server_sock.accept()
                except socket.timeout:
                    checkpoint("tcp-accept-timeout", "no client yet; restart here should close server_sock")
                    continue
                conn.settimeout(0.5)
                print(
                    f"[tcp-server] accepted addr={addr} conn={describe_socket(conn)}",
                    flush=True,
                )
                checkpoint("tcp-client-accepted", "conn socket exists; restart here should close conn and server_sock")

            try:
                data = conn.recv(4096)
            except socket.timeout:
                checkpoint("tcp-recv-timeout", "client connected but no data in this tick")
                continue
            except ConnectionResetError:
                print("[tcp-server] client reset connection", flush=True)
                close_socket("conn", conn)
                conn = None
                addr = None
                continue

            if not data:
                print("[tcp-server] client disconnected", flush=True)
                close_socket("conn", conn)
                conn = None
                addr = None
                continue

            message_count += 1
            text = data.decode("utf-8", errors="replace").strip()
            last_message = text
            checkpoint("tcp-message-received", "inspect data/text/message_count/last_message")

            reply = {
                "run_id": run_id,
                "message_count": message_count,
                "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "received": text,
                "server_sock": describe_socket(server_sock),
                "conn": describe_socket(conn),
            }
            conn.sendall((json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8"))
            checkpoint("tcp-reply-sent", "reply was sent; restart here should still cleanup sockets")

    finally:
        checkpoint("tcp-finally-cleanup", "finally block is closing sockets")
        close_socket("conn", conn)
        close_socket("server_sock", server_sock)
        print(
            f"[tcp-server] cleanup complete run_id={run_id} "
            f"message_count={message_count} last_message={last_message!r}",
            flush=True,
        )


if __name__ == "__main__":
    from remote_pdb_lib import boot

    boot(tcp_echo_server, host="127.0.0.1", port=8765, target_name="tcp_echo_server")
