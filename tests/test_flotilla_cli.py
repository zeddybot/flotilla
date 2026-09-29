"""Focused Flotilla CLI behavior without native or service side effects."""

import json
import subprocess
from pathlib import Path

from slack_conductor import cli, cli_config, cli_sessions
from slack_conductor.config import Config


def _config(path, mode="client"):
    path.mkdir(parents=True, exist_ok=True)
    token = path / "token"
    token.write_text("a-private-token-with-at-least-thirty-two-characters")
    token.chmod(0o600)
    (path / "config.toml").write_text(
        f'mode = "{mode}"\nstate_dir = "{path / "state"}"\n'
        f'remote_token_file = "{token}"\nserver_url = "wss://example.com/client"\n'
    )
    return path


def test_config_discovery_precedence(tmp_path, monkeypatch):
    cwd = _config(tmp_path / "cwd")
    override = _config(tmp_path / "override")
    home = _config(tmp_path / ".config/flotilla")
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert cli_config.discover_root() == cwd
    monkeypatch.setenv("FLOTILLA_CONFIG_DIR", str(override))
    assert cli_config.discover_root() == override
    assert cli_config.discover_root(home / "config.toml") == home
    assert cli_config.discover_root(tmp_path / "missing") == tmp_path / "missing"


def test_help_does_not_load_config_or_native_adapter(monkeypatch, capsys):
    monkeypatch.setattr(
        cli, "load_config", lambda root: (_ for _ in ()).throw(AssertionError(root))
    )
    try:
        cli.main(["--help"])
    except SystemExit as exc:
        assert exc.code == 0
    assert "session" in capsys.readouterr().out


