"""Storage contracts stay meaningful if a future client supplies the observations."""

import sqlite3
from contextlib import closing

from slack_conductor.conductor.db import ConductorDB
from slack_conductor.conductor.journal import Journal
from slack_conductor.store import Store


def test_only_open_thread_bindings_are_restored(tmp_path):
    store = Store(tmp_path / "bridge.sqlite")
    for channel, session in [("C1", "s1"), ("C2", "s2")]:
        store.bind(channel, "1", {"id": "w" + session, "active_session_id": session}, {})
    store.thread_state(store.thread("C2", "1"), "archived")
    assert Store(store.path).session_bindings() == {"s1": "ws1"}


def test_pending_native_prompt_is_session_scoped_and_survives_restart(tmp_path):
    journal = Journal(tmp_path / "native.sqlite")
    journal.enqueue("one", {"op": "send", "sid": "s", "prompt": "hello"})
    journal.enqueue("other", {"op": "configure", "sid": "s", "prompt": "unrelated"})
    restarted = Journal(journal.path)
    assert restarted.pending_prompt("s", "hello")
    assert not restarted.pending_prompt("other-session", "hello")
    assert not restarted.pending_prompt("s", "unrelated")
    restarted.claim("generation")
    assert restarted.pending_prompt("s", "hello")
    restarted.complete("one", "generation", {"value": ""})
    assert not restarted.pending_prompt("s", "hello")


def test_turn_observations_ignore_unsent_and_other_session_messages(tmp_path):
    path = tmp_path / "conductor.db"
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("CREATE TABLE session_messages(session_id,role,turn_id,sent_at,cancelled_at)")
        conn.executemany(
            "INSERT INTO session_messages VALUES (?,?,?,?,?)",
            [
                ("s", "user", "original", "now", None),
                ("other", "user", "foreign", "now", None),
                ("s", "user", "queued", None, None),
                ("s", "assistant", "assistant-only", "now", None),
            ],
        )
    db = ConductorDB(path)
    assert db.latest_turn("s") == "original"
    assert not db.has_later_turn("s", 1, "original")
    assert db.latest_turn("missing") is None
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("UPDATE session_messages SET sent_at='later' WHERE turn_id='queued'")
    assert db.latest_turn("s") == "queued"
    assert db.has_later_turn("s", 1, "original")
