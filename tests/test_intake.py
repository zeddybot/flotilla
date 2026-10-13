import json
import logging
import sqlite3
from types import SimpleNamespace

import pytest

from slack_conductor.config import Config
from slack_conductor.slack_app import BoltSocketModeHandler, Bridge, SocketModeHandler


def test_socket_arrival_is_logged_even_when_bolt_does_not_invoke_listener(caplog, monkeypatch):
    handled = []
    monkeypatch.setattr(BoltSocketModeHandler, "handle", lambda *args: handled.append(args))
    request = SimpleNamespace(
        type="events_api",
        payload={
            "event_id": "Ev1",
            "team_id": "T1",
            "event": {"type": "message", "channel": "D1", "ts": "1", "text": "private"},
        },
    )
    with caplog.at_level(logging.INFO):
        SocketModeHandler.__new__(SocketModeHandler).handle(None, request)
    assert '"decision": "received"' in caplog.text
    assert '"event_id": "Ev1"' in caplog.text
    assert "private" not in caplog.text and len(handled) == 1


def test_file_message_acceptance_and_content_free_audit(bridge_state, caplog):
    store, _, db, slack = bridge_state
    bridge = Bridge(Config(), store, SimpleNamespace(db=db), slack, "B", "U1", "T1")
    body = {
        "team_id": "T1",
        "event_id": "Ev1",
        "event": {
            "type": "message",
            "subtype": "file_share",
            "channel_type": "im",
            "channel": "D1",
            "ts": "1.2",
            "user": "U1",
            "text": "private prompt",
            "files": [{"url_private": "https://secret.test/file"}],
        },
    }
    with caplog.at_level(logging.INFO):
        assert bridge.receive(body)
        assert not bridge.receive(body)
        assert not bridge.receive(
            {**body, "event": {**body["event"], "subtype": "message_changed"}}
        )
    records = [json.loads(r.message.removeprefix("Slack intake ")) for r in caplog.records]
    assert [r["decision"] for r in records] == ["accepted", "duplicate", "unsupported_subtype"]
    assert records[0]["event_id"] == "Ev1" and records[0]["file_count"] == 1
    assert "private prompt" not in caplog.text and "secret.test" not in caplog.text
    assert len(store.rows("SELECT * FROM events")) == 1


def test_persistence_failure_is_logged_and_not_reported_as_accepted(
    bridge_state, caplog, monkeypatch
):
    store, _, db, slack = bridge_state
    bridge = Bridge(Config(), store, SimpleNamespace(db=db), slack, "B", "U1", "T1")

    def fail(*args):
        raise sqlite3.OperationalError("secret database detail")

    monkeypatch.setattr(store, "accept", fail)
    with pytest.raises(sqlite3.OperationalError):
        bridge.receive(
            {
                "team_id": "T1",
                "event": {
                    "type": "message",
                    "channel_type": "im",
                    "channel": "D1",
                    "ts": "1",
                    "user": "U1",
                    "text": "private prompt",
                },
            }
        )
    assert '"decision": "persist_failed"' in caplog.text
    assert "secret database detail" not in caplog.text
    assert store.next_event() is None
