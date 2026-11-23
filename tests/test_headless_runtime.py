"""Headless runtime contract with disposable repos and fake engine executables."""

import json
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from slack_conductor.headless.process_host import launch, terminate
from slack_conductor.headless.runtime import HeadlessRuntime
from slack_conductor.runtime import Unsupported

ENGINE_SCRIPT = """#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
args = sys.argv[1:]
engine = Path(sys.argv[0]).name
with open(os.environ['HEADLESS_CALLS'], 'a') as calls:
    calls.write(json.dumps({'engine':engine,'args':args,'cwd':os.getcwd()})+'\\n')
if 'slow' in args:
    time.sleep(10)
if engine == 'claude':
    identity = args[args.index('--session-id')+1] if '--session-id' in args else args[args.index('--resume')+1]
    first_input = json.loads(sys.stdin.readline())
    prompt = first_input['message']['content']

    def replay(message):
        print(json.dumps({**message, 'isReplay': True, 'session_id': identity}), flush=True)

    print(json.dumps({'type':'system','subtype':'init','session_id':identity}), flush=True)
    replay(first_input)
    if prompt == 'stale-job':
        print(json.dumps({'type': 'system', 'subtype': 'task_notification', 'status': 'completed',
            'session_id': identity, 'task_id': 'job'}), flush=True)
    if prompt in {'background', 'background-early', 'background-exit', 'background-folded'}:
        task = {'type': 'system', 'session_id': identity, 'task_id': 'job'}
        print(json.dumps({**task, 'subtype': 'task_started'}), flush=True)
        if prompt in {'background-early', 'background-folded'}:
            print(json.dumps({**task, 'subtype': 'task_notification', 'status': 'completed'}), flush=True)
        if prompt == 'background-folded':
            print(json.dumps({'type': 'result', 'session_id': identity, 'result': 'Combined answer'}), flush=True)
            assert sys.stdin.readline() == ''
            sys.exit(0)
        print(json.dumps({'type': 'result', 'session_id': identity, 'result': 'Waiting'}), flush=True)
        if prompt == 'background-exit':
            sys.exit(0)
        if prompt == 'background':
            print(json.dumps({**task, 'subtype': 'task_notification', 'status': 'running'}), flush=True)
            correction = json.loads(sys.stdin.readline())
            print(json.dumps({**task, 'subtype': 'task_notification', 'status': 'completed'}), flush=True)
            replay(correction)
        assert sys.stdin.readline() == ''
        release = Path(os.environ['HEADLESS_CALLS'] + '.release')
        while not release.exists():
            time.sleep(0.02)
        print(json.dumps({'type':'system','subtype':'init','session_id':identity}), flush=True)
    if prompt == 'background-overlap':
        task = {'type': 'system', 'session_id': identity}
        def system(subtype, **fields):
            print(json.dumps({**task, 'subtype': subtype, **fields}), flush=True)
        system('task_started', task_id='first-job')
        system('task_started', task_id='second-job')
        system('task_notification', task_id='first-job', status='completed')
        print(json.dumps({'type': 'result', 'session_id': identity, 'result': 'Waiting'}), flush=True)
        system('init')
        system('task_notification', task_id='second-job', status='completed')
        print(json.dumps({'type': 'result', 'session_id': identity, 'result': 'First callback'}), flush=True)
        release = Path(os.environ['HEADLESS_CALLS'] + '.release')
        while not release.exists():
            time.sleep(0.02)
        system('init')
    if prompt == 'slow':
        time.sleep(10)
    output = 'claude answer'
    if prompt in {'steer-me', 'finish-before-replay'}:
        correction = json.loads(sys.stdin.readline())
        release = Path(os.environ['HEADLESS_CALLS'] + '.release')
        while not release.exists():
            time.sleep(0.02)
    if prompt == 'steer-me':
        if isinstance(correction['message']['content'], list):
            output = correction['message']['content'][0]['text']
        else:
            output = correction['message']['content']
        replay(correction)
    print(json.dumps({'type':'assistant','session_id':identity,'message':{'content':[{'type':'text','text':output}]}}), flush=True)
    if prompt != 'no-result':
        print(json.dumps({'type':'result','session_id':identity,'result':output}), flush=True)
    if prompt == 'finish-before-replay':
        replay(correction)
else:
    identity = '22222222-2222-4222-8222-222222222222'
    turn = {'id': 'native-turn', 'status': 'inProgress'}

    def send(**data):
        print(json.dumps(data), flush=True)

    def done(text='codex answer'):
        send(method='item/completed', params={'threadId': identity, 'turnId': turn['id'],
            'item': {'type': 'agentMessage', 'text': text}})
        send(method='turn/completed', params={'threadId': identity,
            'turn': {**turn, 'status': 'completed'}})

    for line in sys.stdin:
        request = json.loads(line)
        method = request['method']
        if method == 'initialized':
            continue
        req_id = request['id']
        params = request['params']
        if method == 'initialize':
            send(id=req_id, result={})
        elif method == 'model/list':
            send(id=req_id, result={'data': [{'model': 'discovered-codex', 'isDefault': True}]})
        elif method in {'thread/start', 'thread/resume'}:
            assert params['approvalPolicy'] == 'never' and params['sandbox'] == 'read-only'
            with open(os.environ['HEADLESS_CALLS'] + '.threads', 'a') as threads:
                threads.write(json.dumps([method, params]) + '\\n')
            send(id=req_id, result={'thread': {'id': identity}, 'model': params['model']})
        elif method == 'turn/start':
            assert params['threadId'] == identity
            send(id=req_id, result={'turn': turn})
            prompt = params['input'][0]['text']
            if prompt not in {'steer-me', 'reject-steer', 'disconnect-steer'}:
                done()
        elif method == 'turn/steer':
            assert params['threadId'] == identity and params['expectedTurnId'] == turn['id']
            if prompt == 'disconnect-steer':
                sys.exit(0)
            if prompt == 'reject-steer':
                send(id=req_id, error={'code': -32600, 'message': 'no active turn'})
                done()
            else:
                send(id=req_id, result={'turnId': turn['id']})
                done(params['input'][0]['text'])

"""


