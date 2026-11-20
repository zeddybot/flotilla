import json
from hashlib import sha256

from slack_conductor import cli_pairing
from slack_conductor.cli import main
from slack_conductor.config import Config, remote_credentials
from slack_conductor.pairing import PairingStore


def test_client_connect_generates_private_credentials_and_finishes_after_dm(
    tmp_path, capsys, monkeypatch
):
    store = PairingStore(tmp_path / "pairing.sqlite")
    root = tmp_path / "client"
    calls = []

    def exchange(server, path, headers):
        calls.append((server, path, headers))
        if path == "/pair":
            store.register(
                headers["X-Flotilla-Pair-Code"],
                headers["X-Flotilla-Token-SHA256"],
                headers["X-Flotilla-Profile"],
                headers["X-Flotilla-Backend"],
            )
            return {"ok": True}
        token = headers["Authorization"].removeprefix("Bearer ")
        profile = store.authenticate(token)
        return {"ok": True, "profile_id": profile.id} if profile else {"ok": False}

    monkeypatch.setattr(cli_pairing, "_exchange", exchange)
    args = ["--json", "--root", str(root), "client", "connect"]
    assert (
        main(
            args
            + [
                "wss://control.test",
                "--profile",
                "daytona",
                "--runtime-adapter",
                "headless",
                "--repo",
                str(tmp_path),
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["data"]["paired"] is False
    assert calls[0][0] == "wss://control.test/client"
    assert "Authorization" not in calls[0][2]
    pending = root / ".client-pairing.json"
    token = json.loads(pending.read_text())["token"]
    assert token not in str(output)
    assert sha256(token.encode()).hexdigest() == calls[0][2]["X-Flotilla-Token-SHA256"]
    assert pending.stat().st_mode & 0o777 == 0o600
    assert not (root / "config.toml").exists()
    assert main(args + ["--check"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "pairing_unconfirmed"

    code = output["data"]["slack_dm"].removeprefix("connect ")
    profile = store.claim(code, "T1", "U1")
    assert main(args + ["--check"]) == 0
    confirmed = json.loads(capsys.readouterr().out)["data"]
    assert confirmed["profile_id"] == profile.id
    assert not pending.exists()
    config = Config.load(root)
    assert config.client_id == profile.id
    assert config.runtime_adapter == "headless"
    assert remote_credentials(config) == token
    assert main(args + ["--check"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["profile_id"] == profile.id
