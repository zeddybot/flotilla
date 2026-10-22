"""Bounded history reconciliation for owner DMs and their known threads."""

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
    THREADS_PER_SCAN = 20

    def __init__(self, bridge, token, stop, *, client=None, clock=time.time, on_recovered=None):
        self.bridge, self.stop, self.clock = bridge, stop, clock
        self.on_recovered = on_recovered
        self.client = client or WebClient(token=token, timeout=10, retry_handlers=[])
        self.channel = None
        self.finished = threading.Event()
        self.worker = threading.Thread(target=self.run, name="slack-intake-recovery", daemon=True)
        self.path = bridge.config.state_dir / "intake-status.json"
        self.checkpoints_path = bridge.config.state_dir / "intake-threads.json"
        self.checkpoints = None

    def save_checkpoints(self):
        temporary = self.checkpoints_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.checkpoints))
        temporary.chmod(0o600)
        os.replace(temporary, self.checkpoints_path)

    def thread_messages(self, thread, oldest, latest):
        messages, cursor = [], None
        for _ in range(10):
            if self.finished.is_set() or self.stop.is_set():
                raise InterruptedError()
            page = self.client.conversations_replies(
                channel=self.channel,
                ts=thread,
                oldest=str(oldest),
                latest=str(latest),
                limit=100,
                **({"cursor": cursor} if cursor else {}),
            )
            messages.extend(page["messages"])
            cursor = page.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                if page.get("has_more"):
                    raise RuntimeError("Thread page missing continuation cursor")
                return messages
        raise RuntimeError("Thread recovery exceeded page budget")

    def recover(self, messages, oldest, latest, thread=None):
        recovered = 0
        for message in sorted(messages, key=lambda m: float(m["ts"])):
            if self.finished.is_set() or self.stop.is_set():
                raise InterruptedError()
            if (
                message.get("user") != self.bridge.owner_id
                or message.get("edited")
                or not oldest < float(message["ts"]) < latest
                or message.get("thread_ts", message["ts"]) != (thread or message["ts"])
                or message["ts"] == thread
            ):
                continue
            accepted = self.bridge.receive(
                {
                    "team_id": self.bridge.team_id,
                    "event": {**message, "channel": self.channel, "channel_type": "im"},
                },
                source="history",
            )
            recovered += accepted
            if accepted and self.on_recovered:
                self.on_recovered()
        return recovered

    def scan_threads(self, oldest, latest):
        if self.checkpoints is None:
            self.checkpoints = (
                json.loads(self.checkpoints_path.read_text())
                if self.checkpoints_path.exists()
                else {}
            )
        threads = self.bridge.store.rows(
            "SELECT thread_ts, MAX(created) AS recent FROM statuses WHERE channel=? "
            "GROUP BY thread_ts ORDER BY recent DESC",
            (self.channel,),
        )
        for row in threads:
            self.checkpoints.setdefault(row["thread_ts"], oldest)
        # Seed every known thread before rotating so a busy channel cannot age a
        # reply out of the five-minute window before its first scan.
        self.save_checkpoints()
        # Keep recent conversations responsive while reserving most of the
        # budget for a fair rotation through older threads.
        hot = threads[: self.THREADS_PER_SCAN // 4] if len(threads) > self.THREADS_PER_SCAN else []
        hot_ids = {row["thread_ts"] for row in hot}
        selected = hot + sorted(
            (row for row in threads if row["thread_ts"] not in hot_ids),
            key=lambda r: self.checkpoints[r["thread_ts"]],
        )
        recovered, errors = 0, []
        for row in selected[: self.THREADS_PER_SCAN]:
            thread = row["thread_ts"]
            # Overlap the boundary: Slack's latest and oldest are exclusive.
            start = self.checkpoints[thread] - 1
            if latest <= start:
                continue
            try:
                messages = self.thread_messages(thread, start, latest)
                recovered += self.recover(messages, start, latest, thread)
            except SlackApiError as exc:
                if exc.response.get("error") != "thread_not_found":
                    errors.append(exc)
                    if exc.response.status_code == 429:
                        break
                    continue
            except InterruptedError:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad thread must not starve the rest
                errors.append(exc)
                continue
            # Advance only after all pages have been accepted. A crash before
            # this write is safe: the original event key deduplicates the retry.
            self.checkpoints[thread] = latest
            self.save_checkpoints()
        if errors:
            raise errors[-1]
        return recovered

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
        return self.recover(messages, oldest, latest) + self.scan_threads(oldest, latest)

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
