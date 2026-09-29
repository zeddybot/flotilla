"""Exercise the private server to Mac wire without a Slack or Conductor process."""

import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from slack_conductor.conductor.journal import Unconfirmed
from slack_conductor.config import Config
from slack_conductor.remote import RemoteConductor
from slack_conductor.remote_client import Dispatcher, run_client

TOKEN = "secret-token-with-at-least-thirty-two-chars"


def eventually(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition did not become true")


class FakeDB:
    def session(self, sid):
        return {"id": sid, "status": "running"}

    def messages(self, sid, cursor):
        return [{"seq": cursor + 1, "content": f"message from {sid}"}]


class FakeConductor:
    def __init__(self):
        self.db = FakeDB()
        self.transport = SimpleNamespace(online=lambda: True, bindings={})
        self.created = self.sent = self.archived = 0
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

    def send(self, wid, sid, prompt, options):
        self.sent += 1
        self.send_started.set()
        if self.block_send:
            assert self.release_send.wait(5)
        return "queued"

    def archive(self, wid, sid):
        self.archived += 1
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
    with pytest.raises(RuntimeError, match="not a bound session"):
        server.db.session("unbound")
    assert native.created == 0


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
    assert server.archive("w1", "s1") is True
    assert server.archive("w1", "s1") is True
    assert native.archived == 1
    from slack_conductor.remote_journal import Receipts

    reopened = Receipts(config.state_dir / "remote.sqlite")
    assert reopened.begin("create:create-1", "native.new_workspace", {"key": "create-1"}) == {
        "value": {"id": "w1", "active_session_id": "s1"}
    }


def test_lost_ack_replay_does_not_repeat_native_send(tmp_path):
    state = tmp_path / "client"
    state.mkdir()
    config = Config(state_dir=state)
    native = FakeConductor()
    request = {
        "method": "native.send",
        "key": "send:fixed",
        "args": {"wid": "w1", "sid": "s1", "prompt": "hello", "options": {}},
    }
    first = Dispatcher(config, native).execute(request)
    assert first == {"value": "queued"}
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
