import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "install_service", Path(__file__).resolve().parents[1] / "scripts/install_service.py"
)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def test_unload_waits_for_launchd_to_remove_the_job(monkeypatch):
    results = iter([0, 0, 0, 113])
    commands, delays = [], []

    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=next(results))

    monkeypatch.setattr(installer.subprocess, "run", run)
    monkeypatch.setattr(installer.time, "sleep", delays.append)
    installer.unload("gui/501")
    assert [command[1] for command in commands] == ["bootout", "print", "print", "print"]
    assert delays == [0.25, 0.25]


def test_unload_stops_installation_if_old_job_never_exits(monkeypatch):
    ticks = iter([0, 26])
    monkeypatch.setattr(installer.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(installer.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0))
    with pytest.raises(RuntimeError, match="runtime was left unchanged"):
        installer.unload("gui/501")


def test_client_plist_and_runtime_config_contain_no_slack_credentials(tmp_path):
    runtime = tmp_path / "runtime"
    source = tmp_path / "config.toml"
    source.write_text(
        'mode = "local"\nremote_token_file = "/old/token"\n[models]\nsol = "GPT-6 Sol"\n'
    )
    settings = installer.runtime_config(source, runtime, "client")
    assert 'mode = "client"' in settings
    assert f'remote_token_file = "{runtime / "remote.token"}"' in settings
    assert "/old/token" not in settings
    assert '[models]\nsol = "GPT-6 Sol"' in settings
    assert installer.plist(runtime, tmp_path, "client")["ProgramArguments"][-1] == "client"