def test_json_config_validation_and_missing_error(tmp_path, capsys):
    root = _config(tmp_path / "config")
    assert cli.main(["config", "validate", "--root", str(root), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is True
    assert result["data"]["mode"] == "client"
    assert cli.main(["--json", "--root", str(tmp_path / "missing"), "config", "validate"]) == 1
    error = json.loads(capsys.readouterr().out)
    assert error == {
        "ok": False,
        "error": {
            "code": "config_missing",
            "message": f"Config file does not exist: {tmp_path / 'missing' / 'config.toml'}",
        },
    }


def test_doctor_unhealthy_exits_nonzero_but_status_is_readable(tmp_path, monkeypatch, capsys):
    root = _config(tmp_path / "config")
    monkeypatch.setattr(cli, "_report", lambda *_: {"ok": False, "checks": {"link": "down"}})
    assert cli.main(["--root", str(root), "--json", "status"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["ok"] is False
    assert cli.main(["--root", str(root), "--json", "doctor"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "unhealthy"


def test_real_status_doctor_and_monitor_shapes(tmp_path, capsys):
    root = _config(tmp_path / "server", mode="server")
    assert cli.main(["--json", "--root", str(root), "status"]) == 0
    status = json.loads(capsys.readouterr().out)["data"]
    assert status["role"] == "server" and status["ok"] is False
    assert cli.main(["--json", "--root", str(root), "doctor"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "unhealthy"
    assert cli.main(["--json", "--root", str(root), "monitor"]) == 0
    monitored = json.loads(capsys.readouterr().out)["data"]
    assert "incidents" in monitored
    assert (root / "state/monitoring.json").exists()


def test_session_calls_runtime_with_distinct_stop_and_archive(monkeypatch):
    calls = []

    class FakeRuntime:
        def probe(self):
            return {"capabilities": {"interrupt": False, "reveal": False}}

        def sessions(self):
            return [{"session_id": "owned"}]

        def describe(self, sid):
            assert sid == "owned"
            return {"session_id": sid}

        def create(self, command_id, spec):
            calls.append(("create", command_id, spec))
            return {"session_id": "owned", "workspace_id": "w"}

        def submit(self, command_id, sid, message_id, run_id, prompt, options):
            calls.append(("submit", command_id, sid, prompt, options))
            return {"state": "received"}

        def close_session(self, command_id, sid):
            calls.append(("archive", command_id, sid))
            return {"archived": True}

        def interrupt(self, command_id, sid):
            calls.append(("interrupt", command_id, sid))

    monkeypatch.setattr(cli_sessions, "runtime_for_cli", lambda *_: FakeRuntime())
    config = Config(mode="client", repo=Path("/repo"))
    assert cli_sessions.list_sessions(config) == {"sessions": [{"session_id": "owned"}]}
    result = cli_sessions.new_session(config, "prompt", command_id="create-1")
    assert result["delivery"]["state"] == "received"
    assert calls[0][0:2] == ("create", "create-1")
    assert calls[1][0] == "submit"
    assert cli_sessions.archive(config, "owned", "archive-1") == {"archived": True}
    assert calls[-1] == ("archive", "archive-1", "owned")
    try:
        cli_sessions.stop(config, "owned")
    except cli_config.CLIError as exc:
        assert exc.code == "unsupported_capability"
    else:
        raise AssertionError("unsupported stop was invoked")
    assert not any(call[0] == "interrupt" for call in calls)


def test_session_tail_uses_cursor(monkeypatch):
    class FakeRuntime:
        def describe(self, sid):
            return {"session_id": sid}

        def observe(self, sid, *, after):
            assert after == 7
            return {"events": [{"type": "text", "cursor": 8}], "cursor": 8}

    monkeypatch.setattr(cli_sessions, "runtime_for_cli", lambda *_: FakeRuntime())
    assert list(cli_sessions.tail(Config(), "owned", after=7)) == [{"type": "text", "cursor": 8}]


def test_send_retry_reuses_message_and_run_ids(monkeypatch):
    attempts = []

    class FakeRuntime:
        def describe(self, sid):
            return {"session_id": sid}

        def submit(self, *args):
            attempts.append(args)
            return {"state": "queued"}

    monkeypatch.setattr(cli_sessions, "runtime_for_cli", lambda *_: FakeRuntime())
    config = Config()
    cli_sessions.send(config, "owned", "hello", command_id="retry-id")
    cli_sessions.send(config, "owned", "hello", command_id="retry-id")
    assert attempts[0] == attempts[1]
    assert attempts[0][0] == "retry-id"
    assert attempts[0][-1] == {"send_mode": config.send_mode}


def test_client_service_dispatch_is_host_local(tmp_path, monkeypatch, capsys):
    root = _config(tmp_path / "config")
    from slack_conductor import service_manager

    calls = []
    monkeypatch.setattr(
        service_manager, "client_status", lambda: calls.append("status") or "running"
    )
    monkeypatch.setattr(
        service_manager,
        "client_logs",
        lambda selected, *, follow, lines: calls.append((selected, follow, lines)) or "log",
    )
    assert cli.main(["--root", str(root), "client", "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["data"] == "running"
    assert cli.main(["client", "logs", "--lines", "3", "--root", str(root)]) == 0
    assert capsys.readouterr().out.strip() == "log"
    assert calls == ["status", (root, False, 3)]


def test_init_creates_private_config_without_copying_token(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("FLOTILLA_CONFIG_DIR", str(tmp_path / "ignored"))
    repo = tmp_path / "repo"
    repo.mkdir()
    workspaces = tmp_path / "workspaces"
    token = tmp_path / "token"
    secret = "a-private-token-with-at-least-thirty-two-characters"
    token.write_text(secret)
    token.chmod(0o600)
    command = [
        "init",
        "--server-url",
        "wss://example.com/client",
        "--token-file",
        str(token),
        "--repo",
        str(repo),
        "--workspace-root",
        str(workspaces),
        "--json",
    ]
    assert cli.main(command) == 0
    result = json.loads(capsys.readouterr().out)
    target = home / ".config/flotilla/config.toml"
    assert result["data"]["path"] == str(target)
    assert target.stat().st_mode & 0o777 == 0o600
    assert secret not in target.read_text()
    assert cli.main(["--root", str(target.parent), "config", "validate", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["credentials_valid"] is True
    assert cli.main(command) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "config_exists"


def test_init_rejects_insecure_token_and_nonlocal_ws(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    token = tmp_path / "token"
    token.write_text("a-private-token-with-at-least-thirty-two-characters")
    command = [
        "--json",
        "--root",
        str(tmp_path / "config"),
        "init",
        "--server-url",
        "ws://example.com/client",
        "--token-file",
        str(token),
        "--repo",
        str(repo),
        "--workspace-root",
        str(tmp_path / "workspaces"),
    ]
    assert cli.main(command) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "credential_error"
    assert not (tmp_path / "config/config.toml").exists()
    token.chmod(0o600)
    assert cli.main(command) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "credential_error"


def test_config_validate_distinguishes_credentials_from_toml(tmp_path, capsys):
    root = tmp_path / "root"
    root.mkdir()
    (root / "config.toml").write_text('mode = "client"\nserver_url = "wss://example.com/client"\n')
    assert cli.main(["--json", "--root", str(root), "config", "validate"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "credential_error"
    (root / "config.toml").write_text("invalid = [")
    assert cli.main(["--json", "--root", str(root), "config", "validate"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "config_error"


def test_prepare_only_reaches_installer(tmp_path, monkeypatch, capsys):
    root = _config(tmp_path / "config")
    from slack_conductor import service_manager

    calls = []
    monkeypatch.setattr(
        service_manager,
        "client_install",
        lambda path, *, prepare_only: calls.append((path, prepare_only)) or "prepared",
    )
    assert cli.main(["--root", str(root), "client", "install", "--prepare-only"]) == 0
    assert capsys.readouterr().out.strip() == "prepared"
    assert calls == [(root, True)]


def test_failed_service_command_is_one_redacted_json_error(tmp_path, monkeypatch, capsys):
    root = _config(tmp_path / "config")
    from slack_conductor import service_manager

    def fail(*_args, **_kwargs):
        raise subprocess.CalledProcessError(
            23, ["installer", "private-command"], output="private-stdout", stderr="private-stderr"
        )

    monkeypatch.setattr(service_manager, "client_install", fail)
    assert cli.main(["--json", "--root", str(root), "client", "install"]) == 1
    output = capsys.readouterr()
    assert output.err == ""
    assert len(output.out.splitlines()) == 1
    response = json.loads(output.out)
    assert response == {
        "ok": False,
        "error": {
            "code": "service_failed",
            "message": "Service command failed with exit status 23",
        },
    }
    assert "private" not in output.out
