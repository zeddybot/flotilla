"""Stdio client for a single Claude stream-json turn."""

import json
import os
import queue
import threading
import time
import uuid
from concurrent.futures import Future

from .process_host import Rejected, write_json_line


class ClaudeStream:
    def __init__(self, process):
        self.process = process
        self._stdin = process.stdin.fileno()
        os.set_blocking(self._stdin, False)
        self.events = queue.Queue()
        self._lock = threading.Lock()
        self._pending = {}
        self._starting = None
        self._started = False
        self._closed = False
        self._write_failed = False
        self._session_id = None
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def start(self, prompt, message_id, timeout=20):
        deadline = time.monotonic() + timeout
        response = self._submit(prompt, message_id, deadline)
        try:
            response.result(timeout=max(0, deadline - time.monotonic()))
        except TimeoutError:
            error = RuntimeError("Claude did not acknowledge the initial input")
            self._close_pending(error, fatal=True)
            raise error from None

    def steer_async(self, thread_id, native_turn_id, prompt, message_id):
        return self._submit(prompt, message_id, time.monotonic() + 5, thread_id=thread_id)

    def _submit(self, prompt, message_id, deadline, *, thread_id=None):
        message_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"flotilla:claude:{message_id}"))
        message = {
            "type": "user",
            "message": {"role": "user", "content": prompt},
            "session_id": "",
            "parent_tool_use_id": None,
            "uuid": message_uuid,
        }
        response = Future()
        with self._lock:
            if self._closed:
                raise Rejected("The Claude turn has already ended")
            if self._write_failed:
                raise Rejected("The Claude input stream is unavailable")
            if thread_id is None:
                if self._started or self._starting:
                    raise Rejected("The Claude turn has already started")
                self._starting = message_uuid
            elif not self._started or thread_id != self._session_id:
                raise Rejected("The Claude input does not match the active turn")
            if message_uuid in self._pending:
                raise Rejected("Claude input is already awaiting acknowledgement")
            self._pending[message_uuid] = response
            try:
                write_json_line(self._stdin, message, deadline)
            except (OSError, ValueError):
                self._pending.pop(message_uuid)
                self._write_failed = True
                raise RuntimeError("Claude input write could not be confirmed") from None
        return response

    def _close_pending(self, error, *, fatal=False):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            pending, self._pending = self._pending, {}
        for response in pending.values():
            response.set_exception(error)
        if fatal:
            self.events.put(error)

    def _read(self):
        try:
            for line in iter(lambda: self.process.stdout.readline(2_000_001), ""):
                if len(line) > 2_000_000:
                    raise RuntimeError("Headless event exceeded size limit")
                try:
                    source = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(source, dict):
                    continue
                if not (
                    source.get("parent_tool_use_id")
                    or (source.get("conductor_sdk_metadata") or {}).get("isForeignResult")
                ):
                    self._observe(source)
                self.events.put(line)
                if self._closed:
                    return
        except (OSError, RuntimeError, ValueError) as exc:
            self._close_pending(exc, fatal=True)
        finally:
            self._close_pending(
                RuntimeError("Claude stream disconnected before completing the turn"), fatal=True
            )
            self.events.put(None)

    def _observe(self, source):
        kind = source.get("type")
        session_id = source.get("session_id")
        if kind == "result":
            if not self._started:
                raise RuntimeError("Claude turn ended before acknowledging the initial input")
            if session_id != self._session_id:
                raise RuntimeError("Claude completed an unexpected session")
            self._close_pending(RuntimeError("Claude turn ended before acknowledging the steer"))
            return

        with self._lock:
            if self._closed:
                return
            if kind == "system" and source.get("subtype") == "init":
                if not isinstance(session_id, str) or not session_id or self._session_id:
                    raise RuntimeError("Claude reported an unexpected session identity")
                self._session_id = session_id
            if kind != "user" or source.get("isReplay") is not True:
                return
            message_uuid = source.get("uuid")
            if not isinstance(message_uuid, str) or message_uuid not in self._pending:
                return
            if not session_id or session_id != self._session_id:
                raise RuntimeError("Claude acknowledged input for an unexpected session")
            response = self._pending.pop(message_uuid)
            if message_uuid == self._starting:
                self._started = True
        response.set_result(None)

    def lines(self):
        while (line := self.events.get()) is not None:
            if isinstance(line, Exception):
                raise line
            yield line

    def close(self):
        self._close_pending(
            RuntimeError("Claude stream closed before completing the turn"), fatal=True
        )
        with self._lock:
            self.process.stdin.close()
        self._reader.join(timeout=2)
