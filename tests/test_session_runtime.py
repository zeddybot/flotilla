"""Contract checks for the current native adapter behind generic runtime calls."""

import json
import threading
import time
from types import SimpleNamespace

import pytest

from slack_conductor.conductor.session_runtime import ConductorRuntime
from slack_conductor.config import Config
from slack_conductor.lifecycle import Turn
from slack_conductor.remote import RemoteConductor
from slack_conductor.remote_client import Dispatcher, run_client
from slack_conductor.runtime import Unsupported, dispatch


class FakeDB:
    def __init__(self):
        self.rows = []

    def workspace(self, wid):
        return {"local_id": wid, "state": "ready", "workspace_path": "/tmp/native"}

    def session(self, sid):
        return {"id": sid, "status": "idle", "model": "model"}

    def messages(self, sid, after):
        return [row for row in self.rows if row["seq"] > after]

    def cursor(self, sid):
        return max((row["seq"] for row in self.rows), default=0)

    def pickup(self, sid, prompt):
        return None


class FakeNative:
    def __init__(self):
        self.db = FakeDB()
        self.transport = SimpleNamespace(bindings={}, online=lambda: True)
        self.creates = []
        self.sends = []
        self.archives = []

    def ready(self):
        return True

    def start(self, bindings):
        self.transport.bindings.update(bindings)

    def close(self):
        pass

    def new_workspace(self, key):
        self.creates.append(key)
        return {"id": "w1", "active_session_id": "s1"}

    def send(self, wid, sid, prompt, options, *, message_id=None):
        self.sends.append((wid, sid, prompt, options, message_id))
        return ""

    def archive(self, wid, sid):
        self.archives.append((wid, sid))


def _runtime(tmp_path, native=None):
    config = Config(state_dir=tmp_path)
    native = native or FakeNative()
    return config, native, ConductorRuntime(config, native)


def _owned(runtime):
    return runtime.create("create-1", {"repo": str(runtime.config.repo)})


def _row(seq, content):
    return {
        "id": f"event-{seq}",
        "seq": seq,
        "session_id": "s1",
        "turn_id": "native-turn",
        "role": "assistant",
        "content": json.dumps(content),
    }


def test_unknown_session_and_unknown_operation_are_rejected(tmp_path):
    _config, _native, runtime = _runtime(tmp_path)
    with pytest.raises(ValueError, match="not owned"):
        runtime.describe("foreign")
    with pytest.raises(ValueError, match="not owned"):
        runtime.observe("foreign")
    with pytest.raises(Unsupported, match="Unknown runtime operation"):
        dispatch(runtime, "native.send", {})
    with pytest.raises(ValueError, match="command_id"):
        dispatch(runtime, "create", {"spec": {}})


def test_restart_replays_create_and_submit_without_native_duplicates(tmp_path):
    config, native, runtime = _runtime(tmp_path)
    created = _owned(runtime)
    options = {"model": config.model, "effort": config.effort, "fast": False, "send_mode": "queue"}
    first = runtime.submit("send-1", "s1", "m1", "run-1", "hello", options)
    restarted = ConductorRuntime(config, native)
    assert restarted.create("create-1", {"repo": str(config.repo)}) == created
    assert restarted.submit("send-1", "s1", "m1", "run-1", "hello", options) == first
    assert native.creates == ["create-1"]
    assert len(native.sends) == 1
    assert first["message_id"] == "m1" and first["run_id"] == "run-1"
    assert first["state"] in {"received", "queued", "picked_up"}
    with pytest.raises(ValueError, match="different arguments"):
        restarted.submit("send-1", "s1", "m1", "run-1", "changed", options)
    with pytest.raises(ValueError, match="different arguments"):
        restarted.create("create-1", {"repo": str(config.repo), "model": "changed"})


