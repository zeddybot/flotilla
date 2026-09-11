"""Bound Slack transport failures independently of the SDK's reconnect loop."""

import logging
import sys
import threading
import time

log = logging.getLogger(__name__)


def continuous_time():
    # macOS MONOTONIC_RAW includes sleep; Linux's RAW does not, so use BOOTTIME.
    clock = time.CLOCK_MONOTONIC_RAW if sys.platform == "darwin" else time.CLOCK_BOOTTIME
    return time.clock_gettime(clock)


class ConnectionHealth:
    def __init__(
        self, client, stop, *, timeout=120, clock=time.monotonic, suspend_clock=continuous_time
    ):
        self.client, self.stop = client, stop
        self.timeout, self.clock = timeout, clock
        self.suspend_clock = suspend_clock
        self.last_progress = clock()
        self.sleep_offset = suspend_clock() - self.last_progress
        self.last_pong = None
        self.session_id = None
        self.stable_since = None
        self.errors = 0
        self.failed = False
        self.lock = threading.Lock()
        self.finished = threading.Event()
        self.worker = threading.Thread(target=self.watch, name="slack-health", daemon=True)

    def on_error(self, error):
        with self.lock:
            self.errors += 1
            self.stable_since = None
            # Error text can contain authenticated URLs; only log its type here.
            log.warning("Slack transport error %s (%s/5)", type(error).__name__, self.errors)

    def fail(self, reason):
        self.failed = True
        log.error("Restarting Slack daemon: %s; transport_errors=%s", reason, self.errors)
        self.stop.set()

    def check(self):
        # Both the worker and the service loop check health. Either can recover if
        # the other stalls; serialize their observations and SDK error callbacks.
        with self.lock:
            if self.failed or self.stop.is_set():
                return
            try:
                self.observe()
            except Exception as exc:  # noqa: BLE001 - a broken health check must fail closed
                self.fail(f"health observation failed ({type(exc).__name__})")

    def observe(self):
        now = self.clock()
        offset = self.suspend_clock() - now
        slept = offset - self.sleep_offset
        self.sleep_offset = offset
        if slept >= 30:
            self.fail(f"resumed after {slept:.0f}s asleep")
            return
        if self.errors >= 5:
            self.fail("repeated transport failures")
            return
        session = self.client.current_session
        session_id = session.session_id if session is not None else None
        pong = session.last_ping_pong_time if session is not None else None
        if session_id != self.session_id:
            self.session_id = session_id
            self.stable_since = None
        if now - self.last_progress >= 15:
            self.stable_since = None
        marker = (session_id, pong) if pong is not None else None
        if marker is not None and marker != self.last_pong:
            self.last_pong = marker
            self.last_progress = now
            if self.stable_since is None:
                self.stable_since = now
            # One pong on each short-lived socket must not forgive a failure loop.
            if now - self.stable_since >= 30:
                self.errors = 0
        if now - self.last_progress >= self.timeout:
            self.fail(f"no pong progress for {now - self.last_progress:.0f}s")

    def watch(self):
        while not self.finished.wait(5) and not self.stop.is_set():
            self.check()

    def start(self):
        self.client.on_error_listeners.append(self.on_error)
        self.worker.start()

    def close(self):
        self.finished.set()
        self.worker.join(timeout=1)
        self.client.on_error_listeners.remove(self.on_error)
