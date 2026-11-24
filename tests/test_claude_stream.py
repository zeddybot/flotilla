import json
import os
from concurrent.futures import ThreadPoolExecutor
from select import select
from types import SimpleNamespace

import pytest

from slack_conductor.headless.claude_stream import ClaudeStream
from slack_conductor.headless.process_host import Rejected


@pytest.fixture()
def channel(request, tmp_path):
    engine_in, client_out = os.pipe()
    client_in, engine_out = os.pipe()
    with (
        os.fdopen(engine_in) as inputs,
        os.fdopen(engine_out, "w", buffering=1) as outputs,
        os.fdopen(client_in) as client_stdout,
        os.fdopen(client_out, "w") as client_stdin,
    ):
        mode = getattr(request, "param", None)
        stream = ClaudeStream(
            SimpleNamespace(stdin=client_stdin, stdout=client_stdout),
            resume_session=None if mode == "fresh" else mode,
            fresh_transcript=tmp_path / "session.jsonl" if mode == "fresh" else None,
        )
        try:
            yield stream, inputs, outputs
        finally:
            outputs.close()
            stream.close()


def receive(inputs):
    assert select([inputs], [], [], 2)[0], "Claude input was not written"
    return json.loads(inputs.readline())


def send(outputs, kind, **fields):
    outputs.write(json.dumps({"type": kind, "session_id": "session", **fields}) + "\n")


def start(pool, channel):
    stream, inputs, outputs = channel
    initializing = pool.submit(stream.start, "first prompt", "first-id", 2)
    request = receive(inputs)
    send(outputs, "system", subtype="init")
    send(outputs, "user", uuid=request["uuid"], isReplay=True)
    initializing.result(timeout=2)
    assert request["message"] == {"role": "user", "content": "first prompt"}
    return request["uuid"]


def finish_result(stream, lines):
    for line in lines:
        source = json.loads(line)
        if source.get("type") == "result" and not source.get("parent_tool_use_id"):
            stream.finish()
            assert list(lines) == []
            return source["result"]
    raise AssertionError("Claude did not emit a result")


def test_steer_confirms_exact_top_level_replay_and_finishes_at_first_result(channel):
    stream, inputs, outputs = channel
    with ThreadPoolExecutor() as pool:
        initial_uuid = start(pool, channel)
        steering = stream.steer_async("session", "turn", "correction", "steer-id")
        request = receive(inputs)
        steer_uuid = request["uuid"]
        assert steer_uuid != initial_uuid
        assert request["message"]["content"] == "correction"
        send(outputs, "user", uuid=initial_uuid, isReplay=True)
        send(outputs, "user", uuid=steer_uuid, isReplay=True, parent_tool_use_id="nested")
        send(outputs, "user", uuid=steer_uuid)
        send(outputs, "result", parent_tool_use_id="nested")
        send(outputs, "system", subtype="barrier")
        lines = stream.lines()
        for line in lines:
            if json.loads(line).get("subtype") == "barrier":
                break
        assert not steering.done()
        send(outputs, "user", uuid=steer_uuid, isReplay=True)
        steering.result(timeout=2)
        send(outputs, "result", result="corrected")
        assert finish_result(stream, lines) == "corrected"
        with pytest.raises(Rejected, match="already ended"):
            stream.steer_async("session", "turn", "late correction", "late-id")
        assert not select([inputs], [], [], 0)[0]


def test_result_before_acknowledgement_marks_steer_ambiguous(channel):
    stream, inputs, outputs = channel
    with ThreadPoolExecutor() as pool:
        start(pool, channel)
        steering = stream.steer_async("session", "turn", "correction", "steer-id")
        request = receive(inputs)
        send(outputs, "result", result="original")
        send(outputs, "user", uuid=request["uuid"], isReplay=True)
        assert finish_result(stream, stream.lines()) == "original"
        with pytest.raises(RuntimeError, match="turn ended before acknowledging"):
            steering.result(timeout=2)
        with pytest.raises(Rejected, match="already ended"):
            stream.steer_async("session", "turn", "correction", "steer-id")
        assert not select([inputs], [], [], 0)[0]


