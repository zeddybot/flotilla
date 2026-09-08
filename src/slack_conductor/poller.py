import json
import logging
import time

from .conductor.db import classify
from .lifecycle import Turn
from .protocol import chunks

log = logging.getLogger(__name__)


class Poller:
    def __init__(self, store, db, slack, *, clock=time.time, settle_seconds=2):
        self.store, self.db, self.slack = store, db, slack
        self.idle_since = {}
        self.last_text = {}
        self.clock, self.settle_seconds = clock, settle_seconds

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
        # Older runtimes persisted only a cursor. Reconstruct task state for
        # still-active turns before advancing past their historical events.
        for status in self.store.active():
            if status["state"] == "active" and status["tracking"] == "{}":
                self.store.update(status["id"], cursor=0)
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
                            # Reconstruct the whole shared turn on steering so
                            # outstanding jobs and prior content aren't lost.
                            status["cursor"] = 0
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
        now = self.clock()
        if session["status"] == "idle":
            self.idle_since.setdefault(status["id"], now)
        else:
            self.idle_since.pop(status["id"], None)
        if status["state"] == "active":
            turn = Turn.load(status["tracking"])
            for row in self.db.messages(status["session_id"], status["cursor"]):
                if turn.accepts(row, status["turn_id"]):
                    turn.consume(row)
                    for kind, text in classify(row):
                        if kind == "tool" and json.loads(status["options"])["verbose"]:
                            self.slack.post(
                                status["channel"],
                                status["thread_ts"],
                                text,
                                f"tool:{status['event_key']}:{row['id']}:{text}",
                            )
                status["cursor"] = row["seq"]
            # A queued/user-started next turn can occupy the session while this
            # turn is already at rest. Its output must remain separate.
            later_turn = turn.result_seq and self.db.query(
                "SELECT rowid FROM session_messages WHERE session_id=? AND rowid>? "
                "AND role='user' AND sent_at IS NOT NULL AND turn_id IS NOT NULL "
                "AND turn_id!=? LIMIT 1",
                (status["session_id"], turn.result_seq, status["turn_id"]),
            )
            ready = turn.ready(
                now, session["status"] == "idle" or bool(later_turn), self.settle_seconds
            )
            status["latest"] = (
                f"waiting for {len(turn.tasks)} background job(s)"
                if turn.tasks and turn.result_seq
                else turn.detail or turn.phase
            )
            self.store.update(
                status["id"], cursor=status["cursor"], latest=status["latest"], tracking=turn.dump()
            )
            if ready:
                self.finish(status, ("error: " if turn.is_error else "") + turn.content)
                return
        if status["state"] == "waiting" and now - self.idle_since.get(status["id"], now) > 30:
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
