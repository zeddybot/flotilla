import json
import logging
import time

from .conductor.db import classify
from .protocol import chunks

log = logging.getLogger(__name__)


class Poller:
    def __init__(self, store, db, slack):
        self.store, self.db, self.slack = store, db, slack
        self.idle_since = {}
        self.last_text = {}

    def finish(self, status, text):
        self.store.update(status["id"], state="delivering", result=text)
        self.deliver(self.store.status(status["id"]))

    def deliver(self, status):
        parts = chunks(status["result"])
        for index in range(status["delivered"], len(parts)):
            if index == 0:
                self.slack.edit(status, parts[index])
            else:
                self.slack.post(
                    status["channel"],
                    status["thread_ts"],
                    parts[index],
                    f"result:{status['id']}:{index}:{status['event_key']}",
                )
            self.store.update(status["id"], delivered=index + 1)
        self.store.update(status["id"], state="complete")
        self.idle_since.pop(status["id"], None)
        self.last_text.pop(status["id"], None)
        if not self.store.rows(
            "SELECT id FROM statuses WHERE session_id=? AND state IN "
            "('waiting','active','delivering')",
            (status["session_id"],),
        ):
            self.store.thread_state(status, "idle")

    def reconcile(self):
        # An AX action cannot participate in a SQLite transaction. Never replay
        # an interrupted action automatically; pickup handles already-sent ones.
        for status in self.store.rows("SELECT * FROM statuses WHERE state='preparing'"):
            if status["status_ts"]:
                self.finish(
                    status,
                    "bridge restarted before send was confirmed; check Conductor before retrying",
                )
            else:
                self.store.update(status["id"], state="failed")
        self.store.execute("UPDATE events SET state='interrupted' WHERE state='processing'")
        self.tick()

    def tick(self):
        for status in self.store.active():
            try:
                if status["state"] == "waiting":
                    pickup = self.db.pickup(status["session_id"], status["prompt"])
                    if pickup:
                        for old in self.store.rows(
                            "SELECT * FROM statuses WHERE session_id=? AND turn_id=? "
                            "AND state='active' AND id<?",
                            (status["session_id"], pickup["turn_id"], status["id"]),
                        ):
                            self.slack.delete(old)
                            self.store.update(old["id"], state="superseded")
                            self.idle_since.pop(old["id"], None)
                            self.last_text.pop(old["id"], None)
                        self.store.update(
                            status["id"],
                            state="active",
                            turn_id=pickup["turn_id"],
                            cursor=min(status["cursor"], pickup["seq"] - 1),
                        )
            except Exception:
                log.exception("Pickup failed for status %s", status["id"])
        for status in self.store.active():
            try:
                self.poll(status)
            except Exception:
                log.exception("Polling failed for status %s", status["id"])

    def poll(self, status):
        if status["state"] == "delivering":
            self.deliver(status)
            return
        workspace = self.db.workspace(status["workspace_id"])
        if workspace["state"] == "archived":
            self.finish(status, "workspace archived in Conductor")
            self.store.thread_state(status, "archived")
            return
        session = self.db.session(status["session_id"])
        now = time.time()
        if session["status"] == "idle":
            self.idle_since.setdefault(status["id"], now)
        else:
            self.idle_since.pop(status["id"], None)
        if status["state"] == "active":
            for row in self.db.messages(status["session_id"], status["cursor"]):
                if row["turn_id"] == status["turn_id"]:
                    for kind, text in classify(row):
                        if kind == "result":
                            self.finish(status, text)
                            return
                        status["latest"] = text
                        if kind == "tool" and json.loads(status["options"])["verbose"]:
                            self.slack.post(
                                status["channel"],
                                status["thread_ts"],
                                text,
                                f"tool:{status['event_key']}:{row['id']}:{text}",
                            )
                status["cursor"] = row["seq"]
            self.store.update(status["id"], cursor=status["cursor"], latest=status["latest"])
        if now - self.idle_since.get(status["id"], now) > 30:
            queued = self.db.query(
                "SELECT id FROM session_messages WHERE session_id=? AND role='user' "
                "AND content=? AND sent_at IS NULL AND cancelled_at IS NULL",
                (status["session_id"], status["prompt"]),
            )
            if not queued:
                self.finish(
                    status, "agent is idle without a matching reply; check the Conductor session"
                )
                return
        text = status["latest"] + (f" ({status['note']})" if status["note"] else "")
        if now - status["last_edit"] >= 1.5 and self.last_text.get(status["id"]) != text:
            self.slack.edit(status, text[:3900])
            self.last_text[status["id"]] = text
            self.store.update(status["id"], last_edit=now)