def _fixture(tmp_path, engine="claude"):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "README.md").write_text("test\n")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "initial",
        ],
        check=True,
    )
    for name in ("claude", "codex"):
        binary = tmp_path / name
        binary.write_text(ENGINE_SCRIPT)
        binary.chmod(0o755)
    calls = tmp_path / "calls.jsonl"
    return SimpleNamespace(
        state_dir=tmp_path / "state",
        repo=repo,
        workspace_root=tmp_path / "workspaces",
        engine=engine,
        claude_binary=tmp_path / "claude",
        codex_binary=tmp_path / "codex",
        engine_model="fallback-model",
        engine_effort="medium",
        engine_permission_policy="read-only",
    ), calls


def _wait(predicate, timeout=5):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("headless worker did not reach expected state")


@pytest.mark.parametrize("engine", ["claude", "codex"])
def test_create_queue_resume_and_archive(tmp_path, monkeypatch, engine):
    config, calls = _fixture(tmp_path, engine)
    monkeypatch.setenv("HEADLESS_CALLS", str(calls))
    runtime = HeadlessRuntime(config)
    spec = {"repo": str(config.repo), "engine": engine, "model": "chosen", "effort": "high"}
    created = runtime.create("create-1", spec)
    sid = created["session_id"]
    assert runtime.create("create-1", spec) == created
    assert Path(runtime.describe(sid)["path"]).is_dir()
    assert runtime.ready() is False
    assert not calls.exists()
    first = runtime.submit("send-1", sid, "m1", "r1", "first", {"send_mode": "queue"})
    second = runtime.submit("send-2", sid, "m2", "r2", "second", {"send_mode": "queue"})
    assert first["state"] == second["state"] == "queued"
    assert runtime.submit("send-1", sid, "m1", "r1", "first", {"send_mode": "queue"}) == first
    runtime.start({sid: sid})
    _wait(lambda: len(calls.read_text().splitlines()) == 2 if calls.exists() else False)
    _wait(lambda: runtime.observe(sid, view="snapshot")["rest"]["confirmed"])
    rows = [json.loads(line) for line in calls.read_text().splitlines()]
    assert all(row["cwd"] == runtime.describe(sid)["path"] for row in rows)
    if engine == "claude":
        assert all("chosen" in row["args"] and "high" in " ".join(row["args"]) for row in rows)
    assert "--dangerously-skip-permissions" not in rows[0]["args"]
    assert "--dangerously-bypass-approvals-and-sandbox" not in rows[0]["args"]
    if engine == "claude":
        assert "--session-id" in rows[0]["args"]
        assert "--resume" in rows[1]["args"]
    else:
        requests = [
            json.loads(line) for line in Path(str(calls) + ".threads").read_text().splitlines()
        ]
        assert [method for method, _ in requests] == ["thread/start", "thread/resume"]
        for _, params in requests:
            assert params["model"] == "chosen"
            assert params["config"] == {"model_reasoning_effort": "high"}
        assert runtime.describe(sid)["native_id"] == "22222222-2222-4222-8222-222222222222"
    observed = runtime.observe(sid, view="events", limit=2)
    assert len(observed["events"]) == 2 and observed["has_more"]
    assert observed["events"][0]["event"]["kind"] == "input"
    assert runtime.observe(sid, view="receipt", prompt="first")["receipt"]["state"] == "picked_up"
    assert (
        runtime.observe(sid, view="snapshot")["snapshot"]["configuration"]["requested"]["model"]
        == "chosen"
    )
    assert runtime.close_session("close-1", sid)["state"] == "archived"
    assert Path(runtime.describe(sid)["path"]).is_dir()
    runtime.shutdown()