def test_disconnect_unblocks_pending_initial_input(channel):
    stream, inputs, outputs = channel
    with ThreadPoolExecutor() as pool:
        initializing = pool.submit(stream.start, "a prompt", "first-id", 2)
        receive(inputs)
        outputs.close()
        with pytest.raises(RuntimeError, match="disconnected"):
            initializing.result(timeout=2)


@pytest.mark.parametrize("channel", ["fresh"], indirect=True)
@pytest.mark.parametrize("outcome", ["deadline", "result", "unmatched"])
def test_fresh_input_requires_exact_persisted_row_without_stream_replay(channel, tmp_path, outcome):
    stream, inputs, outputs = channel
    path = tmp_path / "session.jsonl"
    with ThreadPoolExecutor() as pool:
        initializing = pool.submit(
            stream.start, "first prompt", "input-id", 2 if outcome == "result" else 0.5
        )
        request = receive(inputs)
        send(outputs, "system", subtype="init")
        entry = {
            "type": "user",
            "uuid": request["uuid"],
            "sessionId": "session",
            "isSidechain": False,
            "parentUuid": None,
        }
        with path.open("w") as transcript:
            for fields in (
                {"sessionId": "other"},
                {"uuid": "other"},
                {"isSidechain": True},
                {"parentUuid": "other"},
            ):
                transcript.write(json.dumps({**entry, **fields}) + "\n")
            if outcome != "unmatched":
                transcript.write(json.dumps(entry) + "\n")
        if outcome == "unmatched":
            with pytest.raises(RuntimeError, match="did not acknowledge"):
                initializing.result(timeout=2)
            return
        if outcome == "result":
            send(outputs, "result", result="completed")
        initializing.result(timeout=2)
        if outcome == "deadline":
            send(outputs, "result", result="completed")
        assert finish_result(stream, stream.lines()) == "completed"


@pytest.mark.parametrize("channel", ["session"], indirect=True)
@pytest.mark.parametrize(
    "result", [{"is_error": True}, {"origin": {}}, {"user_message_uuids": ["own"]}]
)
def test_unacknowledged_resume_does_not_ignore_a_launch_result(channel, result):
    stream, inputs, outputs = channel
    with ThreadPoolExecutor() as pool:
        initializing = pool.submit(stream.start, "new prompt", "input-id", 2)
        message_id = receive(inputs)["uuid"]
        send(outputs, "system", subtype="init")
        fields = {
            "subtype": "success",
            "is_error": False,
            "origin": {"kind": "task-notification"},
            **result,
        }
        if fields.get("user_message_uuids"):
            fields["user_message_uuids"] = [message_id]
        send(outputs, "result", **fields)
        with pytest.raises(RuntimeError, match="before acknowledging"):
            initializing.result(timeout=2)


def test_replay_from_wrong_session_fails_pending_input(channel):
    stream, inputs, outputs = channel
    with ThreadPoolExecutor() as pool:
        initializing = pool.submit(stream.start, "a prompt", "first-id", 2)
        request = receive(inputs)
        send(outputs, "system", subtype="init")
        send(outputs, "user", uuid=request["uuid"], isReplay=True, session_id="other-session")
        with pytest.raises(RuntimeError, match="unexpected session"):
            initializing.result(timeout=2)


def test_pending_replays_are_tracked_independently(channel):
    stream, inputs, outputs = channel
    with ThreadPoolExecutor() as pool:
        start(pool, channel)
        first = stream.steer_async("session", "turn", "first correction", "steer-id-1")
        first_uuid = receive(inputs)["uuid"]
        second = stream.steer_async("session", "turn", "second correction", "steer-id-2")
        second_uuid = receive(inputs)["uuid"]
        assert not first.done() and not second.done()
        send(outputs, "user", uuid=second_uuid, isReplay=True)
        second.result(timeout=2)
        assert not first.done()
        send(outputs, "user", uuid=first_uuid, isReplay=True)
        first.result(timeout=2)
        send(outputs, "result", result="corrected")
        assert finish_result(stream, stream.lines()) == "corrected"
