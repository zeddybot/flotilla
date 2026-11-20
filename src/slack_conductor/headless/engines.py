"""Versioned CLI invocation and event translation for headless engines."""

import json
import re

from ..claude_events import translate
from ..runtime import Unsupported
from .permissions import validate

CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")


def command(config, engine, native_id, *, first=False, spec=None):
    spec = spec or {}
    policy = spec.get("permission_policy") or config.engine_permission_policy
    validate(policy)
    model = spec.get("model", getattr(config, "engine_model", None))
    effort = spec.get("effort", getattr(config, "engine_effort", None))
    if engine == "claude":
        args = [
            str(config.claude_binary),
            "-p",
            "--input-format",
            "stream-json",
            "--replay-user-messages",
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--permission-mode",
            {
                "read-only": "plan",
                "workspace-write": "acceptEdits",
                "external-sandbox": "bypassPermissions",
            }[policy],
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
        return [str(config.codex_binary), "app-server", "--listen", "stdio://"]
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
            return None, {
                "kind": "result",
                "error": True,
                "text": source.get("error_text") or "Headless turn failed",
            }
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
                return None, {
                    "kind": "update",
                    "parts": [
                        {
                            "kind": "tool",
                            "name": name,
                            "summary": name,
                            "description": name,
                        }
                    ],
                }
        return None, None
    if engine == "claude":
        event = translate(source)
        if event and (event.get("foreign") or event.get("nested")):
            return None, None
        return source.get("session_id"), event
    return None, None


def codex_thread_options(spec, cwd):
    return {
        "cwd": cwd,
        "approvalPolicy": "never",
        "sandbox": {
            "read-only": "read-only",
            "workspace-write": "workspace-write",
            "external-sandbox": "danger-full-access",
        }[spec["permission_policy"]],
        "model": spec["model"],
        "config": {"model_reasoning_effort": spec["effort"]} if spec["effort"] else {},
    }


def codex_notification(source, thread_id, turn_id, model):
    method = source.get("method")
    params = source.get("params")
    if not isinstance(params, dict) or params.get("threadId") != thread_id:
        return None
    if method == "turn/completed":
        turn = params.get("turn", {})
        if turn.get("id") == turn_id:
            if turn["status"] == "completed":
                return {"type": "turn.completed"}
            error = (turn.get("error") or {}).get("message", "").lower()
            unsupported = "model" in error and any(
                phrase in error for phrase in ("not supported", "not available", "does not exist")
            )
            error_text = None
            if unsupported:
                name = f" {model}" if re.fullmatch(r"[a-zA-Z0-9_.:-]{1,100}", model or "") else ""
                error_text = (
                    f"Codex model{name} is unavailable for this worker account. "
                    "Start a new session with an available Codex model."
                )
            return {"type": "turn.failed", "error_text": error_text}
    if method in {"item/started", "item/completed"} and params.get("turnId") == turn_id:
        item = params.get("item", {})
        kind = {
            "agentMessage": "agent_message",
            "commandExecution": "command_execution",
            "fileChange": "file_change",
            "mcpToolCall": "mcp_tool_call",
        }.get(item.get("type"))
        if kind == "agent_message" and method == "item/started":
            return None
        if kind:
            return {
                "type": "item.started" if method == "item/started" else "item.completed",
                "item": {"type": kind, "text": item.get("text")},
            }
    return None
