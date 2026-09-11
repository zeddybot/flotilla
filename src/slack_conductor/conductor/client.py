"""Local CLI access to the daemon's native command journal."""

import fcntl
import json
import time
import uuid

from .journal import Journal
from .process import incarnation
from .runtime import fresh, session_snapshot


class Client:
    def __init__(self, state_dir):
        self.state_dir = state_dir
        self.journal = Journal(state_dir / "native.sqlite")
        self.bindings = {}

    def call(self, body, key=None, timeout=30):
        with (self.state_dir / "daemon.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                raise RuntimeError("Start the Conductor Slack daemon before using this command")
        key = key or str(uuid.uuid4())
        self.journal.enqueue(key, body)
        return self.journal.wait(key, timeout)

    def snapshot(self):
        try:
            value = json.loads((self.state_dir / "native-heartbeat.json").read_text())
        except (ValueError, FileNotFoundError):
            return {}
        if (
            not isinstance(value, dict)
            or not fresh(value.get("at"), time.time())
            or not value.get("ready")
            or value.get("incarnation") != incarnation()
        ):
            return {}
        return value

    def online(self):
        return bool(self.snapshot().get("ready"))

    def session(self, sid):
        return session_snapshot(self.snapshot(), sid)
