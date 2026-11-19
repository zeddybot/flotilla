"""Translate Conductor transcript rows at the client boundary."""

import json

from ..claude_events import translate


def normalize(row):
    if "event" in row:
        return {k: row.get(k) for k in ("id", "seq", "session_id", "turn_id", "role", "event")}
    event = {"kind": "input" if row.get("role") == "user" else "ignored"}
    if row.get("role") == "assistant":
        try:
            source = json.loads(row.get("content") or "null")
        except (ValueError, TypeError):
            source = None
        if isinstance(source, dict):
            event = translate(source) or event
    return {
        "id": row.get("id", str(row.get("seq", ""))),
        "seq": row.get("seq", 0),
        "session_id": row.get("session_id"),
        "turn_id": row.get("turn_id"),
        "role": row.get("role"),
        "event": event,
    }
