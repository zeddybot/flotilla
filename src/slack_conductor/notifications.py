"""Persistent, personal notifications independent of final-answer delivery."""

import html
import logging
import time
import uuid

from slack_sdk.errors import SlackApiError, SlackRequestError

from .presentation import STATES

log = logging.getLogger(__name__)


class Notifications:
    def __init__(self, store, slack, owner_id, *, clock=time.time):
        self.store, self.slack, self.owner_id, self.clock = store, slack, owner_id, clock

    def observe(self, status, phase, detail=""):
        """Record transitions atomically; old completed replies are never backfilled."""
        phase = phase if phase in {"done", "failed", "attention"} else ""
        with self.store.connect() as conn:
            row = conn.execute("SELECT * FROM statuses WHERE id=?", (status["id"],)).fetchone()
            if row["notice_phase"] == phase:
                return
            seq = row["notice_seq"] + 1
            conn.execute(
                "UPDATE statuses SET notice_phase=?,notice_seq=? WHERE id=?",
                (phase, seq, row["id"]),
            )
            if phase and row["status_ts"]:
                prompt = row["prompt"]
                if prompt.startswith("[slack]"):
                    prompt = prompt.partition("\n\n")[2]
                title = " ".join(prompt.split())[:120] or "Flotilla session"
                key = str(uuid.uuid5(uuid.NAMESPACE_URL, f"notice:{row['event_key']}:{seq}"))
                conn.execute(
                    "INSERT OR IGNORE INTO notifications "
                    "(key,status_id,owner_id,phase,title,detail) VALUES (?,?,?,?,?,?)",
                    (key, row["id"], self.owner_id, phase, title, detail),
                )

    def save(self, row, **values):
        self.store.execute(
            "UPDATE notifications SET " + ",".join(f"{key}=?" for key in values) + " WHERE key=?",
            (*values.values(), row["key"]),
        )
        row.update(values)

    def reply_target(self, channel, thread_ts, *, actor=None):
        if actor is not None and actor != self.owner_id:
            return None
        rows = self.store.rows(
            "SELECT * FROM notifications WHERE dm_channel=? AND dm_ts=? AND owner_id=?",
            (channel, thread_ts, self.owner_id),
        )
        if rows:
            return rows[0]
        # A user can reply after Slack accepted a DM but before we saved its ts.
        pending = self.store.rows(
            "SELECT * FROM notifications WHERE state='sending' AND owner_id=?",
            (self.owner_id,),
        )
        if channel.startswith("D") and pending:
            response = self.slack.call(
                "conversations_history",
                channel=channel,
                oldest=thread_ts,
                latest=thread_ts,
                inclusive=True,
                limit=1,
            )
            for message in response["messages"]:
                ids = {b.get("block_id") for b in message.get("blocks", [])}
                for row in pending:
                    if f"notice:{row['key']}" in ids:
                        self.save(row, state="sent", dm_channel=channel, dm_ts=thread_ts)
                        return row
        return None

    def acknowledge(self, event):
        """Slack's own bot-message echo can recover a lost first-DM response."""
        if not str(event.get("channel", "")).startswith("D") or not event.get("ts"):
            return
        ids = {b.get("block_id") for b in event.get("blocks", [])}
        for row in self.store.rows(
            "SELECT * FROM notifications WHERE state='sending' AND owner_id=?",
            (self.owner_id,),
        ):
            if f"notice:{row['key']}" in ids:
                self.save(row, state="sent", dm_channel=event["channel"], dm_ts=event["ts"])

    def known_dm(self, owner):
        rows = self.store.rows(
            "SELECT dm_channel AS channel FROM notifications WHERE owner_id=? "
            "AND dm_channel IS NOT NULL LIMIT 1",
            (owner,),
        )
        if not rows:
            rows = self.store.rows(
                "SELECT json_extract(payload,'$.channel') AS channel FROM events "
                "WHERE json_extract(payload,'$.user')=? "
                "AND json_extract(payload,'$.channel_type')='im' ORDER BY created DESC LIMIT 1",
                (owner,),
            )
        return rows[0]["channel"] if rows else None

    def find_sent(self, row):
        cursor = ""
        while True:
            response = self.slack.call(
                "conversations_history",
                channel=row["dm_channel"],
                oldest=str(max(0, row["attempted_at"] - 5)),
                limit=100,
                cursor=cursor,
            )
            for message in response["messages"]:
                if message.get("client_msg_id") == row["key"] or any(
                    b.get("block_id") == f"notice:{row['key']}" for b in message.get("blocks", [])
                ):
                    return message["ts"]
            cursor = response.get("response_metadata", {}).get("next_cursor", "")
            if not cursor:
                if response.get("has_more"):
                    raise RuntimeError("Notification history is incomplete")
                return None

    def tick(self):
        rows = self.store.rows(
            "SELECT * FROM notifications WHERE state IN ('pending','sending') AND retry_at<=? "
            "ORDER BY rowid LIMIT 1",
            (self.clock(),),
        )
        for row in rows:
            try:
                self.deliver(row)
            except (
                SlackApiError,
                SlackRequestError,
                OSError,
                RuntimeError,
                ValueError,
                KeyError,
            ) as exc:
                attempts = row["attempts"] + 1
                self.save(
                    row, attempts=attempts, retry_at=self.clock() + min(60, 2 ** min(attempts, 6))
                )
                log.warning("Notification %s will retry (%s)", row["key"], type(exc).__name__)

    def deliver(self, row):
        status = self.store.status(row["status_id"])
        # Reconcile an ambiguous send even if the agent has since moved on.
        if not row["dm_channel"]:
            self.save(row, dm_channel=self.known_dm(row["owner_id"]))
        if row["state"] == "sending":
            if not row["dm_channel"]:
                raise RuntimeError("Awaiting the owner's DM identity to reconcile delivery")
            if ts := self.find_sent(row):
                self.save(row, state="sent", dm_ts=ts)
                return
        current = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"notice:{status['event_key']}:{status['notice_seq']}")
        )
        if status["state"] == "superseded" or current != row["key"]:
            self.save(row, state="cancelled")
            return
        if status["state"] != "complete" and row["phase"] != "attention":
            return
        if not row["permalink"]:
            self.save(
                row,
                permalink=self.slack.call(
                    "chat_getPermalink",
                    channel=status["channel"],
                    message_ts=status["status_ts"],
                )["permalink"],
            )
        emoji, label = STATES[row["phase"]]
        heading = f"{emoji} {label} · {row['title']}"
        context = (
            f"<#{status['channel']}>" if status["channel"].startswith("C") else "Direct message"
        )
        link = f"{context} · <{row['permalink']}|View reply>"
        detail = " ".join(row["detail"].split())[:160]
        self.save(row, state="sending", attempted_at=row["attempted_at"] or self.clock())
        response = self.slack.call(
            "chat_postMessage",
            channel=row["dm_channel"] or row["owner_id"],
            text=html.escape(heading) + "\n" + link,
            blocks=[
                {
                    "type": "section",
                    "block_id": f"notice:{row['key']}",
                    "text": {
                        "type": "plain_text",
                        "text": heading + (f"\n{detail}" if detail else ""),
                    },
                },
                {"type": "section", "text": {"type": "mrkdwn", "text": link}},
            ],
            client_msg_id=row["key"],
            unfurl_links=False,
            unfurl_media=False,
        )
        self.save(row, state="sent", dm_channel=response["channel"], dm_ts=response["ts"])
