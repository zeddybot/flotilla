"""Conductor's native services are the sole agent-control path."""

import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .attachment import Attachment, frontend_version
from .client import Client
from .db import ConductorDB
from .journal import Unconfirmed
from .models import configuration, model_id
from .process import incarnation
from .transport import Transport

log = logging.getLogger(__name__)


@dataclass
class Watch:
    key: str | None = None
    retry_at: float = 0
    ready: bool = False


def identity(value):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, value))


class NativeDB(ConductorDB):
    def __init__(self, path, transport):
        super().__init__(path)
        self.transport = transport

    def session(self, sid):
        value = super().session(sid)
        runtime = self.transport.session(sid)
        if runtime is None:
            return {**value, "status": "reconnecting"}
        live = runtime["live"]
        # SQLite may reset to idle after reload although the native runner is busy.
        if value["status"] not in {
            "needs_user_input",
            "needs_user_permission",
            "needs_plan_response",
        }:
            value["status"] = (
                "working"
                if live.get("status") == "working"
                else "waiting"
                if live.get("backgroundTasks")
                else "idle"
            )
        return {
            **value,
            "runtime": {
                "instance_id": runtime["incarnation"],
                "paused": runtime.get("paused", False),
                "activity": live.get("status"),
                "jobs": [
                    {"id": t.get("taskId", t.get("id", t.get("task_id")))}
                    for t in live.get("backgroundTasks", [])
                ],
            },
        }

    def accepted(self, sid, mid):
        return bool(
            self.query("SELECT id FROM session_messages WHERE id=? AND session_id=?", (mid, sid))
            or self.query(
                "SELECT message_id FROM session_messages_outbox WHERE message_id=? AND session_id=?",
                (mid, sid),
            )
        )

    def pending(self, sid, prompt):
        if super().pending(sid, prompt):
            return True
        return self.transport.journal.pending_prompt(sid, prompt)


