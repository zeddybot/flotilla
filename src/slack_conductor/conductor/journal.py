"""Durable native commands and queue intent, independent of Conductor's database."""

import json
import sqlite3
import time
from contextlib import contextmanager


class Unconfirmed(RuntimeError):
    """The operation remains journaled; reconcile it before attempting another send."""


class Journal:
    def __init__(self, path):
        self.path = path
        with self.connect() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS commands (
                    id TEXT PRIMARY KEY, body TEXT NOT NULL, state TEXT NOT NULL,
                    generation TEXT, response TEXT
                );
                CREATE TABLE IF NOT EXISTS pauses (
                    session_id TEXT PRIMARY KEY, paused INTEGER NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS pause_requests (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, paused INTEGER NOT NULL
                );
            """)
        path.chmod(0o600)

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA synchronous=FULL")
        try:
            with c:
                yield c
        finally:
            c.close()

    def enqueue(self, key, body):
        body = json.dumps(body, sort_keys=True)
        with self.connect() as c:
            c.execute(
                "INSERT OR IGNORE INTO commands VALUES (?,?,'pending',NULL,NULL)", (key, body)
            )
            existing = c.execute("SELECT body FROM commands WHERE id=?", (key,)).fetchone()
            if existing["body"] != body:
                raise ValueError("Command identity reused with different arguments")

    def claim(self, generation):
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            # Shutdown belongs to one attachment, never its replacement. This
            # also retires commands left by versions without a target lease.
            c.execute(
                "UPDATE commands SET state='done',response=? WHERE state IN ('pending','running') "
                "AND json_extract(body,'$.op')='detach' "
                "AND json_extract(body,'$.target_generation') IS NOT ?",
                (json.dumps({"value": True}), generation),
            )
            row = c.execute(
                "SELECT * FROM commands WHERE state='pending' ORDER BY rowid LIMIT 1"
            ).fetchone()
            if not row:
                return None
            c.execute(
                "UPDATE commands SET state='running',generation=? WHERE id=?",
                (generation, row["id"]),
            )
            return {"id": row["id"], "body": json.loads(row["body"])}

    def complete(self, key, generation, response):
        with self.connect() as c:
            deferred = (
                response.get("value", {}).get("deferred")
                if isinstance(response.get("value"), dict)
                else False
            )
            retry = response.get("retry") is True
            return (
                c.execute(
                    "UPDATE commands SET state=?,response=? WHERE id=? AND generation=? AND state='running'",
                    (
                        "pending" if retry else "blocked" if deferred else "done",
                        None if deferred or retry else json.dumps(response),
                        key,
                        generation,
                    ),
                ).rowcount
                == 1
            )

    def wait(self, key, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.connect() as c:
                row = c.execute("SELECT response,state FROM commands WHERE id=?", (key,)).fetchone()
            if row and row["state"] == "blocked":
                raise Unconfirmed("Queue paused in Conductor")
            if row and row["response"]:
                result = json.loads(row["response"])
                if "error" in result:
                    raise RuntimeError(result["error"])
                return result.get("value")
            time.sleep(0.05)
        raise Unconfirmed("Conductor connection interrupted; recovering the recorded operation")

    def reconnect(self, generation, native):
        """Only idempotent operations may be redelivered to a fresh frontend."""
        with self.connect() as c:
            # Reconciliation and frontend acknowledgments must not overwrite
            # each other's decisions while the native database is being read.
            c.execute("BEGIN IMMEDIATE")
            rows = c.execute("SELECT * FROM commands WHERE state='running'").fetchall()
            for row in rows:
                body = json.loads(row["body"])
                if body["op"] == "send" and native.accepted(body["sid"], body["message_id"]):
                    c.execute(
                        "UPDATE commands SET state='done',response=? WHERE id=?",
                        (json.dumps({"value": ""}), row["id"]),
                    )
                elif row["generation"] != generation:
                    c.execute(
                        "UPDATE commands SET state='pending',generation=NULL WHERE id=?",
                        (row["id"],),
                    )

    def pause(self, request, sid, paused):
        if type(paused) is not bool:
            raise ValueError("paused must be boolean")
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            old = c.execute("SELECT * FROM pause_requests WHERE id=?", (request,)).fetchone()
            if old and (old["session_id"] != sid or bool(old["paused"]) != paused):
                raise ValueError("Pause identity reused")
            if not old:
                c.execute("INSERT INTO pause_requests VALUES (?,?,?)", (request, sid, paused))
                c.execute(
                    "INSERT INTO pauses(session_id,paused) VALUES (?,?) ON CONFLICT(session_id) "
                    "DO UPDATE SET paused=excluded.paused,revision=revision+1",
                    (sid, paused),
                )
            state = dict(c.execute("SELECT * FROM pauses WHERE session_id=?", (sid,)).fetchone())
            if not state["paused"]:
                c.execute(
                    "UPDATE commands SET state='pending',generation=NULL WHERE state='blocked' AND json_extract(body,'$.sid')=?",
                    (sid,),
                )
            return state

    def pending_prompt(self, sid, prompt):
        with self.connect() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM commands WHERE state!='done' "
                    "AND json_extract(body,'$.op')='send' "
                    "AND json_extract(body,'$.sid')=? AND json_extract(body,'$.prompt')=?",
                    (sid, prompt),
                ).fetchone()
                is not None
            )

    def pause_state(self, sid):
        with self.connect() as c:
            row = c.execute("SELECT * FROM pauses WHERE session_id=?", (sid,)).fetchone()
            return dict(row) if row else None
