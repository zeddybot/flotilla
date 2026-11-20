"""Exercise shared Slack routing over loopback with both runtime adapters."""

import json
import threading
import time
from contextlib import ExitStack
from dataclasses import replace

from slack_bolt import App, BoltRequest
from slack_bolt.authorization import AuthorizeResult
from slack_sdk.signature import SignatureVerifier

from slack_conductor.cli import main
from slack_conductor.config import Config, remote_credentials
from slack_conductor.remote import RemoteConductor
from slack_conductor.remote_client import run_client
from slack_conductor.slack_app import Bridge
from slack_conductor.slack_routing import Routing
from slack_conductor.store import Store
from tests.support import Slack
from tests.test_remote_bridge import Native, NativeDB, eventually
from tests.test_worker_transport import _fake_engine, _repo

SIGNING_SECRET = "synthetic-slack-signing-secret"
ADMIN_TOKEN = "synthetic-admin-token-with-at-least-32-chars"


class CompletingNative(Native):
    def __init__(self):
        super().__init__(CompletingDB())

    def send(self, wid, sid, prompt, options, **kwargs):
        result = super().send(wid, sid, prompt, options, **kwargs)
        self.db.append_turn(sid, prompt)
        return result


class CompletingDB(NativeDB):
    def __init__(self):
        self.rows = {}

    def session(self, sid):
        return {"id": sid, "status": "idle"}

    def cursor(self, sid):
        return len(self.rows.get(sid, []))

    def messages(self, sid, cursor):
        return self.rows.get(sid, [])[cursor:]

    def pickup(self, sid, prompt):
        return next((row for row in self.rows.get(sid, []) if row["content"] == prompt), None)

    def has_later_turn(self, sid, after, own_turn):
        return any(
            row["role"] == "user" and row["turn_id"] != own_turn
            for row in self.messages(sid, after)
        )

    def append_turn(self, sid, prompt):
        rows = self.rows.setdefault(sid, [])
        turn = f"turn-{len(rows)}"
        rows.extend(
            {
                "seq": index,
                "session_id": sid,
                "turn_id": turn,
                "role": role,
                "content": content,
            }
            for index, (role, content) in enumerate(
                [
                    ("user", prompt),
                    (
                        "assistant",
                        json.dumps({"type": "result", "result": "conductor answer"}),
                    ),
                ],
                start=len(rows) + 1,
            )
        )


def slack_events(router):
    def authorize():
        return AuthorizeResult(
            enterprise_id=None, team_id="T1", bot_token="xoxb-synthetic", bot_user_id="B"
        )

    app = App(
        signing_secret=SIGNING_SECRET,
        authorize=authorize,
        process_before_response=True,
        token_verification_enabled=False,
    )

    @app.event("message")
    @app.event("app_mention")
    def on_event(body):
        router.receive(body)

    verifier = SignatureVerifier(SIGNING_SECRET)

    def dispatch(user, timestamp, text, *, dm=False, thread=None, signed=True):
        event = {
            "type": "message" if dm else "app_mention",
            "channel_type": "im" if dm else "channel",
            "channel": f"D{user}" if dm else "C1",
            "user": user,
            "ts": timestamp,
            "text": text if dm else f"<@B> {text}",
        }
        if thread:
            event["thread_ts"] = thread
        now = str(int(time.time()))
        body = json.dumps({"type": "event_callback", "team_id": "T1", "event": event})
        response = app.dispatch(
            BoltRequest(
                body=body,
                headers={
                    "content-type": "application/json",
                    "x-slack-request-timestamp": now,
                    "x-slack-signature": verifier.generate_signature(timestamp=now, body=body)
                    if signed
                    else "invalid",
                },
            )
        )
        assert response.status == (200 if signed else 401), response.body

    return dispatch


def enroll(root, repo, url, adapter, user, dispatch, capsys):
    arguments = ["--json", "--root", str(root), "client", "connect"]
    assert (
        main(
            arguments
            + [url, "--profile", "desk", "--runtime-adapter", adapter, "--repo", str(repo)]
        )
        == 0
    )
    reply = json.loads(capsys.readouterr().out)["data"]
    dispatch(user, "100", reply["slack_dm"], dm=True)
    assert main(arguments + ["--check"]) == 0
    confirmed = json.loads(capsys.readouterr().out)["data"]
    config = Config.load(root)
    assert config.client_id == confirmed["profile_id"]
    return config


def start_client(resources, config, **kwargs):
    stop = threading.Event()
    client = threading.Thread(
        target=run_client,
        args=(config, remote_credentials(config), stop),
        kwargs=kwargs,
        daemon=True,
    )
    client.start()

    def shutdown():
        stop.set()
        client.join(timeout=5)
        assert not client.is_alive()

    resources.callback(shutdown)


def process(bridge):
    pending = bridge.store.next_event()
    assert pending is not None
    bridge.process(pending)


