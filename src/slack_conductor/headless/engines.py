"""Versioned CLI invocation and event translation for headless engines."""

import json

from ..claude_events import translate
from ..runtime import Unsupported
from .permissions import validate


def command(config, engine, native_id, prompt, *, first=False, spec=None):
    spec = spec or {}
    policy = spec.get("permission_policy") or config.engine_permission_policy
    validate(policy)
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
        args = [
            str(config.codex_binary),
            "exec",
            "-s",
            "danger-full-access" if policy == "external-sandbox" else policy,
        ]
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
