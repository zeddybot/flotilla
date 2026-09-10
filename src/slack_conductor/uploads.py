"""Durable file snapshots and resumable Slack uploads, scoped to one final reply."""

import json
import os
import stat
from pathlib import Path
from urllib.request import Request, urlopen

from slack_sdk.errors import SlackApiError, SlackRequestError

from .file_links import local_path, references, substitute

MAX_BYTES = 25 * 1024 * 1024
MAX_FILES = 10


def upload_bytes(url, path):
    request = Request(
        url,
        data=Path(path).read_bytes(),
        method="POST",
        headers={"Content-Type": "application/octet-stream"},
    )
    with urlopen(request, timeout=15) as response:
        if response.status != 200:
            raise OSError("Slack rejected file bytes")


class Uploads:
    def __init__(self, store, slack, *, clock, send_bytes=upload_bytes):
        self.store, self.slack, self.clock, self.send_bytes = store, slack, clock, send_bytes

    def save(self, status, plan):
        self.store.update(status["id"], attachments=json.dumps(plan))

    def prepare(self, status, workspace):
        text = status["result"].replace("\r\n", "\n").replace("\r", "\n")
        plan = {"text": text, "refs": [], "files": []}
        paths, end = {}, 0
        for ref in references(text):
            if ref["start"] < end:
                continue
            try:
                path = local_path(ref["href"], workspace)
            except (ValueError, OSError) as exc:
                path, error = None, str(exc)
            else:
                error = ""
                if path is None:
                    continue
            key = str(path) if path else ref["href"]
            if key not in paths:
                index = len(plan["files"])
                item = {"name": path.name if path else ref["label"], "error": error}
                try:
                    if error:
                        raise ValueError(error)
                    if index >= MAX_FILES:
                        raise ValueError(f"maximum {MAX_FILES} files per reply")
                    # Nonblocking open prevents FIFOs/devices from stalling the daemon.
                    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                    with os.fdopen(fd, "rb") as source:
                        info = os.fstat(source.fileno())
                        if not stat.S_ISREG(info.st_mode):
                            raise ValueError("not a regular file")
                        if info.st_size > MAX_BYTES:
                            raise ValueError("file exceeds 25 MiB")
                        data = source.read(MAX_BYTES + 1)
                    if len(data) > MAX_BYTES:
                        raise ValueError("file exceeds 25 MiB")
                    directory = self.store.path.parent / "uploads" / str(status["id"])
                    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                    snapshot = directory / str(index)
                    snapshot.write_bytes(data)
                    snapshot.chmod(0o600)
                    item.update(
                        snapshot=str(snapshot),
                        length=len(data),
                        phase="new",
                        image=data.startswith(
                            (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")
                        ),
                    )
                except (OSError, ValueError) as exc:
                    item["error"] = exc.strerror if isinstance(exc, OSError) else str(exc)
                paths[key] = index
                plan["files"].append(item)
            plan["refs"].append(dict(ref, file=paths[key]))
            end = ref["end"]
        self.save(status, plan)
        return plan

    def step(self, status, plan):
        """Advance one network operation per poll; resume the same file after a crash."""
        pending = [f for f in plan["files"] if not f.get("permalink") and not f.get("error")]
        if not pending:
            return True
        item = pending[0]
        if self.clock() < item.get("retry_at", 0):
            return False
        try:
            phase = item["phase"]
            if phase == "new":
                response = self.slack.call(
                    "files_getUploadURLExternal", filename=item["name"], length=item["length"]
                )
                item.update(
                    file_id=response["file_id"], upload_url=response["upload_url"], phase="bytes"
                )
            elif phase == "bytes":
                self.send_bytes(item["upload_url"], item["snapshot"])
                item["phase"] = "complete"
            elif phase == "complete":
                # Persist ambiguity before a finalize call: recovery checks files.info
                # before attempting to finalize the same file again.
                item["phase"] = "confirm"
                self.save(status, plan)
                self.slack.call(
                    "files_completeUploadExternal",
                    files=[{"id": item["file_id"], "title": item["name"]}],
                )
            else:
                try:
                    info = self.slack.call("files_info", file=item["file_id"])["file"]
                except SlackApiError as exc:
                    if exc.response.get("error") not in {"file_not_found", "file_deleted"}:
                        raise
                    item["phase"] = "complete"
                    raise
                else:
                    item.update(
                        permalink=info["permalink"],
                        image=item.get("image")
                        or info.get("mimetype") in {"image/png", "image/jpeg", "image/gif"},
                    )
        except (SlackApiError, SlackRequestError, OSError, ValueError, KeyError) as exc:
            # Error messages must not expose signed upload URLs or token-bearing requests.
            reason = (
                exc.response.get("error") if isinstance(exc, SlackApiError) else type(exc).__name__
            )
            item["failures"] = item.get("failures", 0) + 1
            item["retry_at"] = self.clock() + min(60, 2 ** item["failures"])
            if item["failures"] >= 5:
                item["error"] = f"upload failed ({reason})"
        self.save(status, plan)
        return False

    def result(self, plan):
        text = substitute(plan["text"], plan["refs"], plan["files"])
        images = {
            f["permalink"]: f["file_id"]
            for f in plan["files"]
            if f.get("permalink") and f.get("image")
        }
        ids = [f["file_id"] for f in plan["files"] if f.get("permalink")]
        return text, images, ids

    def cleanup(self, status):
        plan = json.loads(status["attachments"])
        for item in plan.get("files", []):
            if item.get("snapshot"):
                Path(item["snapshot"]).unlink(missing_ok=True)