def complete(bridge, key):
    def tick():
        bridge.poller.tick()
        (status,) = bridge.store.rows("SELECT * FROM statuses WHERE event_key=?", (key,))
        return status["state"] == "complete"

    bridge.poller.settle_seconds = 0
    eventually(tick)


def test_two_enrolled_users_keep_thread_routing_and_retries_isolated(tmp_path, capsys, monkeypatch):
    repo = _repo(tmp_path)
    engine, calls = _fake_engine(tmp_path)
    monkeypatch.setenv("WORKER_ENGINE_CALLS", str(calls))
    native = CompletingNative()
    server_state = tmp_path / "server"
    server_state.mkdir()
    config = Config(
        mode="server", state_dir=server_state, server_port=0, pairing_allowed_users=["U2"]
    )
    remote = RemoteConductor(config, ADMIN_TOKEN)
    store = Store(server_state / "bridge.sqlite")
    slack = Slack()
    notices = []

    def slack_call(method, **kwargs):
        if method == "chat_getPermalink":
            return {"permalink": f"https://slack.test/{kwargs['message_ts']}"}
        if method == "chat_postMessage":
            notices.append(kwargs)
            return {"channel": f"D{kwargs['channel']}", "ts": str(len(notices))}
        raise AssertionError(method)

    slack.call = slack_call
    routing = Routing(Bridge(config, store, remote, slack, "B", "U1", "T1", routed=True))
    dispatch = slack_events(routing)
    remote.start()
    with ExitStack() as resources:
        # Close the server after both client workers have been stopped.
        resources.callback(remote.close)
        port = remote.server.socket.getsockname()[1]
        url = f"ws://127.0.0.1:{port}"
        dispatch("U1", "90", "profiles", dm=True, signed=False)
        assert not slack.posts
        mac = enroll(tmp_path / "mac", repo, url, "conductor", "U1", dispatch, capsys)
        worker = enroll(tmp_path / "worker", repo, url, "headless", "U2", dispatch, capsys)
        worker = replace(worker, claude_binary=str(engine))
        mac_bridge = routing.to_bridge(mac.client_id)
        worker_bridge = routing.to_bridge(worker.client_id)
        start_client(resources, worker)
        eventually(worker_bridge.conductor.ready)

        dispatch("U1", "1", "[profile:desk] first summoner")
        dispatch("U1", "1", "[profile:desk] first summoner")
        process(mac_bridge)
        assert mac_bridge.store.rows("SELECT state FROM events") == [{"state": "pending"}]
        assert native.sends == []
        assert store.route("T1", "C1", "1") == {"owner_id": "U1", "client_id": mac.client_id}

        dispatch("U2", "2", "[profile:desk] independent worker")
        process(worker_bridge)
        complete(worker_bridge, "T1:C1:2")
        assert worker_bridge.store.rows("SELECT state FROM events") == [{"state": "done"}]
        assert store.route("T1", "C1", "2") == {"owner_id": "U2", "client_id": worker.client_id}

        start_client(resources, mac, conductor=native)
        eventually(mac_bridge.conductor.ready)
        mac_bridge.store.execute("UPDATE events SET retry_at=0")
        process(mac_bridge)
        complete(mac_bridge, "T1:C1:1")

        dispatch("U2", "3", "collaborator continuation", thread="1")
        dispatch("U2", "3", "collaborator continuation", thread="1")
        process(mac_bridge)
        complete(mac_bridge, "T1:C1:3")
        dispatch("U2", "4", "[stop]", thread="1")
        process(mac_bridge)
        dispatch("U2", "5", "[profile:desk] steal route", thread="1")

        assert native.creates == 1
        assert len(native.sends) == 2
        assert native.sends[0][:2] == native.sends[1][:2]
        assert "collaborator continuation" in native.sends[1][2]
        assert native.sends[1][3]["send_mode"] == "queue"
        assert len(calls.read_text().splitlines()) == 1
        assert mac_bridge.store.next_event() is None and worker_bridge.store.next_event() is None
        assert [post[3] for post in slack.posts if post[3].startswith("status:")] == [
            "status:T1:C1:1",
            "status:T1:C1:2",
            "status:T1:C1:3",
        ]
        assert any("Only the thread owner" in post[2] for post in slack.posts)
        assert any("already linked" in post[2] for post in slack.posts)
        assert sum("headless answer" in text for _, text in slack.edits) == 1
        assert sum("conductor answer" in text for _, text in slack.edits) == 2
        assert [notice["channel"] for notice in notices] == ["U2", "U1", "U2"]
        assert mac_bridge.notifications.reply_target("DU2", "3", actor="U1") is None
        dispatch("U2", "101", "how do I follow up?", dm=True, thread="3")
        process(mac_bridge)
        assert "original thread" in slack.posts[-1][2]
        assert store.route("T1", "DU2", "3") is None
        assert len(native.sends) == 2 and len(calls.read_text().splitlines()) == 1
