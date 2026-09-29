"""Durable, bounded normalized event replay shared by runtime adapters."""

import json
import sqlite3
import time
from contextlib import contextmanager

MAX_PAGE = 500
MAX_PAGE_BYTES = 2 * 1024 * 1024


class CursorExpired(ValueError):
    """The caller must explicitly resynchronize after retained history was removed."""


class EventLog:
    def __init__(self, path):
        self.path = path
        with self.connect() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS runtime_events (
                    session_id TEXT NOT NULL, seq INTEGER NOT NULL, body TEXT NOT NULL,
                    PRIMARY KEY(session_id,seq)
                );
                CREATE TABLE IF NOT EXISTS runtime_snapshots (
                    session_id TEXT PRIMARY KEY, body TEXT NOT NULL, at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runtime_retention (
                    session_id TEXT PRIMARY KEY, floor INTEGER NOT NULL
                );
            """)
        path.chmod(0o600)

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.execute("PRAGMA synchronous=FULL")
        try:
            with c:
                yield c
        finally:
            c.close()

    def append(self, session_id, event, *, seq=None):
        """Source cursors can be retained during migration; otherwise allocate in order."""
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            if seq is None:
                seq = c.execute(
                    "SELECT coalesce(max(seq),0)+1 FROM runtime_events WHERE session_id=?",
                    (session_id,),
                ).fetchone()[0]
                floor = c.execute(
                    "SELECT floor FROM runtime_retention WHERE session_id=?", (session_id,)
                ).fetchone()
                seq = max(seq, floor[0] + 1 if floor else 1)
            value = {**event, "session_id": session_id, "seq": seq}
            value.setdefault("id", f"{session_id}:{seq}")
            body = json.dumps(value, ensure_ascii=True)
            if len(body.encode()) > MAX_PAGE_BYTES:
                raise ValueError("Runtime event exceeds the replay frame limit")
            c.execute(
                "INSERT OR IGNORE INTO runtime_events VALUES (?,?,?)", (session_id, seq, body)
            )
        return value

    def cursor(self, session_id):
        with self.connect() as c:
            row = c.execute(
                "SELECT max(n) FROM (SELECT coalesce(max(seq),0) n FROM runtime_events "
                "WHERE session_id=? UNION ALL SELECT floor FROM runtime_retention "
                "WHERE session_id=?)",
                (session_id, session_id),
            ).fetchone()
            return row[0]

    def page(self, session_id, after=0, limit=200):
        if type(after) is not int or after < 0:
            raise ValueError("after must be a nonnegative event cursor")
        if type(limit) is not int or not 1 <= limit <= MAX_PAGE:
            raise ValueError(f"limit must be between 1 and {MAX_PAGE}")
        with self.connect() as c:
            row = c.execute(
                "SELECT floor FROM runtime_retention WHERE session_id=?", (session_id,)
            ).fetchone()
            floor = row[0] if row else 0
            if after < floor:
                raise CursorExpired(
                    f"Event cursor expired; recover snapshot and resume after {floor}"
                )
            rows = c.execute(
                "SELECT body FROM runtime_events WHERE session_id=? AND seq>? ORDER BY seq LIMIT ?",
                (session_id, after, limit + 1),
            )
            events, size, more = [], 0, False
            for (body,) in rows:
                length = len(body.encode())
                if len(events) == limit or size + length > MAX_PAGE_BYTES:
                    more = True
                    break
                events.append(json.loads(body))
                size += length
        return {
            "events": events,
            "cursor": events[-1]["seq"] if events else after,
            "has_more": more,
            "retained_after": floor,
        }

    def snapshot(self, session_id, value=None):
        with self.connect() as c:
            if value is not None:
                c.execute(
                    "INSERT OR REPLACE INTO runtime_snapshots VALUES (?,?,?)",
                    (session_id, json.dumps(value), time.time()),
                )
            row = c.execute(
                "SELECT body,at FROM runtime_snapshots WHERE session_id=?", (session_id,)
            ).fetchone()
        return {"value": json.loads(row[0]), "at": row[1]} if row else None

    def prune(self, session_id, through):
        """Explicit maintenance only, after consumers acknowledge the cursor; never automatic."""
        if type(through) is not int or not 0 <= through <= self.cursor(session_id):
            raise ValueError("Invalid retention cursor")
        with self.connect() as c:
            c.execute(
                "INSERT INTO runtime_retention VALUES (?,?) ON CONFLICT(session_id) "
                "DO UPDATE SET floor=max(floor,excluded.floor)",
                (session_id, through),
            )
            c.execute(
                "DELETE FROM runtime_events WHERE session_id=? AND seq<=?", (session_id, through)
            )
