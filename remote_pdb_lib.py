#!/usr/bin/env python3
"""
Small standard-library remote PDB library.

Typical use:

    from remote_pdb_lib import run_debugged

    def main():
        ...

    if __name__ == "__main__":
        run_debugged(main)
"""

import asyncio
import base64
import hashlib
import json
import pdb
import queue
import reprlib
import struct
import threading
import time
from pathlib import Path


class DebugControl:
    def __init__(self, target_name="python-target"):
        self.target_name = target_name
        self.lock = threading.RLock()
        self.resume_event = threading.Event()
        self.outbox = queue.Queue()
        self.mode = "pause"
        self.pause_requested = True
        self.breakpoints = set()
        self.current_frame = None
        self.current_event = "startup"
        self.current_arg = None
        self.last_command = {"command": "startup", "at": time.time()}
        self.connected_clients = 0
        self.stop_requested = False

    def command(self, command, **payload):
        with self.lock:
            self.last_command = {"command": command, "payload": payload, "at": time.time()}
            if command == "pause":
                self.pause_requested = True
                self.mode = "pause"
            elif command == "continue":
                self.pause_requested = False
                self.mode = "continue"
                self.resume_event.set()
            elif command in {"step", "next"}:
                self.pause_requested = False
                self.mode = command
                self.resume_event.set()
            elif command == "stop":
                self.stop_requested = True
                self.resume_event.set()
            elif command == "breakpoint":
                line = int(payload["line"])
                enabled = bool(payload.get("enabled", True))
                if enabled:
                    self.breakpoints.add(line)
                else:
                    self.breakpoints.discard(line)
            elif command == "eval":
                expression = str(payload.get("expression", ""))
                self.publish(self.evaluate(expression))
        self.publish_status("command")

    def publish(self, payload):
        self.outbox.put(payload)

    def publish_status(self, reason):
        with self.lock:
            self.publish(self.snapshot(reason))

    def wait_if_needed(self, frame, event, arg, force=False):
        with self.lock:
            self.current_frame = frame
            self.current_event = event
            self.current_arg = arg
            line = frame.f_lineno
            should_stop = (
                force
                or self.pause_requested
                or self.mode in {"step", "next"}
                or line in self.breakpoints
            )
            if not should_stop:
                return
            self.mode = "pause"
            self.pause_requested = True
            self.resume_event.clear()
            self.publish(self.snapshot("paused"))

        self.resume_event.wait()
        with self.lock:
            if self.stop_requested:
                raise SystemExit("remote debugger stopped")

    def evaluate(self, expression):
        frame = self.current_frame
        if frame is None:
            return {"type": "eval", "ok": False, "error": "no current frame"}
        try:
            result = eval(expression, frame.f_globals, frame.f_locals)
            return {"type": "eval", "ok": True, "expression": expression, "result": safe_repr(result)}
        except Exception as exc:
            return {"type": "eval", "ok": False, "expression": expression, "error": repr(exc)}

    def snapshot(self, reason):
        frame = self.current_frame
        if frame is None:
            return {
                "type": "status",
                "target": self.target_name,
                "reason": reason,
                "state": "starting",
                "last_command": self.last_command,
                "breakpoints": sorted(self.breakpoints),
                "clients": self.connected_clients,
            }

        return {
            "type": "status",
            "target": self.target_name,
            "reason": reason,
            "state": "paused" if self.pause_requested else "running",
            "event": self.current_event,
            "arg": safe_repr(self.current_arg),
            "file": frame.f_code.co_filename,
            "line": frame.f_lineno,
            "function": frame.f_code.co_name,
            "locals": snapshot_locals(frame),
            "globals_sample": {k: safe_repr(v) for k, v in list(frame.f_globals.items())[:12]},
            "stack": snapshot_stack(frame),
            "source": source_window(frame.f_code.co_filename, frame.f_lineno),
            "breakpoints": sorted(self.breakpoints),
            "last_command": self.last_command,
            "clients": self.connected_clients,
        }


class RemotePdb(pdb.Pdb):
    def __init__(self, control):
        super().__init__()
        self.control = control

    def trace_dispatch(self, frame, event, arg):
        if event in {"line", "call", "return", "exception"}:
            self.control.wait_if_needed(frame, event, arg)
        return self.trace_dispatch


def safe_repr(value):
    try:
        return reprlib.repr(value)
    except Exception as exc:
        return f"<repr failed: {exc!r}>"


def snapshot_locals(frame):
    return {key: safe_repr(value) for key, value in frame.f_locals.items()}


def snapshot_stack(frame):
    stack = []
    while frame is not None:
        stack.append(
            {
                "file": frame.f_code.co_filename,
                "line": frame.f_lineno,
                "function": frame.f_code.co_name,
            }
        )
        frame = frame.f_back
    return stack[:20]


