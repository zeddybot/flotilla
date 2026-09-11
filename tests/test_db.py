import json
import sqlite3
from contextlib import closing

import pytest

from slack_conductor.conductor.db import ConductorDB, classify


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
