import json

from slack_conductor.config import Config
from slack_conductor.headless.engines import command, decode


def test_explicit_session_resume_and_permissions_are_preserved():
    config = Config(runtime_adapter="headless", engine_permission_policy="workspace-write")
    for engine in ("claude", "codex"):
        argv = command(config, engine, "known-session", "prompt", spec={"model": "chosen"})
        assert "known-session" in argv and "chosen" in argv
        assert "--last" not in argv
        assert not any("bypass" in arg or "skip-permissions" in arg for arg in argv)
    assert "dontAsk" in command(config, "claude", "sid", "prompt")
    assert 'approval_policy="never"' in command(config, "codex", "sid", "prompt")


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
