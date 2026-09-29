import base64

import pytest

from slack_conductor.remote_files import AssetSource


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


def test_source_freezes_deduplicated_snapshot_and_replays_after_restart(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    image = workspace / "plot.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\noriginal")
    conductor = Conductor(workspace)
    source = AssetSource(tmp_path / "state", conductor)
    args = request("event:1", "![plot](plot.png) and [again](./plot.png)")
    plan = source.call("files.prepare", args)
    assert len(plan["files"]) == 1
    assert plan["files"][0]["image"]
    assert "snapshot" not in plan["files"][0]
    assert plan["files"][0]["remote_snapshot"]
    image.unlink()
    source = AssetSource(tmp_path / "state", conductor)
    assert source.call("files.prepare", args) == plan
    chunk = source.call(
        "files.chunk",
        {"key": "event:1", "snapshot": plan["files"][0]["remote_snapshot"], "offset": 0},
    )
    assert base64.b64decode(chunk["data"]) == b"\x89PNG\r\n\x1a\noriginal"
    with pytest.raises(ValueError, match="unknown file snapshot"):
        source.call("files.chunk", {"key": "event:1", "snapshot": "other", "offset": 0})
    with pytest.raises(ValueError, match="different reply"):
        source.call("files.prepare", request("event:1", "different"))
    source.call("files.cleanup", {"key": "event:1"})
    source.call("files.cleanup", {"key": "event:1"})
    with pytest.raises(ValueError, match="already cleaned"):
        source.call("files.prepare", args)
    with pytest.raises(FileNotFoundError):
        source.call(
            "files.chunk",
            {"key": "event:1", "snapshot": plan["files"][0]["remote_snapshot"], "offset": 0},
        )


def test_invalid_binding_prevents_snapshot(tmp_path):
    source = AssetSource(tmp_path / "state", Conductor(tmp_path))
    with pytest.raises(ValueError, match="invalid binding"):
        source.call(
            "files.prepare", dict(request("event:1", "[x](secret.txt)"), session_id="wrong")
        )
    assert not list((tmp_path / "state" / "remote_assets").glob("*/plan.json"))


def test_archived_workspace_preserves_answer_and_existing_snapshots(tmp_path):
    class Archived(Conductor):
        archived = False

        def validate(self, workspace_id, session_id):
            result = super().validate(workspace_id, session_id)
            if self.archived:
                raise RuntimeError("workspace archived; use [new]")
            return result

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "ready.txt").write_text("frozen")
    conductor = Archived(workspace)
    source = AssetSource(tmp_path / "state", conductor)
    ready = request("ready", "[file](ready.txt)")
    plan = source.call("files.prepare", ready)
    conductor.archived = True
    assert source.call("files.prepare", ready) == plan
    chunk = source.call(
        "files.chunk",
        {"key": "ready", "snapshot": plan["files"][0]["remote_snapshot"], "offset": 0},
    )
    assert base64.b64decode(chunk["data"]) == b"frozen"
    unavailable = source.call(
        "files.prepare", request("late", "Answer. [missing](ready.txt) [site](https://example.com)")
    )
    assert len(unavailable["files"]) == 1
    assert unavailable["files"][0]["error"] == "workspace archived before file snapshot"
    assert "remote_snapshot" not in unavailable["files"][0]
    assert unavailable["text"].startswith("Answer.")