def test_observation_normalizes_events_without_raw_or_hidden_content(tmp_path):
    _config, native, runtime = _runtime(tmp_path)
    _owned(runtime)
    native.db.rows = [
        _row(
            1,
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "thinking", "thinking": "secret"},
                        {"type": "text", "text": "Visible"},
                    ]
                },
            },
        ),
        _row(2, {"type": "result", "result": "nested", "parent_tool_use_id": "parent"}),
        _row(
            3,
            {
                "type": "result",
                "result": "foreign",
                "conductor_sdk_metadata": {"isForeignResult": True},
            },
        ),
        _row(
            4,
            {
                "type": "system",
                "subtype": "task_started",
                "task_id": "job",
                "description": "deploy",
            },
        ),
        _row(
            5,
            {
                "type": "system",
                "subtype": "task_notification",
                "task_id": "job",
                "status": "completed",
            },
        ),
        _row(6, {"type": "result", "result": "Done"}),
    ]
    observed = runtime.observe("s1", after=0)
    assert observed["cursor"] == 6
    events = observed["events"]
    assert all("content" not in event for event in events)
    assert "secret" not in json.dumps(events)
    assert "text" not in events[1]["event"]
    assert "text" not in events[2]["event"]
    turn = Turn()
    for event in events:
        turn.consume(event)
    assert turn.content == "Done"
    assert turn.result_seq == 6
    assert turn.ended == ["job"]
    assert "nested" not in turn.content and "foreign" not in turn.content
    assert events[0]["event"]["parts"][0] == {"kind": "activity", "phase": "thinking"}


def test_distinct_messages_can_repeat_prompt_without_collapsing_identity(tmp_path):
    _config, native, runtime = _runtime(tmp_path)
    _owned(runtime)
    runtime.submit("command-a", "s1", "message-a", "run-a", "same prompt", {"send_mode": "queue"})
    runtime.submit("command-b", "s1", "message-b", "run-b", "same prompt", {"send_mode": "queue"})
    assert [call[-1] for call in native.sends] == ["message-a", "message-b"]


def test_configuration_is_fixed_and_archive_replays(tmp_path):
    _config, native, runtime = _runtime(tmp_path)
    runtime.create("create", {"model": "chosen", "effort": "high", "fast": True})
    receipt = runtime.submit(
        "send", "s1", "m1", "r1", "hello", {"model": "other", "effort": "low", "send_mode": "queue"}
    )
    assert native.sends[0][3]["model"] == "chosen"
    assert native.sends[0][3]["effort"] == "high"
    assert "use [new]" in receipt["warning"]
    assert runtime.close_session("close", "s1") == runtime.close_session("close", "s1")
    assert native.archives == [("w1", "s1")]


def test_dispatcher_accepts_runtime_wire_only(tmp_path):
    config, native, runtime = _runtime(tmp_path)
    dispatcher = Dispatcher(config, native, runtime=runtime)
    response = dispatcher.execute(
        {
            "method": "runtime.create",
            "key": "wire-create",
            "args": {"command_id": "wire-create", "spec": {}},
        }
    )
    assert response["value"]["session_id"] == "s1"
    assert dispatcher.execute({"method": "native.send", "args": {}}) == {
        "error": "Unknown remote operation"
    }
    assert dispatcher.execute({"method": "db.query", "args": {}}) == {
        "error": "Unknown remote operation"
    }


def test_real_transport_uses_runtime_methods_and_sends_normalized_events(tmp_path):
    server_state, client_state = tmp_path / "server", tmp_path / "client"
    server_state.mkdir()
    client_state.mkdir()
    token = "a-private-token-with-at-least-thirty-two-characters"
    server = RemoteConductor(Config(state_dir=server_state, server_port=0), token)
    server.start()
    port = server.server.socket.getsockname()[1]
    native = FakeNative()
    native.db.rows = [_row(1, {"type": "result", "result": "Visible", "private": "raw-secret"})]
    config = Config(state_dir=client_state, server_url=f"ws://127.0.0.1:{port}/client")
    stop = threading.Event()
    thread = threading.Thread(
        target=run_client, args=(config, token, stop), kwargs={"conductor": native}, daemon=True
    )
    methods = []
    original_rpc = server.rpc

    def recording_rpc(method, *args, **kwargs):
        methods.append(method)
        return original_rpc(method, *args, **kwargs)

    server.rpc = recording_rpc
    try:
        thread.start()
        deadline = time.monotonic() + 3
        while not server.ready() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.ready()
        assert server.new_workspace(key="new-1")["id"] == "w1"
        assert server.send("w1", "s1", "hello", {"send_mode": "steer"}) == ""
        events = server.db.messages("s1", 0)
        assert events[0]["event"]["text"] == "Visible"
        assert "content" not in events[0]
        assert "raw-secret" not in json.dumps(events)
        assert methods and all(method.startswith("runtime.") for method in methods)
    finally:
        stop.set()
        server.close()
        thread.join(timeout=3)
        assert not thread.is_alive()
