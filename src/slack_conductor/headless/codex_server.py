"""Stdio client for the Codex app-server v2 protocol."""

import json
import os
import queue
import threading
import time
from concurrent.futures import Future

from .process_host import Rejected, write_json_line


def text_input(prompt):
    return [{"type": "text", "text": prompt, "text_elements": []}]


class CodexServer:
    def __init__(self, process):
        self.process = process
        self._stdin = process.stdin.fileno()
        os.set_blocking(self._stdin, False)
        self.events = queue.Queue()
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._requests = {}
        self._next_id = 0
        self._closed = False
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _write(self, message, deadline):
        with self._write_lock:
            write_json_line(self._stdin, message, deadline)

    def request(self, method, params, timeout=20):
        deadline = time.monotonic() + timeout
        response = Future()
        with self._lock:
            if self._closed:
                raise RuntimeError("Codex app-server disconnected")
            self._next_id += 1
            request_id = self._next_id
            self._requests[request_id] = response
        try:
            self._write({"id": request_id, "method": method, "params": params}, deadline)
            return response.result(timeout=max(0, deadline - time.monotonic()))
        finally:
            with self._lock:
                self._requests.pop(request_id, None)

    def steer(self, thread_id, turn_id, prompt, message_id):
        response = self.request(
            "turn/steer",
            {
                "threadId": thread_id,
                "expectedTurnId": turn_id,
                "clientUserMessageId": message_id,
                "input": text_input(prompt),
            },
            timeout=5,
        )
        if response.get("turnId") != turn_id:
            raise RuntimeError("Codex returned an unexpected steering turn identity")

    def initialize(self):
        self.request(
            "initialize",
            {"clientInfo": {"name": "flotilla", "version": "1"}, "capabilities": None},
        )
        self._write({"method": "initialized"}, time.monotonic() + 5)

    def _read(self):
        try:
            for line in iter(lambda: self.process.stdout.readline(2_000_001), ""):
                if len(line) > 2_000_000:
                    raise RuntimeError("Headless event exceeded size limit")
                source = json.loads(line)
                if not isinstance(source, dict):
                    raise TypeError("Invalid Codex app-server message")
                request_id = source.get("id")
                if request_id is None:
                    self.events.put(source)
                elif "method" in source:
                    # Approval policy is never. Reject any unexpected server request
                    # so the engine cannot wait forever on an unsupported input.
                    self._write(
                        {"id": request_id, "error": {"code": -32601, "message": "Unsupported"}},
                        time.monotonic() + 5,
                    )
                else:
                    self._receive(request_id, source)
        except TypeError:
            self.events.put(RuntimeError("Invalid Codex app-server message"))
        except (OSError, RuntimeError, ValueError) as exc:
            self.events.put(exc)
        finally:
            with self._lock:
                self._closed = True
                for response in self._requests.values():
                    if not response.done():
                        response.set_exception(RuntimeError("Codex app-server disconnected"))
            self.events.put(None)

    def _receive(self, request_id, source):
        with self._lock:
            response = self._requests.get(request_id)
            if response is None or response.done():
                return
            if "error" in source:
                response.set_exception(Rejected("Codex rejected the request"))
            elif isinstance(result := source.get("result"), dict):
                response.set_result(result)
            else:
                response.set_exception(RuntimeError("Invalid Codex app-server response"))

    def notifications(self):
        while (source := self.events.get()) is not None:
            if isinstance(source, Exception):
                raise source
            yield source

    def close(self):
        self._reader.join(timeout=2)
        self.process.stdin.close()
