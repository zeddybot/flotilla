import json
from types import SimpleNamespace

import pytest

from slack_conductor import slack_app
from slack_conductor.config import Config
from slack_conductor.notifications import Notifications
from slack_conductor.poller import Poller
from slack_conductor.slack_app import Bridge
from slack_conductor.store import Store
from tests.support import event, message, result, status


def attach(store, slack):
    store.accept("dm", {"user": "U1", "channel": "D1", "channel_type": "im"})
    sent = []

    def call(method, **kwargs):
        if method == "chat_getPermalink":
            return {"permalink": f"https://slack.test/{kwargs['message_ts']}"}
        if method == "chat_postMessage":
            sent.append(kwargs)
            return {"channel": "D1", "ts": f"dm-{len(sent)}"}
        raise AssertionError(method)

    slack.call = call
    return Notifications(store, slack, "U1"), sent


def test_background_steer_notifies_only_replacement_and_queue_notifies_separately(bridge_state):
    store, conn, db, slack = bridge_state
    notices, sent = attach(store, slack)
    first = status(store, "first", "watch")
    message(conn, "user", "watch", "turn")
    event(conn, "task_started", task_id="build")
    result(conn, "Waiting for build.", "turn")
    poller = Poller(store, db, slack, notifications=notices, settle_seconds=0)
    poller.tick()
    assert not sent
    second = status(store, "second", "stop", cursor=db.cursor("s"))
    message(conn, "user", "stop", "stop")
    event(conn, "task_notification", task_id="build", status="stopped", turn="stop")
    result(conn, "Stopped.", "stop")
    poller.tick()
    assert store.status(first)["state"] == "superseded"
    assert len(sent) == 1 and f"status-{second}" in sent[0]["text"]
    third = status(store, "third", "next", send_mode="queue")
    message(conn, "user", "next", "next")
    result(conn, "Next done.", "next")
    poller.tick()
    assert len(sent) == 2 and f"status-{third}" in sent[-1]["text"]
    Poller(Store(store.path), db, slack, notifications=notices).reconcile()
    assert len(sent) == 2


@pytest.mark.parametrize(
    "native", ["needs_user_input", "needs_user_permission", "needs_plan_response"]
)
def test_blocked_agent_notifies_once_then_failure_notifies(bridge_state, native):
    store, conn, db, slack = bridge_state
    notices, sent = attach(store, slack)
    status(store, "request", "task")
    message(conn, "user", "task", "turn")
    conn.execute("UPDATE sessions SET status=?", (native,))
    conn.commit()
    poller = Poller(store, db, slack, notifications=notices, settle_seconds=0)
    poller.tick()
    poller.reconcile()
    assert len(sent) == 1 and "Needs attention" in sent[0]["text"]
    conn.execute("UPDATE sessions SET status='idle'")
    conn.commit()
    message(
        conn,
        "assistant",
        json.dumps({"type": "result", "result": "failed", "is_error": True}),
        "turn",
    )
    poller.tick()
    assert len(sent) == 2 and "Failed" in sent[-1]["text"]


def test_notification_reply_redirects_without_creating_agent(bridge_state):
    store, _, db, slack = bridge_state
    notices, sent = attach(store, slack)
    sid = status(store, "request", "task")
    store.update(sid, state="complete")
    notices.observe(store.status(sid), "done")
    notices.tick()
    bridge = Bridge(Config(), store, SimpleNamespace(db=db), slack, "B", "U1", "T1")
    event_body = {
        "type": "message",
        "channel_type": "im",
        "channel": "D1",
        "thread_ts": "dm-1",
        "ts": "reply",
        "user": "U1",
        "text": "continue",
    }
    store.accept("reply", event_body)
    bridge.process(store.rows("SELECT * FROM events WHERE key='reply'")[0])
    assert len(store.rows("SELECT * FROM statuses")) == 1
    assert store.thread("D1", "dm-1") is None
    assert "original thread" in slack.posts[-1][2]
    assert len(sent) == 1


def test_only_our_bot_echo_confirms_ambiguous_notification(bridge_state):
    store, _, db, slack = bridge_state
    notices, sent = attach(store, slack)
    sid = status(store, "request", "task")
    notices.observe(store.status(sid), "attention")
    row = store.rows("SELECT * FROM notifications")[0]
    notices.save(row, state="sending", attempted_at=100)
    bridge = Bridge(Config(), store, SimpleNamespace(db=db), slack, "B", "U1", "T1")
    echo = {
        "type": "message",
        "user": "B",
        "channel": "D1",
        "ts": "dm-1",
        "blocks": [{"block_id": f"notice:{row['key']}"}],
    }
    bridge.receive({"team_id": "T2", "event": echo})
    bridge.receive({"team_id": "T1", "event": echo | {"user": "OTHER"}})
    assert store.rows("SELECT state FROM notifications")[0]["state"] == "sending"
    bridge.receive({"team_id": "T1", "event": echo})
    assert notices.reply_target("D1", "dm-1")["state"] == "sent"
    assert not sent


def test_notification_waits_until_all_final_pages_delivered(bridge_state):
    store, _, db, slack = bridge_state
    notices, sent = attach(store, slack)
    sid = status(store, "request", "task")
    poller = Poller(store, db, slack, notifications=notices)
    post = slack.post
    slack.post = lambda *a, **kw: (_ for _ in ()).throw(TimeoutError())
    with pytest.raises(TimeoutError):
        poller.finish(store.status(sid), "long response " * 900)
    notices.tick()
    assert not sent
    slack.post = post
    poller.tick()
    assert store.status(sid)["state"] == "complete" and len(sent) == 1


def test_startup_delivers_pending_dm_without_dispatching_agent_work(bridge_state, monkeypatch):
    store, _, db, slack = bridge_state
    notices, sent = attach(store, slack)
    sid = status(store, "request", "task")
    store.update(sid, state="complete")
    notices.observe(store.status(sid), "done")
    client = SimpleNamespace(
        auth_test=lambda: {"team_id": "T1", "user_id": "B", "team": "test"},
        users_info=lambda **_: {"user": {}},
    )
    app = SimpleNamespace(client=client, event=lambda _: lambda f: f)
    monkeypatch.setattr(slack_app, "App", lambda **_: app)
    monkeypatch.setattr(slack_app, "SlackIO", lambda _: slack)
    monkeypatch.setattr(
        slack_app,
        "SocketModeHandler",
        lambda *_: SimpleNamespace(
            client=None,
            connect=lambda: None,
            close=lambda: None,
        ),
    )
    monkeypatch.setattr(
        slack_app,
        "ConnectionHealth",
        lambda *_: SimpleNamespace(
            start=lambda: None,
            close=lambda: None,
            failed=False,
        ),
    )
    slack_app.run(
        Config(),
        store,
        SimpleNamespace(db=db, start=lambda _: None, close=lambda: None),
        {
            "SLACK_BOT_TOKEN": "test",
            "SLACK_APP_TOKEN": "test",
            "SLACK_TEAM_ID": "T1",
            "OWNER_SLACK_USER_ID": "U1",
        },
        SimpleNamespace(is_set=lambda: True),
    )
    assert len(sent) == 1
