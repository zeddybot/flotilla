"""Durable headless runtime with one isolated worktree and process per session."""

import json
import subprocess
import threading
import uuid
from pathlib import Path

from ..remote_files import AssetSource
from ..runtime import RUNTIME_VERSION, Unsupported
from ..runtime_log import EventLog
from .execution import Execution
from .observations import observe as observe_view
from .process_host import terminate
from .store import Store


class HeadlessRuntime(Execution):
    def __init__(self, config):
        self.config = config
        self.state_dir = Path(config.state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.store = Store(self.state_dir / "headless.sqlite")
        self.events = EventLog(self.state_dir / "headless-events.sqlite")
        self.instance_id = str(uuid.uuid4())
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._workers = {}
        self._processes = {}
        self._lock = threading.RLock()

    def start(self, bindings):
        self.bind(bindings)
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            # A prior process might have accepted a prompt. Never replay it blindly.
            with self.store.connect() as db:
                db.execute(
                    "UPDATE turns SET state='attention' WHERE state IN ('launching','running')"
                )
                db.execute(
                    "UPDATE sessions SET activity='attention',attention_reason='ambiguous' WHERE id IN "
                    "(SELECT session_id FROM turns WHERE state='attention')"
                )
            self._stop.clear()
            self._thread = threading.Thread(target=self._dispatch_loop, daemon=True)
            self._thread.start()

    def bind(self, bindings):
        for session_id, workspace_id in bindings.items():
            row = self.store.session(session_id)
            if row is None or row["workspace_id"] != workspace_id:
                raise ValueError("Headless binding is not in the durable journal")

    def ready(self):
        return self._thread is not None and self._thread.is_alive()

    def shutdown(self):
        self._stop.set()
        self._wake.set()
        with self._lock:
            processes = list(self._processes.values())
        for process in processes:
            terminate(process)
        if self._thread:
            self._thread.join(timeout=5)
        for worker in list(self._workers.values()):
            worker.join(timeout=5)

    def files(self, operation, arguments):
        return AssetSource(self.state_dir, self).call(operation, arguments)

    def validate(self, workspace_id, session_id=None):
        row = next(
            (item for item in self.store.sessions() if item["workspace_id"] == workspace_id), None
        )
        if row is None or (session_id is not None and row["id"] != session_id):
            raise ValueError("Workspace is not bound to this runtime")
        if row["state"] == "archived":
            raise RuntimeError("workspace archived")
        return {"id": workspace_id, "state": row["state"], "workspace_path": row["path"]}

    def probe(self):
        return {
            "version": RUNTIME_VERSION,
            "adapter": "headless",
            "ready": self.ready(),
            "capabilities": {
                "create": True,
                "recover": True,
                "queue": True,
                "steer": False,
                "events": True,
                "files": True,
                "close_session": True,
                "interrupt": True,
                "reveal": False,
                "respond": False,
            },
            "configuration_policy": "session_start_fixed",
            "configuration": {
                "engines": ["claude", "codex"],
                "permission_policy": ["read-only", "workspace-write"],
            },
        }

    def sessions(self):
        return [self.describe(row["id"]) for row in self.store.sessions()]

    def _session(self, session_id):
        row = self.store.session(session_id)
        if row is None:
            raise ValueError("Session is not owned by this runtime")
        return row

    def describe(self, session_id):
        row = self._session(session_id)
        requested = json.loads(row["spec"])
        effective = json.loads(row["effective"]) if row["effective"] else None
        return {
            "session_id": row["id"],
            "workspace_id": row["workspace_id"],
            "adapter": "headless",
            "engine": row["engine"],
            "native_id": row["native_id"],
            "state": row["state"],
            "activity": row["activity"],
            "path": row["path"],
            "configuration": {"requested": requested, "effective": effective},
        }

    def create(self, command_id, spec):
        prior = self.store.command(command_id, "create", spec)
        if prior is not None:
            return prior
        repo = Path(spec.get("repo") or self.config.repo).expanduser().resolve()
        if repo != Path(self.config.repo).expanduser().resolve():
            raise ValueError("Repository must match configured repository")
        engine = spec.get("engine") or self.config.engine
        if engine not in {"claude", "codex"}:
            raise Unsupported("Headless engine must be claude or codex")
        if getattr(self.config, "engine_permission_policy", None) not in {
            "read-only",
            "workspace-write",
        }:
            raise ValueError("Headless permission policy must be explicit")
        policy = spec.get("permission_policy") or self.config.engine_permission_policy
        if policy not in {"read-only", "workspace-write"}:
            raise ValueError("Unsupported headless permission policy")
        if spec.get("fast"):
            raise Unsupported("Headless fast mode is unsupported")
        effective = {
            "engine": engine,
            "model": spec.get("model") or getattr(self.config, "engine_model", None),
            "effort": spec.get("effort") or getattr(self.config, "engine_effort", None),
            "permission_policy": policy,
        }
        sid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"headless:{command_id}"))
        workspace_id = sid
        native_id = str(uuid.uuid4()) if engine == "claude" else None
        path = Path(self.config.workspace_root) / "headless-sessions" / sid
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.exists():
            raise RuntimeError("Workspace exists without a completed headless journal entry")
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", "--detach", str(path), "HEAD"],
            check=True,
            capture_output=True,
        )
        value = {"session_id": sid, "workspace_id": workspace_id, "spec": effective}
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO sessions(id,workspace_id,path,engine,native_id,spec) VALUES (?,?,?,?,?,?)",
                (sid, workspace_id, str(path), engine, native_id, json.dumps(effective)),
            )
            self.store.record(db, command_id, "create", spec, value)
        return value

    def recover(self, workspace_id, session_id=None):
        workspace = self.validate(workspace_id, session_id)
        row = next(item for item in self.store.sessions() if item["workspace_id"] == workspace_id)
        if not Path(row["path"]).is_dir():
            raise RuntimeError("Headless workspace is missing")
        return {"workspace_id": workspace_id, "session_id": row["id"], "workspace": workspace}

    def submit(self, command_id, session_id, message_id, run_id, prompt, options):
        row = self._session(session_id)
        if row["state"] != "active":
            raise ValueError("Session is archived")
        if options.get("send_mode", "queue") != "queue":
            raise Unsupported("Headless steering is unsupported; use queue")
        fixed = json.loads(row["spec"])
        for key in ("model", "effort", "permission_policy"):
            if key in options and options[key] != fixed[key]:
                raise Unsupported(f"Headless {key} is fixed at session creation")
        arguments = {
            "session_id": session_id,
            "message_id": message_id,
            "run_id": run_id,
            "prompt": prompt,
            "options": options,
        }
        prior = self.store.command(command_id, "submit", arguments)
        if prior is not None:
            return prior
        value = {
            "command_id": command_id,
            "message_id": message_id,
            "run_id": run_id,
            "state": "queued",
            "warning": None,
        }
        with self.store.connect() as db:
            if row["activity"] == "attention":
                if row["attention_reason"] != "interrupted":
                    raise Unsupported("Session needs explicit recovery before accepting more work")
                db.execute(
                    "UPDATE sessions SET activity='idle',attention_reason=NULL WHERE id=?",
                    (session_id,),
                )
            old = db.execute("SELECT * FROM turns WHERE message_id=?", (message_id,)).fetchone()
            if old:
                if (
                    old["session_id"] != session_id
                    or old["run_id"] != run_id
                    or old["prompt"] != prompt
                ):
                    raise ValueError("Message identity reused with different arguments")
            else:
                db.execute(
                    "INSERT INTO turns(id,session_id,command_id,message_id,run_id,prompt) "
                    "VALUES (?,?,?,?,?,?)",
                    (str(uuid.uuid4()), session_id, command_id, message_id, run_id, prompt),
                )
            self.store.record(db, command_id, "submit", arguments, value)
        self._wake.set()
        return value

    def observe(self, session_id, after=0, prompt=None, segment_id=None, view="all", limit=200):
        return observe_view(self, session_id, after, prompt, segment_id, view, limit)

    def close_session(self, command_id, session_id):
        self._session(session_id)
        arguments = {"session_id": session_id}
        prior = self.store.command(command_id, "close_session", arguments)
        if prior is not None:
            return prior
        value = {"session_id": session_id, "state": "archived"}
        with self._lock:
            with self.store.connect() as db:
                db.execute(
                    "UPDATE sessions SET state='archived',activity='attention' WHERE id=?",
                    (session_id,),
                )
                db.execute(
                    "UPDATE turns SET state='cancelled' WHERE session_id=? AND state IN ('queued','launching')",
                    (session_id,),
                )
                self.store.record(db, command_id, "close_session", arguments, value)
            process = self._processes.get(session_id)
            if process:
                terminate(process)
        return value

    def interrupt(self, command_id, session_id):
        self._session(session_id)
        arguments = {"session_id": session_id}
        prior = self.store.command(command_id, "interrupt", arguments)
        if prior is not None:
            return prior
        value = {"session_id": session_id, "state": "interrupted"}
        with self._lock:
            with self.store.connect() as db:
                active = db.execute(
                    "SELECT 1 FROM turns WHERE session_id=? AND state IN ('launching','running')",
                    (session_id,),
                ).fetchone()
                if not active:
                    raise Unsupported("No active headless process to interrupt")
                db.execute(
                    "UPDATE sessions SET activity='attention',attention_reason='interrupted' WHERE id=?",
                    (session_id,),
                )
                db.execute(
                    "UPDATE turns SET state='cancelled' WHERE session_id=? "
                    "AND state IN ('queued','launching','running')",
                    (session_id,),
                )
                self.store.record(db, command_id, "interrupt", arguments, value)
            process = self._processes.get(session_id)
            if process:
                terminate(process)
        return value

    def reveal(self, command_id, session_id):
        self._session(session_id)
        raise Unsupported("Headless sessions have no qualified interactive attach")

    def respond(self, command_id, session_id, question_id, response):
        self._session(session_id)
        raise Unsupported("Headless approval responses are unsupported")
