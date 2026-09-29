import json

import pytest

from slack_conductor.config import Config, remote_credentials


def test_client_uses_private_token_and_localhost_dev_url(tmp_path):
    token_file = tmp_path / "remote.token"
    token_file.write_text("x" * 32 + "\n")
    token_file.chmod(0o600)
    (tmp_path / "config.toml").write_text(
        'mode = "client"\nserver_url = "ws://localhost:8765"\n'
        'remote_token_file = "remote.token"\n'
        f"state_dir = {json.dumps(str(tmp_path / 'state'))}\n"
    )
    config = Config.load(tmp_path)
    assert config.remote_token_file == token_file
    assert remote_credentials(config) == "x" * 32


@pytest.mark.parametrize(
    "url", ["ws://example.com", "http://localhost", "", "wss://user:pass@example.com"]
)
def test_client_rejects_insecure_or_invalid_url(tmp_path, url):
    token_file = tmp_path / "remote.token"
    token_file.write_text("x" * 32)
    token_file.chmod(0o600)
    config = Config(mode="client", server_url=url, remote_token_file=token_file)
    with pytest.raises(ValueError, match="server_url"):
        remote_credentials(config)


def test_server_only_needs_private_token(tmp_path):
    token_file = tmp_path / "remote.token"
    token_file.write_text("x" * 32)
    token_file.chmod(0o600)
    config = Config(mode="server", remote_token_file=token_file)
    assert remote_credentials(config) == "x" * 32
    token_file.chmod(0o644)
    with pytest.raises(RuntimeError, match="permissions 600"):
        remote_credentials(config)
    token_file.chmod(0o600)
    token_file.write_text("short")
    with pytest.raises(RuntimeError, match="at least 32"):
        remote_credentials(config)


def test_mode_validation_precedes_state_creation(tmp_path):
    (tmp_path / "config.toml").write_text(
        f'mode = "invalid"\nstate_dir = {json.dumps(str(tmp_path / "state"))}\n'
    )
    with pytest.raises(ValueError, match="mode"):
        Config.load(tmp_path)
    assert not (tmp_path / "state").exists()
