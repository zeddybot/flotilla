import json

import pytest
from test_bridge import fixture, message, result, status

from slack_conductor.lifecycle import Turn
from slack_conductor.poller import Poller
from slack_conductor.store import Store


def event(conn, subtype, *, turn="turn", **values):
    message(conn, "assistant", json.dumps({"type": "system", "subtype": subtype, **values}), turn)


@pytest.mark.parametrize("polled_job", [False, True])
@pytest.mark.parametrize("callback_pending", [False, True])
def test_background_steer_on_new_turn_moves_job_and_answer_below_followup(
    tmp_path, polled_job, callback_pending
):
    store, conn, db, slack = fixture(tmp_path)
    first = status(store, "first", "watch deploy")
    message(conn, "user", "watch deploy", "turn")
    poller = Poller(store, db, slack, settle_seconds=0)
    poller.tick()
    event(conn, "task_started", task_id="deploy", description="staging deployment")
    result(conn, "Waiting for deployment.", "turn")
    if callback_pending:
        event(conn, "task_notification", task_id="deploy", status="completed")
    if polled_job:
        poller.tick()
    second = status(store, "second", "stop watching", cursor=db.cursor("s"))
    message(conn, "user", "stop watching", "newer")
    poller.tick()
    inherited = Turn.load(store.status(second)["tracking"])
    assert inherited.content == "Waiting for deployment."
    assert inherited.awaiting_callback if callback_pending else "deploy" in inherited.tasks
    assert store.status(first)["state"] == "superseded"
    assert slack.deletes == ["status-1"]
    assert slack.edits[-1][0] == "status-2"
    # Pickup and inherited job ownership must survive daemon restart.
    poller = Poller(Store(store.path), db, slack, settle_seconds=0)
    poller.reconcile()
    event(conn, "task_notification", task_id="deploy", status="stopped", turn="newer")
    result(conn, "Stopped the staging deploy watch.", "newer")
    poller.tick()
    assert store.status(second)["state"] == "complete"
    assert store.status(second)["result"] == "Stopped the staging deploy watch."
    assert not Turn.load(store.status(second)["tracking"]).ignore_callback
    assert not any("Stopped the staging" in text for ts, text in slack.edits if ts == "status-1")
    conn.close()


@pytest.mark.parametrize("completed_before_followup", [False, True])
def test_background_handoff_uses_state_at_each_user_message_when_polling_catches_up(
    tmp_path, completed_before_followup
):
    store, conn, db, slack = fixture(tmp_path)
    first = status(store, "first", "watch deploy")
    message(conn, "user", "watch deploy", "turn")
    event(conn, "task_started", task_id="deploy")
    result(conn, "Waiting for deployment.", "turn")
    poller = Poller(store, db, slack, settle_seconds=0)
    poller.tick()
    if completed_before_followup:
        event(conn, "task_notification", task_id="deploy", status="completed")
        result(conn, "Deployment verified.", "turn")
    second = status(store, "second", "stop watching", cursor=db.cursor("s"))
    message(conn, "user", "stop watching", "stop")
    if not completed_before_followup:
        event(conn, "task_notification", task_id="deploy", status="stopped", turn="stop")
    result(conn, "Stopped watching.", "stop")
    third = status(store, "third", "watch again", cursor=db.cursor("s"))
    message(conn, "user", "watch again", "restart")
    event(conn, "task_started", task_id="new-deploy", turn="restart")
    result(conn, "Watching again.", "restart")
    # All three native turns are available in one poll, as after slow Slack IO.
    poller.tick()
    if completed_before_followup:
        assert store.status(first)["state"] == "complete"
        assert store.status(first)["result"] == "Deployment verified."
        assert slack.deletes == []
    else:
        assert store.status(first)["state"] == "superseded"
        assert slack.deletes == ["status-1"]
    assert store.status(second)["state"] == "complete"
    assert store.status(second)["result"] == "Stopped watching."
    assert store.status(third)["state"] == "active"
    assert Turn.load(store.status(third)["tracking"]).tasks == {"new-deploy": "background job"}
    conn.close()


