"""Integrated Conductor implementation of the host-neutral session runtime."""

import json
import sqlite3
import time
from pathlib import Path

from ..remote_journal import Receipts
from ..runtime import RUNTIME_VERSION, Unsupported
from .events import normalize


class ConductorRuntime:
    def __init__(self, config, native):
        self.config, self.native = config, native
        self.receipts = Receipts(config.state_dir / "runtime.sqlite")
        with self.receipts.connect() as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS session_specs (session_id TEXT PRIMARY KEY, spec TEXT NOT NULL)"
            )
            c.execute(
                "CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, run_id TEXT NOT NULL, prompt TEXT NOT NULL, segment_id TEXT)"
            )
        # Import existing bindings without rewriting server-owned thread identities.
        remote = config.state_dir / "remote.sqlite"
        if remote.exists():
            with sqlite3.connect(f"file:{remote}?mode=ro", uri=True) as c:
                self.receipts.bind(dict(c.execute("SELECT session_id,workspace_id FROM bindings")))

    def start(self, bindings):
        self.bind(bindings)
        self.native.start(self.receipts.bindings())

    def bind(self, bindings):
        self.receipts.bind(bindings)
        self.native.transport.bindings.update(bindings)

    def ready(self):
        return self.native.ready()

    def shutdown(self):
        self.native.close()

    def files(self, operation, arguments):
        from ..remote_files import AssetSource

        return AssetSource(self.config.state_dir, self.native).call(operation, arguments)

    def probe(self):
        return {
            "version": RUNTIME_VERSION,
            "adapter": "conductor",
            "ready": self.native.ready(),
            "capabilities": {
                "create": True,
                "recover": True,
                "queue": True,
                "steer": True,
                "events": True,
                "files": True,
                "close_session": True,
                "interrupt": False,
                "reveal": False,
                "respond": False,
            },
            "configuration_policy": "session_start_with_visible_fallback",
            "configuration": {
                "models": list(self.config.models.values()),
                "effort": True,
                "fast": True,
            },
        }

    def _workspace(self, session_id):
        wid = self.receipts.bindings().get(session_id)
        if not wid:
            raise ValueError("Session is not owned by this client")
        return wid

    def sessions(self):
        values = []
        for sid in self.receipts.bindings():
            try:
                values.append(self.describe(sid))
            except RuntimeError:
                continue
        return values

    def describe(self, session_id):
        wid = self._workspace(session_id)
        workspace, session = self.native.db.workspace(wid), self.native.db.session(session_id)
        return {
            "session_id": session_id,
            "workspace_id": wid,
            "adapter": "conductor",
            "state": workspace["state"],
            "activity": session.get("status", "unknown"),
            "path": workspace.get("workspace_path"),
            "configuration": {k: session.get(k) for k in ("model", "thinking_level", "fast_mode")},
        }

    def create(self, command_id, spec):
        if spec.get("repo") and Path(spec["repo"]).resolve() != self.config.repo.resolve():
            raise ValueError("repo must match the configured repository")
        saved = self.receipts.begin(command_id, "create", spec)
        if saved is not None:
            return saved["value"]
        workspace = self.native.new_workspace(command_id)
        sid, wid = workspace["active_session_id"], workspace["id"]
        self.receipts.bind({sid: wid})
        with self.receipts.connect() as c:
            c.execute("INSERT OR IGNORE INTO session_specs VALUES (?,?)", (sid, json.dumps(spec)))
        value = {"session_id": sid, "workspace_id": wid, "spec": spec}
        self.receipts.finish(command_id, {"value": value})
        return value

    def recover(self, workspace_id, session_id=None):
        workspace = self.native.validate(workspace_id, session_id)
        sid = workspace["active_session_id"]
        self.receipts.bind({sid: workspace_id})
        return {"workspace_id": workspace_id, "session_id": sid}

    def submit(self, command_id, session_id, message_id, run_id, prompt, options):
        wid = self._workspace(session_id)
        args = {
            "session_id": session_id,
            "message_id": message_id,
            "run_id": run_id,
            "prompt": prompt,
            "options": options,
        }
        saved = self.receipts.begin(command_id, "submit", args)
        if saved is not None:
            return saved["value"]
        mode = options.get("send_mode", "steer")
        if mode not in {"queue", "steer"}:
            raise Unsupported(f"Unsupported delivery mode: {mode}")
        with self.receipts.connect() as c:
            row = c.execute(
                "SELECT spec FROM session_specs WHERE session_id=?", (session_id,)
            ).fetchone()
            spec = json.loads(row[0]) if row else {}
            existing = c.execute(
                "SELECT session_id,run_id,prompt FROM messages WHERE id=?", (message_id,)
            ).fetchone()
            if existing and existing != (session_id, run_id, prompt):
                raise ValueError("Message identity reused with different arguments")
            c.execute(
                "INSERT OR IGNORE INTO messages VALUES (?,?,?,?,NULL)",
                (message_id, session_id, run_id, prompt),
            )
        settings = {
            "model": self.config.model,
            "effort": self.config.effort,
            "fast": False,
            **options,
            **spec,
            "send_mode": mode,
        }
        warning = self.native.send(wid, session_id, prompt, settings, message_id=message_id)
        picked = self.native.db.pickup(session_id, prompt)
        if picked:
            with self.receipts.connect() as c:
                prior = c.execute(
                    "SELECT run_id FROM messages WHERE session_id=? AND segment_id=? LIMIT 1",
                    (session_id, picked["turn_id"]),
                ).fetchone()
                if prior and mode == "steer":
                    run_id = prior[0]
                c.execute(
                    "UPDATE messages SET segment_id=?,run_id=? WHERE id=?",
                    (picked["turn_id"], run_id, message_id),
                )
        value = {
            "command_id": command_id,
            "message_id": message_id,
            "run_id": run_id,
            "state": "picked_up" if picked else "queued",
            "warning": warning,
        }
        self.receipts.finish(command_id, {"value": value})
        return value

    def observe(self, session_id, after=0, prompt=None, segment_id=None, view="all"):
        wid = self._workspace(session_id)
        if not isinstance(after, int) or after < 0:
            raise ValueError("after must be a nonnegative event cursor")
        db = self.native.db
        # Views bound individual observations; full replay remains the default.
        snapshot = db.session(session_id) if view in {"all", "snapshot"} else None
        events = (
            [normalize(row) for row in db.messages(session_id, after)]
            if view in {"all", "events"}
            else []
        )
        if snapshot is not None:
            snapshot = {k: snapshot[k] for k in ("id", "status", "runtime") if k in snapshot}
        with self.receipts.connect() as c:
            correlations = {
                r[0]: (r[1], r[2])
                for r in c.execute(
                    "SELECT segment_id,id,run_id FROM messages WHERE session_id=? AND segment_id IS NOT NULL",
                    (session_id,),
                )
            }
        for event in events:
            message_id, run_id = correlations.get(event["turn_id"], (None, None))
            event.update(message_id=message_id, run_id=run_id)
        picked = db.pickup(session_id, prompt) if prompt is not None else None
        receipt = (
            None
            if prompt is None
            else {
                "state": "picked_up"
                if picked
                else "queued"
                if db.pending(session_id, prompt)
                else "received",
                "seq": picked["seq"] if picked else None,
                "segment_id": picked["turn_id"] if picked else None,
            }
        )
        return {
            "version": RUNTIME_VERSION,
            "session_id": session_id,
            "observed_at": time.time(),
            "snapshot": snapshot,
            "events": events,
            "workspace": {
                k: v for k, v in db.workspace(wid).items() if k in {"id", "state", "workspace_path"}
            }
            if view == "workspace"
            else None,
            "cursor": db.cursor(session_id)
            if view == "cursor"
            else (events[-1]["seq"] if events else after),
            "receipt": receipt,
            "later_input": db.has_later_turn(session_id, after, segment_id)
            if view == "later_input"
            else None,
            "latest_segment": db.latest_turn(session_id) if view == "latest_segment" else None,
        }

    def close_session(self, command_id, session_id):
        wid = self._workspace(session_id)
        self.native.archive(wid, session_id)
        return {"session_id": session_id, "state": "archived"}

    def interrupt(self, command_id, session_id):
        self._workspace(session_id)
        raise Unsupported("Conductor interruption is not qualified; stop the agent in Conductor")

    def reveal(self, command_id, session_id):
        self._workspace(session_id)
        raise Unsupported(
            "Conductor reveal is not qualified; open the existing session in Conductor"
        )

    def respond(self, command_id, session_id, question_id, response):
        self._workspace(session_id)
        raise Unsupported("Answer questions and approvals in Conductor")