def test_ambiguous_restart_blocks_replay_and_steering(tmp_path, monkeypatch):
    config, calls = _fixture(tmp_path)
    monkeypatch.setenv("HEADLESS_CALLS", str(calls))
    runtime = HeadlessRuntime(config)
    sid = runtime.create("create", {})["session_id"]
    runtime.submit("send", sid, "m", "r", "first", {"send_mode": "queue"})
    with runtime.store.connect() as db:
        db.execute("UPDATE turns SET state='launching' WHERE session_id=?", (sid,))
    restarted = HeadlessRuntime(config)
    restarted.start({sid: sid})
    time.sleep(0.1)
    assert not calls.exists()
    assert restarted.observe(sid, view="snapshot")["freshness"] == "fresh"
    assert restarted.observe(sid, view="snapshot")["rest"]["confirmed"] is False
    with pytest.raises(Unsupported, match="explicit recovery"):
        restarted.submit("new", sid, "m2", "r2", "second", {"send_mode": "queue"})
    with pytest.raises(Unsupported, match="steering"):
        restarted.submit("steer", sid, "m3", "r3", "steer", {"send_mode": "steer"})
    with pytest.raises(ValueError, match="binding"):
        restarted.bind({"unknown": "unknown"})
    restarted.shutdown()


def test_missing_completion_requires_attention(tmp_path, monkeypatch):
    config, calls = _fixture(tmp_path)
    monkeypatch.setenv("HEADLESS_CALLS", str(calls))
    runtime = HeadlessRuntime(config)
    sid = runtime.create("create", {})["session_id"]
    runtime.submit("send", sid, "m", "r", "no-result", {"send_mode": "queue"})
    runtime.start({sid: sid})
    _wait(lambda: runtime.describe(sid)["activity"] == "attention")
    assert runtime.observe(sid, view="snapshot")["rest"]["confirmed"] is False
    runtime.shutdown()


@pytest.mark.parametrize("prompt", ["background", "background-early"])
def test_background_steering_waits_for_callback_result(tmp_path, monkeypatch, prompt):
    config, calls = _fixture(tmp_path)
    monkeypatch.setenv("HEADLESS_CALLS", str(calls))
    runtime = HeadlessRuntime(config)
    sid = runtime.create("create", {})["session_id"]
    runtime.start({sid: sid})
    try:
        runtime.submit("send", sid, "m", "r", prompt, {})
        _wait(lambda: runtime.observe(sid)["rest"]["result_candidate"])
        assert runtime.observe(sid)["rest"]["pending_jobs"] == (
            ["job"] if prompt == "background" else []
        )
        assert runtime.observe(sid)["snapshot"]["status"] == "working"

        def steer():
            return runtime.submit("steer", sid, "m2", "r2", "stop", {"send_mode": "steer"})

        if prompt == "background-early":
            receipt = steer()
            assert receipt["state"] == "queued"
            assert steer() == receipt
            assert runtime.observe(sid, prompt="stop")["receipt"]["state"] == "queued"
        else:
            assert steer()["state"] == "pending"
            _wait(lambda: runtime.observe(sid, prompt="stop")["receipt"]["state"] == "picked_up")
            _wait(lambda: not runtime.observe(sid)["rest"]["pending_jobs"])
            assert steer()["state"] == "picked_up"
        assert not runtime.observe(sid)["rest"]["confirmed"]
        Path(str(calls) + ".release").touch()
        if prompt == "background-early":
            _wait(lambda: runtime.observe(sid, prompt="stop")["receipt"]["state"] == "picked_up")
        _wait(lambda: runtime.observe(sid)["rest"]["confirmed"])
        observed = runtime.observe(sid)
        assert observed["events"][-1]["event"]["text"] == "claude answer"
        launches = [json.loads(line)["args"] for line in calls.read_text().splitlines()]
        if prompt == "background-early":
            assert len(launches) == 2 and "--resume" in launches[-1]
        else:
            assert len(launches) == 1
    finally:
        runtime.shutdown()


