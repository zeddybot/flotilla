"""Durable RPC receipts on each side of the single-client connection."""

import json
import sqlite3
from contextlib import contextmanager


class Receipts:
    def __init__(self, path):
        self.path = path
        with self.connect() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS calls (
                    id TEXT PRIMARY KEY, request TEXT NOT NULL, response TEXT
                );
                CREATE TABLE IF NOT EXISTS bindings (
                    session_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL
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

    def begin(self, key, method, args):
        request = json.dumps({"method": method, "args": args}, sort_keys=True)
        with self.connect() as c:
            c.execute("INSERT OR IGNORE INTO calls VALUES (?,?,NULL)", (key, request))
            saved, response = c.execute(
                "SELECT request,response FROM calls WHERE id=?", (key,)
            ).fetchone()
            if saved != request:
                raise ValueError("Remote command identity reused with different arguments")
            return json.loads(response) if response is not None else None

    def finish(self, key, response):
        if response.get("retry"):
            return
        with self.connect() as c:
            c.execute("UPDATE calls SET response=? WHERE id=?", (json.dumps(response), key))

    def saved(self, key):
        with self.connect() as c:
            row = c.execute(
                "SELECT request,response FROM calls WHERE id=? AND response IS NOT NULL",
                (key,),
            ).fetchone()
        if row:
            return json.loads(row[0]), json.loads(row[1])
        return None

    def bind(self, bindings):
        with self.connect() as c:
            c.executemany("INSERT OR REPLACE INTO bindings VALUES (?,?)", bindings.items())

    def bindings(self):
        with self.connect() as c:
            return dict(c.execute("SELECT session_id,workspace_id FROM bindings"))
