"""Exercise the private server to Mac wire without a Slack or Conductor process."""

import json
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from slack_conductor.cli_sessions import runtime_for_cli
from slack_conductor.conductor.attachment import Attachment
from slack_conductor.conductor.client import Client
from slack_conductor.conductor.journal import Unconfirmed
from slack_conductor.config import Config
from slack_conductor.remote import RemoteConductor
from slack_conductor.remote_client import Dispatcher, run_client
from slack_conductor.remote_peer import RemotePeer

TOKEN = "secret-token-with-at-least-thirty-two-chars"


def test_default_client_starts_native_daemon(tmp_path, monkeypatch):
    # Keep the real transport and attachment lifecycle without probing the desktop.
    monkeypatch.setattr(Attachment, "tick", lambda self, _: self.stop.wait())
    stop = threading.Event()
    stop.set()
    config = Config(mode="client", state_dir=tmp_path)

    run_client(config, TOKEN, stop)

    assert isinstance(runtime_for_cli(config).native.transport, Client)


def eventually(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition did not become true")


class FakeDB:
    def workspace(self, wid):
        return {"id": wid, "state": "ready"}

    def session(self, sid):
        return {"id": sid, "status": "running"}

    def messages(self, sid, cursor):
        return (
            [
                {
                    "seq": 1,
                    "session_id": sid,
                    "turn_id": "turn",
                    "role": "assistant",
                    "content": json.dumps({"type": "result", "result": f"message from {sid}"}),
                }
            ]
            if cursor < 1
            else []
        )

    def cursor(self, sid):
        return 1

    def pickup(self, sid, prompt):
        return None

    def pending(self, sid, prompt):
        return False


class FakeConductor:
    def __init__(self):
        self.db = FakeDB()
        self.transport = SimpleNamespace(online=lambda: True, bindings={})
        self.created = self.sent = self.archived = 0
        self.archive_commands = []
        self.archive_error = None
        self.closed = False
        self.send_started = threading.Event()
        self.release_send = threading.Event()
        self.block_send = False

    def start(self, bindings):
        self.transport.bindings.update(bindings)

    def close(self):
        self.closed = True

    def ready(self):
        return True

    def validate(self, wid, sid=None):
        return {"id": wid, "active_session_id": sid or "s1"}

    def new_workspace(self, key=None):
        self.created += 1
        return {"id": "w1", "active_session_id": "s1"}

    def send(self, wid, sid, prompt, options, *, message_id=None):
        self.sent += 1
        self.send_started.set()
        if self.block_send:
            assert self.release_send.wait(5)
        return "queued"

    def archive(self, wid, sid, *, command_id=None):
        self.archived += 1
        self.archive_commands.append(command_id)
        if self.archive_error:
            raise RuntimeError(self.archive_error)
        return True


@pytest.fixture
def remote_pair(tmp_path):
    server_state, client_state = tmp_path / "server", tmp_path / "client"
    server_state.mkdir()
    client_state.mkdir()
    server = RemoteConductor(
        Config(state_dir=server_state, server_host="127.0.0.1", server_port=0), TOKEN
    )
    server.start({"s1": "w1"})
    port = server.server.socket.getsockname()[1]
    client_config = Config(
        state_dir=client_state, server_url=f"ws://127.0.0.1:{port}/client", client_id="mac"
    )
    native = FakeConductor()
    stop = threading.Event()
    worker = threading.Thread(
        target=run_client,
        args=(client_config, TOKEN, stop),
        kwargs={"conductor": native},
        daemon=True,
    )
    try:
        yield server, client_config, native, stop, worker
    finally:
        stop.set()
        native.release_send.set()
        server.close()
        if worker.ident is not None:
            worker.join(timeout=5)
            assert not worker.is_alive()


def test_observations_are_bound_and_sql_is_not_exposed(remote_pair):
    server, _config, native, _stop, worker = remote_pair
    worker.start()
    eventually(server.ready)
    assert server.ready()
    assert server.db.session("s1")["status"] == "running"
    assert server.db.messages("s1", 0)[0]["seq"] == 1
    with pytest.raises(RuntimeError, match="Unknown remote operation"):
        server.rpc("db.query", {"args": ["SELECT * FROM repos"]})
    with pytest.raises(RuntimeError, match="not owned"):
        server.db.session("unbound")
    assert native.created == 0


def test_session_inventory_distinguishes_empty_runtime_from_unavailable_peer(tmp_path, monkeypatch):
    peer = RemotePeer(Config(state_dir=tmp_path))
    calls = []

    def rpc(method, args, **kwargs):
        calls.append((method, args, kwargs.get("key")))
        return []

    monkeypatch.setattr(peer, "rpc", rpc)
    assert peer.session_inventory() is None
    assert calls == []
    monkeypatch.setattr(peer, "ready", lambda: True)
    assert peer.session_inventory() == []
    assert calls == [("runtime.sessions", {}, None)]

    def reconnecting(*args, **kwargs):
        raise Unconfirmed("Runtime is reconnecting")

    monkeypatch.setattr(peer, "rpc", reconnecting)
    assert peer.session_inventory() is None


def test_durable_responses_survive_receipt_reopen(remote_pair):
    server, config, native, _stop, worker = remote_pair
    worker.start()
    eventually(server.ready)
    assert server.new_workspace(key="create-1")["id"] == "w1"
    assert server.new_workspace(key="create-1")["id"] == "w1"
    assert native.created == 1
    assert server.send("w1", "s1", "hello", {"send_mode": "queue"}) == "queued"
    assert server.send("w1", "s1", "hello", {"send_mode": "queue"}) == "queued"
    assert native.sent == 1
    assert server.archive("w1", "s1") == ""
    assert server.archive("w1", "s1") == ""
    assert native.archived == 1
    assert native.archive_commands == [None]
    from slack_conductor.remote_journal import Receipts

    reopened = Receipts(config.state_dir / "remote.sqlite")
    assert reopened.begin(
        "runtime:create:create-1", "runtime.create", {"command_id": "create-1", "spec": {}}
    ) == {"value": {"workspace_id": "w1", "session_id": "s1", "spec": {}}}


def test_new_archive_event_retries_a_journaled_failure(remote_pair):
    server, _config, native, _stop, worker = remote_pair
    worker.start()
    eventually(server.ready)
    server.new_workspace(key="create-1")
    native.archive_error = "native safe archive failed"
    for command_id in (None, "event-1"):
        with pytest.raises(RuntimeError, match="native safe archive failed"):
            server.archive("w1", "s1", command_id=command_id)
    native.archive_error = None
    for command_id in (None, "event-1"):
        with pytest.raises(RuntimeError, match="native safe archive failed"):
            server.archive("w1", "s1", command_id=command_id)
    assert server.archive("w1", "s1", command_id="event-2") == ""
    assert server.archive("w1", "s1", command_id="event-2") == ""
    assert native.archive_commands == [
        None,
        "runtime:close-event:event-1",
        "runtime:close-event:event-2",
    ]


def test_lost_ack_replay_does_not_repeat_native_send(tmp_path):
    state = tmp_path / "client"
    state.mkdir()
    config = Config(state_dir=state)
    native = FakeConductor()
    dispatcher = Dispatcher(config, native)
    assert dispatcher.execute(
        {
            "method": "runtime.recover",
            "args": {"workspace_id": "w1", "session_id": "s1"},
        }
    ) == {"value": {"workspace_id": "w1", "session_id": "s1"}}
    request = {
        "method": "runtime.submit",
        "key": "runtime:send:fixed",
        "args": {
            "command_id": "runtime:send:fixed",
            "session_id": "s1",
            "message_id": "message-fixed",
            "run_id": "run-fixed",
            "prompt": "hello",
            "options": {"send_mode": "steer"},
        },
    }
    first = dispatcher.execute(request)
    assert first["value"]["state"] == "queued"
    assert first["value"]["message_id"] == "message-fixed"
    assert Dispatcher(config, native).execute(request) == first
    assert native.sent == 1


def test_offline_observation_reports_reconnecting(remote_pair):
    server, _config, _native, _stop, _worker = remote_pair
    assert server.db.session("s1") == {"status": "reconnecting"}
    with pytest.raises(Unconfirmed):
        server.db.messages("s1", 0)


def test_missing_file_snapshot_is_reported_without_reconnecting(tmp_path):
    dispatcher = Dispatcher(Config(state_dir=tmp_path), FakeConductor())
    response = dispatcher.execute(
        {"method": "files.chunk", "args": {"key": "missing", "snapshot": "one", "offset": 0}}
    )
    assert response == {"error": "File snapshot is unavailable on the Mac"}


def test_server_restart_reconnects_and_restores_observations(remote_pair):
    server, _config, native, _stop, worker = remote_pair
    worker.start()
    eventually(server.ready)
    port = server.server.socket.getsockname()[1]
    server.close()
    assert server.db.session("s1") == {"status": "reconnecting"}
    successor = RemoteConductor(replace(server.config, server_port=port), TOKEN)
    try:
        successor.start({"s1": "w1"})
        eventually(successor.ready)
        assert successor.db.session("s1")["status"] == "running"
        assert native.created == 0
    finally:
        successor.close()


def test_heartbeat_continues_while_native_send_is_slow(remote_pair):
    server, _config, native, _stop, worker = remote_pair
    native.block_send = True
    worker.start()
    eventually(server.ready)
    result = []
    sender = threading.Thread(target=lambda: result.append(server.send("w1", "s1", "slow", {})))
    sender.start()
    assert native.send_started.wait(3)
    first_pulse = server.last_ready
    eventually(lambda: server.last_ready > first_pulse, timeout=3)
    assert server.ready()
    native.release_send.set()
    sender.join(timeout=5)
    assert result == ["queued"]
