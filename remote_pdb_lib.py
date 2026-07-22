#!/usr/bin/env python3
"""
Small standard-library remote PDB library.

Typical use:

    from remote_pdb_lib import boot, checkpoint

    def main():
        ...

    if __name__ == "__main__":
        boot(main)

Or run from this library directly:

    python3 remote_pdb_lib.py your_module:main
"""

import asyncio
import base64
import bdb
import hashlib
import importlib
import json
import pdb
import queue
import reprlib
import struct
import sys
import threading
import time
from pathlib import Path


LIBRARY_FILE = str(Path(__file__).resolve())
PDB_FILE = str(Path(pdb.__file__).resolve())
BDB_FILE = str(Path(bdb.__file__).resolve())
STDLIB_ROOT = str(Path(sys.base_prefix).resolve())


class DebugControl:
    def __init__(self, target_name="python-target", restart_callback=None, traced_files=None):
        self.target_name = target_name
        self.restart_callback = restart_callback
        self.traced_files = {str(Path(item).resolve()) for item in traced_files or []}
        self.lock = threading.RLock()
        self.resume_event = threading.Event()
        self.subscribers = set()
        self.mode = "pause"
        self.pause_requested = True
        self.breakpoints = set()
        self.current_frame = None
        self.current_event = "startup"
        self.current_arg = None
        self.last_command = {"command": "startup", "at": time.time()}
        self.connected_clients = 0
        self.stop_requested = False
        self.special_breakpoints = {}
        self.enabled_special_breakpoints = set()
        self.current_special_breakpoint = None

    def reset_for_restart(self):
        with self.lock:
            self.resume_event.clear()
            self.mode = "pause"
            self.pause_requested = True
            self.current_frame = None
            self.current_event = "restart"
            self.current_arg = None
            self.current_special_breakpoint = None
            self.last_command = {"command": "restart", "at": time.time()}
            self.stop_requested = False
        self.publish_status("restarted")

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
            elif command == "restart":
                self.stop_requested = True
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
            elif command == "special_breakpoint":
                name = str(payload["name"])
                enabled = bool(payload.get("enabled", True))
                if enabled:
                    self.enabled_special_breakpoints.add(name)
                else:
                    self.enabled_special_breakpoints.discard(name)
            elif command == "goto_special":
                name = str(payload["name"])
                self.enabled_special_breakpoints.add(name)
                self.pause_requested = False
                self.mode = "continue"
                self.resume_event.set()
        self.publish_status("command")
        if command == "restart" and self.restart_callback:
            self.restart_callback()

    def publish(self, payload):
        with self.lock:
            subscribers = list(self.subscribers)
        for subscriber in subscribers:
            subscriber.put(payload)

    def add_subscriber(self):
        subscriber = queue.Queue()
        with self.lock:
            self.subscribers.add(subscriber)
        return subscriber

    def remove_subscriber(self, subscriber):
        with self.lock:
            self.subscribers.discard(subscriber)

    def publish_status(self, reason):
        with self.lock:
            self.publish(self.snapshot(reason))

    def wait_if_needed(self, frame, event, arg, force=False):
        with self.lock:
            if self.stop_requested:
                raise SystemExit("remote debugger stopped")
            self.current_frame = frame
            self.current_event = event
            self.current_arg = arg
            self.current_special_breakpoint = None
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

    def special_breakpoint(self, name, frame, note=None):
        with self.lock:
            self.current_frame = frame
            self.current_event = "special"
            self.current_arg = note
            self.special_breakpoints[name] = {
                "name": name,
                "note": note or "",
                "file": frame.f_code.co_filename,
                "line": frame.f_lineno,
                "function": frame.f_code.co_name,
            }
            self.current_special_breakpoint = name
            should_stop = name in self.enabled_special_breakpoints or self.pause_requested
            if not should_stop:
                self.publish_status("special-breakpoint-seen")
                return
            self.mode = "pause"
            self.pause_requested = True
            self.resume_event.clear()
            self.publish(self.snapshot("special-breakpoint"))

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
                "special_breakpoints": list(self.special_breakpoints.values()),
                "enabled_special_breakpoints": sorted(self.enabled_special_breakpoints),
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
            "special_breakpoints": list(self.special_breakpoints.values()),
            "enabled_special_breakpoints": sorted(self.enabled_special_breakpoints),
            "current_special_breakpoint": self.current_special_breakpoint,
            "last_command": self.last_command,
            "clients": self.connected_clients,
        }


class RemotePdb:
    def __init__(self, control):
        self.control = control

    def trace_dispatch(self, frame, event, arg):
        filename = str(Path(frame.f_code.co_filename).resolve())
        if filename in {LIBRARY_FILE, PDB_FILE, BDB_FILE}:
            return self.trace_dispatch
        if self.control.traced_files and filename not in self.control.traced_files:
            return self.trace_dispatch
        if filename.startswith(STDLIB_ROOT):
            return self.trace_dispatch
        if event in {"line", "call", "return", "exception"}:
            self.control.wait_if_needed(frame, event, arg)
        return self.trace_dispatch

    def runcall(self, target, *args, **kwargs):
        sys.settrace(self.trace_dispatch)
        try:
            return target(*args, **kwargs)
        finally:
            sys.settrace(None)


