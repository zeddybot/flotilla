"""Startup failures release acquired resources without launching real agents."""

import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from slack_conductor import slack_app
from slack_conductor.config import Config
from slack_conductor.connection_health import ConnectionHealth
from slack_conductor.store import Store


@pytest.mark.parametrize("failure", ["reconcile", "socket_construct", "connect", "health", "none"])
def test_acquired_resources_close_even_when_startup_fails(tmp_path, monkeypatch, failure):
    store = Store(tmp_path / "bridge.sqlite")
    closed = []
    client = SimpleNamespace(
        current_session=None,
        on_error_listeners=[],
        auth_test=lambda: {"team_id": "T", "user_id": "B", "team": "test"},
        users_info=lambda **_: {"user": {}},
    )
    monkeypatch.setattr(
        slack_app,
        "App",
        lambda **_: SimpleNamespace(client=client, event=lambda _: lambda callback: callback),
    )

    def attempt(stage):
        if failure == "health" and stage == "connect":
            for _ in range(5):
                for listener in client.on_error_listeners:
                    listener(BrokenPipeError())
        if failure == stage:
            raise RuntimeError(stage)

    monkeypatch.setattr(slack_app.Poller, "reconcile", lambda _: attempt("reconcile"))

    def handler(*_):
        attempt("socket_construct")
        return SimpleNamespace(
            client=client, connect=lambda: attempt("connect"), close=lambda: closed.append("socket")
        )

    monkeypatch.setattr(slack_app, "SocketModeHandler", handler)

    def health(*args):
        result = ConnectionHealth(*args)
        close = result.close

        def cleanup():
            close()
            closed.append("health")

        result.close = cleanup
        return result

    monkeypatch.setattr(slack_app, "ConnectionHealth", health)
    conductor = SimpleNamespace(
        db=None, start=Mock(), ready=Mock(), close=lambda: closed.append("native")
    )
    stop = threading.Event()
    if failure != "health":
        stop.set()
    args = (
        Config(),
        store,
        conductor,
        {
            "SLACK_BOT_TOKEN": "token",
            "SLACK_APP_TOKEN": "app",
            "SLACK_TEAM_ID": "T",
            "OWNER_SLACK_USER_ID": "U",
        },
        stop,
    )
    if failure == "none":
        slack_app.run(*args)
    elif failure == "health":
        with pytest.raises(ConnectionError, match="Socket Mode stalled"):
            slack_app.run(*args)
        conductor.ready.assert_not_called()
    else:
        with pytest.raises(RuntimeError, match=failure):
            slack_app.run(*args)
    assert closed == (
        ["native"]
        if failure in {"reconcile", "socket_construct"}
        else ["health", "socket", "native"]
    )
    conductor.start.assert_called_once_with({})