class Conductor:
    client_mode = False

    def __init__(self, config, *, client=False):
        self.config = config
        self.client_mode = client
        self.transport = (
            Client(config.state_dir)
            if client
            else Transport(
                config.state_dir, lambda current: incarnation() == current, frontend_version()
            )
        )
        self.db = NativeDB(config.conductor_db, self.transport)
        self.attachment = Attachment(self.transport)
        self.generation = None
        self.watches = {}

    def start(self, bindings=None):
        """Restore session ownership without knowing how the caller persists it."""
        self.transport.bindings.update(bindings or {})
        self.transport.start()
        self.attachment.start()

    def close(self):
        # The frontend stays installed: the next daemon adopts it, where a detached
        # one would cost a bootstrap that foregrounds Conductor.
        self.attachment.close()
        self.transport.rest()
        self.transport.close()

    def ready(self):
        if not self.transport.online():
            return False
        if self.client_mode:
            return True
        generation = self.transport.attachment["generation"]
        self.transport.journal.reconnect(generation, self.db)
        if generation != self.generation:
            self.watches.clear()
            self.generation = generation
        for sid, wid in list(self.transport.bindings.items()):
            watch = self.watches.setdefault(sid, Watch())
            if watch.ready or time.monotonic() < watch.retry_at:
                continue
            try:
                if self.db.workspace(wid)["state"] == "archived":
                    self.transport.bindings.pop(sid, None)
                    self.watches.pop(sid, None)
                    continue
                if watch.key is None:
                    watch.key = f"watch:{generation}:{sid}:{uuid.uuid4()}"
                self.transport.call(
                    {"op": "watch", "wid": wid, "sid": sid}, key=watch.key, timeout=1
                )
            except Unconfirmed:
                # Keep the same durable command while its outcome is unknown.
                watch.retry_at = time.monotonic() + 1
            except (RuntimeError, sqlite3.Error) as exc:
                # A settled error is cached in the journal. A later retry needs
                # a fresh identity, but must never change the session binding.
                watch.key = None
                watch.retry_at = time.monotonic() + 30
                log.warning("Conductor watch failed for session %s; retrying in 30s: %s", sid, exc)
            else:
                watch.ready = True
        return True

    def validate(self, wid, sid=None):
        workspace = self.db.workspace(wid)
        path = Path(workspace.get("workspace_path") or "/").resolve()
        repos = self.db.query("SELECT id FROM repos WHERE root_path=?", (str(self.config.repo),))
        if not path.is_relative_to(self.config.workspace_root.resolve()) or workspace.get(
            "repository_id"
        ) not in {r["id"] for r in repos}:
            raise RuntimeError("Refusing workspace outside configured repository")
        if workspace["state"] == "archived":
            raise RuntimeError("workspace archived; use [new]")
        if sid and ConductorDB.session(self.db, sid)["workspace_id"] != wid:
            raise RuntimeError("Conductor session does not belong to this workspace")
        return {**workspace, **({"active_session_id": sid} if sid else {})}

    def new_workspace(self, key=None):
        key = key or str(uuid.uuid4())
        wid = identity("workspace:" + key)
        repos = self.db.query("SELECT id FROM repos WHERE root_path=?", (str(self.config.repo),))
        if len(repos) != 1:
            raise RuntimeError("Configured repository must be registered in Conductor")
        args = {
            "localWorkspaceId": wid,
            "repositoryId": repos[0]["id"],
            "hostingServerUrl": "/",
            "model": model_id(self.config.model) or "fable-5-1",
            "thinkingLevel": "high",
            "fastMode": False,
        }
        self.transport.call({"op": "create", "wid": wid, "args": args}, key="create:" + wid)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            workspaces = self.db.query(
                "SELECT local_id AS id,* FROM workspaces WHERE local_id=?", (wid,)
            )
            sessions = self.db.query("SELECT * FROM sessions WHERE workspace_id=?", (wid,))
            if workspaces and workspaces[0]["state"] == "ready" and len(sessions) == 1:
                sid = sessions[0]["id"]
                if not workspaces[0]["active_session_id"]:
                    self.transport.call(
                        {"op": "createdSession", "wid": wid, "sid": sid},
                        key="created-session:" + wid,
                    )
                return self.validate(wid, sid)
            if len(sessions) > 1:
                raise RuntimeError("Workspace creation has ambiguous sessions")
            time.sleep(0.2)
        raise Unconfirmed("Workspace is still initializing; recovery will reuse it")

    def send(self, wid, sid, prompt, options, *, message_id=None):
        self.validate(wid, sid)
        self.transport.bindings[sid] = wid
        config, warning = configuration(options, self.config.model)
        if not self.db.has_user_messages(sid):
            try:
                self.transport.call(
                    {"op": "configure", "wid": wid, "sid": sid, "configuration": config},
                    key=f"configure:{sid}:{identity(json.dumps(config))}",
                )
            except Unconfirmed:
                raise
            except RuntimeError:
                fallback, _ = configuration(
                    {**options, "model": self.config.model, "effort": "high", "fast": False},
                    self.config.model,
                )
                self.transport.call(
                    {"op": "configure", "wid": wid, "sid": sid, "configuration": fallback},
                    key=f"default:{sid}",
                )
                warning = f"requested configuration unavailable; using default {self.config.model}"
        else:
            current = ConductorDB.session(self.db, sid)
            if current["model"] != config["model"]:
                warning = f"session model remains {current['model']}; use [new] to change it"
        mid = message_id or identity(sid + ":" + prompt)
        if not self.db.accepted(sid, mid):
            self.transport.call(
                {
                    "op": "send",
                    "wid": wid,
                    "sid": sid,
                    "message_id": mid,
                    "mode": options["send_mode"],
                    "prompt": prompt,
                },
                key="send:" + mid,
                timeout=12,
            )
        return warning

    def archive(self, wid, sid):
        if self.db.workspace(wid)["state"] != "archived":
            self.validate(wid, sid)
            self.transport.call({"op": "archive", "wid": wid, "sid": sid}, key="archive:" + wid)
        self.transport.bindings.pop(sid, None)
        return ""
