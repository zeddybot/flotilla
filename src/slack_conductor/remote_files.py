"""Bounded, durable file snapshots served by the Mac client."""

import base64
import hashlib
import json
import os
from pathlib import Path

from .file_links import local_path, references
from .uploads import Uploads

CHUNK_BYTES = 256 * 1024


def _identity(key):
    if not isinstance(key, str) or not key:
        raise ValueError("file request key is required")
    return hashlib.sha256(key.encode()).hexdigest()


def _atomic_json(path, value):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o600)
    temporary.replace(path)


class _SnapshotStore:
    def __init__(self, path):
        self.path = path

    def update(self, *_args, **_kwargs):
        pass


class AssetSource:
    """The only file operations exposed by the Mac over the remote RPC channel."""

    def __init__(self, state_dir, conductor):
        self.state_dir = Path(state_dir) / "remote_assets"
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.conductor = conductor

    def _directory(self, key):
        return self.state_dir / _identity(key)

    def call(self, method, args):
        if method == "files.prepare":
            return self.prepare(args)
        if method == "files.chunk":
            return self.chunk(args)
        if method == "files.cleanup":
            return self.cleanup(args)
        raise ValueError("unknown file operation")

    def prepare(self, args):
        key = args["key"]
        status = args["status"]
        if status["event_key"] != key:
            raise ValueError("file request key does not match reply")
        workspace_id, session_id = args["workspace_id"], args["session_id"]
        directory = self._directory(key)
        manifest = directory / "plan.json"
        if (directory / "cleaned.json").exists():
            raise ValueError("file snapshots already cleaned for this reply")
        if manifest.exists():
            saved = json.loads(manifest.read_text())
            if (saved["workspace_id"], saved["session_id"], saved["result"]) != (
                workspace_id,
                session_id,
                status["result"],
            ):
                raise ValueError("file request key reused for a different reply")
            return saved["public"]
        try:
            workspace = self.conductor.validate(workspace_id, session_id)
        except RuntimeError as exc:
            if "workspace archived" not in str(exc):
                raise
            # An archived workspace cannot safely yield a new snapshot. Keep the
            # answer deliverable and mark each local attachment unavailable.
            workspace = None
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        local_status = {"id": _identity(key), "result": status["result"]}
        if workspace is None:
            plan = self._archived_plan(status["result"])
        else:
            # Reuse the current link parser and safe, bounded snapshot implementation.
            local = Uploads(_SnapshotStore(self.state_dir / "source.sqlite"), None, clock=lambda: 0)
            plan = local.prepare(local_status, workspace.get("workspace_path"))
        public = {"text": plan["text"], "refs": plan["refs"], "files": []}
        for index, item in enumerate(plan["files"]):
            exported = {name: value for name, value in item.items() if name != "snapshot"}
            if item.get("snapshot"):
                snapshot = Path(item["snapshot"])
                exported["remote_snapshot"] = hashlib.sha256(
                    f"{_identity(key)}:{index}".encode()
                ).hexdigest()
                exported["digest"] = hashlib.sha256(snapshot.read_bytes()).hexdigest()
            public["files"].append(exported)
        _atomic_json(
            manifest,
            {
                "workspace_id": workspace_id,
                "session_id": session_id,
                "result": status["result"],
                "local": plan,
                "public": public,
            },
        )
        return public

    @staticmethod
    def _archived_plan(text):
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        plan = {"text": text, "refs": [], "files": []}
        paths, end = {}, 0
        for ref in references(text):
            if ref["start"] < end:
                continue
            try:
                path = local_path(ref["href"], "/")
            except (ValueError, OSError):
                path = None
            if path is None:
                continue
            key = str(path)
            if key not in paths:
                paths[key] = len(plan["files"])
                plan["files"].append(
                    {"name": path.name, "error": "workspace archived before file snapshot"}
                )
            plan["refs"].append(dict(ref, file=paths[key]))
            end = ref["end"]
        return plan

    def chunk(self, args):
        directory = self._directory(args["key"])
        saved = json.loads((directory / "plan.json").read_text())
        snapshot_id = args["snapshot"]
        offset = args["offset"]
        if not isinstance(offset, int) or offset < 0:
            raise ValueError("invalid file offset")
        for item, exported in zip(saved["local"]["files"], saved["public"]["files"]):
            if exported.get("remote_snapshot") != snapshot_id:
                continue
            path = Path(item["snapshot"])
            length = item["length"]
            if offset > length:
                raise ValueError("file offset exceeds snapshot")
            with path.open("rb") as stream:
                stream.seek(offset)
                data = stream.read(CHUNK_BYTES)
            return {"data": base64.b64encode(data).decode("ascii"), "length": length}
        raise ValueError("unknown file snapshot")

    def cleanup(self, args):
        directory = self._directory(args["key"])
        manifest = directory / "plan.json"
        if not manifest.exists():
            return {"ok": True}
        saved = json.loads(manifest.read_text())
        _atomic_json(directory / "cleaned.json", {"cleaned": True})
        for item in saved["local"]["files"]:
            if item.get("snapshot"):
                Path(item["snapshot"]).unlink(missing_ok=True)
        manifest.unlink(missing_ok=True)
        return {"ok": True}
