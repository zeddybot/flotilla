from types import SimpleNamespace

from slack_conductor.conductor.journal import Unconfirmed
from slack_conductor.config import Config
from slack_conductor.lifecycle import Turn
from slack_conductor.poller import Poller
from slack_conductor.slack_app import Bridge
from tests.support import fixture, message, result, status


def runtime(epoch, state, tasks=()):
    return {
        "incarnation": epoch,
        "live": {"status": state, "backgroundTasks": [{"id": t} for t in tasks]},
    }


def test_helper_restart_retains_background_tasks_and_old_result():
    turn = Turn(tasks={"job": "deploy"}, result_seq=10, runtime_epoch="app")
    turn = Turn.load(turn.dump())
    turn.observe_runtime(runtime("app", "idle", ["job"]), 100)
    assert turn.tasks == {"job": "deploy"} and turn.result_seq == 10
    assert not turn.interrupted


def test_app_restart_clears_killed_tasks_but_waits_for_running_agent(tmp_path):
    store, conn, db, slack = fixture(tmp_path)
    sid = status(store, "crash", "watch deploy")
    message(conn, "user", "watch deploy", "turn")
    turn = Turn(
        tasks={"job": "deploy"}, content="Waiting on deploy", result_seq=2, runtime_epoch="old"
    )
    store.update(sid, state="active", turn_id="turn", tracking=turn.dump(), cursor=1)
    db.session = lambda _: {"status": "working", "runtime": runtime("new", "working")}
    now = [100]
    poller = Poller(store, db, slack, clock=lambda: now[0], settle_seconds=2)
    poller.tick()
    recovered = Turn.load(store.status(sid)["tracking"])
    assert not recovered.tasks and recovered.interrupted
    assert store.status(sid)["state"] == "active"
    assert "Working" in slack.edits[-1][1] and "Waiting ·" not in slack.edits[-1][1]
    result(conn, "The job was interrupted; I have stopped watching.", "turn")
    db.session = lambda _: {"status": "idle", "runtime": runtime("new", "idle")}
    poller.tick()
    now[0] += 3
    poller.tick()
    assert store.status(sid)["state"] == "complete"
    assert store.status(sid)["result"] == "The job was interrupted; I have stopped watching."
    assert "Done" in slack.edits[-1][1]


def test_killed_job_without_callback_settles_to_attention(tmp_path):
    store, _, db, slack = fixture(tmp_path)
    sid = status(store, "crash", "watch")
    store.update(
        sid,
        state="active",
        turn_id="t",
        tracking=Turn(tasks={"job": "watch"}, result_seq=1, runtime_epoch="old").dump(),
    )
    db.session = lambda _: {"status": "idle", "runtime": runtime("new", "unknown")}
    now = [100]
    poller = Poller(store, db, slack, clock=lambda: now[0], settle_seconds=2)
    poller.tick()
    now[0] += 3
    poller.tick()
    assert store.status(sid)["state"] == "complete"
    assert "Needs attention" in slack.edits[-1][1]
    assert "no longer running" in slack.edits[-1][1]


def test_missing_runtime_never_concludes_background_job_is_dead(tmp_path):
    store, _, db, slack = fixture(tmp_path)
    sid = status(store, "offline", "watch")
    store.update(
        sid,
        state="active",
        turn_id="t",
        tracking=Turn(tasks={"job": "watch"}, runtime_epoch="old").dump(),
    )
    db.session = lambda _: {"status": "reconnecting"}
    poller = Poller(store, db, slack, clock=lambda: 10000)
    poller.tick()
    assert store.status(sid)["state"] == "active"
    assert Turn.load(store.status(sid)["tracking"]).tasks
    assert "Reconnecting" in slack.edits[-1][1]


def test_creation_crash_reuses_slack_status_and_original_creation_intent(tmp_path):
    store, _, db, slack = fixture(tmp_path)
    attempts, sends = [], []

    def create(key):
        attempts.append(key)
        if len(attempts) == 1:
            raise Unconfirmed("crash after creation")
        return {"id": "w", "active_session_id": "s"}

    slack.call = lambda *a, **kw: {"permalink": "https://slack.test/message"}
    conductor = SimpleNamespace(
        db=db, new_workspace=create, send=lambda *args: sends.append(args) or ""
    )
    bridge = Bridge(Config(), store, conductor, slack, "B", "OWNER", "TEAM")
    event = {
        "type": "message",
        "channel_type": "im",
        "channel": "D",
        "ts": "1",
        "user": "OWNER",
        "text": "hello",
    }
    store.accept("event", event)
    bridge.process(store.next_event())
    assert store.rows("SELECT 1 FROM events WHERE state='pending'") and len(slack.posts) == 1
    store.execute("UPDATE events SET retry_at=0")
    bridge.poller.reconcile()
    bridge.process(store.next_event())
    assert attempts == ["event", "event"] and len(sends) == 1
    assert len(slack.posts) == 1
    assert len(store.rows("SELECT * FROM statuses")) == 1
    assert not store.next_event()


def test_restored_foreign_callback_does_not_hide_followup_result(tmp_path):
    import json

    store, conn, db, slack = fixture(tmp_path)
    sid = status(store, "resume", "stop watching")
    message(conn, "user", "stop watching", "new")
    for event in [
        {
            "type": "system",
            "subtype": "task_notification",
            "task_id": "old-job",
            "status": "stopped",
        },
        {
            "type": "result",
            "result": "old callback",
            "conductor_sdk_metadata": {"isForeignResult": True},
        },
        {"type": "system", "subtype": "init"},
        {
            "type": "result",
            "result": "resumed response",
            "conductor_sdk_metadata": {"isForeignResult": False},
        },
    ]:
        message(conn, "assistant", json.dumps(event), "new")
    poller = Poller(store, db, slack, settle_seconds=0)
    poller.tick()
    assert store.status(sid)["state"] == "complete"
    assert store.status(sid)["result"] == "resumed response"
    assert "old callback" not in slack.edits[-1][1]


def test_crashed_job_cannot_claim_queued_reply_after_foreign_callback(tmp_path):
    import json

    store, conn, db, slack = fixture(tmp_path)
    first = status(store, "job", "watch")
    message(conn, "user", "watch", "old")
    store.update(
        first,
        state="active",
        turn_id="old",
        cursor=1,
        tracking=Turn(
            tasks={"job": "watch"}, content="Watching", result_seq=1, runtime_epoch="before"
        ).dump(),
    )
    second = status(store, "queue", "next", send_mode="queue")
    message(conn, "user", "next", "new")
    for event in [
        {"type": "system", "subtype": "task_notification", "task_id": "job", "status": "stopped"},
        {"type": "system", "subtype": "init"},
        {"type": "result", "conductor_sdk_metadata": {"isForeignResult": True}},
        {"type": "system", "subtype": "init"},
        {"type": "result", "result": "queued answer"},
    ]:
        message(conn, "assistant", json.dumps(event), "new")
    db.session = lambda _: {"status": "idle", "runtime": runtime("after", "idle")}
    poller = Poller(store, db, slack, settle_seconds=0)
    poller.tick()
    assert store.status(first)["state"] == store.status(second)["state"] == "complete"
    assert "interrupted" in store.status(first)["result"]
    assert "queued answer" not in store.status(first)["result"]
    assert store.status(second)["result"] == "queued answer"
