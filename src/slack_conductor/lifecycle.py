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
        try:
            event = json.loads(row["content"])
        except (ValueError, TypeError):
            event = {}
        if (
            isinstance(event, dict)
            and event.get("type") == "system"
            and event.get("subtype") == "task_notification"
            and not event.get("parent_tool_use_id")
            and not event.get("conductor_sdk_metadata", {}).get("isForeignResult")
        ):
            task = event.get("task_id")
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
        try:
            event = json.loads(row["content"])
        except (ValueError, TypeError):
            return False
        if (
            not isinstance(event, dict)
            or event.get("parent_tool_use_id")
            or event.get("conductor_sdk_metadata", {}).get("isForeignResult")
        ):
            return False
        kind, subtype = event.get("type"), event.get("subtype")
        if kind == "system" and subtype == "task_started" and event.get("task_id"):
            task = event["task_id"]
            if task in self.ended or task in self.tasks:
                return False
            self.tasks[task] = event.get("description") or "background job"
        elif kind == "system" and subtype == "task_notification" and event.get("task_id"):
            if event.get("status") not in {
                "completed",
                "failed",
                "stopped",
                "cancelled",
                "canceled",
                "interrupted",
            }:
                return False
            task = event["task_id"]
            if task in self.ended:
                return False
            self.tasks.pop(task, None)
            self.ended.append(task)
            self.awaiting_callback = True
            self.phase, self.detail = "working", "checking background job results"
        elif kind == "result":
            self.content = str(event.get("result") or "Agent finished without a text reply.")
            self.is_error = bool(event.get("is_error"))
            self.result_seq = row["seq"]
            self.awaiting_callback = False
            self.phase, self.detail = "waiting" if self.tasks else "working", ""
            self.settled_at = None
            return True
        elif kind == "assistant":
            blocks = event.get("message", {}).get("content", [])
            changed = False
            text = [b["text"] for b in blocks if b.get("type") == "text" and b.get("text")]
            if text:
                self.content = "\n\n".join(text)
                changed = True
            for block in blocks:
                if block.get("type") == "thinking":
                    self.phase, self.detail = "thinking", ""
                    changed = True
                elif block.get("type") == "tool_use":
                    args = block.get("input", {})
                    self.phase = "working"
                    self.detail = str(
                        args.get("description") or block.get("name") or "running tool"
                    )
                    if block.get("name") == "AskUserQuestion":
                        self.phase, self.detail = "attention", "answer requested in Conductor"
                    changed = True
            if not changed:
                return False
        elif kind == "system" and subtype == "init":
            self.phase, self.detail = "starting", ""
        else:
            return False
        # Activity after a result invalidates that candidate. In particular, a
        # completed job may wake the agent later; its earlier result isn't final.
        self.result_seq = 0
        self.settled_at = None
        return True

    def ready(self, now, idle, settle_seconds):
        if not self.result_seq or self.tasks or self.awaiting_callback or not idle:
            self.settled_at = None
            return False
        if self.settled_at is None:
            self.settled_at = now
        return now - self.settled_at >= settle_seconds
