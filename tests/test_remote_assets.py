import base64
import json
from pathlib import Path

import pytest

from slack_conductor.conductor.journal import Unconfirmed
from slack_conductor.remote_assets import RemoteUploads
from slack_conductor.remote_files import AssetSource
from slack_conductor.store import Store
from tests.support import status


class Conductor:
    def __init__(self, path):
        self.path = path
        self.calls = []

    def validate(self, workspace_id, session_id):
        self.calls.append((workspace_id, session_id))
        if (workspace_id, session_id) != ("w1", "s1"):
            raise ValueError("invalid binding")
        return {"workspace_path": str(self.path)}


def request(key, text):
    return {
        "key": key,
        "status": {"id": 12, "result": text, "event_key": key},
        "workspace_id": "w1",
        "session_id": "s1",
    }


def test_remote_transfer_resumes_and_uploads_exact_bytes(tmp_path, bridge_state):
    store, _, _, slack = bridge_state
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    data = b"A" * (256 * 1024 + 7)
    (workspace / "report.bin").write_bytes(data)
    source = AssetSource(tmp_path / "client", Conductor(workspace))
    sid = status(store, "request", "report")
    store.update(sid, result="[report](report.bin)")
    row = dict(store.status(sid))
    row.update(event_key="event:1", workspace_id="w1", session_id="s1")
    calls = []
    disconnected = [False]

    def rpc(method, args, key=None):
        calls.append((method, key))
        if disconnected[0] and method == "files.chunk":
            raise OSError("Mac offline")
        return source.call(method, args)

    slack.call = lambda method, **kwargs: (
        {"file_id": "F1", "upload_url": "https://slack.test/upload"}
        if method == "files_getUploadURLExternal"
        else {
            "file": {"permalink": "https://slack.test/F1", "mimetype": "application/octet-stream"}
        }
        if method == "files_info"
        else {}
    )
    sent = []
    uploads = RemoteUploads(
        store,
        slack,
        clock=lambda: 0,
        rpc=rpc,
        send_bytes=lambda url, path: sent.append(Path(path).read_bytes()),
    )
    plan = uploads.prepare(row)
    uploads.step(row, plan)
    part = store.path.parent / "uploads" / str(row["id"]) / "0.part"
    assert part.stat().st_size == 256 * 1024
    disconnected[0] = True
    with pytest.raises(Unconfirmed):
        uploads.step(row, plan)
    assert part.stat().st_size == 256 * 1024
    disconnected[0] = False
    uploads = RemoteUploads(
        Store(store.path),
        slack,
        clock=lambda: 0,
        rpc=rpc,
        send_bytes=lambda url, path: sent.append(Path(path).read_bytes()),
    )
    plan = json.loads(store.status(sid)["attachments"])
    for _ in range(8):
        if uploads.step(row, plan):
            break
    assert sent == [data]
    assert uploads.result(plan)[0] == "[report](https://slack.test/F1)"
    source_path = next((tmp_path / "client" / "remote_assets" / "uploads").rglob("0"))
    assert source_path.exists()
    uploads.cleanup(dict(row, attachments=json.dumps(plan)))
    assert not source_path.exists()
    assert not Path(plan["files"][0]["snapshot"]).exists()
    assert len([name for name, _ in calls if name == "files.chunk"]) == 3


def test_corrupt_partial_snapshot_restarts_download(tmp_path, bridge_state):
    store, _, _, slack = bridge_state
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    data = b"A" * (256 * 1024 + 3)
    (workspace / "report.bin").write_bytes(data)
    source = AssetSource(tmp_path / "client", Conductor(workspace))
    sid = status(store, "request", "report")
    store.update(sid, result="[report](report.bin)")
    row = dict(store.status(sid))
    row.update(event_key="corrupt", workspace_id="w1", session_id="s1")
    uploads = RemoteUploads(
        store, slack, clock=lambda: 0, rpc=lambda method, args, key=None: source.call(method, args)
    )
    plan = uploads.prepare(row)
    uploads.step(row, plan)
    part = store.path.parent / "uploads" / str(row["id"]) / "0.part"
    part.write_bytes(b"X" + part.read_bytes()[1:])
    uploads = RemoteUploads(
        Store(store.path),
        slack,
        clock=lambda: 0,
        rpc=lambda method, args, key=None: source.call(method, args),
    )
    plan = json.loads(store.status(sid)["attachments"])
    uploads.step(row, plan)
    uploads.step(row, plan)
    assert not part.exists()
    assert plan["files"][0]["transfer_failures"] == 1
    uploads.step(row, plan)
    assert part.read_bytes() == data[: 256 * 1024]


