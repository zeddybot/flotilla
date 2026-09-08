import json

from test_bridge import fixture, message, result, status

from slack_conductor.lifecycle import Turn
from slack_conductor.poller import Poller
from slack_conductor.store import Store


def event(conn, subtype, **values):
    message(conn, "assistant", json.dumps({"type": "system", "subtype": subtype, **values}), "turn")


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


def test_callback_on_newer_turn_updates_job_owner_without_overwriting_queued_reply(tmp_path):
    store, conn, db, slack = fixture(tmp_path)
    first = status(store, "first", "build")
    message(conn, "user", "build", "turn")
    event(conn, "task_started", task_id="build")
    result(conn, "Waiting for build.", "turn")
    poller = Poller(store, db, slack, settle_seconds=0)
    poller.tick()
    second = status(store, "second", "queued", cursor=db.cursor("s"))
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
