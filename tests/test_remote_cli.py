"""Mode-specific diagnostics across the server boundary."""

import json
import sys
import time
from types import SimpleNamespace

import pytest
from websockets.sync.client import connect

from slack_conductor.__main__ import main
from slack_conductor.config import Config
from slack_conductor.remote import VERSION, RemoteConductor

TOKEN = "secret-token-with-at-least-thirty-two-chars"


def test_removed_daemon_command_is_rejected_before_loading_configuration(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["sc", "daemon"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2


@pytest.mark.parametrize(
    "command",
    [["new", "/repo", "prompt"], ["send", "w", "prompt"], ["archive", "w"], ["tail", "s"]],
)
def test_native_commands_reject_server_configuration(tmp_path, monkeypatch, command):
    (tmp_path / "config.toml").write_text(f'mode = "server"\nstate_dir = "{tmp_path / "state"}"\n')
    monkeypatch.setattr(sys, "argv", ["sc", "--root", str(tmp_path), *command])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2


def test_server_doctor_checks_authenticated_health_without_native_adapter(
    tmp_path, monkeypatch, capsys
):
    state = tmp_path / "state"
    state.mkdir()
    remote = RemoteConductor(Config(state_dir=state, server_port=0), TOKEN)
    remote.start({"s1": "w1"})
    try:
        port = remote.server.socket.getsockname()[1]
        token_file = tmp_path / "token"
        token_file.write_text(TOKEN)
        token_file.chmod(0o600)
        (tmp_path / "config.toml").write_text(
            f'mode = "server"\nserver_port = {port}\n'
            f'remote_token_file = "{token_file}"\nstate_dir = "{state}"\n'
        )
        monkeypatch.setattr(sys, "argv", ["sc", "--root", str(tmp_path), "doctor"])
        with pytest.raises(RuntimeError, match="not ready"):
            main()
        url = f"ws://127.0.0.1:{port}/client"
        with connect(
            url, additional_headers={"Authorization": "Bearer " + TOKEN, "X-Client-ID": "mac"}
        ) as client:
            client.recv()
            client.send(json.dumps({"version": VERSION, "type": "ready", "ready": True}))
            deadline = time.monotonic() + 2
            while not remote.ready() and time.monotonic() < deadline:
                time.sleep(0.01)
            main()
        assert "Server health: ready" in capsys.readouterr().out
    finally:
        remote.close()


def test_client_doctor_requires_fresh_remote_link_and_native_attachment(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    (tmp_path / "config.toml").write_text(f'mode = "client"\nstate_dir = "{state}"\n')
    attached = [True]
    from slack_conductor.conductor import native

    monkeypatch.setattr(
        native,
        "Conductor",
        lambda *_args, **_kwargs: SimpleNamespace(
            db=SimpleNamespace(query=lambda *_: []),
            transport=SimpleNamespace(online=lambda: attached[0]),
        ),
    )
    monkeypatch.setattr(sys, "argv", ["sc", "--root", str(tmp_path), "doctor"])
    with pytest.raises(RuntimeError, match="link is not connected"):
        main()
    path = state / "remote-status.json"
    path.write_text(json.dumps({"connected": True, "at": time.time() - 120, "client_id": "mac"}))
    with pytest.raises(RuntimeError, match="link is not connected"):
        main()
    path.write_text(json.dumps({"connected": True, "at": time.time(), "client_id": "mac"}))
    main()
    attached[0] = False
    with pytest.raises(RuntimeError, match="Native attachment"):
        main()
