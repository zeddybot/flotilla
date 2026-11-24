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
CREATE TABLE IF NOT EXISTS notifications (
 key TEXT PRIMARY KEY, status_id INTEGER NOT NULL, owner_id TEXT NOT NULL,
 phase TEXT NOT NULL, title TEXT NOT NULL, detail TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending', dm_channel TEXT, dm_ts TEXT, permalink TEXT,
 attempted_at REAL, retry_at REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS slack_routes (
 team_id TEXT NOT NULL, channel TEXT NOT NULL, thread_ts TEXT NOT NULL,
 owner_id TEXT NOT NULL, client_id TEXT NOT NULL,
 PRIMARY KEY(team_id,channel,thread_ts)
);
CREATE TABLE IF NOT EXISTS slack_route_migrations (team_id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS slack_legacy_defaults (
 team_id TEXT PRIMARY KEY, client_id TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path):
        self.path = path
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            if "retry_at" not in {row[1] for row in conn.execute("PRAGMA table_info(events)")}:
                conn.execute("ALTER TABLE events ADD COLUMN retry_at REAL NOT NULL DEFAULT 0")
            if "control" not in {row[1] for row in conn.execute("PRAGMA table_info(events)")}:
                conn.execute("ALTER TABLE events ADD COLUMN control TEXT")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(statuses)")}
            if "tracking" not in columns:
                conn.execute("ALTER TABLE statuses ADD COLUMN tracking TEXT NOT NULL DEFAULT '{}'")
            if "discard_ts" not in columns:
                conn.execute(
                    "ALTER TABLE statuses ADD COLUMN discard_ts TEXT NOT NULL DEFAULT '[]'"
                )
            if "attachments" not in columns:
                conn.execute(
                    "ALTER TABLE statuses ADD COLUMN attachments TEXT NOT NULL DEFAULT '{}'"
                )
            for name, definition in (
                ("notice_phase", "TEXT NOT NULL DEFAULT ''"),
                ("notice_seq", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in columns:
                    conn.execute(f"ALTER TABLE statuses ADD COLUMN {name} {definition}")
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

    def migrate_legacy_routes(self, team_id, owner_id, client_id):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute(
                "INSERT OR IGNORE INTO slack_route_migrations VALUES (?)", (team_id,)
            ).rowcount:
                return
            conn.execute(
                "INSERT OR IGNORE INTO slack_routes "
                "SELECT ?,channel,thread_ts,?,? FROM threads "
                "UNION SELECT ?,channel,thread_ts,?,? FROM statuses",
                (team_id, owner_id, client_id, team_id, owner_id, client_id),
            )
            conn.execute(
                "INSERT OR IGNORE INTO slack_routes "
                "SELECT ?,json_extract(payload,'$.channel'),"
                "coalesce(json_extract(payload,'$.thread_ts'),json_extract(payload,'$.ts')),?,? "
                "FROM events WHERE json_extract(payload,'$.user')=? "
                "AND json_extract(payload,'$.channel') IS NOT NULL "
                "AND json_extract(payload,'$.ts') IS NOT NULL",
                (team_id, owner_id, client_id, owner_id),
            )

    def route(self, team_id, channel, thread_ts):
        routes = self.rows(
            "SELECT owner_id,client_id FROM slack_routes WHERE team_id=? AND channel=? AND thread_ts=?",
            (team_id, channel, thread_ts),
        )
        return routes[0] if routes else None

    def list_sessions(self, team_id, owner_id, client_id, destination):
        """List the current Slack bindings and their recorded activity for one profile."""
        with self.connect() as conn:
            schema = "main"
            if destination.path != self.path:
                conn.execute("ATTACH DATABASE ? AS endpoint", (str(destination.path),))
                schema = "endpoint"
            # The opening prompt identifies the session even after a steer supersedes it.
            # A queued follow-up should not hide a turn that is already active.
            rows = conn.execute(
                f"""
                WITH owned AS (
                    SELECT t.* FROM slack_routes r
                    JOIN {schema}.threads t USING (channel, thread_ts)
                    WHERE r.team_id=? AND r.owner_id=? AND r.client_id=? AND t.state!='archived'
                ), ranked AS (
                    SELECT t.channel, t.thread_ts, t.session_id, t.workspace_id, t.options,
                        first_value(s.prompt) OVER (thread ORDER BY s.id) AS prompt,
                        first_value(s.event_key) OVER (thread ORDER BY s.id) AS event_key,
                        CASE
                            WHEN s.state IN ('active', 'delivering', 'complete')
                                AND json_extract(s.tracking, '$.phase')='attention' THEN 'attention'
                            WHEN s.state='failed' OR (s.state IN ('delivering', 'complete')
                                AND json_extract(s.tracking, '$.phase')='failed') THEN 'failed'
                            WHEN s.state='active' THEN 'running'
                            WHEN s.state='waiting' AND s.latest='queued' THEN 'queued'
                            WHEN s.state='waiting' THEN 'starting'
                            WHEN s.state='delivering' THEN 'delivering'
                            ELSE 'idle'
                        END AS status,
                        coalesce(max(s.created) OVER thread,
                            cast(t.thread_ts AS REAL)) AS updated_at,
                        row_number() OVER (thread ORDER BY
                            CASE s.state
                                WHEN 'active' THEN 1
                                WHEN 'waiting' THEN 2
                                WHEN 'delivering' THEN 3
                                WHEN 'superseded' THEN 5
                                ELSE 4
                            END, s.id DESC) AS rank
                    FROM owned t
                    LEFT JOIN {schema}.statuses s ON s.channel=t.channel AND s.thread_ts=t.thread_ts
                        AND s.session_id=t.session_id AND s.workspace_id=t.workspace_id
                        AND s.prompt!=''
                    WINDOW thread AS (PARTITION BY t.channel, t.thread_ts)
                )
                SELECT channel, thread_ts, session_id, workspace_id, options, prompt, event_key, status, updated_at
                FROM ranked WHERE rank=1 ORDER BY updated_at DESC, thread_ts DESC, channel
                """,
                (team_id, owner_id, client_id),
            )
            sessions = []
            for row in rows:
                session = dict(row)
                options = json.loads(session.pop("options"))
                prompt = session["prompt"] or ""
                permalink = ""
                if prompt.startswith("[slack] channel="):
                    header = prompt.partition("\n")[0]
                    permalink = header.partition(" permalink=")[2].partition(" ")[0]
                    prompt = prompt.partition("\n\n")[2]
                session.update(prompt=prompt, permalink=permalink, model=options.get("model") or "")
                sessions.append(session)
            return sessions

    def legacy_default(self, team_id, fallback):
        rows = self.rows("SELECT client_id FROM slack_legacy_defaults WHERE team_id=?", (team_id,))
        return rows[0]["client_id"] if rows else fallback

    def set_legacy_default(self, team_id, client_id):
        self.execute(
            "INSERT INTO slack_legacy_defaults VALUES (?,?) "
            "ON CONFLICT(team_id) DO UPDATE SET client_id=excluded.client_id",
            (team_id, client_id),
        )

    def accept_routed(self, team_id, event, route, destination):
        """Atomically claim the thread and queue its first prompt in the winning client's store."""
        channel, timestamp = event["channel"], event["ts"]
        identity = (team_id, channel, event.get("thread_ts") or timestamp)
        key = f"{team_id}:{channel}:{timestamp}"
        with self.connect() as conn:
            table = "main.events"
            if destination.path != self.path:
                conn.execute("ATTACH DATABASE ? AS endpoint", (str(destination.path),))
                table = "endpoint.events"
            # Bridge databases use SQLite's rollback-journal mode, which makes
            # the two-file commit atomic, including after process interruption.
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT OR IGNORE INTO slack_routes VALUES (?,?,?,?,?)",
                (*identity, route["owner_id"], route["client_id"]),
            )
            winning = dict(
                conn.execute(
                    "SELECT owner_id,client_id FROM slack_routes "
                    "WHERE team_id=? AND channel=? AND thread_ts=?",
                    identity,
                ).fetchone()
            )
            if winning != route:
                return winning, False
            inserted = conn.execute(
                f"INSERT OR IGNORE INTO {table}(key,payload) VALUES (?,?)",
                (key, json.dumps(event)),
            ).rowcount
            return winning, bool(inserted)

    def next_event(self):
        rows = self.rows(
            "SELECT e.* FROM events e WHERE e.state='pending' AND e.retry_at<=unixepoch('now') "
            "AND NOT EXISTS (SELECT 1 FROM events prior "
            "WHERE prior.rowid<e.rowid AND prior.state IN ('pending','processing') "
            "AND json_extract(prior.payload,'$.channel')=json_extract(e.payload,'$.channel') "
            "AND coalesce(json_extract(prior.payload,'$.thread_ts'),json_extract(prior.payload,'$.ts'))="
            "coalesce(json_extract(e.payload,'$.thread_ts'),json_extract(e.payload,'$.ts'))) "
            "ORDER BY e.created,e.rowid LIMIT 1"
        )
        return rows[0] if rows else None

    def session_bindings(self):
        return {
            row["session_id"]: row["workspace_id"]
            for row in self.rows(
                "SELECT session_id,workspace_id FROM threads WHERE state!='archived' "
                "UNION SELECT session_id,workspace_id FROM statuses "
                "WHERE state IN ('waiting','active','delivering') AND session_id IS NOT NULL"
            )
        }

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
            "UPDATE threads SET state=? WHERE channel=? AND thread_ts=? AND session_id=? "
            "AND state!='archived'",
            (state, status["channel"], status["thread_ts"], status["session_id"]),
        )

    def add_status(self, event_key, channel, thread_ts, prompt, options):
        with self.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO statuses(event_key,channel,thread_ts,prompt,options) VALUES (?,?,?,?,?)",
                (event_key, channel, thread_ts, prompt, json.dumps(options)),
            )
            return conn.execute(
                "SELECT id FROM statuses WHERE event_key=?", (event_key,)
            ).fetchone()[0]

    def update(self, status_id, **values):
        allowed = {
            "options",
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
            "discard_ts",
            "attachments",
        }
        if not values.keys() <= allowed:
            raise ValueError("Unknown status field")
        self.execute(
            "UPDATE statuses SET " + ",".join(f"{key}=?" for key in values) + " WHERE id=?",
            (*values.values(), status_id),
        )

    def status(self, status_id):
        return self.rows("SELECT * FROM statuses WHERE id=?", (status_id,))[0]

    def handoff(self, new_id, old_id, *, tracking=None, cursor=None):
        """Move replay state to the acknowledgment below the steering message."""
        with self.connect() as conn:
            new = conn.execute("SELECT * FROM statuses WHERE id=?", (new_id,)).fetchone()
            old = conn.execute("SELECT * FROM statuses WHERE id=?", (old_id,)).fetchone()
            tracking = json.loads(old["tracking"] if tracking is None else tracking)
            tracking.update(
                result_seq=0, settled_at=None, phase="working", detail="applying follow-up"
            )
            discard = json.loads(old["discard_ts"]) + json.loads(new["discard_ts"])
            if not new["status_ts"]:
                raise ValueError("Steering requires a replacement Slack message")
            if old["status_ts"] and new["status_ts"] != old["status_ts"]:
                discard.append(old["status_ts"])
            if cursor is None:
                cursor = old["cursor"] if old["tracking"] != "{}" else 0
            conn.execute(
                "UPDATE statuses SET state='superseded',discard_ts='[]' WHERE id=?", (old_id,)
            )
            conn.execute(
                "UPDATE statuses SET cursor=?,tracking=?,discard_ts=?,last_edit=0 WHERE id=?",
                (
                    cursor,
                    json.dumps(tracking),
                    json.dumps(list(dict.fromkeys(discard))),
                    new_id,
                ),
            )

    def active(self):
        return self.rows(
            "SELECT * FROM statuses WHERE state IN ('waiting','active','delivering') ORDER BY id"
        )
