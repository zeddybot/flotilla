"""Exercise connection boundaries without a running desktop or Slack account."""

import json
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from urllib.parse import urlparse

import pytest

from slack_conductor.conductor.client import Client
from slack_conductor.conductor.journal import Journal
from slack_conductor.conductor.runtime import session_snapshot
from slack_conductor.conductor.transport import Transport


def observation(at=100):
    return {
        "ready": True,
        "at": at,
        "incarnation": "app",
        "sessions": {"s": {"at": at, "live": {"status": "working"}}},
    }


@pytest.mark.parametrize(
    "snapshot",
    [
        {},
        {**observation(), "ready": False},
        {**observation(), "sessions": None},
        {**observation(), "sessions": {"s": None}},
        observation(90),
        observation(110),
        {**observation(), "sessions": {"s": {"at": "bad", "live": {}}}},
    ],
)
def test_absent_or_invalid_runtime_never_looks_idle(snapshot):
    assert session_snapshot(snapshot, "s", now=100) is None


def test_fresh_runtime_preserves_agent_state():
    assert session_snapshot(observation(), "s", now=101) == {
        "at": 100,
        "live": {"status": "working"},
        "incarnation": "app",
    }
    assert session_snapshot(observation(), "other", now=101) is None


def test_cli_rejects_stale_or_replaced_app_heartbeat(tmp_path, monkeypatch):
    from slack_conductor.conductor import client as module

    monkeypatch.setattr(module.time, "time", lambda: 100)
    monkeypatch.setattr(module, "incarnation", lambda: "app")
    client = Client(tmp_path)
    path = tmp_path / "native-heartbeat.json"
    assert not client.online()
    for value in [
        None,
        [],
        observation(80),
        {**observation(), "incarnation": "old"},
        {**observation(), "ready": False},
    ]:
        path.write_text(json.dumps(value))
        assert not client.online()
        assert client.session("s") is None
    path.write_text(json.dumps(observation()))
    assert client.online()
    assert client.session("s")["live"]["status"] == "working"
    monkeypatch.setattr(module, "incarnation", lambda: None)
    assert not client.online()


@pytest.mark.parametrize(
    ("body", "length"), [("[]", None), ("null", None), ("{", None), ("", "-1"), ("", "2000001")]
)
def test_malformed_http_request_is_rejected_without_poisoning_transport(tmp_path, body, length):
    transport = Transport(tmp_path, lambda _: True)
    transport.start()
    config = transport.attach("app")
    url = urlparse(config["base"])
    connection = HTTPConnection(url.hostname, url.port, timeout=2)
    try:
        connection.request(
            "POST",
            url.path + "/heartbeat",
            body=body,
            headers={"Content-Length": length or str(len(body))},
        )
        response = connection.getresponse()
        assert response.status == 400
        response.read()
        connection.request("GET", url.path + "/valid?generation=" + config["generation"])
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read()) is True
        assert not transport.online()
    finally:
        connection.close()
        transport.close()


def test_concurrent_consumers_claim_each_command_once(tmp_path):
    path = tmp_path / "native.sqlite"
    journal = Journal(path)
    for i in range(20):
        journal.enqueue(str(i), {"op": "watch", "sid": str(i)})

    def claim_all(_):
        reader = Journal(path)
        claimed = []
        while command := reader.claim("app"):
            claimed.append(command["id"])
        return claimed

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.map(claim_all, range(2))
    assert len(first + second) == 20
    assert set(first + second) == {str(i) for i in range(20)}


def request(config, route):
    url = urlparse(config["base"])
    connection = HTTPConnection(url.hostname, url.port, timeout=2)
    try:
        connection.request("GET", f"{url.path}/{route}?generation={config['generation']}")
        response = connection.getresponse()
        response.read()
        return response.status
    finally:
        connection.close()


def served(tmp_path, alive):
    transport = Transport(tmp_path, alive, "v1")
    transport.start()
    try:
        return transport.attach("app")
    finally:
        transport.close()


def restarted(tmp_path, config, version="v1"):
    transport = Transport(tmp_path, lambda app: app == "app", version)
    transport.start()
    try:
        return transport.attachment, request(config, "valid")
    finally:
        transport.close()


def test_restarted_daemon_serves_the_address_its_frontend_already_has(tmp_path):
    config = served(tmp_path, lambda app: app == "app")
    assert (tmp_path / "native-attachment.json").stat().st_mode & 0o077 == 0
    attachment, status = restarted(tmp_path, config)
    assert attachment == {"generation": config["generation"], "incarnation": "app"}
    assert status == 200


def test_upgraded_daemon_retires_the_old_frontend(tmp_path):
    config = served(tmp_path, lambda app: app == "app")
    upgraded = Transport(tmp_path, lambda app: app == "app", "v2")
    upgraded.start()
    try:
        assert upgraded.attachment is None
        assert upgraded.token not in config["base"]
    finally:
        upgraded.close()


def test_restarted_conductor_is_never_adopted(tmp_path):
    config = served(tmp_path, lambda app: app == "relaunched")
    second = Transport(tmp_path, lambda app: app == "relaunched", "v1")
    second.start()
    try:
        assert not second.expects("relaunched")
        assert request(config, "valid") == 409
    finally:
        second.close()


OUT_OF_RANGE = json.dumps(
    {"port": 70000, "token": "t", "generation": "g", "incarnation": "app", "version": ""}
)


@pytest.mark.parametrize(
    "record", ["", "[]", "{}", '{"port": "x"}', '{"port": 1, "version": ""}', OUT_OF_RANGE]
)
def test_unreadable_attachment_record_starts_fresh(tmp_path, record):
    (tmp_path / "native-attachment.json").write_text(record)
    transport = Transport(tmp_path, lambda _: True)
    transport.start()
    try:
        assert transport.attachment is None
        assert transport.attach("app")["generation"]
    finally:
        transport.close()


def test_occupied_address_starts_fresh_instead_of_failing(tmp_path):
    import socket

    config = served(tmp_path, lambda _: True)
    with socket.socket() as squatter:
        squatter.bind(("127.0.0.1", urlparse(config["base"]).port))
        squatter.listen()
        transport = Transport(tmp_path, lambda _: True, "v1")
        transport.start()
        try:
            assert transport.attachment is None
            assert transport.server.server_port != squatter.getsockname()[1]
        finally:
            transport.close()


def test_shutdown_tells_the_frontend_to_rest_and_stops_counting_it_online(tmp_path):
    transport = Transport(tmp_path, lambda _: True, "v1")
    transport.start()
    config = transport.attach("app")
    url = urlparse(config["base"])

    def beat():
        connection = HTTPConnection(url.hostname, url.port, timeout=2)
        try:
            connection.request(
                "POST",
                url.path + "/heartbeat",
                body=json.dumps({"generation": config["generation"], "ready": True}),
            )
            return json.loads(connection.getresponse().read())
        finally:
            connection.close()

    try:
        transport.rest(timeout=0)
        assert not transport.rested.is_set()
        transport.resting = False
        assert beat() is True
        assert transport.online()
        with ThreadPoolExecutor(max_workers=1) as pool:
            resting = pool.submit(transport.rest, 5)
            while not transport.resting:
                pass
            assert beat() == {"rest": True}
            resting.result(timeout=5)
        assert not transport.online()
    finally:
        transport.close()
