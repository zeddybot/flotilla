import pytest

from tests.test_session_runtime import _owned, _runtime


def test_native_control_acknowledgments_are_durable_and_do_not_claim_rest(tmp_path):
    _, native, runtime = _runtime(tmp_path)
    _owned(runtime)
    calls = []
    native.interrupt = lambda wid, sid, **args: (
        calls.append(args) or {"state": "requested", "turn_id": "original"}
    )
    first = runtime.interrupt("stop", "s1")
    assert first["state"] == "requested"
    assert runtime.interrupt("stop", "s1") == first
    assert calls == [{"command_id": "stop"}]


def test_response_identity_is_bound_to_original_question(tmp_path):
    _, native, runtime = _runtime(tmp_path)
    _owned(runtime)
    calls = []
    native.respond = lambda wid, sid, **args: calls.append(args) or {"state": "answered"}
    reply = {"kind": "approval", "decision": "deny"}
    first = runtime.respond("reply", "s1", "question-1", reply)
    assert runtime.respond("reply", "s1", "question-1", reply) == first
    assert len(calls) == 1
    with pytest.raises(ValueError, match="different arguments"):
        runtime.respond("reply", "s1", "question-2", reply)


def test_question_transport_failure_does_not_hide_session(tmp_path):
    _, native, runtime = _runtime(tmp_path)
    _owned(runtime)

    def unavailable(*args):
        raise RuntimeError("reconnecting")

    native.questions = unavailable
    description = runtime.describe("s1")
    assert description["questions"] == [] and not description["questions_available"]
    assert len(runtime.sessions()) == 1
