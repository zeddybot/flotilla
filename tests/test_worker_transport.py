"""Headless worker over the real loopback transport and server poller."""

import base64
import subprocess
import threading
import time
from pathlib import Path

from slack_conductor.config import Config
from slack_conductor.poller import Poller
from slack_conductor.remote import RemoteConductor
from slack_conductor.remote_client import run_client
from slack_conductor.runtime_local import RuntimeProxy
from slack_conductor.slack_app import Bridge
from slack_conductor.store import Store
from tests.support import Slack

TOKEN = "worker-test-token-with-at-least-thirty-two-characters"


def eventually(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("headless transport did not reach the expected state")


def _repo(tmp_path):
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
    return repo


def _fake_engine(tmp_path):
    calls = tmp_path / "engine-calls.jsonl"
    binary = tmp_path / "claude"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json,os,sys\n"
        "from pathlib import Path\n"
        "args=sys.argv[1:]\n"
        "sid=args[args.index('--session-id')+1] if '--session-id' in args else args[args.index('--resume')+1]\n"
        "with open(os.environ['WORKER_ENGINE_CALLS'],'a') as f: f.write(json.dumps(args)+'\\n')\n"
        "Path('artifact.txt').write_text('headless file bytes')\n"
        "print(json.dumps({'type':'system','subtype':'init','session_id':sid}),flush=True)\n"
        "print(json.dumps({'type':'assistant','session_id':sid,'message':{'content':[{'type':'text','text':'headless answer'}]}}),flush=True)\n"
        "print(json.dumps({'type':'result','session_id':sid,'result':'headless answer'}),flush=True)\n"
    )
    binary.chmod(0o755)
    return binary, calls


def _event(ts, text, *, thread_ts=None):
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


def test_headless_worker_transports_queue_receipts_poller_files_and_reconnect(
    tmp_path, monkeypatch
):
    repo = _repo(tmp_path)
    binary, calls = _fake_engine(tmp_path)
    monkeypatch.setenv("WORKER_ENGINE_CALLS", str(calls))
    server_state, client_state = tmp_path / "server", tmp_path / "client"
    server_state.mkdir()
    client_state.mkdir()
    server_config = Config(
        mode="server",
        state_dir=server_state,
        server_host="127.0.0.1",
        server_port=0,
        client_id="worker",
        send_mode="queue",
        model="",
        effort="",
    )
    server = RemoteConductor(server_config, TOKEN)
    server.start()
    port = server.server.socket.getsockname()[1]
    client_config = Config(
        mode="client",
        runtime_adapter="headless",
        state_dir=client_state,
        server_url=f"ws://127.0.0.1:{port}/client",
        client_id="worker",
        repo=repo,
        workspace_root=tmp_path / "workspaces",
        engine="claude",
        claude_binary=str(binary),
        engine_permission_policy="read-only",
        send_mode="queue",
    )
    store = Store(server_state / "bridge.sqlite")
    slack = Slack()
    slack.call = lambda *_args, **_kwargs: {"permalink": "https://slack.test/thread"}
    bridge = Bridge(server_config, store, server, slack, "B", "U1", "T1")
    stop = threading.Event()
    client = threading.Thread(target=run_client, args=(client_config, TOKEN, stop), daemon=True)
    client.start()
    try:
        eventually(server.ready)
        proxy = RuntimeProxy(client_state)
        assert proxy.probe()["adapter"] == "headless"
        first = _event("1", "<@B> [queue] first task")
        bridge.receive(first)
        bridge.receive(first)
        assert len(store.rows("SELECT * FROM events")) == 1
        bridge.process(store.next_event())
        status = store.rows("SELECT * FROM statuses")[0]
        sid, wid = status["session_id"], status["workspace_id"]
        assert sid and wid and len(store.rows("SELECT * FROM statuses")) == 1
        eventually(lambda: len(calls.read_text().splitlines()) == 1 if calls.exists() else False)
        eventually(lambda: proxy.observe(sid, view="snapshot")["rest"]["confirmed"])
        assert server.db.pickup(sid, status["prompt"])
        assert server.db.session(sid)["status"] == "idle"
        assert server.db.messages(sid, 0)[0]["event"]["kind"] == "input"
        bridge.poller.settle_seconds = 0
        eventually(lambda: _tick_complete(bridge.poller, store, status["id"]))
        assert store.status(status["id"])["result"] == "headless answer"
        assert any("headless answer" in text for _, text in slack.edits)

        # Duplicate durable submit returns the same receipt and never reruns the prompt.
        sent = proxy.submit(
            "repeat", sid, "m-repeat", "r-repeat", "follow-up", {"send_mode": "queue"}
        )
        assert (
            proxy.submit("repeat", sid, "m-repeat", "r-repeat", "follow-up", {"send_mode": "queue"})
            == sent
        )
        eventually(lambda: len(calls.read_text().splitlines()) == 2)
        assert (
            len(
                [
                    e
                    for e in proxy.observe(sid, view="events")["events"]
                    if e["event"]["kind"] == "input"
                ]
            )
            == 2
        )

        workspace = Path(proxy.describe(sid)["path"])
        assert (workspace / "artifact.txt").read_bytes() == b"headless file bytes"
        key = "file-transfer"
        prepared = server.rpc(
            "files.prepare",
            {
                "key": key,
                "status": {"id": "reply", "event_key": key, "result": "[file](artifact.txt)"},
                "workspace_id": wid,
                "session_id": sid,
            },
            key=key,
        )
        item = prepared["files"][0]
        chunk = server.rpc(
            "files.chunk",
            {"key": key, "snapshot": item["remote_snapshot"], "offset": 0},
            key="file-chunk",
        )
        assert base64.b64decode(chunk["data"]) == b"headless file bytes"

        stop.set()
        client.join(timeout=5)
        assert not client.is_alive()
        restarted_stop = threading.Event()
        restarted = threading.Thread(
            target=run_client, args=(client_config, TOKEN, restarted_stop), daemon=True
        )
        restarted.start()
        try:
            eventually(server.ready)
            eventually(lambda: server.db.session(sid)["status"] == "idle")
            assert server.db.workspace(wid)["workspace_path"] == str(workspace)
            assert len(calls.read_text().splitlines()) == 2
        finally:
            restarted_stop.set()
            restarted.join(timeout=5)
            assert not restarted.is_alive()
    finally:
        stop.set()
        server.close()
        client.join(timeout=5)


def _tick_complete(poller: Poller, store: Store, status_id: int):
    poller.tick()
    return store.status(status_id)["state"] == "complete"
