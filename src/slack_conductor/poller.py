import json
import logging
import time

from .conductor.db import classify
from .lifecycle import Turn
from .presentation import live, pages, render

log = logging.getLogger(__name__)


class Poller:
    def __init__(self, store, db, slack, *, clock=time.time, settle_seconds=2):
        self.store, self.db, self.slack = store, db, slack
        self.idle_since = {}
        self.last_text = {}
        self.clock, self.settle_seconds = clock, settle_seconds

    def finish(self, status, text, phase="done", detail=""):
        turn = Turn.load(status["tracking"])
        turn.phase, turn.detail = phase, detail
        self.store.update(status["id"], state="delivering", result=text, tracking=turn.dump())
        self.deliver(self.store.status(status["id"]))

    def deliver(self, status):
        turn = Turn.load(status["tracking"])
        # Existing delivering rows from older runtimes have no presentation state.
        phase = turn.phase if turn.phase in {"done", "failed", "attention", "archived"} else "done"
        parts = pages(phase, status["result"], turn.detail, status["note"])
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
                    "attention",
                )
            else:
                self.store.update(status["id"], state="failed")
        self.store.execute("UPDATE events SET state='interrupted' WHERE state='processing'")
        # Older runtimes persisted only a cursor. Reconstruct task state for
        # still-active turns before advancing past their historical events.
        for status in self.store.active():
            if status["state"] == "active" and status["tracking"] == "{}":
                self.store.update(status["id"], cursor=0)
            if status["status_ts"] and status["state"] != "delivering":
                self.slack.edit(
                    status, render("reconnecting", Turn.load(status["tracking"]).content)
                )
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
                            self.store.handoff(status["id"], old["id"])
                            self.idle_since.pop(old["id"], None)
                            self.last_text.pop(old["id"], None)
                            status = self.store.status(status["id"])
                        self.store.update(
                            status["id"],
                            state="active",
                            turn_id=pickup["turn_id"],
                            cursor=min(status["cursor"], pickup["seq"] - 1),
                            note="" if status["note"].startswith("error:") else status["note"],
                        )
            except Exception:
                log.exception("Pickup failed for status %s", status["id"])
        for status in self.store.active():
            try:
                self.poll(status)
            except Exception:
                log.exception("Polling failed for status %s", status["id"])

    def poll(self, status):
        if status["discard_ts"] != "[]":
            # Populate the replacement before deleting the older response. If
            # either Slack action fails, durable cleanup resumes on the next tick.
            self.slack.edit(status, live(Turn.load(status["tracking"]), status["note"]))
        for ts in json.loads(status["discard_ts"]):
            self.slack.delete(dict(status, status_ts=ts))
        if status["discard_ts"] != "[]":
            self.store.update(status["id"], discard_ts="[]")
        if status["state"] == "delivering":
            self.deliver(status)
            return
        workspace = self.db.workspace(status["workspace_id"])
        if workspace["state"] == "archived":
            self.finish(status, Turn.load(status["tracking"]).content, "archived")
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
                                render("working", text, "tool activity"),
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
            status["tracking"] = turn.dump()
            if ready:
                self.finish(status, turn.content, "failed" if turn.is_error else "done")
                return
            if (
                not turn.result_seq
                and not turn.tasks
                and not turn.awaiting_callback
                and turn.phase != "attention"
                and now - self.idle_since.get(status["id"], now) > 30
            ):
                self.finish(
                    status,
                    turn.content
                    + "\n\nThe agent stopped without a final response. Check Conductor before retrying.",
                    "attention",
                    "agent stopped",
                )
                return
        if status["state"] == "waiting" and now - self.idle_since.get(status["id"], now) > 30:
            queued = self.db.query(
                "SELECT id FROM session_messages WHERE session_id=? AND role='user' "
                "AND content=? AND sent_at IS NULL AND cancelled_at IS NULL",
                (status["session_id"], status["prompt"]),
            )
            if not queued:
                self.finish(
                    status,
                    "Agent is idle without a matching reply; check the Conductor session.",
                    "attention",
                )
                return
        if status["state"] == "active":
            text = live(turn, status["note"])
        elif status["note"].startswith("error:"):
            text = render("unconfirmed", status["note"], "send unconfirmed")
        else:
            text = render(
                "queued" if status["latest"] == "queued" else "starting",
                detail="waiting for current turn" if status["latest"] == "queued" else "",
                note=status["note"],
            )
        if now - status["last_edit"] >= 1.5 and self.last_text.get(status["id"]) != text:
            self.slack.edit(status, text[:3900])
            self.last_text[status["id"]] = text
            self.store.update(status["id"], last_edit=now)
