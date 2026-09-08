import json
import sqlite3
from contextlib import closing
from pathlib import Path


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
        return self.one("SELECT * FROM workspaces WHERE id=?", (wid,))

    def session(self, sid):
        return self.one("SELECT * FROM sessions WHERE id=?", (sid,))

    def workspace_ids(self):
        return {r["id"] for r in self.query("SELECT id FROM workspaces")}

    def new_workspaces(self, before):
        return [
            r
            for r in self.query("SELECT * FROM workspaces ORDER BY created_at DESC")
            if r["id"] not in before
        ]

    def cursor(self, sid):
        return self.one(
            "SELECT coalesce(max(rowid),0) AS n FROM session_messages WHERE session_id=?", (sid,)
        )["n"]

    def messages(self, sid, after):
        # Timestamps can collide. rowid gives insertion order; unsent queue rows
        # are matched independently because Conductor can update them in place.
        return self.query(
            "SELECT rowid AS seq,* FROM session_messages "
            "WHERE session_id=? AND rowid>? ORDER BY rowid",
            (sid, after),
        )

    def pickup(self, sid, prompt):
        rows = self.query(
            "SELECT rowid AS seq,* FROM session_messages WHERE session_id=? "
            "AND role='user' AND content=? AND sent_at IS NOT NULL "
            "AND cancelled_at IS NULL AND turn_id IS NOT NULL ORDER BY rowid LIMIT 1",
            (sid, prompt),
        )
        return rows[0] if rows else None

    def send_mode(self):
        # TOML supersedes legacy database settings in current Conductor.
        import tomllib

        p = Path.home() / ".conductor/settings.toml"
        data = tomllib.loads(p.read_text()) if p.exists() else {}

        def find(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if key in {
                        "default_message_send_mode",
                        "message_send_mode",
                        "follow_up_behavior",
                    }:
                        return child
                    found = find(child)
                    if found:
                        return found
            return None

        mode = find(data)
        if not mode:
            rows = self.query("SELECT value FROM settings WHERE key='default_message_send_mode'")
            mode = rows[0]["value"].strip('"') if rows else "steering"
        return "queue" if mode in {"queue", "queued"} else "steer"


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
