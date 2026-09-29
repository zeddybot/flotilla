"""Authenticated server status over the actual local transport."""

import json
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from websockets.sync.client import connect

from slack_conductor.config import Config
from slack_conductor.remote import VERSION, RemoteConductor
from slack_conductor.store import Store

TOKEN = "private-server-token-with-at-least-thirty-two-characters"


def _status(url, token=None):
    headers = {"Connection": "close"}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    with urlopen(Request(url, headers=headers), timeout=3) as response:
        return json.load(response)


def test_status_requires_bearer_and_exposes_health_without_private_content(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    store = Store(state / "bridge.sqlite")
    secret_prompt = "private customer prompt must stay on server"
    store.execute(
        "INSERT INTO statuses(event_key,channel,thread_ts,prompt,options,state) "
        "VALUES (?,?,?,?,?,'active')",
        ("event-1", "C1", "1", secret_prompt, "{}"),
    )
    server = RemoteConductor(Config(mode="server", state_dir=state, server_port=0), TOKEN)
    server.slack_ready = True
    server.start()
    port = server.server.socket.getsockname()[1]
    endpoint = f"http://127.0.0.1:{port}/status"
    try:
        for credential in (None, "wrong-token"):
            with pytest.raises(HTTPError) as error:
                _status(endpoint, credential)
            assert error.value.code == 401
            error.value.close()
        offline = _status(endpoint, TOKEN)
        assert offline["role"] == "server"
        assert offline["checks"]["runtime"]["state"] == "warning"
        assert offline["checks"]["slack"]["state"] == "ok"
        assert offline["checks"]["active_work"]["count"] == 1
        assert secret_prompt not in json.dumps(offline)
        assert TOKEN not in json.dumps(offline)
        assert not (state / "monitoring.json").exists()

        with connect(
            f"ws://127.0.0.1:{port}/client",
            additional_headers={"Authorization": "Bearer " + TOKEN, "X-Client-ID": "mac"},
        ) as client:
            client.recv()
            client.send(json.dumps({"version": VERSION, "type": "ready", "ready": True}))
            deadline = time.monotonic() + 2
            while not server.ready() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.ready()
            online = _status(endpoint, TOKEN)
            assert online["checks"]["runtime"]["state"] == "ok"
            assert online["checks"]["slack"]["state"] == "ok"
            assert secret_prompt not in json.dumps(online)
    finally:
        server.close()
