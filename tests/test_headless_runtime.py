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
    print(json.dumps({'type':'system','subtype':'init','session_id':identity}), flush=True)
    print(json.dumps({'type':'assistant','session_id':identity,'message':{'content':[{'type':'text','text':'claude answer'}]}}), flush=True)
    if 'no-result' not in args:
        print(json.dumps({'type':'result','session_id':identity,'result':'claude answer'}), flush=True)
else:
    identity = '22222222-2222-4222-8222-222222222222'
    print(json.dumps({'type':'thread.started','thread_id':identity}), flush=True)
    print(json.dumps({'type':'turn.started'}), flush=True)
    print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'codex answer'}}), flush=True)
    if 'no-result' not in args:
        print(json.dumps({'type':'turn.completed'}), flush=True)
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
    assert all("chosen" in row["args"] and "high" in " ".join(row["args"]) for row in rows)
    assert "--dangerously-skip-permissions" not in rows[0]["args"]
    assert "--dangerously-bypass-approvals-and-sandbox" not in rows[0]["args"]
    if engine == "claude":
        assert "--session-id" in rows[0]["args"]
        assert "--resume" in rows[1]["args"]
    else:
        assert "resume" not in rows[0]["args"]
        assert "resume" in rows[1]["args"]
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
