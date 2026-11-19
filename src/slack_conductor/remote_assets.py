"""Server-side resumable transfer and Slack upload of remote file snapshots."""

import base64
import binascii
import hashlib
import json
import logging
import os
import re

from . import remote_files
from .conductor.journal import Unconfirmed
from .file_links import references
from .remote_files import CHUNK_BYTES
from .uploads import MAX_BYTES, MAX_FILES, Uploads

AssetSource = remote_files.AssetSource
log = logging.getLogger(__name__)


def receive_plan(reply, content):
    """Build server-owned upload state from the client's attachment descriptors."""
    text = content.replace("\r\n", "\n").replace("\r", "\n")
    if not isinstance(reply, dict) or reply.get("text") != text:
        raise ValueError("file manifest does not match the answer")
    spans = {(r["start"], r["end"]): r for r in references(text)}
    refs, files = [], []
    if not isinstance(reply.get("files"), list) or len(reply["files"]) > len(spans):
        raise ValueError("unexpected number of attachment descriptors")
    for index, item in enumerate(reply["files"]):
        name = item["name"]
        if not isinstance(name, str) or not name:
            raise ValueError("invalid attachment name")
        error = item.get("error")
        if isinstance(error, str) and error:
            files.append({"name": name, "error": error})
            continue
        length = item["length"]
        if index >= MAX_FILES or type(length) is not int or not 0 <= length <= MAX_BYTES:
            raise ValueError("invalid attachment size")
        if not all(
            re.fullmatch(r"[a-f0-9]{64}", item[key]) for key in ("digest", "remote_snapshot")
        ):
            raise ValueError("invalid attachment identity")
        files.append(
            {
                "name": name,
                "length": length,
                "phase": "new",
                "image": item.get("image") is True,
                "digest": item["digest"],
                "remote_snapshot": item["remote_snapshot"],
            }
        )
    end = 0
    for ref in reply["refs"]:
        original = spans[(ref["start"], ref["end"])]
        index = ref["file"]
        if type(index) is not int or not 0 <= index < len(files) or original["start"] < end:
            raise ValueError("invalid attachment reference")
        refs.append({**original, "file": index})
        end = original["end"]
    if {ref["file"] for ref in refs} != set(range(len(files))):
        raise ValueError("unreferenced attachment")
    return {"text": text, "refs": refs, "files": files}


class RemoteUploads(Uploads):
    def __init__(self, store, slack, *, clock, rpc, send_bytes=None):
        kwargs = {"clock": clock}
        if send_bytes is not None:
            kwargs["send_bytes"] = send_bytes
        super().__init__(store, slack, **kwargs)
        self.rpc = rpc
        with self.store.connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS remote_asset_cleanup ("
                "event_key TEXT PRIMARY KEY, retry_at REAL NOT NULL DEFAULT 0, "
                "attempts INTEGER NOT NULL DEFAULT 0)"
            )

    def prepare(self, status, workspace=None):
        key = status["event_key"]
        try:
            plan = self.rpc(
                "files.prepare",
                {
                    "key": key,
                    "status": {"id": status["id"], "result": status["result"], "event_key": key},
                    "workspace_id": status["workspace_id"],
                    "session_id": status["session_id"],
                },
                key=f"files.prepare:{key}",
            )
        except (OSError, TimeoutError, ConnectionError) as exc:
            raise Unconfirmed("Mac client unavailable while preparing files") from exc
        try:
            plan = receive_plan(plan, status["result"])
        except (KeyError, TypeError, ValueError):
            # Deliver the original text even if the client returned an invalid manifest.
            plan = {
                "text": status["result"],
                "refs": [],
                "files": [{"name": "attachments", "error": "invalid attachment manifest"}],
            }
        self.save(status, plan)
        return plan

    def step(self, status, plan):
        for index, item in enumerate(plan["files"]):
            if item.get("remote_snapshot") and not item.get("snapshot") and not item.get("error"):
                try:
                    self._download(status, plan, index, item)
                except Unconfirmed:
                    # A sleeping or disconnected Mac can reconnect at any time.
                    raise
                except (
                    OSError,
                    RuntimeError,
                    ValueError,
                    KeyError,
                    TypeError,
                    binascii.Error,
                ) as exc:
                    failures = item.get("transfer_failures", 0) + 1
                    item["transfer_failures"] = failures
                    if failures >= 5:
                        reason = (
                            "source unavailable"
                            if isinstance(exc, (OSError, RuntimeError))
                            else "integrity check failed"
                        )
                        item["error"] = f"file transfer failed ({reason})"
                    self.save(status, plan)
                return False
        return super().step(status, plan)

    def _download(self, status, plan, index, item):
        directory = self.store.path.parent / "uploads" / str(status["id"])
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        part = directory / f"{index}.part"
        offset = part.stat().st_size if part.exists() else 0
        length = item["length"]
        if offset > length:
            part.unlink()
            offset = 0
        if offset < length:
            try:
                response = self.rpc(
                    "files.chunk",
                    {
                        "key": status["event_key"],
                        "snapshot": item["remote_snapshot"],
                        "offset": offset,
                    },
                    key=f"files.chunk:{status['event_key']}:{index}:{offset}",
                )
            except (OSError, TimeoutError, ConnectionError) as exc:
                raise Unconfirmed("Mac client unavailable while transferring files") from exc
            data = base64.b64decode(response["data"], validate=True)
            if (
                response["length"] != length
                or not data
                or len(data) > CHUNK_BYTES
                or offset + len(data) > length
            ):
                raise ValueError("invalid remote file chunk")
            with part.open("ab") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            part.chmod(0o600)
            return
        if not part.exists():
            part.touch(mode=0o600)
        digest = hashlib.sha256(part.read_bytes()).hexdigest()
        if digest != item["digest"]:
            part.unlink(missing_ok=True)
            raise ValueError("remote file digest mismatch")
        snapshot = directory / str(index)
        part.replace(snapshot)
        item["snapshot"] = str(snapshot)
        self.save(status, plan)

    def cleanup(self, status):
        plan = json.loads(status["attachments"])
        if plan:
            with self.store.connect() as connection:
                connection.execute(
                    "INSERT OR IGNORE INTO remote_asset_cleanup(event_key) VALUES (?)",
                    (status["event_key"],),
                )
        super().cleanup(status)
        for index, item in enumerate(plan.get("files", [])):
            if item.get("remote_snapshot"):
                (self.store.path.parent / "uploads" / str(status["id"]) / f"{index}.part").unlink(
                    missing_ok=True
                )
        self.maintenance()

    def maintenance(self):
        """Retry one saved client cleanup; completed Slack replies never wait on it."""
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT event_key, attempts FROM remote_asset_cleanup WHERE retry_at<=? "
                "ORDER BY retry_at, event_key LIMIT 1",
                (self.clock(),),
            ).fetchone()
        if not row:
            return
        key = row["event_key"]
        try:
            self.rpc("files.cleanup", {"key": key}, key=f"files.cleanup:{key}")
        except (OSError, TimeoutError, ConnectionError, RuntimeError) as exc:
            attempts = row["attempts"] + 1
            with self.store.connect() as connection:
                connection.execute(
                    "UPDATE remote_asset_cleanup SET attempts=?, retry_at=? WHERE event_key=?",
                    (attempts, self.clock() + min(60, 2 ** min(attempts, 6)), key),
                )
            log.warning("Remote file cleanup pending for %s: %s", key, type(exc).__name__)
        else:
            with self.store.connect() as connection:
                connection.execute("DELETE FROM remote_asset_cleanup WHERE event_key=?", (key,))
