"""Test-owned resources with teardown even when a scenario fails."""

import sqlite3

import pytest

from slack_conductor.conductor.db import ConductorDB
from slack_conductor.store import Store
from tests.support import Slack


@pytest.fixture
def bridge_state(tmp_path):
    store = Store(tmp_path / "bridge.sqlite")
    path = tmp_path / "conductor.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript("""
          CREATE TABLE workspaces(local_id,state);
          INSERT INTO workspaces VALUES ('w','ready');
          CREATE TABLE sessions(id,status);
          INSERT INTO sessions VALUES ('s','idle');
          CREATE TABLE session_messages(id,session_id,role,content,turn_id,sent_at,cancelled_at,created_at);
        """)
        conn.commit()
        yield store, conn, ConductorDB(path), Slack()
    finally:
        conn.close()
