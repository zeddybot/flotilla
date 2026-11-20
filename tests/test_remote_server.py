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


def test_paired_clients_use_registry_identity_and_isolated_state(server, tmp_path):
    from hashlib import sha256

    from slack_conductor.monitoring import _server_health
    from slack_conductor.store import Store

    remote, url = server
    origin = url.removesuffix("/client")
    tokens = ["paired-token-" + suffix * 32 for suffix in ("a", "b")]
    profiles = []
    for index, token in enumerate(tokens, start=1):
        code = format(index, "032x")
        with connect(
            origin + "/pair",
            additional_headers={
                "X-Flotilla-Pair-Code": code,
                "X-Flotilla-Token-SHA256": sha256(token.encode()).hexdigest(),
                "X-Flotilla-Profile": f"mac-{index}",
                "X-Flotilla-Backend": "conductor",
            },
        ) as enrollment:
            assert json.loads(enrollment.recv()) == {"ok": True}
        authorization = {"Authorization": "Bearer " + token, "X-Client-ID": "mac"}
        with pytest.raises(InvalidStatus) as pending:
            connect(url, additional_headers=authorization)
        assert pending.value.response.status_code == 401

        profile = remote.pairing.claim(code, "T1", f"U{index}")
        profiles.append(profile)
        with connect(origin + "/pair/check", additional_headers=authorization) as check:
            assert json.loads(check.recv()) == {"ok": True, "profile_id": profile.id}
        state_dir = remote.config.state_dir / "clients" / profile.id
        state_dir.mkdir(parents=True)
        Store(state_dir / "bridge.sqlite").bind(
            "C1", "1", {"id": f"workspace-{index}", "active_session_id": "shared"}, {}
        )

    first, second = profiles
    credentials = tmp_path / "paired.token"
    credentials.write_text(tokens[1])
    credentials.chmod(0o600)
    client_config = Config(server_url=url, remote_token_file=credentials)
    assert _server_health(client_config)["detail"] == "server not ready"
    with (
        connect(url, additional_headers=headers()) as legacy,
        connect(
            url,
            additional_headers={"Authorization": "Bearer " + tokens[0], "X-Client-ID": second.id},
        ) as one,
        connect(url, additional_headers={"Authorization": "Bearer " + tokens[1]}) as two,
    ):
        assert json.loads(legacy.recv())["bindings"] == {"s1": "w1"}
        assert json.loads(one.recv())["bindings"] == {"shared": "workspace-1"}
        assert json.loads(two.recv())["bindings"] == {"shared": "workspace-2"}
        first_endpoint = remote.for_client(first.id)
        second_endpoint = remote.for_client(second.id)
        first_endpoint.receipts.begin("overlap", "runtime.create", {"client": 1})
        first_endpoint.receipts.finish("overlap", {"value": 1})
        assert second_endpoint.receipts.begin("overlap", "runtime.create", {"client": 2}) is None

        assert remote.pairing.revoke(first.team_id, first.user_id, first.name)
        with pytest.raises(Exception, match="Client access revoked"):
            one.recv(timeout=3)
        assert not first_endpoint.ready()
        with pytest.raises(InvalidStatus) as denied:
            connect(url, additional_headers={"Authorization": "Bearer " + tokens[0]})
        assert denied.value.response.status_code == 401
        with pytest.raises(InvalidStatus) as private:
            connect(origin + "/status", additional_headers={"Authorization": "Bearer " + tokens[1]})
        assert private.value.response.status_code == 401
        two.send(json.dumps({"version": VERSION, "type": "ready", "ready": True}))
        deadline = time.monotonic() + 2
        while not second_endpoint.ready() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert second_endpoint.ready()
        assert _server_health(client_config)["detail"] == "server ready"
        assert not remote.ready()
