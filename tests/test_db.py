import json
import sqlite3
from contextlib import closing

import pytest

from slack_conductor.conductor.db import ConductorDB, classify
from slack_conductor.poller import Poller
from tests.support import message, status


@pytest.mark.parametrize(
    ("agent", "is_error", "expected"),
    [
        ("acp", False, "Actual Alpha answer."),
        ("claude", False, "end_turn"),
        ("acp", True, "end_turn"),
    ],
)
def test_acp_completion_marker_preserves_answer_through_slack_delivery(
    bridge_state, agent, is_error, expected
):
    store, conn, db, slack = bridge_state
    conn.execute("ALTER TABLE sessions ADD COLUMN agent_type TEXT")
    conn.execute("UPDATE sessions SET agent_type=?", (agent,))
    request = status(store, "alpha", "hello")
    message(conn, "user", "hello", "turn")
    message(
        conn,
        "assistant",
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Actual Alpha answer."}]},
            }
        ),
        "turn",
    )
    message(
        conn,
        "assistant",
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "result": "end_turn",
                "is_error": is_error,
            }
        ),
        "turn",
    )
    Poller(store, db, slack, settle_seconds=0).tick()
    assert store.status(request)["state"] == "complete"
    assert store.status(request)["result"] == expected
    assert expected in slack.edits[-1][1]
    # Observation normalization never rewrites Conductor's persisted transcript.
    assert (
        json.loads(
            conn.execute(
                "SELECT content FROM session_messages ORDER BY rowid DESC LIMIT 1"
            ).fetchone()[0]
        )["result"]
        == "end_turn"
    )


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
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("CREATE TABLE session_messages(session_id, content, created_at)")
        conn.executemany(
            "INSERT INTO session_messages VALUES ('s',?,'same-time')", [("a",), ("b",)]
        )
    db = ConductorDB(path)
    assert [r["content"] for r in db.messages("s", 1)] == ["b"]
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        db.query("DELETE FROM session_messages")
