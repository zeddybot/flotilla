"""Versioned CLI invocation and event translation for headless engines."""

import json

from ..runtime import Unsupported


def command(config, engine, native_id, prompt, *, first=False, spec=None):
    spec = spec or {}
    policy = spec.get("permission_policy") or config.engine_permission_policy
    if policy not in {"read-only", "workspace-write"}:
        raise ValueError("Headless permission policy must be read-only or workspace-write")
    model = spec.get("model") or getattr(config, "engine_model", None)
    effort = spec.get("effort") or getattr(config, "engine_effort", None)
    if engine == "claude":
        args = [
            str(config.claude_binary),
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            "plan" if policy == "read-only" else "dontAsk",
        ]
        args += (
            (["--session-id", native_id] if first else ["--resume", native_id]) if native_id else []
        )
        if model:
            args += ["--model", model]
        if effort:
            args += ["--effort", effort]
        return args
    if engine == "codex":
        args = [str(config.codex_binary), "exec", "-s", policy]
        args += ["-c", 'approval_policy="never"']
        if native_id:
            args += ["resume", native_id]
        args += ["--json"]
        if model:
            args += ["-m", model]
        if effort:
            args += ["-c", f'model_reasoning_effort="{effort}"']
        return args + [prompt]
    raise Unsupported(f"Unknown headless engine: {engine}")


def decode(engine, line):
    """Return a native ID and normalized event, ignoring unsupported noise."""
    try:
        source = json.loads(line)
    except (ValueError, TypeError):
        return None, None
    if not isinstance(source, dict):
        return None, None
    kind = source.get("type")
    if engine == "codex":
        if kind == "thread.started":
            return source.get("thread_id"), {"kind": "started"}
        if kind in {"turn.started", "turn.completed"}:
            return None, {"kind": "started" if kind == "turn.started" else "result", "text": ""}
        if kind == "turn.failed":
            return None, {"kind": "result", "error": True, "text": "Headless turn failed"}
        item = source.get("item", {})
        if not isinstance(item, dict):
            return None, None
        if kind in {"item.started", "item.updated", "item.completed"}:
            if item.get("type") == "agent_message":
                text = item.get("text")
                if text:
                    return None, {"kind": "update", "parts": [{"kind": "output", "text": text}]}
            if item.get("type") in {"command_execution", "file_change", "mcp_tool_call"}:
                name = item.get("type")
                detail = item.get("command") or item.get("title") or name
                return None, {
                    "kind": "update",
                    "parts": [
                        {
                            "kind": "tool",
                            "name": name,
                            "summary": str(detail)[:120],
                            "description": str(detail)[:120],
                        }
                    ],
                }
        return None, None
    if engine == "claude":
        if kind == "system" and source.get("subtype") in {"task_started", "task_notification"}:
            return source.get("session_id"), {
                "kind": "job_started" if source["subtype"] == "task_started" else "job_finished",
                "job_id": source.get("task_id"),
                "status": source.get("status"),
                "description": source.get("description"),
            }
        if kind == "system" and source.get("subtype") == "init":
            return source.get("session_id"), {"kind": "started"}
        if kind == "result":
            return source.get("session_id"), {
                "kind": "result",
                "text": source.get("result") or "",
                "error": bool(source.get("is_error")),
            }
        if kind == "assistant":
            parts = []
            nested = bool(source.get("parent_tool_use_id"))
            for block in source.get("message", {}).get("content", []):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text"):
                    parts.append({"kind": "output", "text": block["text"]})
                elif block.get("type") == "tool_use":
                    name = block.get("name", "tool")
                    parts.append(
                        {"kind": "tool", "name": name, "summary": name, "description": name}
                    )
                    if name in {"AskUserQuestion", "ExitPlanMode"}:
                        parts.append(
                            {"kind": "question", "description": "Input required in a terminal"}
                        )
            if parts:
                return source.get("session_id"), {
                    "kind": "update",
                    "parts": [] if nested else parts,
                    "nested": nested,
                }
        return source.get("session_id"), None
    return None, None
