"""Bounded history reconciliation for missing top-level owner DMs."""

import json
import logging
import os
import threading
import time

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

log = logging.getLogger(__name__)


class OwnerDMUnknown(RuntimeError):
    """Wait for a trusted owner DM to establish the history channel."""


class IntakeRecovery:
    INTERVAL = 60
    WINDOW = 300
    GRACE = 10

    def __init__(self, bridge, token, stop, *, client=None, clock=time.time):
        self.bridge, self.stop, self.clock = bridge, stop, clock
        self.client = client or WebClient(token=token, timeout=10, retry_handlers=[])
        self.channel = None
        self.finished = threading.Event()
        self.worker = threading.Thread(target=self.run, name="slack-intake-recovery", daemon=True)
        self.path = bridge.config.state_dir / "intake-status.json"

    def status(self, ready, recovered=0, error=None):
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "at": self.clock(),
                    "ready": ready,
                    "recovered": recovered,
                    "error_type": type(error).__name__ if error else None,
                }
            )
        )
        temporary.chmod(0o600)
        os.replace(temporary, self.path)

    def scan(self):
        if self.channel is None:
            # Reuse the channel of an accepted owner DM; no additional Slack scopes.
            rows = self.bridge.store.rows(
                "SELECT json_extract(payload,'$.channel') AS channel FROM events "
                "WHERE json_extract(payload,'$.user')=? "
                "AND json_extract(payload,'$.channel_type')='im' "
                "AND json_extract(payload,'$.channel') LIKE 'D%' ORDER BY created DESC LIMIT 1",
                (self.bridge.owner_id,),
            )
            if not rows:
                raise OwnerDMUnknown()
            self.channel = rows[0]["channel"]
        now = self.clock()
        oldest, latest = now - self.WINDOW, now - self.GRACE
        messages, cursor = [], None
        for _ in range(10):
            if self.finished.is_set() or self.stop.is_set():
                return 0
            page = self.client.conversations_history(
                channel=self.channel,
                oldest=str(oldest),
                latest=str(latest),
                limit=100,
                **({"cursor": cursor} if cursor else {}),
            )
            messages.extend(page["messages"])
            cursor = page.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                if page.get("has_more"):
                    raise RuntimeError("History page missing continuation cursor")
                break
        else:
            raise RuntimeError("History recovery exceeded page budget")
        recovered = 0
        for message in sorted(messages, key=lambda m: float(m["ts"])):
            if self.finished.is_set() or self.stop.is_set():
                break
            # History isn't an event stream: do not replay edits or thread replies.
            if (
                message.get("user") != self.bridge.owner_id
                or message.get("edited")
                or message.get("thread_ts", message["ts"]) != message["ts"]
                or not oldest < float(message["ts"]) < latest
            ):
                continue
            recovered += self.bridge.receive(
                {
                    "team_id": self.bridge.team_id,
                    "event": {**message, "channel": self.channel, "channel_type": "im"},
                },
                source="history",
            )
        return recovered

    def tick(self):
        delay = self.INTERVAL
        try:
            recovered = self.scan()
            self.status(True, recovered)
            log.info("Slack intake history checked; recovered=%s", recovered)
        except Exception as exc:  # noqa: BLE001 - preserve recovery after malformed API responses
            # API errors may contain request payloads or authenticated URLs.
            if isinstance(exc, SlackApiError) and exc.response.status_code == 429:
                delay = max(delay, int(exc.response.headers.get("Retry-After", "60")))
            log.warning("Slack intake history unavailable (%s)", type(exc).__name__)
            self.status(False, error=exc)
        return delay

    def run(self):
        while not self.stop.is_set() and not self.finished.is_set():
            try:
                delay = self.tick()
            except OSError:
                # A status-file failure must not permanently kill reconciliation.
                log.error("Cannot persist Slack intake health")
                delay = self.INTERVAL
            if self.finished.wait(delay):
                break

    def start(self):
        self.status(False)
        self.worker.start()

    def close(self):
        self.finished.set()
        self.worker.join(timeout=2)