def test_repeated_corrupt_remote_bytes_bound_file_error(tmp_path, bridge_state):
    store, _, _, slack = bridge_state
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "report.txt").write_bytes(b"A")
    source = AssetSource(tmp_path / "client", Conductor(workspace))
    sid = status(store, "request", "report")
    store.update(sid, result="Answer. [report](report.txt)")
    row = dict(store.status(sid))
    row.update(event_key="bad-bytes", workspace_id="w1", session_id="s1")

    def rpc(method, args, key=None):
        response = source.call(method, args)
        if method == "files.chunk":
            return {"data": base64.b64encode(b"Z").decode(), "length": 1}
        return response

    uploads = RemoteUploads(store, slack, clock=lambda: 0, rpc=rpc)
    plan = uploads.prepare(row)
    for _ in range(3):
        uploads.step(row, plan)
    uploads = RemoteUploads(Store(store.path), slack, clock=lambda: 0, rpc=rpc)
    plan = json.loads(store.status(sid)["attachments"])
    for _ in range(10):
        if uploads.step(row, plan):
            break
    assert uploads.step(row, plan)
    assert plan["files"][0]["transfer_failures"] == 5
    assert plan["files"][0]["error"] == "file transfer failed (integrity check failed)"
    assert "Answer." in uploads.result(plan)[0]
    assert "file unavailable" in uploads.result(plan)[0]


def test_more_than_ten_files_delivers_first_ten_and_reports_limit(tmp_path, bridge_state):
    store, _, _, slack = bridge_state
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for index in range(11):
        (workspace / f"{index}.txt").write_text(str(index))
    source = AssetSource(tmp_path / "client", Conductor(workspace))
    sid = status(store, "request", "files")
    text = " ".join(f"[file{index}]({index}.txt)" for index in range(11))
    store.update(sid, result=text)
    row = dict(store.status(sid))
    row.update(event_key="eleven", workspace_id="w1", session_id="s1")
    issued = []

    def slack_call(method, **kwargs):
        if method == "files_getUploadURLExternal":
            file_id = f"F{len(issued)}"
            issued.append(file_id)
            return {"file_id": file_id, "upload_url": "https://slack.test/upload"}
        if method == "files_info":
            return {
                "file": {
                    "permalink": f"https://slack.test/{kwargs['file']}",
                    "mimetype": "text/plain",
                }
            }
        return {}

    slack.call = slack_call
    uploads = RemoteUploads(
        store,
        slack,
        clock=lambda: 0,
        rpc=lambda method, args, key=None: source.call(method, args),
        send_bytes=lambda url, path: None,
    )
    plan = uploads.prepare(row)
    assert len(plan["files"]) == 11
    assert plan["files"][-1]["error"] == "maximum 10 files per reply"
    for _ in range(70):
        if uploads.step(row, plan):
            break
    assert uploads.step(row, plan)
    rendered, _, file_ids = uploads.result(plan)
    assert len(file_ids) == 10
    assert "file10 (file unavailable: maximum 10 files per reply)" in rendered


def test_cleanup_queue_retries_offline_and_includes_zero_file_plan(tmp_path, bridge_state):
    store, _, _, slack = bridge_state
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = AssetSource(tmp_path / "client", Conductor(workspace))
    sid = status(store, "request", "plain")
    store.update(sid, result="plain answer")
    row = dict(store.status(sid))
    row.update(event_key="plain", workspace_id="w1", session_id="s1")
    online, now = [True], [0]

    def rpc(method, args, key=None):
        if method == "files.cleanup" and not online[0]:
            raise Unconfirmed("Mac offline")
        return source.call(method, args)

    uploads = RemoteUploads(store, slack, clock=lambda: now[0], rpc=rpc)
    plan = uploads.prepare(row)
    assert not plan["files"]
    assert uploads.step(row, plan)
    online[0] = False
    uploads.cleanup(dict(row, attachments=json.dumps(plan)))
    assert store.rows("SELECT event_key FROM remote_asset_cleanup") == [{"event_key": "plain"}]
    assert list((tmp_path / "client" / "remote_assets").glob("*/plan.json"))
    now[0] = 100
    online[0] = True
    restarted = RemoteUploads(Store(store.path), slack, clock=lambda: now[0], rpc=rpc)
    restarted.maintenance()
    assert not store.rows("SELECT event_key FROM remote_asset_cleanup")
    assert not list((tmp_path / "client" / "remote_assets").glob("*/plan.json"))


def test_remote_descriptor_can_only_initialize_transfer_metadata():
    from slack_conductor.file_links import references
    from slack_conductor.remote_assets import receive_plan

    answer = "[report](report.txt)"
    (ref,) = references(answer)
    descriptor = {
        "name": "report.txt",
        "digest": "0" * 64,
        "remote_snapshot": "a" * 64,
        "length": 7,
        "phase": "complete",
        "permalink": "unexpected",
        "snapshot": "unexpected",
    }
    result = receive_plan(
        {"text": answer, "refs": [{**ref, "file": 0}], "files": [descriptor]}, answer
    )
    assert result["files"] == [
        {
            "name": "report.txt",
            "digest": "0" * 64,
            "remote_snapshot": "a" * 64,
            "length": 7,
            "phase": "new",
            "image": False,
        }
    ]
