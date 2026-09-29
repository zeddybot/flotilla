"""Small durable journal for session bindings, commands, and queued turns."""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, workspace_id TEXT UNIQUE NOT NULL,
                    path TEXT NOT NULL, engine TEXT NOT NULL, native_id TEXT,
                    spec TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'active',
                    activity TEXT NOT NULL DEFAULT 'idle', attention_reason TEXT,
                    effective TEXT
                );
                CREATE TABLE IF NOT EXISTS commands (
                    id TEXT PRIMARY KEY, operation TEXT NOT NULL, arguments TEXT NOT NULL,
                    result TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS turns (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, command_id TEXT UNIQUE NOT NULL,
                    message_id TEXT NOT NULL UNIQUE, run_id TEXT NOT NULL,
                    prompt TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
                    pickup_seq INTEGER, result_seq INTEGER,
                    FOREIGN KEY(session_id) REFERENCES sessions(id)
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    session_id TEXT NOT NULL, id TEXT NOT NULL, state TEXT NOT NULL,
                    PRIMARY KEY(session_id,id)
                );
            """)
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def row(row):
        return dict(row) if row is not None else None

    def session(self, session_id):
        with self.connect() as db:
            return self.row(
                db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            )

    def sessions(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM sessions ORDER BY rowid")]

    def command(self, command_id, operation, arguments):
        with self.connect() as db:
            row = db.execute("SELECT * FROM commands WHERE id=?", (command_id,)).fetchone()
        if row is None:
            return None
        if row["operation"] != operation or json.loads(row["arguments"]) != arguments:
            raise ValueError("Command identity reused with different arguments")
        return json.loads(row["result"])

    @staticmethod
    def record(db, command_id, operation, arguments, result):
        db.execute(
            "INSERT INTO commands VALUES (?,?,?,?)",
            (command_id, operation, json.dumps(arguments), json.dumps(result)),
        )

    def next_turn(self):
        with self.connect() as db:
            row = db.execute(
                "SELECT turns.* FROM turns JOIN sessions ON sessions.id=turns.session_id "
                "WHERE turns.state='queued' AND sessions.state='active' "
                "AND sessions.activity='idle' "
                "AND NOT EXISTS (SELECT 1 FROM turns running WHERE "
                "running.session_id=turns.session_id AND running.state IN ('launching','running')) "
                "ORDER BY turns.rowid LIMIT 1"
            ).fetchone()
            return self.row(row)
