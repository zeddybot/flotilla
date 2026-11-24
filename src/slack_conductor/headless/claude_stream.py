"""Stdio client for a Claude stream-json turn and its background callbacks."""

import json
import os
import queue
import stat
import threading
import time
import uuid
from concurrent.futures import Future

from .process_host import Rejected, write_json_line


class ClaudeStream:
    def __init__(self, process, *, resume_session=None, fresh_transcript=None):
        self.process = process
        self._stdin = process.stdin.fileno()
        os.set_blocking(self._stdin, False)
        self.events = queue.Queue()
        self._lock = threading.Lock()
        self._result_handled = threading.Event()
        self._result_handled.set()
        self._pending = {}
        self._starting = None
        self._started = False
        self._closed = False
        self._input_closed = False
        self._write_failed = False
        self._session_id = None
        self._resume_session = resume_session
        self._fresh_transcript = fresh_transcript
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def start(self, prompt, message_id, timeout=20):
        deadline = time.monotonic() + timeout
        response = self._submit(prompt, message_id, deadline)
        try:
            response.result(timeout=max(0, deadline - time.monotonic()))
        except TimeoutError:
            with self._lock:
                self._confirm_persisted_initial_input()
            error = RuntimeError("Claude did not acknowledge the initial input")
            self._close_pending(error, fatal=True, unless_started=True)
            response.result()

    def _confirm_persisted_initial_input(self):
        # Called with the lock held. A fresh CLI can persist the exact user row
        # before it emits the replay on stdout.
        path = self._fresh_transcript
        if self._closed or self._started or not self._starting or path is None:
            return
        if self._session_id != path.stem:
            return
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as transcript:
                if not stat.S_ISREG(os.fstat(transcript.fileno()).st_mode):
                    return
                prefix = transcript.read(2_000_000)
        except OSError:
            return
        for line in prefix.split(b"\n")[:-1]:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if (
                isinstance(entry, dict)
                and entry.get("type") == "user"
                and entry.get("sessionId") == self._session_id
                and entry.get("uuid") == self._starting
                and entry.get("isSidechain") is False
                and "parentUuid" in entry
                and entry["parentUuid"] is None
            ):
                self._started = True
                self._pending.pop(self._starting).set_result(None)
                return

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
            if self._input_closed:
                raise Rejected("The Claude input stream has closed")
            if not self._result_handled.is_set():
                raise Rejected("Claude is settling the current result")
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

    def _close_pending(self, error, *, fatal=False, unless_started=False):
        with self._lock:
            if self._closed or (unless_started and self._started):
                return
            self._closed = True
            pending, self._pending = self._pending, {}
        for response in pending.values():
            response.set_exception(error)
        self._result_handled.set()
        if fatal:
            self.events.put(error)

    def _read(self):
        resume_init = None
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
                top_level = not (
                    source.get("parent_tool_use_id")
                    or (source.get("conductor_sdk_metadata") or {}).get("isForeignResult")
                )
                if top_level:
                    self._observe(source)
                if self._resume_session and not self._started:
                    # A resumed CLI can process a previous job before reading our
                    # input. Publish only the init that precedes our exact replay.
                    if (
                        top_level
                        and source.get("type") == "system"
                        and source.get("subtype") == "init"
                    ):
                        resume_init = line
                    continue
                if resume_init is not None:
                    self.events.put(resume_init)
                    resume_init = None
                self.events.put(line)
                # Read no more native acknowledgements until execution decides
                # whether this result is provisional or closes the turn.
                self._result_handled.wait()
                if self._closed:
                    return
        except (OSError, RuntimeError, ValueError) as exc:
            self._close_pending(exc, fatal=True)
        finally:
            self._close_pending(
                RuntimeError("Claude stream disconnected before completing the turn"),
                fatal=not self._input_closed,
            )
            self.events.put(None)

    def _observe(self, source):
        kind = source.get("type")
        session_id = source.get("session_id")
        if kind == "result":
            with self._lock:
                if session_id == self._session_id:
                    self._confirm_persisted_initial_input()
                if not self._started:
                    if (
                        self._resume_session is not None
                        and self._resume_session == session_id == self._session_id
                        and isinstance(source.get("origin"), dict)
                        and source["origin"].get("kind") == "task-notification"
                        and source.get("subtype") == "success"
                        and source.get("is_error") is False
                        and not source.get("user_message_uuid")
                        and not source.get("user_message_uuids")
                    ):
                        return
                    raise RuntimeError("Claude turn ended before acknowledging the initial input")
                if session_id != self._session_id:
                    raise RuntimeError("Claude completed an unexpected session")
                if not self._closed and not self._input_closed:
                    self._result_handled.clear()
                return

        with self._lock:
            if self._closed:
                return
            if kind == "system" and source.get("subtype") == "init":
                if (
                    not isinstance(session_id, str)
                    or not session_id
                    or self._resume_session not in (None, session_id)
                    or self._session_id not in (None, session_id)
                    or (self._fresh_transcript and self._fresh_transcript.stem != session_id)
                ):
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

    def finish(self):
        self._close_pending(RuntimeError("Claude turn ended before acknowledging the steer"))

    def continue_callbacks(self):
        self._result_handled.set()

    def close_input(self):
        with self._lock:
            self._input_closed = True
            self.process.stdin.close()
            self._result_handled.set()

    def close(self):
        self._close_pending(
            RuntimeError("Claude stream closed before completing the turn"), fatal=True
        )
        with self._lock:
            self.process.stdin.close()
        self._reader.join(timeout=2)
