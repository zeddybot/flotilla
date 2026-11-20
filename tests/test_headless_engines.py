import json

from slack_conductor.config import Config
from slack_conductor.headless.engines import (
    codex_notification,
    codex_thread_options,
    command,
    decode,
)


def test_explicit_session_resume_and_permissions_are_preserved():
    config = Config(runtime_adapter="headless", engine_permission_policy="workspace-write")
    argv = command(config, "claude", "known-session", spec={"model": "chosen"})
    assert "known-session" in argv and "chosen" in argv
    assert "--last" not in argv
    assert not any("bypass" in arg or "skip-permissions" in arg for arg in argv)
    assert "acceptEdits" in command(config, "claude", "sid")
    assert command(config, "codex", "sid")[1:] == ["app-server", "--listen", "stdio://"]
    params = codex_thread_options(
        {"permission_policy": "workspace-write", "model": "chosen", "effort": "high"}, "/work"
    )
    assert params == {
        "sandbox": "workspace-write",
        "approvalPolicy": "never",
        "model": "chosen",
        "cwd": "/work",
        "config": {"model_reasoning_effort": "high"},
    }


def test_codex_completion_is_distinct_from_text_and_errors():
    _, output = decode(
        "codex",
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "answer"}}),
    )
    _, completed = decode("codex", '{"type":"turn.completed"}')
    _, failed = decode("codex", '{"type":"turn.failed"}')
    assert output["kind"] == "update"
    assert completed["kind"] == "result" and not completed.get("error")
    assert failed["error"]


def test_foreign_claude_output_and_thinking_do_not_leak():
    _, event = decode(
        "claude",
        json.dumps(
            {
                "type": "assistant",
                "parent_tool_use_id": "parent",
                "message": {"content": [{"type": "text", "text": "other agent secret"}]},
            }
        ),
    )
    assert event is None or "other agent secret" not in json.dumps(event)
    _, event = decode(
        "claude",
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "thinking", "thinking": "hidden reasoning"}]},
            }
        ),
    )
    assert "hidden reasoning" not in json.dumps(event)


def test_codex_model_error_is_actionable_and_omits_untrusted_details():
    raw = {
        "method": "turn/completed",
        "params": {
            "threadId": "thread",
            "turn": {
                "id": "turn",
                "status": "failed",
                "error": {"message": "Model is not supported by this account. sensitive details"},
            },
        },
    }
    notification = codex_notification(raw, "thread", "turn", "example-model")
    _, result = decode("codex", json.dumps(notification))
    assert result["error"]
    assert "example-model is unavailable" in result["text"]
    assert "sensitive details" not in result["text"]
