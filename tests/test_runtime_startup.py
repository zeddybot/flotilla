"""Startup failures release acquired resources without launching real agents."""

from itertools import pairwise
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from slack_conductor import slack_app
from slack_conductor.config import Config
from slack_conductor.connection_health import ConnectionHealth
from slack_conductor.store import Store


class Clock:
    """A stop event whose waits advance the service loop's clock instead of sleeping."""

    def __init__(self, monkeypatch):
        self.now, self.stopped = 0, False
        monkeypatch.setattr(slack_app.time, "monotonic", lambda: self.now)

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, seconds):
        self.now += seconds
        return self.stopped


def service(tmp_path, monkeypatch, attempt, stop):
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
    monkeypatch.setattr(slack_app.Poller, "reconcile", lambda _: attempt("reconcile", client))
    monkeypatch.setattr(slack_app.Poller, "tick", lambda _: attempt("tick", client))

    def handler(*_):
        attempt("socket_construct", client)
        return SimpleNamespace(
            client=client,
            connect=lambda: attempt("connect", client),
            close=lambda: closed.append("socket"),
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
        db=None, start=Mock(), ready=Mock(return_value=True), close=lambda: closed.append("native")
    )
    args = (
        Config(state_dir=tmp_path),
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
    return args, conductor, closed


@pytest.mark.parametrize("failure", ["reconcile", "socket_construct", "connect", "none"])
def test_acquired_resources_close_even_when_startup_fails(tmp_path, monkeypatch, failure):
    stop = Clock(monkeypatch)

    def attempt(stage, _client):
        if stage in {failure, "tick"}:
            stop.set()
        if failure == stage:
            raise RuntimeError(stage)

    args, conductor, closed = service(tmp_path, monkeypatch, attempt, stop)
    if failure == "reconcile":
        with pytest.raises(RuntimeError, match=failure):
            slack_app.run(*args)
    else:
        slack_app.run(*args)
    assert closed == (
        ["native"]
        if failure in {"reconcile", "socket_construct"}
        else ["health", "socket", "native"]
    )
    conductor.start.assert_called_once_with({})


def test_failed_connection_is_replaced_without_restarting_the_daemon(tmp_path, monkeypatch):
    stop = Clock(monkeypatch)
    connections, dispatched = [], []

    def attempt(stage, client):
        if stage == "connect":
            connections.append(stop.now)
            if len(connections) == 1:
                for _ in range(5):
                    for listener in client.on_error_listeners:
                        listener(BrokenPipeError())
        if stage == "tick":
            dispatched.append(len(connections))
            stop.set()

    args, _conductor, closed = service(tmp_path, monkeypatch, attempt, stop)
    slack_app.run(*args)
    assert len(connections) == 2
    # Nothing is dispatched over the connection that failed.
    assert dispatched == [2]
    assert closed == ["health", "socket", "health", "socket", "native"]


def test_sustained_outage_still_ends_in_a_fresh_process(tmp_path, monkeypatch, caplog):
    stop = Clock(monkeypatch)
    attempts = []

    def attempt(stage, _client):
        if stage == "connect":
            attempts.append(stop.now)
            raise OSError("offline")
        assert stage != "tick"

    args, _conductor, closed = service(tmp_path, monkeypatch, attempt, stop)
    with pytest.raises(ConnectionError, match="Socket Mode stalled"):
        slack_app.run(*args)
    assert stop.now == pytest.approx(slack_app.Link.OUTAGE_LIMIT, abs=2)
    gaps = [later - earlier for earlier, later in pairwise(attempts)]
    assert gaps[:3] == pytest.approx([1, 2, 4], abs=1) and max(gaps) <= 61
    assert "Slack connection attempt failed (OSError)" in caplog.text
    assert "offline" not in caplog.text
    assert closed[-1] == "native"


def test_offline_startup_waits_for_slack_instead_of_exiting(caplog):
    from urllib.error import URLError

    attempts, waits = [], []

    def auth_test():
        attempts.append(1)
        if len(attempts) < 4:
            raise URLError("nodename nor servname provided, or not known")
        return {"team_id": "T"}

    client = SimpleNamespace(auth_test=auth_test, users_info=lambda **_: {"user": {"id": "U"}})
    stop = SimpleNamespace(wait=lambda delay: waits.append(delay))
    assert slack_app.identify(client, "U", stop) == ({"team_id": "T"}, {"id": "U"})
    assert waits == [1, 2, 4]
    assert "Slack unreachable at startup (URLError)" in caplog.text


def test_shutdown_ends_the_offline_wait_and_a_rejected_token_still_fails():
    from slack_sdk.errors import SlackApiError

    def offline():
        raise TimeoutError()

    stopping = SimpleNamespace(wait=lambda _: True)
    assert slack_app.identify(SimpleNamespace(auth_test=offline), "U", stopping) is None

    def rejected():
        raise SlackApiError("invalid_auth", {"error": "invalid_auth"})

    with pytest.raises(SlackApiError):
        slack_app.identify(SimpleNamespace(auth_test=rejected), "U", stopping)
