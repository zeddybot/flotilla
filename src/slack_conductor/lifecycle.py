"""Replay a Slack-owned turn, including results followed by background callbacks."""

import json
from dataclasses import asdict, dataclass, field


@dataclass
class Turn:
    tasks: dict = field(default_factory=dict)
    ended: list = field(default_factory=list)
    content: str = ""
    phase: str = "starting"
    detail: str = ""
    result_seq: int = 0
    awaiting_callback: bool = False
    is_error: bool = False
    settled_at: float | None = None
    callback_turn_id: str = ""
    ignore_callback: bool = False
    runtime_epoch: str = ""
    runtime_missing_since: float | None = None
    interrupted: bool = False

    @classmethod
    def load(cls, value):
        return cls(**json.loads(value or "{}"))

    def dump(self):
        return json.dumps(asdict(self))

    def accepts(self, row, own_turn):
        """Task notifications may resume under a newer user turn's ID."""
        if row["role"] == "user":
            if row["turn_id"] == own_turn:
                self.ignore_callback = False
                self.callback_turn_id = ""
            elif row["turn_id"] == self.callback_turn_id:
                self.callback_turn_id = ""
            return row["turn_id"] == own_turn
        event = row.get("event", {})
        if (
            isinstance(event, dict)
            and event.get("kind") == "result"
            and event.get("foreign")
            and row["turn_id"] in {own_turn, self.callback_turn_id}
        ):
            # A restored job can emit a foreign result before this user message's
            # own response. That boundary ends the unrelated callback segment.
            self.ignore_callback = False
            self.callback_turn_id = ""
            return False
        if (
            isinstance(event, dict)
            and event.get("kind") == "job_finished"
            and not event.get("nested")
            and not event.get("foreign")
        ):
            task = event.get("job_id")
            if task in self.tasks or task in self.ended:
                self.callback_turn_id = row["turn_id"]
                self.ignore_callback = False
                return True
            if row["turn_id"] == own_turn and not self.callback_turn_id:
                self.ignore_callback = True
        return (row["turn_id"] == own_turn and not self.ignore_callback) or bool(
            self.callback_turn_id and row["turn_id"] == self.callback_turn_id
        )

    def consume(self, row):
        if row["role"] != "assistant":
            return False
        event = row.get("event", {})
        if not isinstance(event, dict) or event.get("nested") or event.get("foreign"):
            return False
        kind = event.get("kind")
        if kind == "job_started" and event.get("job_id"):
            task = event["job_id"]
            if task in self.ended or task in self.tasks:
                return False
            self.tasks[task] = event.get("description") or "background job"
        elif kind == "job_finished" and event.get("job_id"):
            if event.get("status") not in {
                "completed",
                "failed",
                "stopped",
                "cancelled",
                "canceled",
                "interrupted",
            }:
                return False
            task = event["job_id"]
            if task in self.ended:
                return False
            self.tasks.pop(task, None)
            self.ended.append(task)
            self.awaiting_callback = True
            self.phase, self.detail = "working", "checking background job results"
        elif kind == "result":
            # Codex emits the answer as assistant text, followed by a completion
            # event containing only usage metadata. Keep the answer in that case.
            self.content = str(
                event.get("text") or self.content or "Agent finished without a text reply."
            )
            self.is_error = bool(event.get("error"))
            self.result_seq = row["seq"]
            self.awaiting_callback = False
            self.phase, self.detail = "waiting" if self.tasks else "working", ""
            self.settled_at = None
            return True
        elif kind == "update":
            parts = event.get("parts", [])
            changed = False
            text = [p["text"] for p in parts if p["kind"] == "output"]
            if text:
                self.content = "\n\n".join(text)
                changed = True
            for part in parts:
                if part["kind"] == "activity":
                    self.phase, self.detail = part["phase"], ""
                    changed = True
                elif part["kind"] in {"tool", "question"}:
                    self.phase = "attention" if part["kind"] == "question" else "working"
                    self.detail = part["description"]
                    changed = True
            if not changed:
                return False
        elif kind == "started":
            self.phase, self.detail = "starting", ""
        else:
            return False
        # Activity after a result invalidates that candidate. In particular, a
        # completed job may wake the agent later; its earlier result isn't final.
        self.result_seq = 0
        self.settled_at = None
        return True

    def observe_runtime(self, runtime, now):
        epoch = runtime["instance_id"]
        changed = bool(self.runtime_epoch and self.runtime_epoch != epoch)
        self.runtime_epoch = epoch
        active = {t["id"] for t in runtime.get("jobs", [])}
        missing = set(self.tasks) - active
        pending = bool(missing or self.awaiting_callback)
        idle = runtime.get("activity") != "working"
        if pending and idle:
            if self.runtime_missing_since is None:
                self.runtime_missing_since = now
        else:
            self.runtime_missing_since = None
        expired = self.runtime_missing_since is not None and now - self.runtime_missing_since >= 5
        if pending and (changed or expired):
            for task in missing:
                self.tasks.pop(task, None)
                if task not in self.ended:
                    self.ended.append(task)
            self.awaiting_callback = False
            self.result_seq = 0
            self.settled_at = None
            self.interrupted = True
            self.phase = "working"
            self.detail = "recovering after interrupted background work"
        if self.interrupted and self.result_seq:
            self.interrupted = False

    def ready(self, now, idle, settle_seconds):
        if not self.result_seq or self.tasks or self.awaiting_callback or not idle:
            self.settled_at = None
            return False
        if self.settled_at is None:
            self.settled_at = now
        return now - self.settled_at >= settle_seconds