def source_window(filename, lineno, radius=5):
    try:
        all_lines = Path(filename).read_text(encoding="utf-8").splitlines()
    except Exception as exc:
        return [{"line": lineno, "text": f"<source unavailable: {exc}>", "current": True}]
    start_line = max(1, lineno - radius)
    end_line = min(len(all_lines), lineno + radius)
    return [
        {"line": idx, "text": all_lines[idx - 1], "current": idx == lineno}
        for idx in range(start_line, end_line + 1)
    ]


def run_debugged(target, *args, host="127.0.0.1", port=8765, target_name=None, **kwargs):
    """Run a Python callable under remote PDB control."""
    control = DebugControl(target_name or getattr(target, "__name__", "python-target"))
    thread = threading.Thread(
        target=_run_debuggee,
        args=(control, target, args, kwargs),
        daemon=True,
    )
    thread.start()
    try:
        asyncio.run(_serve(control, host, port))
    except KeyboardInterrupt:
        print("\n[remote-pdb] stopped")


def _run_debuggee(control, target, args, kwargs):
    debugger = RemotePdb(control)
    control.publish_status("debugger-ready")
    try:
        debugger.runcall(target, *args, **kwargs)
    except SystemExit as exc:
        control.publish({"type": "terminated", "reason": str(exc)})
    except BaseException as exc:
        control.publish({"type": "exception", "error": repr(exc)})
        raise
    else:
        control.publish({"type": "terminated", "reason": "target returned"})


async def _serve(control, host, port):
    server = await asyncio.start_server(
        lambda reader, writer: websocket_client(reader, writer, control),
        host,
        port,
    )
    sockets = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
    print(f"[remote-pdb] websocket listening on {sockets}")
    print(f"[remote-pdb] open remote_pdb_control.html, then connect to ws://{host}:{port}")
    async with server:
        await server.serve_forever()


async def read_http_header(reader):
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = await reader.read(1024)
        if not chunk:
            raise ConnectionError("client closed before websocket handshake")
        data += chunk
    return data.decode("latin1")


def parse_headers(header_text):
    lines = header_text.split("\r\n")
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.lower()] = value.strip()
    return headers


async def send_ws(writer, payload):
    if isinstance(payload, dict):
        payload = json.dumps(payload, ensure_ascii=False)
    data = payload.encode("utf-8")
    if len(data) < 126:
        header = bytes([0x81, len(data)])
    elif len(data) < 65536:
        header = bytes([0x81, 126]) + struct.pack("!H", len(data))
    else:
        header = bytes([0x81, 127]) + struct.pack("!Q", len(data))
    writer.write(header + data)
    await writer.drain()


async def recv_ws(reader):
    first = await reader.readexactly(2)
    opcode = first[0] & 0x0F
    masked = first[1] & 0x80
    length = first[1] & 0x7F
    if length == 126:
        length = struct.unpack("!H", await reader.readexactly(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", await reader.readexactly(8))[0]
    mask = await reader.readexactly(4) if masked else b""
    data = await reader.readexactly(length)
    if masked:
        data = bytes(byte ^ mask[idx % 4] for idx, byte in enumerate(data))
    if opcode == 8:
        raise ConnectionError("websocket closed")
    return data.decode("utf-8")


async def websocket_client(reader, writer, control):
    header_text = await read_http_header(reader)
    headers = parse_headers(header_text)
    key = headers.get("sec-websocket-key")
    if not key:
        writer.close()
        return

    accept = base64.b64encode(
        hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
    ).decode()
    writer.write(
        (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        ).encode("latin1")
    )
    await writer.drain()

    with control.lock:
        control.connected_clients += 1
    await send_ws(writer, {"type": "hello", "message": "remote pdb connected"})
    await send_ws(writer, control.snapshot("client-connected"))

    pump_task = asyncio.create_task(pump_outbox(writer, control))
    try:
        while True:
            text = await recv_ws(reader)
            try:
                message = json.loads(text)
                command = message.get("command")
                payload = message.get("payload") or {}
                if command:
                    control.command(command, **payload)
                else:
                    await send_ws(writer, {"type": "error", "error": "missing command"})
            except Exception as exc:
                await send_ws(writer, {"type": "error", "error": repr(exc), "raw": text})
    except Exception:
        with control.lock:
            control.connected_clients -= 1
            if control.connected_clients <= 0:
                control.connected_clients = 0
                control.pause_requested = True
                control.mode = "pause"
                control.last_command = {"command": "auto-pause-on-disconnect", "at": time.time()}
        control.publish_status("client-disconnected")
    finally:
        pump_task.cancel()
        writer.close()
        await writer.wait_closed()


async def pump_outbox(writer, control):
    while True:
        payload = await asyncio.to_thread(control.outbox.get)
        await send_ws(writer, payload)