def test_waiting_queue_does_not_hide_running_job_from_later_steer(tmp_path):
    store, conn, db, slack = fixture(tmp_path)
    first = status(store, "first", "watch deploy")
    message(conn, "user", "watch deploy", "turn")
    event(conn, "task_started", task_id="deploy")
    result(conn, "Waiting for deployment.", "turn")
    poller = Poller(store, db, slack, settle_seconds=0)
    poller.tick()
    queued = status(store, "queued", "next task", send_mode="queue")
    message(conn, "user", "next task", None, sent=None)
    steer = status(store, "steer", "stop watching", cursor=db.cursor("s"))
    message(conn, "user", "stop watching", "stop")
    event(conn, "task_notification", task_id="deploy", status="stopped", turn="stop")
    result(conn, "Stopped watching.", "stop")
    poller.tick()
    assert store.status(first)["state"] == "superseded"
    assert store.status(steer)["state"] == "complete"
    assert store.status(steer)["result"] == "Stopped watching."
    assert store.status(queued)["state"] == "waiting"
    assert slack.deletes == ["status-1"]
    conn.execute(
        "UPDATE session_messages SET turn_id='queue',sent_at='now' WHERE content='next task'"
    )
    conn.commit()
    result(conn, "Queued task done.", "queue")
    poller.tick()
    assert store.status(queued)["state"] == "complete"
    assert store.status(queued)["result"] == "Queued task done."
    assert slack.deletes == ["status-1"]
    conn.close()


def test_preview_wait_resumes_after_restart_and_only_finishes_after_callback(tmp_path):
    store, conn, db, slack = fixture(tmp_path)
    sid = status(store, "request", "fix bug")
    message(conn, "user", "fix bug", "turn")
    event(conn, "task_started", task_id="deploy", description="preview deployment")
    result(conn, "Waiting for the preview deployment.", "turn")
    clock = [100.0]
    poller = Poller(store, db, slack, clock=lambda: clock[0])
    poller.tick()
    assert store.status(sid)["state"] == "active"
    assert Turn.load(store.status(sid)["tracking"]).content.startswith("Waiting")
    clock[0] += 1000  # Idle without a running foreground turn isn't completion.
    poller = Poller(Store(store.path), db, slack, clock=lambda: clock[0])
    poller.reconcile()
    assert store.status(sid)["state"] == "active"
    event(conn, "task_notification", task_id="deploy", status="completed")
    poller.tick()
    clock[0] += 1000  # The callback may be delayed; never publish the stale result.
    poller.tick()
    assert store.status(sid)["state"] == "active"
    result(conn, "Fixed and preview verified.", "turn")
    poller.tick()
    assert store.status(sid)["state"] == "active"
    clock[0] += 2
    poller.tick()
    assert store.status(sid)["state"] == "complete"
    assert store.status(sid)["result"] == "Fixed and preview verified."
    conn.close()


def test_jobs_are_independent_and_duplicate_notifications_cannot_reopen_them(tmp_path):
    store, conn, db, slack = fixture(tmp_path)
    sid = status(store, "request", "task")
    message(conn, "user", "task", "turn")
    for task in ("one", "two"):
        event(conn, "task_started", task_id=task)
    result(conn, "Waiting.", "turn")
    event(conn, "task_notification", task_id="one", status="failed")
    event(conn, "task_notification", task_id="one", status="failed")
    event(conn, "task_started", task_id="one")
    result(conn, "Recovered; still waiting for two.", "turn")
    poller = Poller(store, db, slack, settle_seconds=0)
    poller.tick()
    assert store.status(sid)["state"] == "active"
    assert set(Turn.load(store.status(sid)["tracking"]).tasks) == {"two"}
    event(conn, "task_notification", task_id="two", status="stopped")
    result(conn, "Done after cancellation.", "turn")
    poller.tick()
    assert store.status(sid)["state"] == "complete"
    conn.close()


def test_active_old_runtime_cursor_rebuilds_outstanding_jobs(tmp_path):
    store, conn, db, slack = fixture(tmp_path)
    sid = status(store, "request", "task")
    message(conn, "user", "task", "turn")
    event(conn, "task_started", task_id="deploy")
    store.update(sid, state="active", turn_id="turn", cursor=db.cursor("s"))
    result(conn, "Still waiting.", "turn")
    Poller(store, db, slack, settle_seconds=0).reconcile()
    assert store.status(sid)["state"] == "active"
    assert "deploy" in Turn.load(store.status(sid)["tracking"]).tasks
    conn.close()


