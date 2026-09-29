"""Persist Conductor observations without exposing its transcript as a wire API."""

import fcntl
import time

from ..lifecycle import Turn
from ..runtime_log import EventLog
from .db import ConductorDB
from .events import normalize


class Observations:
    def __init__(self, runtime):
        self.runtime = runtime
        self.log = EventLog(runtime.config.state_dir / "runtime-events.sqlite")
        with runtime.receipts.connect() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS segment_owners (
                    session_id TEXT NOT NULL, segment_id TEXT NOT NULL,
                    message_id TEXT, run_id TEXT, PRIMARY KEY(session_id,segment_id)
                );
                CREATE TABLE IF NOT EXISTS job_owners (
                    session_id TEXT NOT NULL, job_id TEXT NOT NULL,
                    message_id TEXT, run_id TEXT, PRIMARY KEY(session_id,job_id)
                );
            """)

    def ingest(self, sid):
        # The daemon and short-lived CLI can observe the same native history.
        # Serialize cursor advancement so a concurrent partial import cannot skip rows.
        path = self.runtime.config.state_dir / "runtime-events.lock"
        with path.open("a") as lock:
            path.chmod(0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            self._ingest(sid)

    def _ingest(self, sid):
        correlations = self.runtime._correlations(sid)
        db = self.runtime.native.db
        cursor = self.log.cursor(sid)
        # Native row IDs remain wire cursors, including existing server receipts.
        rows = (
            db.messages(sid, cursor, limit=500)
            if isinstance(db, ConductorDB)
            else db.messages(sid, cursor)[:500]
        )
        for raw in rows:
            row = normalize(raw)
            segment, event = row["turn_id"], row["event"]
            mid, run = correlations.get(segment, (None, None))
            with self.runtime.receipts.connect() as c:
                if event.get("kind") == "job_finished":
                    owner = c.execute(
                        "SELECT message_id,run_id FROM job_owners WHERE session_id=? AND job_id=?",
                        (sid, event.get("job_id")),
                    ).fetchone()
                    if owner:
                        mid, run = owner
                elif event.get("kind") != "input":
                    owner = c.execute(
                        "SELECT message_id,run_id FROM segment_owners "
                        "WHERE session_id=? AND segment_id=?",
                        (sid, segment),
                    ).fetchone()
                    if owner:
                        mid, run = owner
                if segment:
                    if event.get("foreign"):
                        c.execute(
                            "DELETE FROM segment_owners WHERE session_id=? AND segment_id=?",
                            (sid, segment),
                        )
                    elif run:
                        c.execute(
                            "INSERT OR REPLACE INTO segment_owners VALUES (?,?,?,?)",
                            (sid, segment, mid, run),
                        )
                if event.get("kind") == "job_started" and event.get("job_id") and run:
                    c.execute(
                        "INSERT OR IGNORE INTO job_owners VALUES (?,?,?,?)",
                        (sid, event["job_id"], mid, run),
                    )
            row.update(message_id=mid, run_id=run)
            self.log.append(sid, row, seq=row["seq"])

    def snapshot(self, sid):
        source = self.runtime.native.db.session(sid)
        fresh = source.get("status") != "reconnecting" and self.runtime.native.ready()
        prior = self.log.snapshot(sid)
        observed = time.time()
        if not fresh:
            value = dict(prior["value"]) if prior else {}
            value.update(
                id=sid,
                status="reconnecting",
                freshness="stale" if prior else "unknown",
                observed_at=observed,
                last_trusted_at=prior["at"] if prior else None,
            )
            value["rest"] = {"confirmed": False, "reason": "native observation unavailable"}
            return value
        value = {k: source[k] for k in ("id", "status", "runtime") if k in source}
        value.update(
            freshness="fresh",
            observed_at=observed,
            last_trusted_at=observed,
            configuration={k: source.get(k) for k in ("model", "thinking_level", "fast_mode")},
        )
        # A result is only a candidate. Explicit live idleness and all tracked
        # jobs/callbacks must agree before the adapter reports observed rest.
        projection_key = sid + ":projection"
        projection = self.log.snapshot(projection_key)
        turn = Turn.load(projection["value"]["turn"]) if projection else Turn()
        cursor = projection["value"]["cursor"] if projection else 0
        while True:
            page = self.log.page(sid, cursor)
            for row in page["events"]:
                if row["role"] == "user" and row.get("turn_id"):
                    turn.result_seq = 0
                    turn.settled_at = None
                turn.consume(row)
            cursor = page["cursor"]
            if not page["has_more"]:
                break
        self.log.snapshot(projection_key, {"turn": turn.dump(), "cursor": cursor})
        live = value.get("runtime", {})
        confirmed = bool(
            turn.result_seq
            and not turn.tasks
            and not turn.awaiting_callback
            and source.get("status") == "idle"
            and not live.get("jobs")
            and live.get("activity") != "working"
            and self.log.cursor(sid) >= self.runtime.native.db.cursor(sid)
        )
        value["rest"] = {
            "confirmed": confirmed,
            "result_candidate": bool(turn.result_seq),
            "pending_jobs": len(turn.tasks),
            "awaiting_callback": turn.awaiting_callback,
        }
        self.log.snapshot(sid, value)
        return value
