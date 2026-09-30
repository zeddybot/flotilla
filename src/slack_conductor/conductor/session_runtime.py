"""Integrated Conductor implementation of the host-neutral session runtime."""

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from ..remote_journal import Receipts
from ..runtime import RUNTIME_VERSION, Unsupported
from .observations import Observations


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
            c.execute(
                "CREATE TABLE IF NOT EXISTS message_modes (id TEXT PRIMARY KEY, mode TEXT NOT NULL, requested_run TEXT NOT NULL)"
            )
        self.observations = Observations(self)
        # Import existing bindings without rewriting server-owned thread identities.
        remote = config.state_dir / "remote.sqlite"
        if remote.exists():
            with closing(sqlite3.connect(f"file:{remote}?mode=ro", uri=True)) as c:
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
                "interrupt": hasattr(self.native, "interrupt"),
                "reveal": hasattr(self.native, "reveal"),
                "respond": hasattr(self.native, "respond"),
            },
            "configuration_policy": "session_start_with_visible_fallback",
            "response_kinds": [
                "approval",
                "question",
                "plan",
                "codex_permission",
                "mcp_elicitation",
            ],
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
        value = {
            "session_id": session_id,
            "workspace_id": wid,
            "adapter": "conductor",
            "state": workspace["state"],
            "activity": session.get("status", "unknown"),
            "path": workspace.get("workspace_path"),
            "configuration": {k: session.get(k) for k in ("model", "thinking_level", "fast_mode")},
        }
        if hasattr(self.native, "questions") and self.native.ready():
            try:
                value["questions"] = self.native.questions(wid, session_id)
                value["questions_available"] = True
            except RuntimeError:
                value["questions"] = []
                value["questions_available"] = False
        return value

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

    def _pickup(self, sid, prompt, message_id=None):
        if message_id and hasattr(self.native.db, "pickup_message"):
            return self.native.db.pickup_message(sid, message_id)
        return self.native.db.pickup(sid, prompt)

    def _correlations(self, sid):
        with self.receipts.connect() as c:
            pending = list(
                c.execute(
                    "SELECT id,prompt FROM messages WHERE session_id=? AND segment_id IS NULL",
                    (sid,),
                )
            )
        for mid, prompt in pending:
            picked = self._pickup(sid, prompt, mid)
            if picked:
                with self.receipts.connect() as c:
                    mode = c.execute("SELECT mode FROM message_modes WHERE id=?", (mid,)).fetchone()
                    prior = c.execute(
                        "SELECT run_id FROM messages WHERE session_id=? AND segment_id=? ORDER BY rowid LIMIT 1",
                        (sid, picked["turn_id"]),
                    ).fetchone()
                    c.execute(
                        "UPDATE messages SET segment_id=? WHERE id=?", (picked["turn_id"], mid)
                    )
                    if prior and mode and mode[0] == "steer":
                        c.execute("UPDATE messages SET run_id=? WHERE id=?", (prior[0], mid))
        with self.receipts.connect() as c:
            return {
                r[0]: (r[1], r[2])
                for r in c.execute(
                    "SELECT segment_id,id,run_id FROM messages WHERE session_id=? AND segment_id IS NOT NULL ORDER BY rowid",
                    (sid,),
                )
            }

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
                "SELECT m.session_id,coalesce(d.requested_run,m.run_id),m.prompt FROM messages m "
                "LEFT JOIN message_modes d ON d.id=m.id WHERE m.id=?",
                (message_id,),
            ).fetchone()
            if existing and existing != (session_id, run_id, prompt):
                raise ValueError("Message identity reused with different arguments")
            c.execute(
                "INSERT OR IGNORE INTO messages VALUES (?,?,?,?,NULL)",
                (message_id, session_id, run_id, prompt),
            )
            c.execute(
                "INSERT OR IGNORE INTO message_modes VALUES (?,?,?)", (message_id, mode, run_id)
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
        changed = [
            key
            for key in ("model", "effort", "fast")
            if key in spec and key in options and spec[key] != options[key]
        ]
        if changed:
            warning = " ".join(
                filter(
                    None,
                    [
                        warning,
                        "Session configuration is fixed; use [new] to change "
                        + ", ".join(changed)
                        + ".",
                    ],
                )
            )
        picked = self._pickup(session_id, prompt, message_id)
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

    def observe(self, session_id, after=0, prompt=None, segment_id=None, view="all", limit=200):
        wid = self._workspace(session_id)
        if not isinstance(after, int) or after < 0:
            raise ValueError("after must be a nonnegative event cursor")
        if view not in {
            "all",
            "snapshot",
            "events",
            "workspace",
            "cursor",
            "receipt",
            "later_input",
            "latest_segment",
        }:
            raise ValueError("Unknown observation view")
        db = self.native.db
        # Views bound individual observations; full replay remains the default.
        if view in {"all", "snapshot", "events"}:
            self.observations.ingest(session_id)
        snapshot = self.observations.snapshot(session_id) if view in {"all", "snapshot"} else None
        page = (
            self.observations.log.page(session_id, after, limit)
            if view in {"all", "events"}
            else {}
        )
        events = page.get("events", [])
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
            "has_more": page.get("has_more", False)
            or (
                view in {"all", "events"}
                and self.observations.log.cursor(session_id) < db.cursor(session_id)
            ),
            "retained_after": page.get("retained_after", 0),
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
        saved = self.receipts.begin(command_id, "close_session", {"session_id": session_id})
        if saved is not None:
            return saved["value"]
        self.native.archive(wid, session_id)
        value = {"session_id": session_id, "state": "archived"}
        self.receipts.finish(command_id, {"value": value})
        return value

    def interrupt(self, command_id, session_id):
        return self._control("interrupt", command_id, session_id)

    def reveal(self, command_id, session_id):
        return self._control("reveal", command_id, session_id)

    def respond(self, command_id, session_id, question_id, response):
        return self._control(
            "respond", command_id, session_id, question_id=question_id, response=response
        )

    def _control(self, operation, command_id, session_id, **arguments):
        wid = self._workspace(session_id)
        method = getattr(self.native, operation, None)
        if method is None:
            raise Unsupported(f"Conductor {operation} is unsupported")
        saved = self.receipts.begin(command_id, operation, {"session_id": session_id, **arguments})
        if saved is not None:
            return saved["value"]
        result = method(wid, session_id, command_id=command_id, **arguments)
        value = {"session_id": session_id, **(result or {})}
        self.receipts.finish(command_id, {"value": value})
        return value