def test_background_stream_exit_finishes_with_unknown_outcome(tmp_path, monkeypatch):
    config, calls = _fixture(tmp_path)
    monkeypatch.setenv("HEADLESS_CALLS", str(calls))
    runtime = HeadlessRuntime(config)
    sid = runtime.create("create", {})["session_id"]
    runtime.submit("send", sid, "m", "r", "background-exit", {})
    runtime.start({sid: sid})
    try:
        _wait(lambda: runtime.observe(sid)["rest"]["result_candidate"])
        _wait(lambda: runtime.observe(sid)["rest"]["confirmed"])
        observed = runtime.observe(sid)
        assert observed["rest"]["pending_jobs"] == []
        assert observed["events"][-2]["event"]["status"] == "interrupted"
        assert observed["events"][-1]["event"]["error"]
        assert "outcome is unknown" in observed["events"][-1]["event"]["text"]
    finally:
        runtime.shutdown()


def test_background_notification_folded_into_foreground_result(tmp_path, monkeypatch):
    config, calls = _fixture(tmp_path)
    monkeypatch.setenv("HEADLESS_CALLS", str(calls))
    runtime = HeadlessRuntime(config)
    sid = runtime.create("create", {})["session_id"]
    runtime.submit("send", sid, "m", "r", "background-folded", {})
    runtime.start({sid: sid})
    try:
        _wait(lambda: runtime.observe(sid)["rest"]["result_candidate"])
        _wait(lambda: runtime.observe(sid)["rest"]["confirmed"])
        results = [
            row["event"]
            for row in runtime.observe(sid)["events"]
            if row["event"]["kind"] == "result"
        ]
        assert [result["text"] for result in results] == ["Combined answer"]
    finally:
        runtime.shutdown()


def test_job_finishing_during_callback_keeps_its_own_callback_pending(tmp_path, monkeypatch):
    config, calls = _fixture(tmp_path)
    monkeypatch.setenv("HEADLESS_CALLS", str(calls))
    runtime = HeadlessRuntime(config)
    sid = runtime.create("create", {})["session_id"]
    runtime.submit("send", sid, "m", "r", "background-overlap", {})
    runtime.start({sid: sid})
    try:

        def first_callback_observed():
            events = runtime.observe(sid)["events"]
            return events and events[-1]["event"].get("text") == "First callback"

        _wait(first_callback_observed)
        observed = runtime.observe(sid)
        assert observed["rest"]["pending_jobs"] == []
        assert not observed["rest"]["confirmed"]
        Path(str(calls) + ".release").touch()
        _wait(lambda: runtime.observe(sid)["rest"]["confirmed"])
        assert runtime.observe(sid)["events"][-1]["event"]["text"] == "claude answer"
    finally:
        runtime.shutdown()


