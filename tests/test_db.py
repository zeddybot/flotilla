import json
import sqlite3

import pytest

from slack_conductor.conductor.db import ConductorDB, classify
from slack_conductor.conductor.selectors import (
    is_queued_prompt,
    model_menu_label,
    model_picker_label,
)


def test_model_picker_badges_do_not_change_model_identity():
    assert model_menu_label("Fable 5.1 NEW\nSet Fable 5.1 as default and select 1") == "Fable 5.1"
    assert model_menu_label("Opus 5 NEW\n\n3") == "Opus 5"
    assert model_menu_label("Sonnet 5 1M\nSet Sonnet 5 1M as default") == "Sonnet 5 1M"
    assert model_menu_label("5.6 Sol\nSet 5.6 Sol as default") == "5.6 Sol"


@pytest.mark.parametrize(
    ("title", "model"),
    [
        ("Change agent (Fable 5.1)", "Fable 5.1"),
        ("Change agent (Fable 5.1 · High)", "Fable 5.1"),
        ("Change agent (GPT-6 Astra · High)", "GPT-6 Astra"),
        ("Change agent (Sonnet 5 1M · Low)", "Sonnet 5 1M"),
    ],
)
def test_model_picker_effort_does_not_change_model_identity(title, model):
    assert model_picker_label(title) == model


def test_model_menu_effort_preserves_context_and_new_chat_warning():
    assert model_menu_label("Fable 5.1 High") == "Fable 5.1"
    assert model_menu_label("Sonnet 5 1M Low") == "Sonnet 5 1M"
    assert model_menu_label("GPT-5.6 Sol High Opens in new chat") != "GPT-5.6 Sol"


def test_multiline_slack_prompt_matches_flattened_queue_label():
    prompt = "[slack] channel=D1 thread_ts=1\nRead the thread for context.\n\nreply ok"
    label = "[slack] channel=D1 thread_ts=1 Read the thread for context. reply ok Edit Delete Steer"
    assert is_queued_prompt(label, prompt)
    assert not is_queued_prompt(label, prompt.replace("thread_ts=1", "thread_ts=2"))
    assert not is_queued_prompt(prompt, prompt)


def test_classification_ignores_user_and_nested_agent_output():
    result = json.dumps({"type": "result", "result": "done"})
    assert classify({"role": "user", "content": result}) == []
    assert classify({"role": "assistant", "content": "plain text"}) == []
    assert classify({"role": "assistant", "content": result}) == [("result", "done")]
    assert (
        classify(
            {
                "role": "assistant",
                "content": json.dumps(
                    {
                        "type": "result",
                        "result": "nested",
                        "parent_tool_use_id": "nested-agent",
                    }
                ),
            }
        )
        == []
    )


def test_all_content_blocks_are_classified():
    row = {
        "role": "assistant",
        "content": json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "thinking"},
                        {"type": "tool_use", "name": "Bash", "input": {"command": "pwd"}},
                    ],
                },
            }
        ),
    }
    assert classify(row) == [("status", "thinking"), ("tool", "Bash: pwd")]


def test_read_only_and_timestamp_collisions(tmp_path):
    path = tmp_path / "conductor.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE session_messages(session_id, content, created_at)")
    conn.executemany("INSERT INTO session_messages VALUES ('s',?,'same-time')", [("a",), ("b",)])
    conn.commit()
    conn.close()
    db = ConductorDB(path)
    assert [r["content"] for r in db.messages("s", 1)] == ["b"]
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        db.query("DELETE FROM session_messages")
