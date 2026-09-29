import json
import sqlite3
from contextlib import closing
from pathlib import Path

from .events import normalize


class ConductorDB:
    def __init__(self, path: Path):
        self.path = path

    def query(self, sql, args=()):
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            return [dict(row) for row in conn.execute(sql, args)]

    def one(self, sql, args=()):
        rows = self.query(sql, args)
        if not rows:
            raise RuntimeError("Conductor record no longer exists")
        return rows[0]

    def workspace(self, wid):
        # Bridge identities are local; Conductor's separate id can identify a remote workspace.
        return self.one("SELECT local_id AS id,* FROM workspaces WHERE local_id=?", (wid,))

    def session(self, sid):
        return self.one("SELECT * FROM sessions WHERE id=?", (sid,))

    def cursor(self, sid):
        return self.one(
            "SELECT coalesce(max(rowid),0) AS n FROM session_messages WHERE session_id=?", (sid,)
        )["n"]

    def messages(self, sid, after):
        # Timestamps can collide. rowid gives insertion order; unsent queue rows
        # are matched independently because Conductor can update them in place.
        rows = self.query(
            "SELECT rowid AS seq,* FROM session_messages "
            "WHERE session_id=? AND rowid>? ORDER BY rowid",
            (sid, after),
        )
        for row in rows:
            if row.get("role") != "assistant":
                continue
            try:
                event = json.loads(row["content"])
            except (ValueError, TypeError):
                continue
            if (
                isinstance(event, dict)
                and event.get("type") == "result"
                and event.get("result") == "end_turn"
                and not event.get("is_error")
                and ConductorDB.session(self, sid).get("agent_type") == "acp"
            ):
                # ACP's stop reason is a completion boundary, not the answer.
                # Preserve the preceding assistant text, as for Codex completions.
                row["content"] = json.dumps({**event, "result": None})
        return [{**row, "event": normalize(row)["event"]} for row in rows]

    def has_user_messages(self, sid):
        """An accepted outbox message freezes initial configuration too."""
        return bool(
            self.query(
                "SELECT id FROM session_messages WHERE session_id=? AND role='user' LIMIT 1", (sid,)
            )
            or self.query(
                "SELECT message_id FROM session_messages_outbox WHERE session_id=? LIMIT 1", (sid,)
            )
        )

    def has_later_turn(self, sid, after, own_turn):
        """Whether a sent user message has started another turn after this result."""
        return bool(
            self.query(
                "SELECT rowid FROM session_messages WHERE session_id=? AND rowid>? "
                "AND role='user' AND sent_at IS NOT NULL AND turn_id IS NOT NULL "
                "AND turn_id!=? LIMIT 1",
                (sid, after, own_turn),
            )
        )

    def latest_turn(self, sid):
        rows = self.query(
            "SELECT turn_id FROM session_messages WHERE session_id=? AND role='user' "
            "AND sent_at IS NOT NULL AND cancelled_at IS NULL ORDER BY rowid DESC LIMIT 1",
            (sid,),
        )
        return rows[0]["turn_id"] if rows else None

    def pickup(self, sid, prompt):
        rows = self.query(
            "SELECT rowid AS seq,* FROM session_messages WHERE session_id=? "
            "AND role='user' AND content=? AND sent_at IS NOT NULL "
            "AND cancelled_at IS NULL AND turn_id IS NOT NULL ORDER BY rowid LIMIT 1",
            (sid, prompt),
        )
        return rows[0] if rows else None

    def pickup_message(self, sid, message_id):
        rows = self.query(
            "SELECT rowid AS seq,* FROM session_messages WHERE session_id=? AND id=? "
            "AND role='user' AND sent_at IS NOT NULL AND cancelled_at IS NULL "
            "AND turn_id IS NOT NULL ORDER BY rowid LIMIT 1",
            (sid, message_id),
        )
        return rows[0] if rows else None

    def pending(self, sid, prompt):
        if self.query(
            "SELECT id FROM session_messages WHERE session_id=? AND role='user' "
            "AND content=? AND sent_at IS NULL AND cancelled_at IS NULL",
            (sid, prompt),
        ):
            return True
        if not self.query(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='session_messages_outbox'"
        ):
            return False
        for row in self.query(
            "SELECT delivery_payload FROM session_messages_outbox WHERE session_id=? "
            "AND state IN ('waiting','uploading','ready','sending')",
            (sid,),
        ):
            try:
                payload = json.loads(row["delivery_payload"])
            except (TypeError, ValueError):
                continue
            if isinstance(payload, dict) and payload.get("message") == prompt:
                return True
        return False


def classify(row):
    if row["role"] != "assistant":
        return []
    try:
        obj = json.loads(row["content"])
    except (ValueError, TypeError):
        return []
    if not isinstance(obj, dict) or obj.get("parent_tool_use_id"):
        return []
    kind, subtype = obj.get("type"), obj.get("subtype")
    if kind == "result":
        if obj.get("conductor_sdk_metadata", {}).get("isForeignResult"):
            return []
        result = obj.get("result") or "Agent finished without a text reply."
        return [("result", ("error: " if obj.get("is_error") else "") + str(result))]
    if kind == "system" and subtype == "init":
        return [("status", "starting agent")]
    if kind == "system" and subtype == "task_started":
        return [("status", obj.get("description", "working"))]
    output = []
    if kind == "assistant":
        for block in obj.get("message", {}).get("content", []):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "thinking":
                output.append(("status", "thinking"))
            elif block.get("type") == "tool_use":
                arg = next((v for v in block.get("input", {}).values() if isinstance(v, str)), "")
                output.append(("tool", f"{block.get('name', 'tool')}: {arg[:60]}"))
    return output
