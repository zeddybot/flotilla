import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from slack_conductor import service_manager as manager


def _checkout(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    state = tmp_path / "state"
    token = tmp_path / "client.token"
    token.write_text("a" * 40)
    token.chmod(0o600)
    (root / "config.toml").write_text(
        f'mode = "client"\nserver_url = "wss://example.com/client"\n'
        f'state_dir = "{state}"\nremote_token_file = "{token}"\n'
    )
    (root / "pyproject.toml").write_text("[project]\nname='example'\nversion='0.1'\n")
    (root / "uv.lock").write_text("version = 1\n")
    (root / "README.md").write_text("build readme")
    (root / ".env").write_text("SLACK_BOT_TOKEN=never-copy")
    (root / "src").mkdir()
    (root / "src" / "placeholder.py").write_text("pass")
    return root, state


def test_prepare_copies_build_metadata_and_no_slack_env(tmp_path, monkeypatch):
    root, state = _checkout(tmp_path)
    calls = []
    monkeypatch.setattr(
        manager, "_run", lambda *a, **kw: calls.append(a) or SimpleNamespace(returncode=0)
    )
    runtime = state / "runtime"
    staging = state / "runtime-build"
    manager._prepare(root, root, state, runtime, staging, "/bin/uv")
    assert (staging / "README.md").read_text() == "build readme"
    assert not (staging / ".env").exists()
    assert (staging / "remote.token").stat().st_mode & 0o077 == 0
    assert (staging / "config.toml").stat().st_mode & 0o077 == 0
    assert str(runtime / "remote.token") in (staging / "config.toml").read_text()
    assert any(command[:2] == ("/bin/uv", "sync") for command in calls)


def test_install_build_failure_leaves_live_runtime_and_service_untouched(tmp_path, monkeypatch):
    root, state = _checkout(tmp_path)
    runtime = state / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "sentinel").write_text("running")
    monkeypatch.setattr(manager.shutil, "which", lambda name: "/bin/uv")
    monkeypatch.setattr(manager, "_domain", lambda: "gui/501")
    monkeypatch.setattr(manager, "_check_wrapper_target", lambda target: None)
    monkeypatch.setattr(
        manager, "_prepare", lambda *args: (_ for _ in ()).throw(RuntimeError("build failed"))
    )
    monkeypatch.setattr(manager, "unload", lambda *args: pytest.fail("unloaded before build"))
    with pytest.raises(RuntimeError, match="build failed"):
        manager.client_install(root)
    assert (runtime / "sentinel").read_text() == "running"


def test_install_bootstrap_failure_restores_old_runtime_and_plist(tmp_path, monkeypatch):
    root, state = _checkout(tmp_path)
    runtime = state / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "sentinel").write_text("previous")
    target = tmp_path / "service.plist"
    target.write_bytes(b"old plist")
    monkeypatch.setattr(manager.shutil, "which", lambda name: "/bin/uv")
    monkeypatch.setattr(manager, "_domain", lambda: "gui/501")
    monkeypatch.setattr(manager, "_target", lambda: target)
    monkeypatch.setattr(manager, "_check_wrapper_target", lambda target: None)
    monkeypatch.setattr(manager, "unload", lambda domain: None)

    def prepare(root, source_root, state, runtime, staging, uv):
        staging.mkdir()
        (staging / "sentinel").write_text("new")
        candidate = state / "candidate.plist"
        candidate.write_bytes(b"new plist")
        return candidate

    commands = []

    def run(*command, **kwargs):
        commands.append(command)
        if command[1] == "bootstrap" and len([c for c in commands if c[1] == "bootstrap"]) == 1:
            raise RuntimeError("bootstrap failed")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(manager, "_prepare", prepare)
    monkeypatch.setattr(manager, "_run", run)
    with pytest.raises(RuntimeError, match="bootstrap failed"):
        manager.client_install(root)
    assert (runtime / "sentinel").read_text() == "previous"
    assert target.read_bytes() == b"old plist"
    assert len([c for c in commands if c[1] == "bootstrap"]) == 2


def test_install_failure_restores_previous_generation_symlink(tmp_path, monkeypatch):
    root, state = _checkout(tmp_path)
    old = state / "runtime-releases" / "previous"
    old.mkdir(parents=True)
    runtime = state / "runtime"
    runtime.symlink_to(old, target_is_directory=True)
    target = tmp_path / "service.plist"
    target.write_bytes(b"old plist")
    monkeypatch.setattr(manager.shutil, "which", lambda name: "/bin/uv")
    monkeypatch.setattr(manager, "_domain", lambda: "gui/501")
    monkeypatch.setattr(manager, "_target", lambda: target)
    monkeypatch.setattr(manager, "_check_wrapper_target", lambda target: None)
    monkeypatch.setattr(manager, "unload", lambda domain: None)

    def prepare(root, source_root, state, runtime, generation, uv):
        generation.mkdir()
        candidate = state / "candidate.plist"
        candidate.write_bytes(b"new plist")
        return candidate

    bootstraps = 0

    def run(*command, **kwargs):
        nonlocal bootstraps
        if command[1] == "bootstrap":
            bootstraps += 1
            if bootstraps == 1:
                raise RuntimeError("bootstrap failed")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(manager, "_prepare", prepare)
    monkeypatch.setattr(manager, "_run", run)
    with pytest.raises(RuntimeError, match="bootstrap failed"):
        manager.client_install(root)
    assert runtime.is_symlink()
    assert runtime.resolve() == old
    assert target.read_bytes() == b"old plist"
    assert bootstraps == 2


