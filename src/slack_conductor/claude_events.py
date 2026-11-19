"""Normalize Claude messages before they cross a client boundary."""


def translate(source):
    if source.get("parent_tool_use_id") or source.get("conductor_sdk_metadata", {}).get(
        "isForeignResult"
    ):
        return {
            "kind": "result" if source.get("type") == "result" else "ignored",
            "nested": bool(source.get("parent_tool_use_id")),
            "foreign": bool(source.get("conductor_sdk_metadata", {}).get("isForeignResult")),
        }
    kind, subtype = source.get("type"), source.get("subtype")
    if kind == "stream_event":
        stream = source.get("event") or {}
        if stream.get("type") == "message_start":
            return {"kind": "text_start", "message_id": stream.get("message", {}).get("id")}
        if stream.get("type") == "content_block_delta":
            delta = stream.get("delta") or {}
            if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
                return {
                    "kind": "text_delta",
                    "block": stream.get("index", 0),
                    "text": delta["text"],
                }
        return None
    if kind == "result":
        return {
            "kind": "result",
            "text": source.get("result"),
            "error": bool(source.get("is_error")),
        }
    if kind == "system":
        if subtype == "init":
            return {"kind": "started"}
        if subtype in {"task_started", "task_notification"}:
            return {
                "kind": "job_started" if subtype == "task_started" else "job_finished",
                "job_id": source.get("task_id"),
                "status": source.get("status"),
                "description": source.get("description"),
            }
    if kind != "assistant":
        return None
    parts = []
    for block in source.get("message", {}).get("content", []):
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and block.get("text"):
            parts.append({"kind": "output", "text": block["text"]})
        elif block.get("type") == "thinking":
            parts.append({"kind": "activity", "phase": "thinking"})
        elif block.get("type") == "tool_use":
            name = block.get("name") or "tool"
            if name in {"AskUserQuestion", "ExitPlanMode"}:
                parts.append(
                    {
                        "kind": "question",
                        "question_id": block.get("id"),
                        "description": "answer requested"
                        if name == "AskUserQuestion"
                        else "plan approval requested",
                    }
                )
            else:
                parts.append({"kind": "tool", "name": name, "description": name, "summary": name})
    return {"kind": "update", "parts": parts}
