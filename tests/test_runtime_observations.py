from slack_conductor.conductor.session_runtime import ConductorRuntime
from tests.test_session_runtime import _owned, _row, _runtime


def test_replay_retained_after_native_history_deleted(tmp_path):
    config, native, runtime = _runtime(tmp_path)
    _owned(runtime)
    native.db.rows = [_row(23, {"type": "result", "result": "durable answer"})]
    assert runtime.observe("s1")["events"][0]["seq"] == 23
    native.db.rows = []
    restarted = ConductorRuntime(config, native)
    assert restarted.observe("s1")["events"][0]["event"]["text"] == "durable answer"


def test_stale_snapshot_never_claims_rest(tmp_path):
    _, native, runtime = _runtime(tmp_path)
    _owned(runtime)
    native.db.rows = [_row(1, {"type": "result", "result": "done"})]
    snapshot = runtime.observe("s1")["snapshot"]
    assert snapshot["rest"]["confirmed"]
    native.ready = lambda: False
    stale = runtime.observe("s1")["snapshot"]
    assert stale["freshness"] == "stale"
    assert not stale["rest"]["confirmed"]
    assert stale["last_trusted_at"] is not None
    assert stale["configuration"] == snapshot["configuration"]


def test_delayed_steer_pickup_keeps_logical_run(tmp_path):
    _, native, runtime = _runtime(tmp_path)
    _owned(runtime)
    runtime.submit("a", "s1", "m1", "r1", "first", {"send_mode": "queue"})
    runtime.submit("b", "s1", "m2", "r2", "steer", {"send_mode": "steer"})
    native.db.pickup_message = lambda sid, mid: {"seq": 1, "turn_id": "segment"}
    owners = runtime._correlations("s1")
    assert owners["segment"] == ("m2", "r1")
    # The immutable receipt remains replayable after effective run reconciliation.
    assert (
        runtime.submit("b", "s1", "m2", "r2", "steer", {"send_mode": "steer"})["message_id"] == "m2"
    )


def test_background_callback_keeps_original_run_across_native_segments(tmp_path):
    config, native, runtime = _runtime(tmp_path)
    _owned(runtime)
    runtime.submit("a", "s1", "m1", "r1", "first", {"send_mode": "queue"})
    native.db.pickup_message = lambda sid, mid: {"seq": 1, "turn_id": "native-turn"}
    native.db.rows = [_row(1, {"type": "system", "subtype": "task_started", "task_id": "job"})]
    assert runtime.observe("s1")["events"][0]["run_id"] == "r1"
    runtime = ConductorRuntime(config, native)
    callback = _row(
        2,
        {"type": "system", "subtype": "task_notification", "task_id": "job", "status": "completed"},
    )
    callback["turn_id"] = "new-native-segment"
    answer = _row(3, {"type": "result", "result": "background finished"})
    answer["turn_id"] = "new-native-segment"
    native.db.rows += [callback, answer]
    assert [e["run_id"] for e in runtime.observe("s1", after=1)["events"]] == ["r1", "r1"]


def test_pages_do_not_claim_rest_while_native_history_remains_unread(tmp_path):
    _, native, runtime = _runtime(tmp_path)
    _owned(runtime)
    native.db.rows = [_row(i, {"type": "result", "result": str(i)}) for i in range(1, 503)]
    observed = runtime.observe("s1", limit=2)
    assert len(observed["events"]) == 2 and observed["has_more"]
    assert not observed["snapshot"]["rest"]["confirmed"]
