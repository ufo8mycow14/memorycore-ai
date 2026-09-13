"""Bounded local stdio MCP client for explicitly configured development peers."""
import json
import os
import queue
import subprocess
import threading
import time

from .knowledge_layer import canonical, checked


class StdioPeer:
    def __init__(self, command, *, cwd=None, timeout=10, allowed_tools=()):
        if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
            raise ValueError("host must supply an executable argument list")
        if not 0 < timeout <= 60:
            raise ValueError("invalid peer timeout")
        self.allowed_tools = frozenset(allowed_tools)
        self.timeout, self.sequence = timeout, 0
        self.queue = queue.Queue(maxsize=16)
        self.closed = threading.Event()
        self.process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        try:
            result = self.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "memorycore-ai-synthetic", "version": "0.9.0-dev"}})
            if result.get("protocolVersion") not in {"2024-11-05", "2025-03-26", "2025-06-18"}:
                raise ValueError("unsupported MCP version")
            self.notify("notifications/initialized", {})
        except BaseException:
            self.close()
            raise

    def _read(self):
        try:
            while not self.closed.is_set():
                line = self.process.stdout.readline(1024 * 1024 + 1)
                if not line or len(line) > 1024 * 1024:
                    break
                self.queue.put(json.loads(line), timeout=1)
        except (ValueError, OSError, queue.Full):
            pass
        finally:
            try:
                self.queue.put_nowait(None)
            except queue.Full:
                pass

    def _send(self, message):
        data = canonical(message).encode() + b"\n"
        if len(data) > 65536:
            raise ValueError("peer request too large")
        finished = threading.Event()
        failures = []
        def write():
            try:
                self.process.stdin.write(data)
                self.process.stdin.flush()
            except (OSError, ValueError):
                failures.append(True)
            finally:
                finished.set()
        writer = threading.Thread(target=write, daemon=True)
        writer.start()
        if not finished.wait(self.timeout):
            self.process.terminate()
            raise ValueError("MCP peer write timed out")
        if failures:
            raise ValueError("MCP peer write failed")

    def notify(self, method, params):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method, params):
        self.sequence += 1
        mid = self.sequence
        self._send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout
        while True:
            if time.monotonic() >= deadline:
                raise ValueError("MCP peer timed out")
            try:
                response = self.queue.get(timeout=max(0, deadline-time.monotonic()))
            except queue.Empty:
                raise ValueError("MCP peer timed out") from None
            if response is None:
                raise ValueError("MCP peer disconnected")
            if not isinstance(response, dict) or response.get("jsonrpc") != "2.0":
                raise ValueError("invalid MCP response")
            if "method" in response:
                # Refuse server-initiated sampling, roots, elicitation and writes.
                if "id" in response:
                    self._send({"jsonrpc": "2.0", "id": response["id"], "error": {"code": -32601, "message": "unsupported"}})
                continue
            if response.get("id") != mid or "error" in response:
                raise ValueError("MCP peer response rejected")
            return checked(response["result"])

    def call_tool(self, name, arguments):
        if name not in self.allowed_tools:
            raise ValueError("tool is outside the host allowlist")
        return self.request("tools/call", {"name": name, "arguments": checked(arguments)})

    def close(self):
        self.closed.set()
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        for stream in (self.process.stdin, self.process.stdout):
            if stream:
                stream.close()
        self.reader.join(timeout=1)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
