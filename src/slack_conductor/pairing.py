"""Durable enrollment and per-user client profiles for a shared Slack server."""

import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

PAIR_TTL_SECONDS = 10 * 60
MAX_PENDING = 500
PAIR_CODE = re.compile(r"[0-9a-f]{32}")
TOKEN_DIGEST = re.compile(r"[0-9a-f]{64}")
PROFILE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
BACKENDS = {"conductor", "headless"}


@dataclass(frozen=True)
class ClientProfile:
    id: str
    team_id: str
    user_id: str
    name: str
    backend: str


class PairingStore:
    def __init__(self, path: Path, *, clock=time.time):
        self.path = path
        self.clock = clock
        path.touch(mode=0o600, exist_ok=True)
        with self._db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS pending_pairings (
                    code_digest TEXT PRIMARY KEY,
                    token_digest TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    backend TEXT NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS client_profiles (
                    id TEXT PRIMARY KEY,
                    team_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    backend TEXT NOT NULL,
                    token_digest TEXT NOT NULL UNIQUE,
                    code_digest TEXT NOT NULL UNIQUE,
                    claim_event TEXT,
                    revoked INTEGER NOT NULL DEFAULT 0
                );
                CREATE UNIQUE INDEX IF NOT EXISTS active_client_name
                    ON client_profiles(team_id, user_id, name) WHERE revoked = 0;
                CREATE TABLE IF NOT EXISTS client_defaults (
                    team_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    profile_id TEXT NOT NULL REFERENCES client_profiles(id),
                    PRIMARY KEY(team_id, user_id)
                );
                """
            )

    @contextmanager
    def _db(self, *, write=False):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                if write:
                    db.execute("BEGIN IMMEDIATE")
                yield db
        finally:
            db.close()

    def register(self, code: str, token_digest: str, name: str, backend: str):
        if (
            not PAIR_CODE.fullmatch(code)
            or not TOKEN_DIGEST.fullmatch(token_digest)
            or not PROFILE_NAME.fullmatch(name)
            or backend not in BACKENDS
        ):
            raise ValueError("Invalid pairing request")
        code_digest = sha256(code.encode()).hexdigest()
        with self._db(write=True) as db:
            now = self.clock()
            db.execute("DELETE FROM pending_pairings WHERE expires_at <= ?", (now,))
            if db.execute(
                "SELECT 1 FROM client_profiles WHERE token_digest = ? OR code_digest = ?",
                (token_digest, code_digest),
            ).fetchone():
                raise ValueError("Pairing has already been used")
            pending = db.execute(
                """SELECT code_digest, token_digest, name, backend FROM pending_pairings
                   WHERE code_digest = ? OR token_digest = ?""",
                (code_digest, token_digest),
            ).fetchall()
            if pending:
                if len(pending) == 1 and tuple(pending[0]) == (
                    code_digest,
                    token_digest,
                    name,
                    backend,
                ):
                    return
                raise ValueError("Pairing is already pending")
            if db.execute("SELECT count(*) FROM pending_pairings").fetchone()[0] >= MAX_PENDING:
                raise ValueError("Too many pending pairings")
            db.execute(
                "INSERT INTO pending_pairings VALUES (?, ?, ?, ?, ?)",
                (code_digest, token_digest, name, backend, now + PAIR_TTL_SECONDS),
            )

    def claim(
        self,
        code: str,
        team_id: str,
        user_id: str,
        *,
        event_key: str | None = None,
        reserved_names: tuple[str, ...] = (),
    ) -> ClientProfile:
        if not PAIR_CODE.fullmatch(code):
            raise ValueError("Invalid or expired pairing code")
        code_digest = sha256(code.encode()).hexdigest()
        with self._db(write=True) as db:
            if event_key:
                replay = db.execute(
                    """SELECT id, team_id, user_id, name, backend FROM client_profiles
                       WHERE code_digest = ? AND team_id = ? AND user_id = ? AND claim_event = ?""",
                    (code_digest, team_id, user_id, event_key),
                ).fetchone()
                if replay:
                    return ClientProfile(*replay)
            pending = db.execute(
                "SELECT * FROM pending_pairings WHERE code_digest = ? AND expires_at > ?",
                (code_digest, self.clock()),
            ).fetchone()
            if pending is None:
                raise ValueError("Invalid or expired pairing code")
            if pending["name"] in reserved_names:
                raise ValueError("That client profile name is reserved")
            if db.execute(
                """SELECT 1 FROM client_profiles
                   WHERE team_id = ? AND user_id = ? AND name = ? AND revoked = 0""",
                (team_id, user_id, pending["name"]),
            ).fetchone():
                raise ValueError("A client with that name already belongs to you")
            profile = ClientProfile(
                uuid4().hex, team_id, user_id, pending["name"], pending["backend"]
            )
            db.execute(
                """INSERT INTO client_profiles
                   (id, team_id, user_id, name, backend, token_digest, code_digest, claim_event)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    profile.id,
                    team_id,
                    user_id,
                    profile.name,
                    profile.backend,
                    pending["token_digest"],
                    code_digest,
                    event_key,
                ),
            )
            db.execute("DELETE FROM pending_pairings WHERE code_digest = ?", (code_digest,))
            return profile

    def authenticate(self, token: str) -> ClientProfile | None:
        return self._one(
            "WHERE token_digest = ? AND revoked = 0", (sha256(token.encode()).hexdigest(),)
        )

    def get(self, profile_id: str) -> ClientProfile | None:
        return self._one("WHERE id = ?", (profile_id,))

    def is_active(self, profile_id: str) -> bool:
        return self._one("WHERE id = ? AND revoked = 0", (profile_id,)) is not None

    def _one(self, filter_sql, parameters):
        with self._db() as db:
            row = db.execute(
                "SELECT id, team_id, user_id, name, backend FROM client_profiles " + filter_sql,
                parameters,
            ).fetchone()
            return ClientProfile(*row) if row else None

    def find(self, team_id: str, user_id: str, name: str) -> ClientProfile | None:
        return self._one(
            "WHERE team_id = ? AND user_id = ? AND name = ? AND revoked = 0",
            (team_id, user_id, name),
        )

    def profiles(self, team_id: str, user_id: str) -> list[ClientProfile]:
        with self._db() as db:
            rows = db.execute(
                """SELECT id, team_id, user_id, name, backend FROM client_profiles
                   WHERE team_id = ? AND user_id = ? AND revoked = 0 ORDER BY rowid""",
                (team_id, user_id),
            )
            return [ClientProfile(*row) for row in rows]

    def list_active(self, team_id: str) -> list[ClientProfile]:
        with self._db() as db:
            rows = db.execute(
                """SELECT id, team_id, user_id, name, backend FROM client_profiles
                   WHERE team_id = ? AND revoked = 0 ORDER BY rowid""",
                (team_id,),
            )
            return [ClientProfile(*row) for row in rows]

    def get_default(self, team_id: str, user_id: str) -> ClientProfile | None:
        return self._one(
            """WHERE team_id = ? AND user_id = ? AND revoked = 0
               ORDER BY id = (SELECT profile_id FROM client_defaults
                              WHERE team_id = ? AND user_id = ?) DESC, rowid LIMIT 1""",
            (team_id, user_id, team_id, user_id),
        )

    def set_default(self, team_id: str, user_id: str, name: str) -> ClientProfile:
        with self._db(write=True) as db:
            row = db.execute(
                """SELECT id, team_id, user_id, name, backend FROM client_profiles
                   WHERE team_id = ? AND user_id = ? AND name = ? AND revoked = 0""",
                (team_id, user_id, name),
            ).fetchone()
            if row is None:
                raise ValueError("No active client profile with that name")
            db.execute(
                """INSERT INTO client_defaults VALUES (?, ?, ?)
                   ON CONFLICT(team_id, user_id) DO UPDATE SET profile_id = excluded.profile_id""",
                (team_id, user_id, row["id"]),
            )
            return ClientProfile(*row)

    def revoke(self, team_id: str, user_id: str, name: str) -> bool:
        with self._db(write=True) as db:
            row = db.execute(
                """UPDATE client_profiles SET revoked = 1
                   WHERE team_id = ? AND user_id = ? AND name = ? AND revoked = 0
                   RETURNING id""",
                (team_id, user_id, name),
            ).fetchone()
            if row is None:
                return False
            db.execute("DELETE FROM client_defaults WHERE profile_id = ?", (row["id"],))
            return True
