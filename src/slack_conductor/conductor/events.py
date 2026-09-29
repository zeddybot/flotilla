"""Translate Conductor transcript rows at the client boundary."""

import json


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
            event.update(
                nested=bool(source.get("parent_tool_use_id")),
                foreign=bool(source.get("conductor_sdk_metadata", {}).get("isForeignResult")),
            )
            kind, subtype = source.get("type"), source.get("subtype")
            if kind == "result":
                event.update(
                    kind="result", text=source.get("result"), error=bool(source.get("is_error"))
                )
            elif kind == "system" and subtype == "init":
                event.update(kind="started")
            elif kind == "system" and subtype in {"task_started", "task_notification"}:
                event.update(
                    kind="job_started" if subtype == "task_started" else "job_finished",
                    job_id=source.get("task_id"),
                    status=source.get("status"),
                    description=source.get("description"),
                )
            elif kind == "assistant":
                parts = []
                for block in source.get("message", {}).get("content", []):
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text" and block.get("text"):
                        parts.append({"kind": "output", "text": block["text"]})
                    elif block.get("type") == "thinking":
                        parts.append({"kind": "activity", "phase": "thinking"})
                    elif block.get("type") == "tool_use":
                        name, args = block.get("name", "tool"), block.get("input", {})
                        arg = next((v for v in args.values() if isinstance(v, str)), "")
                        part = {
                            "kind": "tool",
                            "name": name,
                            "summary": f"{name}: {arg[:60]}",
                            "description": str(args.get("description") or name),
                        }
                        if name in {"AskUserQuestion", "ExitPlanMode"}:
                            part.update(
                                kind="question",
                                question_id=block.get("id"),
                                description="answer requested in Conductor"
                                if name == "AskUserQuestion"
                                else "plan approval requested in Conductor",
                            )
                        parts.append(part)
                event.update(kind="update", parts=parts)
    return {
        "id": row.get("id", str(row.get("seq", ""))),
        "seq": row.get("seq", 0),
        "session_id": row.get("session_id"),
        "turn_id": row.get("turn_id"),
        "role": row.get("role"),
        "event": event,
    }
