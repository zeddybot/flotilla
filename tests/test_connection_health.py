import threading
from types import SimpleNamespace

from slack_conductor.connection_health import ConnectionHealth


def monitor():
    now = [0]
    client = SimpleNamespace(current_session=None)
    stop = threading.Event()
    health = ConnectionHealth(client, stop, clock=lambda: now[0])
    return health, client, stop, now


def test_repeated_socket_creation_without_pongs_does_not_reset_deadline():
    health, client, stop, now = monitor()
    for elapsed in range(0, 121, 10):
        now[0] = elapsed
        client.current_session = SimpleNamespace(session_id=str(elapsed), last_ping_pong_time=None)
        health.check()
        assert stop.is_set() == (elapsed == 120)
    assert health.failed


def test_real_pongs_keep_quiet_connection_healthy_and_stale_pong_expires():
    health, client, stop, now = monitor()
    session = SimpleNamespace(session_id="a", last_ping_pong_time=None)
    client.current_session = session
    for elapsed in range(0, 601, 5):
        now[0] = elapsed
        session.last_ping_pong_time = elapsed
        health.check()
        assert not stop.is_set()
    now[0] = 719
    health.check()
    assert not stop.is_set()
    now[0] = 720
    health.check()
    assert stop.is_set()


def test_brief_disconnect_and_new_healthy_session_recovers():
    health, client, stop, now = monitor()
    client.current_session = SimpleNamespace(session_id="a", last_ping_pong_time=1000)
    health.check()
    client.current_session = None
    now[0] = 100
    health.check()
    assert not stop.is_set()
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
