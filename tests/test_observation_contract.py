"""Slack delivery depends on observations, not access to a Conductor SQLite connection."""

import pytest

from slack_conductor.poller import Poller
from slack_conductor.store import Store
from tests.support import Slack, status


class SessionObservations:
    """The methods used by the poller; deliberately no SQL/query interface."""

    def __init__(self, state, events):
        self.state, self.events = state, events

    def workspace(self, _):
        return {"state": "ready", "workspace_path": None}

    def session(self, _):
        return {"status": self.state}

    def messages(self, _, after):
        return [event for event in self.events if event["seq"] > after]

    def pickup(self, _, prompt):
        return {"seq": 1, "turn_id": "turn"}

    def pending(self, *_):
        return False

    def has_later_turn(self, *_):
        return False

    def latest_turn(self, _):
        return "turn"


@pytest.mark.parametrize(
    ("state", "header"),
    [
        ("needs_user_input", "Needs attention"),
        ("needs_user_permission", "Needs attention"),
        ("needs_plan_response", "Needs attention"),
        ("working", "Starting"),
        ("reconnecting", "Reconnecting"),
    ],
)
def test_status_delivery_only_requires_session_observations(tmp_path, state, header):
    store, slack = Store(tmp_path / "bridge.sqlite"), Slack()
    request = status(store, "message", "hello")
    Poller(store, SessionObservations(state, []), slack, clock=lambda: 100).tick()
    assert header in slack.edits[-1][1]
    assert store.status(request)["state"] != "complete"


def test_final_reply_survives_poller_restart_without_native_sql(tmp_path):
    store, slack = Store(tmp_path / "bridge.sqlite"), Slack()
    request = status(store, "message", "hello")
    event = {
        "seq": 2,
        "id": "result",
        "role": "assistant",
        "turn_id": "turn",
        "event": {"kind": "result", "text": "the final answer", "error": False},
    }
    observations = SessionObservations("idle", [event])
    Poller(store, observations, slack, clock=lambda: 100).tick()
    restarted = Store(store.path)
    Poller(restarted, observations, slack, clock=lambda: 103).tick()
    assert restarted.status(request)["state"] == "complete"
    assert "Done" in slack.edits[-1][1]
    assert "the final answer" in slack.edits[-1][1]
    delivered = len(slack.edits)
    Poller(Store(store.path), observations, slack, clock=lambda: 106).tick()
    assert len(slack.edits) == delivered