@pytest.mark.parametrize(
    "prior_state", ["legacy", "working", "working_unflushed", "working_callback", "question"]
)
def test_restart_reconciles_background_job_without_relaunch(tmp_path, monkeypatch, prior_state):
    config, calls = _fixture(tmp_path)
    monkeypatch.setenv("HEADLESS_CALLS", str(calls))
    runtime = HeadlessRuntime(config)
    sid = runtime.create("create", {})["session_id"]
    native_id = runtime.describe(sid)["native_id"]
    original = runtime.submit("send", sid, "m", "r", "background", {})
    with runtime.store.connect() as db:
        turn = db.execute("SELECT * FROM turns WHERE session_id=?", (sid,)).fetchone()
        db.execute("UPDATE turns SET state='launching' WHERE id=?", (turn["id"],))
    runtime._pickup(turn)
    runtime._event(turn, "assistant", {"kind": "job_started", "job_id": "job"})
    if prior_state == "question":
        runtime._event(turn, "assistant", {"kind": "update", "parts": [{"kind": "question"}]})
    runtime._event(turn, "assistant", {"kind": "result", "text": "Waiting"})
    finished = prior_state in {"working_unflushed", "working_callback"}
    if prior_state == "working_callback":
        runtime._event(
            turn, "assistant", {"kind": "job_finished", "job_id": "job", "status": "completed"}
        )
    with runtime.store.connect() as db:
        db.execute(
            "INSERT INTO jobs VALUES (?,'job',?)", (sid, "finished" if finished else "active")
        )
        if prior_state.startswith("working"):
            db.execute("UPDATE sessions SET activity='working' WHERE id=?", (sid,))
        else:
            db.execute("UPDATE turns SET state='attention' WHERE id=?", (turn["id"],))
            db.execute(
                "UPDATE sessions SET activity='attention',attention_reason='question_or_job' WHERE id=?",
                (sid,),
            )
    restarted = HeadlessRuntime(config)
    restarted.start({sid: sid})
    try:
        assert not calls.exists()
        assert restarted.submit("send", sid, "m", "r", "background", {}) == original
        if prior_state == "question":
            assert not restarted.observe(sid)["rest"]["confirmed"]
            with pytest.raises(Unsupported, match="explicit recovery"):
                restarted.submit("next", sid, "m2", "r2", "next", {})
            return
        assert restarted.observe(sid)["rest"]["confirmed"]
        expected = "follow-up answer" if prior_state == "working_callback" else "outcome is unknown"
        assert expected in restarted.observe(sid)["events"][-1]["event"]["text"]
        resumed_after = restarted.observe(sid, view="cursor")["cursor"]
        restarted.submit("next", sid, "m2", "r2", "stale-job", {"send_mode": "steer"})
        _wait(lambda: restarted.observe(sid, prompt="stale-job")["receipt"]["state"] == "picked_up")
        _wait(lambda: restarted.observe(sid)["rest"]["confirmed"])
        assert all(
            row["event"]["kind"] != "job_finished"
            for row in restarted.observe(sid, after=resumed_after, view="events")["events"]
        )
        assert ["--resume", native_id] == json.loads(calls.read_text())["args"][-6:-4]
    finally:
        restarted.shutdown()


def test_interrupt_cancels_queue_and_new_submit_resumes(tmp_path, monkeypatch):
    config, calls = _fixture(tmp_path)
    monkeypatch.setenv("HEADLESS_CALLS", str(calls))
    runtime = HeadlessRuntime(config)
    sid = runtime.create("create", {})["session_id"]
    runtime.submit("slow", sid, "m1", "r1", "slow", {"send_mode": "queue"})
    runtime.submit("later", sid, "m2", "r2", "later", {"send_mode": "queue"})
    runtime.start({sid: sid})
    _wait(lambda: bool(runtime._processes))
    _wait(calls.exists)
    assert runtime.interrupt("stop", sid) == {"session_id": sid, "state": "interrupted"}
    assert runtime.interrupt("stop", sid) == {"session_id": sid, "state": "interrupted"}
    _wait(lambda: not runtime._processes)
    with runtime.store.connect() as db:
        states = [row[0] for row in db.execute("SELECT state FROM turns ORDER BY rowid")]
    assert states == ["cancelled", "cancelled"]
    assert len(calls.read_text().splitlines()) == 1
    runtime.submit("fresh", sid, "m3", "r3", "fresh", {"send_mode": "queue"})
    _wait(lambda: len(calls.read_text().splitlines()) == 2)
    _wait(lambda: runtime.observe(sid, view="snapshot")["rest"]["confirmed"])
    runtime.shutdown()


def test_terminate_kills_child_after_leader_exits(tmp_path):
    marker = tmp_path / "survived"
    child = tmp_path / "child.py"
    child.write_text("import sys,time\ntime.sleep(0.5)\nopen(sys.argv[1], 'w').write('survived')\n")
    leader = tmp_path / "leader.py"
    leader.write_text(
        "import subprocess,sys\nsubprocess.Popen([sys.executable,sys.argv[1],sys.argv[2]])\n"
    )
    process = launch([os.sys.executable, str(leader), str(child), str(marker)], tmp_path)
    process.wait(timeout=2)
    terminate(process)
    process.stdout.close()
    time.sleep(0.7)
    assert not marker.exists()


