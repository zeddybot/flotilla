"""Server handshake and single-client ownership without a Mac executor."""

import json
import time

import pytest
from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect

from slack_conductor.config import Config
from slack_conductor.remote import VERSION, RemoteConductor

TOKEN = "secret-token-with-at-least-thirty-two-chars"


@pytest.fixture
def server(tmp_path):
    state = tmp_path / "server"
    state.mkdir()
    remote = RemoteConductor(Config(state_dir=state, server_port=0), TOKEN)
    remote.start({"s1": "w1"})
    url = f"ws://127.0.0.1:{remote.server.socket.getsockname()[1]}/client"
    try:
        yield remote, url
    finally:
        remote.close()


def headers(client_id="mac"):
    return {"Authorization": "Bearer " + TOKEN, "X-Client-ID": client_id}


def test_handshake_rejects_wrong_auth_client_and_protocol(server):
    remote, url = server
    with pytest.raises(InvalidStatus) as denied:
        connect(url)
    assert denied.value.response.status_code == 401
    with pytest.raises(InvalidStatus) as wrong_client:
        connect(url, additional_headers=headers("other"))
    assert wrong_client.value.response.status_code == 403
    with connect(url, additional_headers=headers()) as peer:
        assert json.loads(peer.recv())["version"] == VERSION
        peer.send(json.dumps({"version": 999, "type": "ready", "ready": True}))
        with pytest.raises(Exception, match="Unsupported protocol"):
            peer.recv()
    assert not remote.ready()


def test_duplicate_client_is_fenced_without_eviction(server):
    remote, url = server
    with connect(url, additional_headers=headers()) as first:
        assert json.loads(first.recv())["bindings"] == {"s1": "w1"}
        first.send(json.dumps({"version": VERSION, "type": "ready", "ready": True}))
        with (
            connect(url, additional_headers=headers()) as second,
            pytest.raises(Exception, match="Client already connected"),
        ):
            second.recv()
        first.send(json.dumps({"version": VERSION, "type": "ready", "ready": True}))
        deadline = time.monotonic() + 2
        while not remote.ready() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert remote.ready()
