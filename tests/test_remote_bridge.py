"""Slack admission and thread routing through the real private transport."""

import threading
import time
from types import SimpleNamespace

import pytest

from slack_conductor.config import Config
from slack_conductor.notifications import Notifications
from slack_conductor.poller import Poller
from slack_conductor.remote import RemoteConductor
from slack_conductor.remote_client import run_client
from slack_conductor.slack_app import Bridge
from slack_conductor.store import Store
from tests.support import Slack, message, result, status
from tests.support import event as native_event

TOKEN = "secret-token-with-at-least-thirty-two-chars"


def eventually(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("remote client did not become ready")


class NativeDB:
    def cursor(self, sid):
        return 0

    def pickup(self, sid, prompt):
        return None

    def pending(self, sid, prompt):
        return False

    def workspace(self, wid):
        return {"id": wid, "state": "ready", "workspace_path": "/tmp"}

    def session(self, sid):
        return {"id": sid, "status": "running"}

    def messages(self, sid, cursor):
        return []


class Native:
    def __init__(self, db=None):
        self.db = db or NativeDB()
        self.transport = SimpleNamespace(online=lambda: True, bindings={})
        self.creates = 0
        self.sends = []

    def start(self, bindings):
        self.transport.bindings.update(bindings)

    def close(self):
        pass

    def ready(self):
        return True

    def new_workspace(self, key=None):
        self.creates += 1
        return {"id": f"w{self.creates}", "active_session_id": f"s{self.creates}"}

    def validate(self, wid, sid=None):
        return {"id": wid, "active_session_id": sid}

    def send(self, wid, sid, prompt, options, **kwargs):
        self.sends.append((wid, sid, prompt, options))
        return "queued"


def event(ts, text, *, thread_ts=None):
    body = {
        "type": "app_mention",
        "channel": "C1",
        "ts": ts,
        "user": "U1",
        "text": text,
    }
    if thread_ts:
        body["thread_ts"] = thread_ts
    return {"team_id": "T1", "event": body}


def test_offline_prompt_is_saved_once_then_replayed_and_followups_keep_modes(tmp_path):
    server_state, client_state = tmp_path / "server", tmp_path / "client"
    server_state.mkdir()
    client_state.mkdir()
    store = Store(server_state / "bridge.sqlite")
    config = Config(state_dir=server_state, server_host="127.0.0.1", server_port=0)
    remote = RemoteConductor(config, TOKEN)
    remote.start()
    port = remote.server.socket.getsockname()[1]
    native = Native()
    stop = threading.Event()
    client = threading.Thread(
        target=run_client,
        args=(
            Config(state_dir=client_state, server_url=f"ws://127.0.0.1:{port}/client"),
            TOKEN,
            stop,
        ),
        kwargs={"conductor": native},
        daemon=True,
    )
    slack = Slack()
    slack.call = lambda *args, **kwargs: {"permalink": "https://slack.test/prompt"}
    bridge = Bridge(config, store, remote, slack, "B", "U1", "T1")
    try:
        initial = event("1", "<@B> first task")
        bridge.receive(initial)
        bridge.receive(initial)  # Slack's overlapping delivery remains one request.
        assert len(store.rows("SELECT * FROM events")) == 1
        bridge.process(store.next_event())
        pending = store.rows("SELECT state FROM events")[0]
        status = store.rows("SELECT * FROM statuses")[0]
        assert pending["state"] == "pending"
        assert status["state"] == "preparing" and status["status_ts"]
        assert native.creates == 0 and not native.sends
        assert len(slack.posts) == 1

        client.start()
        eventually(remote.ready)
        store.execute("UPDATE events SET retry_at=0")
        bridge.process(store.next_event())
        assert store.rows("SELECT state FROM events")[0]["state"] == "done"
        assert len(store.rows("SELECT * FROM statuses")) == 1
        assert len(slack.posts) == 1
        assert native.creates == 1 and len(native.sends) == 1
        assert native.sends[0][:2] == ("w1", "s1")

        for ts, text in [("2", "<@B> [queue] next"), ("3", "<@B> [steer] redirect")]:
            bridge.receive(event(ts, text, thread_ts="1"))
            bridge.process(store.next_event())
        assert [send[3]["send_mode"] for send in native.sends] == ["steer", "queue", "steer"]
        assert {send[:2] for send in native.sends} == {("w1", "s1")}

        bridge.receive(event("4", "<@B> [new] different task", thread_ts="1"))
        bridge.process(store.next_event())
        assert native.creates == 2
        assert native.sends[-1][:2] == ("w2", "s2")
        assert store.thread("C1", "1")["session_id"] == "s2"
        # The older waiting reply must remain observable after [new] changes the thread.
        assert store.session_bindings() == {"s1": "w1", "s2": "w2"}
        assert remote.db.session("s1")["id"] == "s1"
    finally:
        stop.set()
        remote.close()
        if client.ident is not None:
            client.join(timeout=5)
            assert not client.is_alive()


@pytest.fixture
def remote_observations(bridge_state, tmp_path):
    store, conn, db, slack = bridge_state
    server_state, client_state = tmp_path / "remote_server", tmp_path / "remote_client"
    server_state.mkdir()
    client_state.mkdir()
    server = RemoteConductor(Config(state_dir=server_state, server_port=0), TOKEN)
    server.start({"s": "w"})
    port = server.server.socket.getsockname()[1]
    native = Native(db)
    stop = threading.Event()
    client = threading.Thread(
        target=run_client,
        args=(
            Config(state_dir=client_state, server_url=f"ws://127.0.0.1:{port}/client"),
            TOKEN,
            stop,
        ),
        kwargs={"conductor": native},
        daemon=True,
    )
    client.start()
    eventually(server.ready)
    try:
        yield store, conn, db, slack, server, native
    finally:
        stop.set()
        server.close()
        client.join(timeout=5)
        assert not client.is_alive()


def notifications(store, slack):
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


def test_remote_poller_waits_for_background_callback_then_notifies(remote_observations):
    store, conn, _db, slack, remote, _native = remote_observations
    notices, sent = notifications(store, slack)
    sid = status(store, "background", "watch")
    message(conn, "user", "watch", "turn")
    native_event(conn, "task_started", task_id="build")
    result(conn, "Waiting for build.", "turn")
    poller = Poller(store, remote.db, slack, notifications=notices, settle_seconds=0)
    poller.tick()
    assert store.status(sid)["state"] == "active"
    assert "Waiting for build." in store.status(sid)["tracking"]
    assert not sent

    native_event(conn, "task_notification", task_id="build", status="completed", turn="callback")
    poller.tick()
    assert store.status(sid)["state"] == "active"
    assert not sent
    result(conn, "Build complete.", "callback")
    poller.tick()
    assert store.status(sid)["state"] == "complete"
    assert store.status(sid)["result"] == "Build complete."
    assert len(sent) == 1
    poller.tick()
    assert len(sent) == 1


def test_remote_poller_preserves_content_offline_and_steer_handoff(remote_observations):
    store, conn, db, slack, remote, _native = remote_observations
    notices, sent = notifications(store, slack)
    first = status(store, "first", "watch")
    message(conn, "user", "watch", "turn")
    native_event(conn, "task_started", task_id="build")
    result(conn, "Waiting for build.", "turn")
    poller = Poller(store, remote.db, slack, notifications=notices, settle_seconds=0)
    poller.tick()
    assert store.status(first)["state"] == "active"
    port = remote.server.socket.getsockname()[1]
    remote.close()
    poller.tick()
    assert store.status(first)["state"] == "active"
    assert "Waiting for build." in slack.edits[-1][1]
    assert "reconnect" in slack.edits[-1][1].lower()
    successor = RemoteConductor(Config(state_dir=remote.config.state_dir, server_port=port), TOKEN)
    try:
        successor.start({"s": "w"})
        eventually(successor.ready)
        second = status(store, "second", "stop", cursor=db.cursor("s"))
        message(conn, "user", "stop", "stop")
        native_event(conn, "task_notification", task_id="build", status="stopped", turn="stop")
        result(conn, "Stopped.", "stop")
        Poller(store, successor.db, slack, notifications=notices, settle_seconds=0).tick()
        assert store.status(first)["state"] == "superseded"
        assert store.status(second)["state"] == "complete"
        assert len(sent) == 1 and f"status-{second}" in sent[0]["text"]
    finally:
        successor.close()
