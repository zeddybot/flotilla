"""User configuration and credentials are tested without touching the live installation."""

import json

import pytest

from slack_conductor.config import Config, credentials

TOKENS = {
    "SLACK_BOT_TOKEN": "xoxb-test",
    "SLACK_APP_TOKEN": "xapp-test",
    "OWNER_SLACK_USER_ID": "U123",
    "SLACK_TEAM_ID": "T123",
}


def test_config_overrides_are_loaded_without_mutating_other_profiles(tmp_path):
    state = tmp_path / "state"
    (tmp_path / "config.toml").write_text(
        f'state_dir = {json.dumps(str(state))}\nsend_mode = "queue"\nmodel = "GPT-6 Astra"\n'
    )
    config = Config.load(tmp_path)
    assert config.send_mode == "queue" and config.model == "GPT-6 Astra"
    assert state.is_dir()
    config.models["custom"] = "test"
    assert "custom" not in Config().models


def test_invalid_send_mode_fails_before_creating_state(tmp_path):
    state = tmp_path / "state"
    (tmp_path / "config.toml").write_text(
        f'state_dir = {json.dumps(str(state))}\nsend_mode = "bogus"\n'
    )
    with pytest.raises(ValueError, match="send_mode"):
        Config.load(tmp_path)
    assert not state.exists()


@pytest.mark.parametrize("key", list(TOKENS))
def test_invalid_credentials_identify_field_without_disclosing_value(tmp_path, monkeypatch, key):
    for name, value in TOKENS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv(key, "private-invalid-value")
    with pytest.raises(RuntimeError, match=key) as error:
        credentials(tmp_path)
    assert "private-invalid-value" not in str(error.value)


def test_private_env_does_not_override_process_credentials(tmp_path, monkeypatch):
    for key, value in TOKENS.items():
        monkeypatch.setenv(key, value)
    path = tmp_path / ".env"
    path.write_text("SLACK_BOT_TOKEN=xoxb-from-file\n")
    path.chmod(0o644)
    with pytest.raises(RuntimeError, match="permissions 600"):
        credentials(tmp_path)
    path.chmod(0o600)
    assert credentials(tmp_path) == TOKENS
