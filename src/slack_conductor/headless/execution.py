"""Queued turn dispatch, native process observation, and durable pickup."""

import json
import threading

from .claude_stream import ClaudeStream
from .codex_server import CodexServer, text_input
from .engines import codex_notification, codex_thread_options, command, decode
from .process_host import launch, terminate


class Execution:
    def _event(self, turn, role, event):
        saved = self.events.append(
            turn["session_id"],
            {
                "turn_id": turn["id"],
                "role": role,
                "event": event,
                "message_id": turn["message_id"],
                "run_id": turn["run_id"],
            },
        )
        if event.get("kind") == "result":
            with self.store.connect() as db:
                db.execute("UPDATE turns SET result_seq=? WHERE id=?", (saved["seq"], turn["id"]))
        return saved

    def _dispatch_loop(self):
        while not self._stop.is_set():
            turn = self.store.next_turn()
            if turn is None:
                self._wake.wait(0.5)
                self._wake.clear()
                continue
            sid = turn["session_id"]
            with self._lock:
                if self._stop.is_set():
                    return
                if sid in self._workers and self._workers[sid].is_alive():
                    self._wake.wait(0.1)
                    continue
                with self.store.connect() as db:
                    claimed = db.execute(
                        "UPDATE turns SET state='launching' WHERE id=? AND state='queued' "
                        "AND EXISTS (SELECT 1 FROM sessions WHERE id=? AND state='active' AND activity='idle')",
                        (turn["id"], sid),
                    ).rowcount
                    if not claimed:
                        continue
                    db.execute("UPDATE sessions SET activity='working' WHERE id=?", (sid,))
                worker = threading.Thread(target=self._run_turn, args=(turn,), daemon=True)
                self._workers[sid] = worker
                worker.start()

    def _run_turn(self, turn):
        sid = turn["session_id"]
        session = self._session(sid)
        first = not self._has_picked_up(sid)
        process = None
        channel = None
        result_seen = False
        result_error = False
        needs_attention = False
        last_output = ""
        native_known = False
        try:
            with self._lock:
                current = self._session(sid)
                with self.store.connect() as db:
                    saved = db.execute(
                        "SELECT state FROM turns WHERE id=?", (turn["id"],)
                    ).fetchone()
                if (
                    current["state"] != "active"
                    or current["activity"] != "working"
                    or saved["state"] != "launching"
                ):
                    return
                args = command(
                    self.config,
                    session["engine"],
                    session["native_id"],
                    first=first,
                    spec=json.loads(session["spec"]),
                )
                is_codex = session["engine"] == "codex"
                process = launch(args, session["path"], duplex=True)
                self._processes[sid] = process
                channel = CodexServer(process) if is_codex else ClaudeStream(process)
            if is_codex:
                lines = self._codex_lines(channel, session, turn)
            else:
                channel.start(turn["prompt"], turn["message_id"])
                lines = channel.lines()
            for line in lines:
                if len(line) > 2_000_000:
                    raise RuntimeError("Headless event exceeded size limit")
                native_id, event = decode(session["engine"], line)
                try:
                    native = json.loads(line)
                except ValueError:
                    native = {}
                if isinstance(native, dict):
                    if native.get("isReplay") is True:
                        with self._lock:
                            self._reconcile_steers(sid)
                    message = native.get("message")
                    model = native.get("model") or (
                        message.get("model") if isinstance(message, dict) else None
                    )
                    if isinstance(model, str) and model:
                        with self.store.connect() as db:
                            effective = {**json.loads(session["spec"]), "model": model}
                            if "effort" in native:
                                effective["effort"] = native["effort"]
                            db.execute(
                                "UPDATE sessions SET effective=? WHERE id=?",
                                (json.dumps(effective), sid),
                            )
                if native_id:
                    if session["native_id"] and native_id != session["native_id"]:
                        raise RuntimeError("Native session identity changed")
                    if not native_known:
                        with self.store.connect() as db:
                            db.execute(
                                "UPDATE sessions SET native_id=? WHERE id=?", (native_id, sid)
                            )
                        native_known = True
                        with self._lock:
                            if self._session(sid)["activity"] == "working":
                                self._pickup(turn)
                                self._active_turns[sid] = (
                                    channel,
                                    native_id,
                                    native.get("turn_id"),
                                    turn,
                                )
                if event and native_known:
                    if event["kind"] == "result":
                        with self._lock:
                            self._reconcile_steers(sid)
                            self._active_turns.pop(sid, None)
                    if event.get("kind") == "update":
                        for part in event.get("parts", []):
                            if part.get("kind") == "output":
                                last_output = part.get("text", "")
                    if event.get("kind") == "result" and not event.get("text") and last_output:
                        event["text"] = last_output
                    self._event(turn, "assistant", event)
                    if any(part.get("kind") == "question" for part in event.get("parts", [])):
                        needs_attention = True
                    if event["kind"] == "result":
                        result_seen = True
                        result_error = bool(event.get("error"))
                        break
                    if event.get("kind") in {"job_started", "job_finished"} and event.get("job_id"):
                        with self.store.connect() as db:
                            db.execute(
                                "INSERT INTO jobs VALUES (?,?,?) ON CONFLICT(session_id,id) "
                                "DO UPDATE SET state=excluded.state",
                                (
                                    sid,
                                    event["job_id"],
                                    "active" if event["kind"] == "job_started" else "finished",
                                ),
                            )
            if not native_known:
                raise RuntimeError("Native session ID was not reported; launch needs attention")
            if not result_seen:
                raise RuntimeError("Headless process ended without a completion event")
            with self.store.connect() as db:
                pending_jobs = db.execute(
                    "SELECT count(*) FROM jobs WHERE session_id=? AND state='active'", (sid,)
                ).fetchone()[0]
            if pending_jobs:
                needs_attention = True
            terminate(process)
            self._finish(
                turn,
                "attention" if needs_attention else "failed" if result_error else "done",
                attention=needs_attention,
                reason="question_or_job" if needs_attention else None,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            # A launch may have reached the engine before the worker failed.
            self._event(turn, "assistant", {"kind": "result", "error": True, "text": str(exc)})
            self._finish(turn, "attention", attention=True, reason="ambiguous")
        finally:
            try:
                if process is not None:
                    terminate(process)
            finally:
                if channel:
                    channel.close()
                if process is not None and process.stdout is not None:
                    process.stdout.close()
                with self._lock:
                    self._reconcile_steers(sid)
                    self._processes.pop(sid, None)
                    self._active_turns.pop(sid, None)
                self._wake.set()

    def _codex_lines(self, codex, session, turn):
        codex.initialize()
        spec = json.loads(session["spec"])
        params = codex_thread_options(spec, session["path"])
        native_id = session["native_id"]
        if not params["model"]:
            effective = json.loads(session["effective"]) if session["effective"] else {}
            params["model"] = effective.get("model")
            if not params["model"]:
                available = codex.request("model/list", {"includeHidden": False})
                params["model"] = next(
                    (
                        model["model"]
                        for model in available.get("data", [])
                        if model.get("isDefault")
                    ),
                    None,
                )
        if native_id:
            params.update(threadId=native_id, excludeTurns=True)
        resumed = codex.request("thread/resume" if native_id else "thread/start", params)
        thread_id = resumed.get("thread", {}).get("id")
        if (
            not isinstance(thread_id, str)
            or not thread_id
            or (native_id and native_id != thread_id)
        ):
            raise RuntimeError("Codex did not return the expected thread identity")
        started = codex.request(
            "turn/start",
            {
                "threadId": thread_id,
                "clientUserMessageId": turn["message_id"],
                "input": text_input(turn["prompt"]),
            },
        )
        turn_id = started.get("turn", {}).get("id")
        if not isinstance(turn_id, str) or not turn_id:
            raise RuntimeError("Codex did not return a turn identity")
        yield json.dumps(
            {
                "type": "thread.started",
                "thread_id": thread_id,
                "turn_id": turn_id,
                "model": resumed.get("model"),
                "effort": resumed.get("reasoningEffort", spec["effort"]),
            }
        )
        for source in codex.notifications():
            event = codex_notification(source, thread_id, turn_id, resumed.get("model"))
            if not event:
                continue
            yield json.dumps(event)

    def _has_picked_up(self, sid):
        with self.store.connect() as db:
            return bool(
                db.execute(
                    "SELECT 1 FROM turns WHERE session_id=? AND pickup_seq IS NOT NULL", (sid,)
                ).fetchone()
            )

    def _pickup(self, turn):
        row = self._event(turn, "user", {"kind": "input", "text": turn["prompt"]})
        with self.store.connect() as db:
            db.execute(
                "UPDATE turns SET state='running',pickup_seq=? WHERE id=? AND state='launching'",
                (row["seq"], turn["id"]),
            )

    def _finish(self, turn, state, *, attention, reason=None):
        sid = turn["session_id"]
        with self.store.connect() as db:
            db.execute(
                "UPDATE turns SET state=? WHERE id=? AND state NOT IN ('attention','cancelled')",
                (state, turn["id"]),
            )
            db.execute(
                "UPDATE sessions SET activity=?,attention_reason=? WHERE id=? AND state='active' "
                "AND activity!='attention'",
                ("attention" if attention else "idle", reason, sid),
            )
