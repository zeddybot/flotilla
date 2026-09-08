import json

from test_bridge import fixture, message, result, status
from test_lifecycle import event

from slack_conductor.lifecycle import Turn
from slack_conductor.poller import Poller
from slack_conductor.presentation import STATES, live, pages, render


def test_headers_and_background_content_survive_state_changes():
    for phase, (emoji, label) in STATES.items():
        assert render(phase, "update", "detail") == f"{emoji} *{label}* · detail\n\nupdate"
    turn = Turn(
        tasks={"deploy": "preview deployment"},
        content="I'll verify after deployment.",
        result_seq=5,
    )
    assert (
        live(turn)
        == "⏳ *Waiting* · preview deployment · 1 background job\n\nI'll verify after deployment."
    )
    turn.result_seq, turn.phase, turn.detail = 0, "working", "verifying preview"
    assert live(turn).startswith("⚙️ *Working* · verifying preview · 1 background job")
    assert live(turn).endswith("I'll verify after deployment.")


def test_all_long_final_parts_have_bounded_headers_and_closed_code_fences():
    output = pages("done", "```python\n" + "print('hello')\n" * 1000 + "```", "<&>" * 100)
    assert len(output) > 1
    assert all(p.startswith("✅ *Done*") and len(p) <= 3900 for p in output)
    assert all(p.count("```") % 2 == 0 for p in output)


def test_steering_reuses_original_message_and_preserves_jobs_across_crash(tmp_path):
    store, conn, db, slack = fixture(tmp_path)
    first = status(store, "e1", "first")
    message(conn, "user", "first", "turn")
    event(conn, "task_started", task_id="job", description="build preview")
    result(conn, "Waiting for build.", "turn")
    poller = Poller(store, db, slack, settle_seconds=0)
    poller.tick()
    second = status(store, "e2", "second", cursor=db.cursor("s"))
    message(conn, "user", "second", "turn")
    # Simulate interruption after the SQLite handoff, before Slack cleanup.
    store.handoff(second, first)
    store.update(second, state="active", turn_id="turn")
    assert json.loads(store.status(second)["discard_ts"]) == ["status-2"]
    poller = Poller(store, db, slack, settle_seconds=0)
    poller.reconcile()
    assert store.status(second)["status_ts"] == "status-1"
    assert set(Turn.load(store.status(second)["tracking"]).tasks) == {"job"}
    assert slack.deletes == ["status-2"]
    event(conn, "task_notification", task_id="job", status="completed")
    result(conn, "Verified the steered change.", "turn")
    poller.tick()
    assert slack.edits[-1] == ("status-1", render("done", "Verified the steered change."))
    assert store.status(first)["state"] == "superseded"
    assert store.status(second)["state"] == "complete"
    conn.close()


def test_error_result_renders_failed_and_preserves_partial_answer(tmp_path):
    store, conn, db, slack = fixture(tmp_path)
    status(store, "e1", "task")
    message(conn, "user", "task", "turn")
    message(
        conn,
        "assistant",
        json.dumps(
            {"type": "result", "is_error": True, "result": "Tests failed; changes retained."}
        ),
        "turn",
    )
    Poller(store, db, slack, settle_seconds=0).tick()
    assert slack.edits[-1][1] == render("failed", "Tests failed; changes retained.")
    conn.close()
