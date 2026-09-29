import json
import logging
import time

from .lifecycle import Turn
from .presentation import live, pages, render
from .runtime_events import classify
from .uploads import Uploads

log = logging.getLogger(__name__)


class Poller:
    def __init__(self, store, db, slack, *, clock=time.time, settle_seconds=2, notifications=None):
        self.store, self.db, self.slack = store, db, slack
        self.idle_since = {}
        self.last_text = {}
        self.clock, self.settle_seconds = clock, settle_seconds
        self.uploads = Uploads(store, slack, clock=clock)
        self.notifications = notifications

    def finish(self, status, text, phase="done", detail=""):
        turn = Turn.load(status["tracking"])
        turn.phase, turn.detail = phase, detail
        self.store.update(status["id"], state="delivering", result=text, tracking=turn.dump())
        self.deliver(self.store.status(status["id"]))

    def deliver(self, status):
        turn = Turn.load(status["tracking"])
        # Existing delivering rows from older runtimes have no presentation state.
        phase = turn.phase if turn.phase in {"done", "failed", "attention", "archived"} else "done"
        content, images, file_ids = status["result"], {}, []
        if phase in {"done", "failed"} and (
            not status["delivered"] or status["attachments"] != "{}"
        ):
            plan = json.loads(status["attachments"])
            if not plan:
                workspace = self.db.workspace(status["workspace_id"])
                plan = self.uploads.prepare(status, workspace.get("workspace_path"))
            if not self.uploads.step(status, plan):
                text = render("sending", content, f"{len(plan['files'])} files", status["note"])
                if self.last_text.get(status["id"]) != text:
                    self.slack.edit(status, text)
                    self.last_text[status["id"]] = text
                return
            content, images, file_ids = self.uploads.result(plan)
            if any(f.get("error") for f in plan["files"]):
                phase = "attention"
                turn.detail = "some files could not be sent"
        parts = pages(phase, content, turn.detail, status["note"])
        for index in range(status["delivered"], len(parts)):
            if index == 0:
                self.slack.edit(
                    status,
                    parts[index],
                    **({"images": images, "file_ids": file_ids} if file_ids else {}),
                )
            else:
                self.slack.post(
                    status["channel"],
                    status["thread_ts"],
                    parts[index],
                    f"result:{status['id']}:{index}:{status['event_key']}",
                    status_header=False,
                    **({"images": images} if images else {}),
                )
            self.store.update(status["id"], delivered=index + 1)
        if self.notifications:
            self.notifications.observe(status, phase, turn.detail)
        self.store.update(status["id"], state="complete")
        self.uploads.cleanup(self.store.status(status["id"]))
        self.idle_since.pop(status["id"], None)
        self.last_text.pop(status["id"], None)
        if not self.store.rows(
            "SELECT id FROM statuses WHERE session_id=? AND state IN "
            "('waiting','active','delivering')",
            (status["session_id"],),
        ):
            self.store.thread_state(status, "idle")

    def reconcile(self):
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

    def handoff(self, status, pickup):
        if json.loads(status["options"]).get("send_mode", "steer") != "steer":
            return status
        previous = self.store.rows(
            "SELECT * FROM statuses WHERE session_id=? AND channel=? AND thread_ts=? "
            "AND id<? AND turn_id IS NOT NULL AND state!='superseded' "
            "ORDER BY id DESC LIMIT 1",
            (status["session_id"], status["channel"], status["thread_ts"], status["id"]),
        )
        if not previous or previous[0]["state"] != "active":
            return status
        old = previous[0]
        turn, cursor = Turn.load(old["tracking"]), old["cursor"]
        # A background run can outlive its native turn ID. Catch up only to the
        # new user message so its stop notification/result cannot be claimed by
        # the old reply. Jobs may have started or finished since the last poll.
        for row in self.db.messages(old["session_id"], cursor):
            if row["seq"] >= pickup["seq"]:
                break
            if turn.accepts(row, old["turn_id"]):
                turn.consume(row)
            cursor = row["seq"]
        if old["turn_id"] != pickup["turn_id"] and not (turn.tasks or turn.awaiting_callback):
            return status
        self.store.handoff(status["id"], old["id"], tracking=turn.dump(), cursor=cursor)
        self.idle_since.pop(old["id"], None)
        self.last_text.pop(old["id"], None)
        return self.store.status(status["id"])

    def tick(self):
        if hasattr(self.uploads, "maintenance"):
            self.uploads.maintenance()
        for status in self.store.active():
            try:
                if status["state"] == "waiting":
                    pickup = self.db.pickup(status["session_id"], status["prompt"])
                    if pickup:
                        status = self.handoff(status, pickup)
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
        if self.notifications:
            self.notifications.tick()

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
        if hasattr(self.db, "online") and not self.db.online():
            self.idle_since.pop(status["id"], None)
            text = render("reconnecting", Turn.load(status["tracking"]).content)
            if self.last_text.get(status["id"]) != text:
                self.slack.edit(status, text)
                self.last_text[status["id"]] = text
            return
        workspace = self.db.workspace(status["workspace_id"])
        if workspace["state"] == "archived":
            self.finish(status, Turn.load(status["tracking"]).content, "archived")
            self.store.thread_state(status, "archived")
            return
        session = self.db.session(status["session_id"])
        now = self.clock()
        if session["status"] == "reconnecting":
            self.idle_since.pop(status["id"], None)
            text = render("reconnecting", Turn.load(status["tracking"]).content)
            if self.last_text.get(status["id"]) != text:
                self.slack.edit(status, text)
                self.last_text[status["id"]] = text
            return
        if session["status"] == "idle":
            self.idle_since.setdefault(status["id"], now)
        else:
            self.idle_since.pop(status["id"], None)
        if status["state"] == "active":
            turn = Turn.load(status["tracking"])
            if session.get("runtime"):
                turn.observe_runtime(session["runtime"], now)
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
            later_turn = turn.result_seq and self.db.has_later_turn(
                status["session_id"], turn.result_seq, status["turn_id"]
            )
            ready = turn.ready(
                now, session["status"] == "idle" or bool(later_turn), self.settle_seconds
            )
            blocked = {
                "needs_user_input": "answer requested in Conductor",
                "needs_user_permission": "permission requested in Conductor",
                "needs_plan_response": "plan approval requested in Conductor",
            }
            if session["status"] in blocked and not turn.result_seq:
                current = self.db.latest_turn(status["session_id"])
                if current and current in {status["turn_id"], turn.callback_turn_id}:
                    turn.phase, turn.detail = "attention", blocked[session["status"]]
            status["latest"] = (
                f"waiting for {len(turn.tasks)} background job(s)"
                if turn.tasks and turn.result_seq
                else turn.detail or turn.phase
            )
            self.store.update(
                status["id"], cursor=status["cursor"], latest=status["latest"], tracking=turn.dump()
            )
            status["tracking"] = turn.dump()
            if (
                turn.interrupted
                and not turn.result_seq
                and not turn.tasks
                and now - self.idle_since.get(status["id"], now) >= self.settle_seconds
            ):
                self.finish(
                    status,
                    turn.content
                    + "\n\nBackground work was interrupted and the agent is no longer running.",
                    "attention",
                    "background work interrupted",
                )
                return
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
            queued = self.db.pending(status["session_id"], status["prompt"])
            if not queued:
                self.finish(
                    status,
                    "Agent is idle without a matching reply; check the Conductor session.",
                    "attention",
                )
                return
        if status["state"] == "active":
            text = live(turn, status["note"])
        elif session.get("runtime", {}).get("paused"):
            text = render("attention", detail="message queued; queue paused in Conductor")
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
        if self.notifications and status["state"] == "active":
            self.notifications.observe(status, turn.phase, turn.detail)
