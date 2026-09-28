import threading
from types import SimpleNamespace

from slack_conductor.connection_health import ConnectionHealth


def monitor():
    now = [0]
    client = SimpleNamespace(current_session=None, on_error_listeners=[])
    stop = threading.Event()
    health = ConnectionHealth(client, stop, clock=lambda: now[0], suspend_clock=lambda: now[0])
    return health, client, stop, now


def test_repeated_socket_creation_without_pongs_does_not_reset_deadline():
    health, client, stop, now = monitor()
    for elapsed in range(0, 121, 10):
        now[0] = elapsed
        client.current_session = SimpleNamespace(session_id=str(elapsed), last_ping_pong_time=None)
        health.check()
        assert health.failed == (elapsed == 120)
    assert not stop.is_set()


def test_real_pongs_keep_quiet_connection_healthy_and_stale_pong_expires():
    health, client, _stop, now = monitor()
    session = SimpleNamespace(session_id="a", last_ping_pong_time=None)
    client.current_session = session
    for elapsed in range(0, 601, 5):
        now[0] = elapsed
        session.last_ping_pong_time = elapsed
        health.check()
        assert not health.failed
    now[0] = 719
    health.check()
    assert not health.failed
    now[0] = 720
    health.check()
    assert health.failed


def test_brief_disconnect_and_new_healthy_session_recovers():
    health, client, _stop, now = monitor()
    client.current_session = SimpleNamespace(session_id="a", last_ping_pong_time=1000)
    health.check()
    client.current_session = None
    now[0] = 100
    health.check()
    assert not health.failed
    client.current_session = SimpleNamespace(session_id="b", last_ping_pong_time=1000)
    health.check()
    now[0] = 150
    health.check()
    assert not health.failed


def test_shutdown_does_not_wait_for_health_timeout():
    health, _client, stop, _now = monitor()
    health.start()
    health.close()
    assert not health.worker.is_alive()
    assert not stop.is_set()


def test_resume_replaces_the_connection_even_if_a_new_pong_arrives():
    health, client, stop, now = monitor()
    slept = [0]
    health.suspend_clock = lambda: now[0] + slept[0]
    client.current_session = SimpleNamespace(session_id="a", last_ping_pong_time=1)
    health.check()
    now[0] = 5
    slept[0] = 8 * 60 * 60
    client.current_session.last_ping_pong_time = 2
    health.check()
    assert health.failed and not stop.is_set()


def test_awake_elapsed_time_and_short_sleep_do_not_force_restart():
    health, client, _stop, now = monitor()
    health.suspend_clock = lambda: now[0] + 10
    for elapsed in range(0, 301, 5):
        now[0] = elapsed
        client.current_session = SimpleNamespace(session_id="a", last_ping_pong_time=elapsed)
        health.check()
    assert not health.failed


def test_flapping_connections_with_one_pong_each_still_fail(caplog):
    health, client, _stop, now = monitor()
    for attempt in range(5):
        now[0] = attempt * 10
        client.current_session = SimpleNamespace(session_id=str(attempt), last_ping_pong_time=1)
        health.check()
        health.on_error(BrokenPipeError("secret-url"))
        health.check()
        assert health.failed == (attempt == 4)
    assert "repeated transport failures" in caplog.text
    assert "secret-url" not in caplog.text


def test_sustained_pongs_forgive_transient_errors():
    health, client, _stop, now = monitor()
    for _ in range(4):
        health.on_error(BrokenPipeError())
    for elapsed in range(0, 31, 5):
        now[0] = elapsed
        client.current_session = SimpleNamespace(session_id="a", last_ping_pong_time=elapsed)
        health.check()
    for _ in range(4):
        health.on_error(BrokenPipeError())
    health.check()
    assert not health.failed


def test_a_stale_pong_gap_does_not_forgive_errors():
    health, client, _stop, now = monitor()
    client.current_session = SimpleNamespace(session_id="a", last_ping_pong_time=1)
    health.on_error(BrokenPipeError())
    health.check()
    now[0] = 25
    health.check()
    now[0] = 30
    client.current_session.last_ping_pong_time = 2
    health.check()
    for _ in range(4):
        health.on_error(BrokenPipeError())
    health.check()
    assert health.failed


def test_observation_failure_fails_the_connection_instead_of_killing_watcher(caplog):
    health, client, stop, _ = monitor()
    client.current_session = object()
    health.check()
    assert health.failed and not stop.is_set()
    assert "health observation failed (AttributeError)" in caplog.text


def test_sdk_error_listener_is_removed_on_shutdown():
    health, client, _stop, _ = monitor()
    health.start()
    try:
        for _ in range(5):
            for listener in client.on_error_listeners:
                listener(BrokenPipeError())
        # The service loop can observe the failure even before the worker's next tick.
        health.check()
        assert health.failed
    finally:
        health.close()
    assert not client.on_error_listeners


def test_real_worker_restarts_a_daemon_whose_service_loop_never_recovers():
    client = SimpleNamespace(current_session=None, on_error_listeners=[])
    stop = threading.Event()
    health = ConnectionHealth(client, stop, timeout=0.01, patience=0)
    health.start()
    try:
        assert stop.wait(10), "background watcher did not enforce heartbeat deadline"
        assert health.failed and health.escalated
    finally:
        health.close()


def test_restart_waits_for_the_service_loop_to_replace_the_connection():
    health, client, stop, now = monitor()
    client.current_session = object()
    health.check()
    now[0] = 59
    health.escalate()
    assert not stop.is_set() and not health.escalated
    now[0] = 60
    health.escalate()
    assert stop.is_set() and health.escalated


def test_healthy_connection_is_never_escalated():
    health, client, stop, now = monitor()
    client.current_session = SimpleNamespace(session_id="a", last_ping_pong_time=1)
    health.check()
    now[0] = 100
    health.escalate()
    assert not stop.is_set()


def test_only_sustained_pongs_count_as_stable():
    health, client, _stop, now = monitor()
    session = SimpleNamespace(session_id="a", last_ping_pong_time=0)
    client.current_session = session
    for elapsed in range(0, 31, 5):
        now[0] = elapsed
        session.last_ping_pong_time = elapsed
        health.check()
        assert health.stable() == (elapsed == 30)