def test_foreign_and_nested_events_do_not_change_lifecycle():
    turn = Turn()
    for extra in (
        {"parent_tool_use_id": "nested"},
        {"conductor_sdk_metadata": {"isForeignResult": True}},
    ):
        assert not turn.consume(
            {
                "seq": 1,
                "role": "assistant",
                "content": json.dumps({"type": "result", "result": "private", **extra}),
            }
        )
    assert not turn.content and not turn.result_seq


@pytest.mark.parametrize("completion", [{}, {"result": None}, {"result": ""}])
def test_codex_completion_preserves_final_text_after_restart_and_steer(tmp_path, completion):
    store, conn, db, slack = fixture(tmp_path)
    first = status(store, "first", "investigate")
    message(conn, "user", "investigate", "turn")
    poller = Poller(store, db, slack, settle_seconds=0)
    poller.tick()
    second = status(store, "second", "related incident", cursor=db.cursor("s"))
    message(conn, "user", "related incident", "turn")
    final = "Created the ticket. [Stack](https://example.com/pr/1).\n\nWaiting for approval."
    message(
        conn,
        "assistant",
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": final}]},
            }
        ),
        "turn",
    )
    poller.tick()
    assert store.status(first)["state"] == "superseded"
    assert Turn.load(store.status(second)["tracking"]).content == final
    message(
        conn,
        "assistant",
        json.dumps(
            {
                "type": "result",
                "conductor_sdk_metadata": {"model": "gpt-6-astra"},
                **completion,
            }
        ),
        "turn",
    )
    Poller(Store(store.path), db, slack, settle_seconds=0).reconcile()
    assert store.status(second)["state"] == "complete"
    assert store.status(second)["result"] == final
    assert slack.edits[-1][0] == f"status-{second}"
    assert final in slack.edits[-1][1]
    conn.close()


def test_explicit_result_wins_and_empty_turn_still_has_fallback():
    turn = Turn(content="Earlier commentary")
    turn.consume(
        {
            "role": "assistant",
            "seq": 1,
            "content": json.dumps(
                {
                    "type": "result",
                    "result": "Final answer",
                }
            ),
        }
    )
    assert turn.content == "Final answer"
    empty = Turn()
    empty.consume({"role": "assistant", "seq": 1, "content": '{"type":"result"}'})
    assert empty.content == "Agent finished without a text reply."


def test_callback_on_newer_turn_updates_job_owner_without_overwriting_queued_reply(tmp_path):
    store, conn, db, slack = fixture(tmp_path)
    first = status(store, "first", "build")
    message(conn, "user", "build", "turn")
    event(conn, "task_started", task_id="build")
    result(conn, "Waiting for build.", "turn")
    poller = Poller(store, db, slack, settle_seconds=0)
    poller.tick()
    second = status(store, "second", "queued", cursor=db.cursor("s"), send_mode="queue")
    message(conn, "user", "queued", "newer")
    result(conn, "Queued request answer.", "newer")
    conn.execute("UPDATE sessions SET status='working'")
    conn.commit()
    message(
        conn,
        "assistant",
        json.dumps(
            {
                "type": "system",
                "subtype": "task_notification",
                "task_id": "build",
                "status": "completed",
            }
        ),
        "newer",
    )
    poller.tick()
    assert Turn.load(store.status(first)["tracking"]).awaiting_callback
    assert Turn.load(store.status(second)["tracking"]).content == "Queued request answer."
    # Routing survives a restart between notification and the resumed answer.
    poller = Poller(Store(store.path), db, slack, settle_seconds=0)
    poller.reconcile()
    result(conn, "Build verified.", "newer")
    message(conn, "assistant", json.dumps({"type": "system", "subtype": "init"}), "newer")
    result(conn, "Build verified; no work remaining.", "newer")
    conn.execute("UPDATE sessions SET status='idle'")
    conn.commit()
    poller.tick()
    assert store.status(first)["state"] == store.status(second)["state"] == "complete"
    assert store.status(first)["result"] == "Build verified; no work remaining."
    assert store.status(second)["result"] == "Queued request answer."
    conn.close()
