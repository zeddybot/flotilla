"""Detect a live daemon whose Socket Mode reconnection loop makes no progress."""

import logging
import threading
import time

log = logging.getLogger(__name__)


class ConnectionHealth:
    def __init__(self, client, stop, *, timeout=120, clock=time.monotonic):
        self.client, self.stop = client, stop
        self.timeout, self.clock = timeout, clock
        self.last_progress = clock()
        self.last_pong = None
        self.failed = False
        self.finished = threading.Event()
        self.worker = threading.Thread(target=self.watch, daemon=True)

    def check(self):
        # is_connected() only proves that a socket exists. The SDK can log a
        # successful connection immediately before it fails with BrokenPipe.
        session = self.client.current_session
        pong = session.last_ping_pong_time if session is not None else None
        marker = (session.session_id, pong) if pong is not None else None
        now = self.clock()
        if marker is not None and marker != self.last_pong:
            self.last_pong = marker
            self.last_progress = now
        if now - self.last_progress >= self.timeout:
            self.failed = True
            log.error("No Socket Mode heartbeat progress for %ss; restarting daemon", self.timeout)
            self.stop.set()

    def watch(self):
        while not self.finished.wait(5) and not self.stop.is_set():
            self.check()

    def start(self):
        self.worker.start()

    def close(self):
        self.finished.set()
        self.worker.join(timeout=1)