def test_server_controls_reject_unsupervised_host(tmp_path):
    with pytest.raises(RuntimeError, match="require the local supervised"):
        manager.server_stop(tmp_path)
    assert not (tmp_path / "HOLD").exists()
    assert manager.server_status(tmp_path).startswith("Server not managed")


def test_wrapper_does_not_overwrite_unmanaged_command(tmp_path):
    target = tmp_path / "flotilla"
    target.write_text("#!/bin/sh\necho original\n")
    with pytest.raises(RuntimeError, match="Refusing to overwrite"):
        manager._check_wrapper_target(target)


def test_install_path_advice_is_returned_without_stdout(tmp_path, monkeypatch, capsys):
    root, _state = _checkout(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(manager.Path, "home", lambda: home)
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setattr(manager.shutil, "which", lambda name: "/bin/uv")
    monkeypatch.setattr(manager, "_domain", lambda: "gui/501")
    monkeypatch.setattr(manager, "_target", lambda: tmp_path / "service.plist")
    monkeypatch.setattr(manager, "_source_root", lambda: root)
    monkeypatch.setattr(manager, "_validate_runtime", lambda runtime: None)
    monkeypatch.setattr(manager, "unload", lambda domain: None)
    monkeypatch.setattr(manager, "_run", lambda *args, **kwargs: SimpleNamespace(returncode=0))

    def prepare(config_root, source_root, state_dir, runtime, generation, uv):
        generation.mkdir()
        candidate = state_dir / "candidate.plist"
        candidate.write_text("plist")
        return candidate

    monkeypatch.setattr(manager, "_prepare", prepare)
    result = manager.client_install(root)
    assert capsys.readouterr().out == ""
    assert f"Add {home / '.local/bin'} to PATH" in result
    assert (home / ".local/bin/flotilla").is_file()


@pytest.mark.skipif(sys.platform != "darwin", reason="plist validation requires macOS plutil")
def test_runtime_entrypoints_work_after_final_symlink_switch(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[1]
    root = tmp_path / "checkout"
    root.mkdir()
    for filename in ("pyproject.toml", "uv.lock", ".python-version", "README.md"):
        if (source / filename).exists():
            shutil.copyfile(source / filename, root / filename)
    shutil.copytree(source / "src", root / "src")
    token = tmp_path / "token"
    token.write_text("a" * 40)
    token.chmod(0o600)
    state = tmp_path / "state"
    state.mkdir()
    (root / "config.toml").write_text(
        f'mode = "client"\nserver_url = "wss://example.com/client"\n'
        f'state_dir = "{state}"\nremote_token_file = "{token}"\n'
    )
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    runtime = state / "runtime"
    generation = state / "runtime-releases" / "fixed-generation"
    generation.parent.mkdir()
    manager._prepare(root, root, state, runtime, generation, shutil.which("uv"))
    manager._link_runtime(runtime, generation)
    manager._validate_runtime(runtime)
    wrapper = manager._install_wrapper(runtime)
    result = subprocess.run([str(wrapper), "--help"], capture_output=True, text=True, check=True)
    assert "flotilla" in result.stdout
    assert runtime.is_symlink()
    assert generation.is_dir()
    assert str(generation) in (generation / ".venv/bin/flotilla").read_text()


@pytest.mark.skipif(sys.platform != "darwin", reason="plist validation requires macOS plutil")
def test_prepare_only_from_separate_config_root_via_install_script(tmp_path):
    source = Path(__file__).resolve().parents[1]
    root, state = _checkout(tmp_path)
    config_root = tmp_path / "config-only"
    config_root.mkdir()
    (root / "config.toml").replace(config_root / "config.toml")
    result = subprocess.run(
        [str(source / "scripts/install.sh"), "--root", str(config_root), "--prepare-only"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "Prepared runtime" in result.stdout
    runtime = state / "runtime-prepared"
    assert runtime.is_symlink()
    assert (runtime / "README.md").is_file()
    assert not (runtime / ".env").exists()
    subprocess.run([str(runtime / ".venv/bin/flotilla"), "--help"], check=True, capture_output=True)


def test_source_discovery_failure_is_explicit(tmp_path):
    with pytest.raises(RuntimeError, match="source files are unavailable"):
        manager._source_root(tmp_path / "isolated.py")
