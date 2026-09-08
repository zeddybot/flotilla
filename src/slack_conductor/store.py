import json
import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
 channel TEXT NOT NULL, thread_ts TEXT NOT NULL, workspace_id TEXT NOT NULL,
 session_id TEXT NOT NULL, options TEXT NOT NULL, state TEXT NOT NULL,
 PRIMARY KEY(channel,thread_ts)
);
CREATE TABLE IF NOT EXISTS events (
 key TEXT PRIMARY KEY, payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
 created REAL NOT NULL DEFAULT (unixepoch('now'))
);
CREATE TABLE IF NOT EXISTS statuses (
 id INTEGER PRIMARY KEY, event_key TEXT UNIQUE NOT NULL, channel TEXT NOT NULL,
 thread_ts TEXT NOT NULL, status_ts TEXT, workspace_id TEXT, session_id TEXT,
 prompt TEXT NOT NULL, options TEXT NOT NULL, turn_id TEXT, cursor INTEGER NOT NULL DEFAULT 0,
 state TEXT NOT NULL DEFAULT 'preparing', note TEXT NOT NULL DEFAULT '',
 latest TEXT NOT NULL DEFAULT 'starting agent', last_edit REAL NOT NULL DEFAULT 0,
 result TEXT, delivered INTEGER NOT NULL DEFAULT 0,
 created REAL NOT NULL DEFAULT (unixepoch('now'))
);
"""


class Store:
    def __init__(self, path):
        self.path = path
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(statuses)")}
            if "tracking" not in columns:
                conn.execute("ALTER TABLE statuses ADD COLUMN tracking TEXT NOT NULL DEFAULT '{}'")
        path.chmod(0o600)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def rows(self, sql, args=()):
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, args)]

    def execute(self, sql, args=()):
        with self.connect() as conn:
            return conn.execute(sql, args).lastrowid

    def accept(self, key, event):
        with self.connect() as conn:
            return (
                conn.execute(
                    "INSERT OR IGNORE INTO events(key,payload) VALUES (?,?)",
                    (key, json.dumps(event)),
                ).rowcount
                == 1
            )

    def next_event(self):
        rows = self.rows(
            "SELECT * FROM events WHERE state='pending' ORDER BY created,rowid LIMIT 1"
        )
        return rows[0] if rows else None

    def thread(self, channel, ts):
        rows = self.rows("SELECT * FROM threads WHERE channel=? AND thread_ts=?", (channel, ts))
        return rows[0] if rows else None

    def bind(self, channel, ts, workspace, options):
        self.execute(
            "INSERT INTO threads VALUES (?,?,?,?,?,?) ON CONFLICT(channel,thread_ts) "
            "DO UPDATE SET workspace_id=excluded.workspace_id,session_id=excluded.session_id,"
            "options=excluded.options,state=excluded.state",
            (
                channel,
                ts,
                workspace["id"],
                workspace["active_session_id"],
                json.dumps(options),
                "running",
            ),
        )

    def thread_state(self, status, state):
        self.execute(
            "UPDATE threads SET state=? WHERE channel=? AND thread_ts=? AND session_id=?",
            (state, status["channel"], status["thread_ts"], status["session_id"]),
        )

    def add_status(self, event_key, channel, thread_ts, prompt, options):
        return self.execute(
            "INSERT INTO statuses(event_key,channel,thread_ts,prompt,options) VALUES (?,?,?,?,?)",
            (event_key, channel, thread_ts, prompt, json.dumps(options)),
        )

    def update(self, status_id, **values):
        allowed = {
            "status_ts",
            "workspace_id",
            "session_id",
            "turn_id",
            "cursor",
            "state",
            "note",
            "latest",
            "last_edit",
            "result",
            "delivered",
            "tracking",
        }
        if not values.keys() <= allowed:
            raise ValueError("Unknown status field")
        self.execute(
            "UPDATE statuses SET " + ",".join(f"{key}=?" for key in values) + " WHERE id=?",
            (*values.values(), status_id),
        )

    def status(self, status_id):
        return self.rows("SELECT * FROM statuses WHERE id=?", (status_id,))[0]

    def active(self):
        return self.rows(
            "SELECT * FROM statuses WHERE state IN ('waiting','active','delivering') ORDER BY id"
        )
