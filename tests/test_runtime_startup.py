"""Startup failures release acquired resources without launching real agents."""

import threading
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
        self.now = 0
        self.service_thread = threading.current_thread()
        self.stopped = threading.Event()
        monkeypatch.setattr(slack_app.time, "monotonic", lambda: self.now)

    def is_set(self):
        return self.stopped.is_set()

    def set(self):
        self.stopped.set()

    def wait(self, seconds):
        if threading.current_thread() is self.service_thread:
            self.now += seconds
        # The worker has its own cadence and cannot advance Socket Mode time.
        return self.stopped.wait(0.01)


def service(tmp_path, monkeypatch, attempt, stop):
    monkeypatch.setattr(
        slack_app,
        "IntakeRecovery",
        lambda *_, **__: SimpleNamespace(start=lambda: None, close=lambda: None),
    )
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


@pytest.mark.parametrize("failure", ["socket_construct", "connect", "none"])
def test_acquired_resources_close_even_when_startup_fails(tmp_path, monkeypatch, failure):
    stop = Clock(monkeypatch)

    def attempt(stage, _client):
        if stage in {failure, "tick"}:
            stop.set()
        if failure == stage:
            raise RuntimeError(stage)

    args, conductor, closed = service(tmp_path, monkeypatch, attempt, stop)
    slack_app.run(*args)
    assert closed == (
        ["native"] if failure == "socket_construct" else ["health", "socket", "native"]
    )
    conductor.start.assert_called_once_with({})


def test_failed_reconciliation_retries_before_dispatching(tmp_path, monkeypatch, caplog):
    stop = Clock(monkeypatch)
    stages = []

    def attempt(stage, _client):
        stages.append(stage)
        if stage == "reconcile" and stages.count(stage) == 1:
            raise RuntimeError("reconcile")
        if stage == "tick":
            stop.set()

    args, conductor, closed = service(tmp_path, monkeypatch, attempt, stop)
    store = args[1]
    store.accept("replayed", {"channel": "D1", "ts": "1", "user": "U"})
    store.execute("UPDATE events SET state='processing' WHERE key='replayed'")

    def process(bridge, envelope):
        assert envelope["key"] == "replayed"
        stages.append("dispatch")
        bridge.store.execute("UPDATE events SET state='done' WHERE key=?", (envelope["key"],))

    monkeypatch.setattr(slack_app.Bridge, "process", process)
    slack_app.run(*args)
    assert stages == ["socket_construct", "connect", "reconcile", "reconcile", "dispatch", "tick"]
    assert store.rows("SELECT state FROM events") == [{"state": "done"}]
    assert "Slack client loop failed" in caplog.text
    assert closed == ["health", "socket", "native"]
    conductor.start.assert_called_once_with({})


def test_worker_setup_preserves_recovery_start_failure(tmp_path, monkeypatch):
    stop = Clock(monkeypatch)
    args, conductor, closed = service(tmp_path, monkeypatch, lambda *_: None, stop)

    def start_recovery():
        raise RuntimeError("cannot start recovery")

    recovery = SimpleNamespace(start=start_recovery, close=Mock())
    monkeypatch.setattr(slack_app, "IntakeRecovery", lambda *_, **__: recovery)
    with pytest.raises(RuntimeError, match="cannot start recovery"):
        slack_app.run(*args)
    recovery.close.assert_not_called()
    assert closed == ["native"]
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


def test_history_recovery_replaces_pong_healthy_socket_without_restarting_native(
    tmp_path, monkeypatch
):
    stop = Clock(monkeypatch)
    connections, refresh = [], []
    ticks = []

    def attempt(stage, client):
        if stage == "connect":
            connections.append(stop.now)
            client.current_session = SimpleNamespace(
                session_id=str(len(connections)),
                last_ping_pong_time=stop.now,
            )
        if stage == "tick":
            ticks.append(1)
            if len(ticks) == 1:
                refresh[0]()
                refresh[0]()  # A recovered batch requests only one replacement.
            if len(connections) == 2:
                stop.set()

    args, conductor, closed = service(tmp_path, monkeypatch, attempt, stop)

    def recovery(*_, on_recovered, receive):
        assert callable(receive)
        refresh.append(on_recovered)
        return SimpleNamespace(start=lambda: None, close=lambda: None)

    monkeypatch.setattr(slack_app, "IntakeRecovery", recovery)
    slack_app.run(*args)
    assert len(connections) == 2
    conductor.start.assert_called_once()
    assert closed == ["health", "socket", "health", "socket", "native"]
