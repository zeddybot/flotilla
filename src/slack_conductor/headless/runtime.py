"""Durable headless runtime with one isolated worktree and process per session."""

import json
import shutil
import subprocess
import threading
import uuid
from functools import wraps
from pathlib import Path

from ..remote_files import AssetSource
from ..runtime import RUNTIME_VERSION, Unsupported
from ..runtime_log import CursorExpired, EventLog
from .engines import CLAUDE_EFFORTS
from .execution import TERMINAL_JOB_STATES, Execution
from .observations import observe as observe_view
from .permissions import within_policy
from .process_host import Rejected, terminate
from .store import Store

STEERING_UNCERTAIN = "The engine did not confirm steering. Check the active turn before resending."


def serialized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return call


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
        self._active_turns = {}
        self._draining = set()
        self._pending_steers = {}
        self._lock = threading.RLock()

    def start(self, bindings):
        self.bind(bindings)
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._recover_background_jobs()
            # A prior process might have accepted a prompt. Never replay it blindly.
            with self.store.connect() as db:
                interrupted_steers = db.execute(
                    "SELECT commands.id, commands.result FROM commands JOIN turns "
                    "ON turns.command_id=commands.id WHERE turns.state='steering'"
                ).fetchall()
                for command in interrupted_steers:
                    value = json.loads(command["result"])
                    value.update(state="uncertain", warning=STEERING_UNCERTAIN)
                    db.execute(
                        "UPDATE commands SET result=? WHERE id=?",
                        (json.dumps(value), command["id"]),
                    )
                db.execute("UPDATE turns SET state='uncertain' WHERE state='steering'")
                db.execute(
                    "UPDATE sessions SET activity='attention',attention_reason='ambiguous' WHERE id IN "
                    "(SELECT session_id FROM turns WHERE state IN ('launching','running'))"
                )
                db.execute(
                    "UPDATE turns SET state='attention' WHERE state IN ('launching','running')"
                )
            self._stop.clear()
            self._thread = threading.Thread(target=self._dispatch_loop, daemon=True)
            self._thread.start()

    def _recover_background_jobs(self):
        # Older workers killed Claude after its first result and then treated
        # orphaned background jobs as equivalent to an unanswered question.
        with self.store.connect() as db:
            candidates = db.execute(
                "SELECT turns.* FROM turns JOIN sessions ON sessions.id=turns.session_id "
                "WHERE sessions.state='active' AND sessions.engine='claude' "
                "AND sessions.native_id IS NOT NULL "
                "AND ((sessions.attention_reason='question_or_job' AND turns.state='attention') "
                "OR (sessions.activity='working' AND turns.state='running')) "
                "AND turns.pickup_seq IS NOT NULL AND turns.result_seq IS NOT NULL "
                "AND turns.pickup_seq=(SELECT max(previous.pickup_seq) FROM turns previous "
                "WHERE previous.session_id=sessions.id AND previous.segment_id IS NULL)"
            ).fetchall()
        for turn in candidates:
            self._recover_background_turn(turn)

    def _recover_background_turn(self, turn):
        sid = turn["session_id"]
        with self.store.connect() as db:
            jobs = {
                row["id"]
                for row in db.execute(
                    "SELECT id FROM jobs WHERE session_id=? AND state='active'", (sid,)
                )
            }
        after = turn["pickup_seq"] - 1
        started = set()
        finished = set()
        result = None
        last_result_seq = 0
        last_job_seq = 0
        last_input_seq = 0
        recovered = False
        outcome_unknown = False
        while True:
            try:
                page = self.events.page(sid, after)
            except CursorExpired:
                return
            for row in page["events"]:
                if row["turn_id"] != turn["id"]:
                    continue
                event = row["event"]
                if any(part.get("kind") == "question" for part in event.get("parts", ())):
                    return
                if event["kind"] == "input":
                    last_input_seq = row["seq"]
                if event["kind"] == "job_started":
                    started.add(event.get("job_id"))
                    last_job_seq = row["seq"]
                if event["kind"] == "job_finished" and event.get("status") in TERMINAL_JOB_STATES:
                    last_job_seq = row["seq"]
                    finished.add(event.get("job_id"))
                    outcome_unknown |= bool(event.get("outcome_unknown"))
                if event["kind"] == "result":
                    result = event
                    last_result_seq = row["seq"]
                    recovered = bool(event.get("background_interrupted"))
            if not page["has_more"]:
                break
            after = page["cursor"]
        if not result or not started or not jobs.issubset(started):
            return
        jobs = started - finished
        failed = bool(result.get("error"))
        if not recovered and (
            jobs or result.get("provisional") or max(last_job_seq, last_input_seq) > last_result_seq
        ):
            for job_id in sorted(jobs):
                self._record_interrupted_job(turn, job_id)
            detail = (
                "The previous worker lost contact with Claude's background work. Its outcome is unknown."
                if jobs or outcome_unknown
                else "The previous worker lost contact with Claude before it returned the follow-up answer."
            )
            answer = result.get("text")
            self._event(
                turn,
                "assistant",
                {
                    "kind": "result",
                    "error": True,
                    "background_interrupted": True,
                    "text": f"{answer}\n\n{detail}" if answer else detail,
                },
            )
            failed = True
        with self.store.connect() as db:
            db.execute(
                "UPDATE jobs SET state='finished' WHERE session_id=? AND state='active'", (sid,)
            )
            db.execute(
                "UPDATE turns SET state=? WHERE id=?", ("failed" if failed else "done", turn["id"])
            )
            db.execute(
                "UPDATE sessions SET activity='idle',attention_reason=NULL WHERE id=?", (sid,)
            )

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
                "steer": True,
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
                "default_engine": self.config.engine,
                "default_send_mode": getattr(self.config, "send_mode", "queue"),
                "engine_capabilities": {
                    "claude": {"queue": True, "steer": True},
                    "codex": {"queue": True, "steer": True},
                },
                "engine_efforts": {"claude": list(CLAUDE_EFFORTS)},
                "permission_policy": ["read-only", "workspace-write", "external-sandbox"],
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
            "capabilities": {"queue": True, "steer": True},
            "native_id": row["native_id"],
            "state": row["state"],
            "activity": row["activity"],
            "path": row["path"],
            "configuration": {"requested": requested, "effective": effective},
        }

    @serialized
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
        if not shutil.which(str(getattr(self.config, engine + "_binary"))):
            raise ValueError(f"The configured {engine} executable is unavailable")
        policy = spec.get("permission_policy") or self.config.engine_permission_policy
        within_policy(policy, self.config.engine_permission_policy)
        if spec.get("fast"):
            raise Unsupported("Headless fast mode is unsupported")
        default_model = default_effort = None
        if engine == self.config.engine:
            default_model = getattr(self.config, "engine_model", None) or None
            default_effort = getattr(self.config, "engine_effort", None) or None
        effective = {
            "engine": engine,
            "model": spec.get("model") or default_model,
            "effort": spec.get("effort") or default_effort,
            "permission_policy": policy,
        }
        if engine == "claude" and effective["effort"] not in (None, *CLAUDE_EFFORTS):
            raise Unsupported("Claude effort must be low, medium, high, xhigh or max")
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

    @serialized
    def submit(self, command_id, session_id, message_id, run_id, prompt, options):
        row = self._session(session_id)
        if row["state"] != "active":
            raise ValueError("Session is archived")
        mode = options.get("send_mode")
        if mode is None:
            mode = "queue"
        if mode not in {"queue", "steer"}:
            raise Unsupported(f"Unsupported delivery mode: {mode}")
        fixed = json.loads(row["spec"])
        for key in ("model", "effort", "permission_policy"):
            if key not in options or (key != "permission_policy" and options[key] in (None, "")):
                continue
            if options[key] != fixed[key]:
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
            if prior["state"] == "rejected":
                raise Unsupported(prior["warning"])
            return prior
        if mode == "steer" and session_id not in self._draining:
            if row["activity"] == "working" and session_id in self._active_turns:
                return self._steer(command_id, arguments)
            if row["activity"] != "idle":
                raise Unsupported("The headless turn is not ready for steering")
            with self.store.connect() as db:
                queued = db.execute(
                    "SELECT 1 FROM turns WHERE session_id=? AND state='queued'", (session_id,)
                ).fetchone()
            if queued:
                raise Unsupported("The queued turn has not started; try steering once it starts")
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
                    old["segment_id"] is not None
                    or old["session_id"] != session_id
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

    def _steer(self, command_id, arguments):
        sid = arguments["session_id"]
        channel, thread_id, native_turn, owner = self._active_turns[sid]
        message_id = arguments["message_id"]
        prompt = arguments["prompt"]
        turn = {
            "id": owner["id"],
            "session_id": sid,
            "message_id": message_id,
            "run_id": owner["run_id"],
        }
        value = {
            "command_id": command_id,
            "message_id": message_id,
            "run_id": owner["run_id"],
            "segment_id": owner["id"],
            "state": "pending",
            "warning": None,
        }
        with self.store.connect() as db:
            if db.execute("SELECT 1 FROM turns WHERE message_id=?", (message_id,)).fetchone():
                raise ValueError("Message identity was already used")
            db.execute(
                "INSERT INTO turns(id,session_id,command_id,message_id,run_id,prompt,state,segment_id) "
                "VALUES (?,?,?,?,?,?,'steering',?)",
                (
                    str(uuid.uuid4()),
                    sid,
                    command_id,
                    message_id,
                    owner["run_id"],
                    prompt,
                    owner["id"],
                ),
            )
            # Commit a receipt before writing to stdin. A lost native reply
            # cannot tell us whether the active turn accepted the input.
            self.store.record(db, command_id, "submit", arguments, value)
        try:
            if native_turn is None:
                response = channel.steer_async(thread_id, native_turn, prompt, message_id)
                self._pending_steers.setdefault(sid, []).append((response, turn, prompt, value))
                return value.copy()
            channel.steer(thread_id, native_turn, prompt, message_id)
        except Rejected:
            self._settle_steer(turn, prompt, value, "rejected")
            raise Unsupported(value["warning"]) from None
        except (OSError, RuntimeError):
            self._settle_steer(turn, prompt, value, "uncertain")
        else:
            self._settle_steer(turn, prompt, value, "steered")
        return value

    def _reconcile_steers(self, sid):
        for item in self._pending_steers.pop(sid, []):
            receipt, turn, prompt, value = item
            if not receipt.done():
                self._pending_steers.setdefault(sid, []).append(item)
                continue
            state = "uncertain" if receipt.exception() else "steered"
            self._settle_steer(turn, prompt, value, state)

    def _settle_steer(self, turn, prompt, value, state):
        if state == "rejected":
            value.update(state=state, warning="The active turn no longer accepts steering")
        elif state == "uncertain":
            value.update(state=state, warning=STEERING_UNCERTAIN)
        else:
            event = self._event(turn, "user", {"kind": "input", "text": prompt})
            value.update(state="picked_up", warning=None, pickup_seq=event["seq"])
        command_id = value["command_id"]
        with self.store.connect() as db:
            db.execute(
                "UPDATE turns SET state=?,pickup_seq=? WHERE command_id=?",
                (state, value.get("pickup_seq"), command_id),
            )
            db.execute("UPDATE commands SET result=? WHERE id=?", (json.dumps(value), command_id))

    def observe(self, session_id, after=0, prompt=None, segment_id=None, view="all", limit=200):
        return observe_view(self, session_id, after, prompt, segment_id, view, limit)

    @serialized
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

    @serialized
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