@pytest.mark.parametrize(
    ("engine", "case"),
    [
        ("codex", "steer-me"),
        ("codex", "reject-steer"),
        ("codex", "disconnect-steer"),
        ("claude", "steer-me"),
        ("claude", "finish-before-replay"),
    ],
)
def test_native_steering(tmp_path, monkeypatch, engine, case):
    config, calls = _fixture(tmp_path, engine)
    monkeypatch.setenv("HEADLESS_CALLS", str(calls))
    runtime = HeadlessRuntime(config)
    sid = runtime.create("create", {})["session_id"]
    assert runtime.describe(sid)["capabilities"]["steer"]
    # Steering an idle session begins its first turn.
    assert (
        runtime.submit("start", sid, "m1", "run-original", case, {"send_mode": "steer"})["state"]
        == "queued"
    )
    runtime.start({sid: sid})
    try:
        _wait(lambda: sid in runtime._active_turns)

        def steer():
            return runtime.submit(
                "steer", sid, "m2", "run-unused", "redirect", {"send_mode": "steer"}
            )

        if engine == "claude":
            submitted = steer()
            assert submitted["state"] == "pending" and steer() == submitted
            assert (
                runtime.observe(sid, prompt="redirect", view="receipt")["receipt"]["state"]
                == "pending"
            )
            assert runtime.describe(sid)["activity"] == "working"
            Path(str(calls) + ".release").touch()
            _wait(lambda: runtime.describe(sid)["activity"] != "working")
        if case == "reject-steer":
            for _ in range(2):
                with pytest.raises(Unsupported, match="no longer accepts steering"):
                    steer()
            expected = "rejected"
        elif case in {"disconnect-steer", "finish-before-replay"}:
            result = steer()
            assert result["state"] == "uncertain" and result["warning"]
            assert steer() == result
            expected = "uncertain"
        else:
            result = steer()
            assert result["state"] == "picked_up" and result["run_id"] == "run-original"
            assert steer() == result
            original = runtime.observe(sid, prompt=case, view="receipt")["receipt"]
            assert original["segment_id"] == result["segment_id"]
            assert not runtime.observe(
                sid, view="later_input", after=original["seq"], segment_id=original["segment_id"]
            )["later_input"]
            expected = "picked_up"
        _wait(lambda: runtime.describe(sid)["activity"] != "working")
        assert (
            runtime.observe(sid, prompt="redirect", view="receipt")["receipt"]["state"] == expected
        )
        events = runtime.observe(sid, view="events")["events"]
        if case == "steer-me":
            assert all(event["turn_id"] == result["segment_id"] for event in events)
            assert events[-1]["event"]["kind"] == "result"
            assert events[-1]["event"]["text"] == "redirect"
        if case == "finish-before-replay":
            assert events[-1]["event"]["text"] == "claude answer"
            assert runtime.describe(sid)["activity"] == "idle"
        assert len(calls.read_text().splitlines()) == 1
    finally:
        runtime.shutdown()


def test_codex_native_default_is_independent_of_configured_claude_model(tmp_path, monkeypatch):
    config, calls = _fixture(tmp_path, "claude")
    monkeypatch.setenv("HEADLESS_CALLS", str(calls))
    runtime = HeadlessRuntime(config)
    sid = runtime.create("create", {"engine": "codex"})["session_id"]
    assert runtime.describe(sid)["configuration"]["requested"]["model"] is None
    runtime.submit("one", sid, "m1", "r1", "task one", {})
    runtime.submit("two", sid, "m2", "r2", "task two", {})
    runtime.start({sid: sid})
    try:
        _wait(lambda: runtime.observe(sid, prompt="task two", view="receipt")["receipt"]["seq"])
        _wait(lambda: runtime.describe(sid)["activity"] == "idle")
        observed = runtime.describe(sid)["configuration"]
        assert observed["effective"]["model"] == "discovered-codex"
        threads = Path(str(calls) + ".threads").read_text().splitlines()
        assert [json.loads(line)[1]["model"] for line in threads] == ["discovered-codex"] * 2
    finally:
        runtime.shutdown()
