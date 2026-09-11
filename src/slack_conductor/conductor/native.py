"""Conductor's native services are the sole agent-control path."""

import json
import time
import uuid
from pathlib import Path

from .attachment import Attachment, incarnation
from .db import ConductorDB
from .journal import Unconfirmed
from .models import configuration, model_id
from .transport import Transport


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
        return {**value, "runtime": runtime}

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
        with self.transport.journal.connect() as c:
            return bool(
                c.execute(
                    "SELECT 1 FROM commands WHERE state!='done' AND json_extract(body,'$.sid')=? AND json_extract(body,'$.prompt')=?",
                    (sid, prompt),
                ).fetchone()
            )


class Conductor:
    def __init__(self, config):
        self.config = config
        self.transport = Transport(config.state_dir, lambda current: incarnation() == current)
        self.db = NativeDB(config.conductor_db, self.transport)
        self.attachment = Attachment(self.transport, config.state_dir)
        self.generation = None
        self.store = None

    def start(self, store=None):
        self.store = store
        if store:
            self.transport.bindings.update(
                {
                    r["session_id"]: r["workspace_id"]
                    for r in store.rows("SELECT * FROM threads WHERE state!='archived'")
                }
            )
        self.transport.start()
        self.attachment.start()

    def close(self):
        self.attachment.close()
        try:
            if self.transport.online():
                self.transport.call({"op": "detach"}, timeout=3)
        except (RuntimeError, TimeoutError):
            pass
        finally:
            self.transport.close()

    def ready(self):
        if not self.transport.online():
            return False
        generation = self.transport.attachment["generation"]
        self.transport.journal.reconnect(generation, self.db)
        if generation != self.generation:
            for sid, wid in list(self.transport.bindings.items()):
                if self.db.workspace(wid)["state"] == "archived":
                    self.transport.bindings.pop(sid, None)
                    continue
                self.transport.call(
                    {"op": "watch", "wid": wid, "sid": sid}, key=f"watch:{generation}:{sid}"
                )
            self.generation = generation
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
        if sid and super(NativeDB, self.db).session(sid)["workspace_id"] != wid:
            raise RuntimeError("Conductor session does not belong to this workspace")
        return {**workspace, **({"active_session_id": sid} if sid else {})}

    def new_workspace(self, key=None):
        key = key or str(uuid.uuid4())
        wid = identity("workspace:" + key)
        repos = self.db.query("SELECT id FROM repos WHERE root_path=?", (str(self.config.repo),))
        if len(repos) != 1:
            raise RuntimeError("Configured repository must be registered in Conductor")
        args = {
            "workspaceId": wid,
            "repositoryId": repos[0]["id"],
            "hostingServerUrl": "/",
            "model": model_id(self.config.model) or "fable-5-1",
            "thinkingLevel": "high",
            "fastMode": False,
        }
        self.transport.call({"op": "create", "wid": wid, "args": args}, key="create:" + wid)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            workspaces = self.db.query("SELECT * FROM workspaces WHERE id=?", (wid,))
            sessions = self.db.query("SELECT * FROM sessions WHERE workspace_id=?", (wid,))
            if workspaces and workspaces[0]["state"] == "ready" and len(sessions) == 1:
                return self.validate(wid, sessions[0]["id"])
            if len(sessions) > 1:
                raise RuntimeError("Workspace creation has ambiguous sessions")
            time.sleep(0.2)
        raise Unconfirmed("Workspace is still initializing; recovery will reuse it")

    def send(self, wid, sid, prompt, options):
        self.validate(wid, sid)
        self.transport.bindings[sid] = wid
        config, warning = configuration(options, self.config.model)
        rows = self.db.query(
            "SELECT id FROM session_messages WHERE session_id=? AND role='user' LIMIT 1", (sid,)
        )
        if not rows:
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
            current = super(NativeDB, self.db).session(sid)
            if current["model"] != config["model"]:
                warning = f"session model remains {current['model']}; use [new] to change it"
        mid = identity(sid + ":" + prompt)
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