class OutputCapture:
    def __init__(self, control, stream_name, wrapped):
        self.control = control
        self.stream_name = stream_name
        self.wrapped = wrapped
        self.buffer = ""
        self.lock = threading.RLock()

    def write(self, text):
        with self.lock:
            written = self.wrapped.write(text)
            self.wrapped.flush()
            self.buffer += text
            while "\n" in self.buffer:
                line, self.buffer = self.buffer.split("\n", 1)
                self.publish_line(line)
            return written

    def flush(self):
        with self.lock:
            if self.buffer:
                self.publish_line(self.buffer)
                self.buffer = ""
            return self.wrapped.flush()

    def publish_line(self, line):
        self.control.publish(
            {
                "type": "output",
                "stream": self.stream_name,
                "text": line,
                "at": time.time(),
            }
        )

    def isatty(self):
        return self.wrapped.isatty()

    def fileno(self):
        return self.wrapped.fileno()

    def __getattr__(self, name):
        return getattr(self.wrapped, name)


_current_control = threading.local()
_registered_target = None
_registered_args = ()
_registered_kwargs = {}


def debug_target(name=None, *target_args, **target_kwargs):
    """Decorator that registers the function launched by boot()."""
    def decorator(func):
        global _registered_target, _registered_args, _registered_kwargs
        func.__remote_pdb_name__ = name or func.__name__
        _registered_target = func
        _registered_args = target_args
        _registered_kwargs = target_kwargs
        return func
    return decorator


def checkpoint(name, note=None):
    """Named business breakpoint shown and controllable from the HTML UI."""
    control = getattr(_current_control, "value", None)
    if control is None:
        return
    control.special_breakpoint(str(name), sys._getframe(1), note=note)


def load_target(target_ref):
    """Load a callable from 'module:function' or 'module.function'."""
    if callable(target_ref):
        return target_ref
    if not isinstance(target_ref, str):
        raise TypeError("target must be a callable or a 'module:function' string")

    module_name, separator, function_name = target_ref.partition(":")
    if not separator:
        module_name, separator, function_name = target_ref.rpartition(".")
    if not module_name or not function_name:
        raise ValueError("target string must look like 'module:function'")

    module = importlib.import_module(module_name)
    target = module
    for part in function_name.split("."):
        target = getattr(target, part)
    if not callable(target):
        raise TypeError(f"{target_ref!r} resolved to a non-callable object")
    return target


def boot(target=None, *args, host="127.0.0.1", port=8765, target_name=None, **kwargs):
    if target is None:
        if _registered_target is None:
            raise RuntimeError("no target supplied; call boot(main) or boot('module:function')")
        target = _registered_target
        args = _registered_args + args
        kwargs = {**_registered_kwargs, **kwargs}
    else:
        target = load_target(target)

    run_debugged(
        target,
        *args,
        host=host,
        port=port,
        target_name=target_name or getattr(target, "__remote_pdb_name__", target.__name__),
        **kwargs,
    )


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
    runner = DebugRunner(target, args, kwargs, target_name or getattr(target, "__name__", "python-target"))
    runner.start()
    try:
        asyncio.run(_serve(runner.control, host, port))
    except KeyboardInterrupt:
        print("\n[remote-pdb] stopped")


class DebugRunner:
    def __init__(self, target, args, kwargs, target_name):
        self.target = target
        self.args = args
        self.kwargs = kwargs
        self.thread = None
        self.lock = threading.RLock()
        traced_files = [target.__code__.co_filename]
        self.control = DebugControl(target_name, restart_callback=self.restart, traced_files=traced_files)

    def start(self):
        with self.lock:
            self.thread = threading.Thread(
                target=_run_debuggee,
                args=(self.control, self.target, self.args, self.kwargs),
                daemon=True,
            )
            self.thread.start()

    def restart(self):
        with self.lock:
            old_thread = self.thread
            self.control.stop_requested = True
            self.control.resume_event.set()
        if old_thread and old_thread.is_alive():
            old_thread.join(timeout=5.0)
        if old_thread and old_thread.is_alive():
            self.control.publish(
                {
                    "type": "error",
                    "error": "restart-blocked: previous debuggee did not exit within 5 seconds",
                }
            )
            return
        self.control.reset_for_restart()
        self.start()


def _run_debuggee(control, target, args, kwargs):
    debugger = RemotePdb(control)
    control.publish_status("debugger-ready")
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    try:
        _current_control.value = control
        sys.stdout = OutputCapture(control, "stdout", original_stdout)
        sys.stderr = OutputCapture(control, "stderr", original_stderr)
        debugger.runcall(target, *args, **kwargs)
    except SystemExit as exc:
        control.publish({"type": "terminated", "reason": str(exc)})
    except BaseException as exc:
        control.publish({"type": "exception", "error": repr(exc)})
        raise
    else:
        control.publish({"type": "terminated", "reason": "target returned"})
    finally:
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        finally:
            sys.stdout = original_stdout
            sys.stderr = original_stderr
        _current_control.value = None


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

    subscriber = control.add_subscriber()
    pump_task = asyncio.create_task(pump_outbox(writer, subscriber))
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
        control.remove_subscriber(subscriber)
        writer.close()
        await writer.wait_closed()


async def pump_outbox(writer, subscriber):
    while True:
        payload = await asyncio.to_thread(subscriber.get)
        await send_ws(writer, payload)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: python3 remote_pdb_lib.py module:function [host] [port]")
        print("example: python3 remote_pdb_lib.py remote_pdb_target:changing_values 127.0.0.1 8765")
        return 2
    target_ref = argv[0]
    host = argv[1] if len(argv) >= 2 else "127.0.0.1"
    port = int(argv[2]) if len(argv) >= 3 else 8765
    boot(target_ref, host=host, port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
