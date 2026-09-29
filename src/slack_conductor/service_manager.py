"""Install and operate the Mac client and the supervised server."""

import fcntl
import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from .config import Config, remote_credentials

LABEL = "com.sameer.slack-conductor"


def _domain():
    if sys.platform != "darwin":
        raise RuntimeError("Mac client services require macOS")
    return f"gui/{os.getuid()}"


def _target():
    return Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"


def _service():
    return f"{_domain()}/{LABEL}"


def _run(*args, check=False):
    return subprocess.run(args, check=check, capture_output=True, text=True)


def unload(domain):
    service = f"{domain}/{LABEL}"
    if _run("launchctl", "print", service).returncode != 0:
        return
    _run("launchctl", "bootout", service)
    deadline = time.monotonic() + 25
    while _run("launchctl", "print", service).returncode == 0:
        if time.monotonic() >= deadline:
            raise RuntimeError("Previous daemon did not stop; runtime was left unchanged")
        time.sleep(0.25)


def plist(runtime, state):
    return {
        "Label": LABEL,
        "ProgramArguments": [
            str(runtime / ".venv/bin/python"),
            "-m",
            "slack_conductor",
            "--root",
            str(runtime),
            "client",
        ],
        "WorkingDirectory": str(runtime),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "LimitLoadToSessionType": "Aqua",
        "ProcessType": "Background",
        "ExitTimeOut": 20,
        "Umask": 0o077,
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONUNBUFFERED": "1",
        },
        "StandardOutPath": str(state / "launchd.out.log"),
        "StandardErrorPath": str(state / "launchd.err.log"),
    }


def runtime_config(source, runtime):
    """Write install-specific settings without placing a token in TOML."""
    lines = source.read_text().splitlines(keepends=True) if source.exists() else []
    section = next((i for i, line in enumerate(lines) if line.lstrip().startswith("[")), len(lines))
    head = [
        line
        for line in lines[:section]
        if line.split("=", 1)[0].strip() not in {"mode", "remote_token_file"}
    ]
    if head and not head[-1].endswith("\n"):
        head[-1] += "\n"
    head.append('mode = "client"\n')
    head.append(f"remote_token_file = {json.dumps(str(runtime / 'remote.token'))}\n")
    return "".join(head + lines[section:])


def _source_root(start=None):
    """Find bundled build inputs beside the checkout or installed runtime."""
    start = Path(start or __file__).resolve()
    for candidate in start.parents:
        if all(
            (candidate / name).is_file()
            for name in (
                "pyproject.toml",
                "uv.lock",
                "README.md",
                "src/slack_conductor/service_manager.py",
            )
        ):
            return candidate
    raise RuntimeError(
        "Flotilla source files are unavailable; install from a checkout with pyproject.toml, uv.lock, README.md, and src/"
    )


def _prepare(config_root, source_root, state, runtime, staging, uv):
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(mode=0o700, parents=True)
    for filename in ("pyproject.toml", "uv.lock", ".python-version", "README.md"):
        source = source_root / filename
        if source.exists():
            shutil.copyfile(source, staging / filename)
            (staging / filename).chmod(0o600)
    (staging / "config.toml").write_text(runtime_config(config_root / "config.toml", runtime))
    (staging / "config.toml").chmod(0o600)
    config = Config.load(config_root)
    shutil.copyfile(config.remote_token_file, staging / "remote.token")
    (staging / "remote.token").chmod(0o600)
    shutil.copytree(
        source_root / "src", staging / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    _run(uv, "sync", "--frozen", "--no-dev", "--project", str(staging), check=True)
    _run(str(staging / ".venv/bin/python"), "-c", "import slack_conductor.cli", check=True)
    candidate = state / "candidate-launchagent.plist"
    candidate.write_bytes(plistlib.dumps(plist(runtime, state)))
    candidate.chmod(0o600)
    _run("plutil", "-lint", str(candidate), check=True)
    return candidate


def _link_runtime(runtime, generation):
    pending = runtime.with_name(runtime.name + ".new")
    pending.unlink(missing_ok=True)
    pending.symlink_to(generation, target_is_directory=True)
    os.replace(pending, runtime)


def _validate_runtime(runtime):
    _run(str(runtime / ".venv/bin/python"), "-c", "import slack_conductor.cli", check=True)
    _run(str(runtime / ".venv/bin/flotilla"), "--help", check=True)


def _install_wrapper(runtime):
    bin_dir = Path.home() / ".local/bin"
    bin_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = bin_dir / "flotilla"
    _check_wrapper_target(target)
    wrapper = (
        "#!/bin/sh\n# flotilla managed wrapper\nexec "
        + _shell_quote(str(runtime / ".venv/bin/python"))
        + ' -m slack_conductor.cli "$@"\n'
    )
    pending = bin_dir / ".flotilla.new"
    pending.write_text(wrapper)
    pending.chmod(0o700)
    pending.replace(target)
    return target


def _check_wrapper_target(target):
    if target.is_symlink() or (
        target.exists()
        and not target.read_text().startswith("#!/bin/sh\n# flotilla managed wrapper\n")
    ):
        raise RuntimeError(f"Refusing to overwrite existing executable: {target}")


def _shell_quote(value):
    return "'" + value.replace("'", "'\\''") + "'"


def client_install(root: Path, *, prepare_only=False):
    os.umask(0o077)
    if not prepare_only:
        _domain()
    root = Path(root).resolve()
    if not (root / "config.toml").is_file():
        raise RuntimeError(f"Flotilla config.toml not found in {root}")
    config = Config.load(root)
    if config.mode != "client":
        raise ValueError("installer requires a Mac client configuration")
    remote_credentials(config)
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv is required to install the runtime")
    state = config.state_dir
    if not prepare_only:
        _check_wrapper_target(Path.home() / ".local/bin/flotilla")
    runtime = state / ("runtime-prepared" if prepare_only else "runtime")
    releases = state / "runtime-releases"
    releases.mkdir(mode=0o700, parents=True, exist_ok=True)
    generation = releases / uuid.uuid4().hex
    source_root = _source_root()
    candidate = _prepare(root, source_root, state, runtime, generation, uv)
    if prepare_only:
        if runtime.is_symlink():
            runtime.unlink()
        elif runtime.exists():
            shutil.rmtree(runtime)
        _link_runtime(runtime, generation)
        _validate_runtime(runtime)
        prepared_plist = state / "prepared-launchagent.plist"
        candidate.replace(prepared_plist)
        return f"Prepared runtime and LaunchAgent: {prepared_plist}"
    target = _target()
    target.parent.mkdir(parents=True, exist_ok=True)
    backup = state / "runtime-previous"
    old_plist = target.read_bytes() if target.exists() else None
    was_running = _run("launchctl", "print", _service()).returncode == 0
    old_generation = runtime.resolve() if runtime.is_symlink() else None
    # All build and plist validation above happens before this point.
    unload(_domain())
    swapped = False
    moved_legacy = False
    try:
        if backup.exists():
            shutil.rmtree(backup)
        if runtime.exists() and not runtime.is_symlink():
            runtime.replace(backup)
            moved_legacy = True
        _link_runtime(runtime, generation)
        swapped = True
        _validate_runtime(runtime)
        candidate.replace(target)
        _run("launchctl", "bootstrap", _domain(), str(target), check=True)
    except Exception:
        if swapped and runtime.is_symlink():
            runtime.unlink()
        if swapped and old_generation is not None:
            _link_runtime(runtime, old_generation)
        elif moved_legacy:
            backup.replace(runtime)
        if old_plist is None:
            target.unlink(missing_ok=True)
        else:
            target.write_bytes(old_plist)
            target.chmod(0o600)
            if was_running:
                _run("launchctl", "bootstrap", _domain(), str(target), check=True)
        raise
    wrapper = _install_wrapper(runtime)
    result = f"Installed {LABEL}; runtime: {runtime}; logs: {state}"
    if str(wrapper.parent) not in os.environ.get("PATH", "").split(os.pathsep):
        result += f'\nAdd {wrapper.parent} to PATH: export PATH={_shell_quote(str(wrapper.parent))}:"$PATH"'
    return result


def client_uninstall():
    unload(_domain())
    _target().unlink(missing_ok=True)
    wrapper = Path.home() / ".local/bin/flotilla"
    if wrapper.exists() and wrapper.read_text().startswith(
        "#!/bin/sh\n# flotilla managed wrapper\n"
    ):
        wrapper.unlink()
    return "LaunchAgent removed; credentials, runtime, and thread history retained"


def client_start():
    target = _target()
    if not target.exists():
        raise RuntimeError("Client is not installed")
    if _run("launchctl", "print", _service()).returncode == 0:
        return "Client already running"
    _run("launchctl", "bootstrap", _domain(), str(target), check=True)
    return "Client started"


def client_stop():
    unload(_domain())
    return "Client stopped"


def client_restart():
    client_stop()
    return client_start()


def client_status():
    target = _target()
    if not target.exists():
        return "Client not installed"
    result = _run("launchctl", "print", _service())
    return f"Client {'running' if result.returncode == 0 else 'stopped'}; service: {_service()}; plist: {target}"


def _logs(path, *, follow=False, lines=50):
    if not path.exists():
        raise RuntimeError(f"Log not found: {path}")
    if lines < 0:
        raise ValueError("lines must be nonnegative")
    if follow:
        return subprocess.run(["tail", "-n", str(lines), "-F", str(path)], check=True).returncode
    return (
        "".join(path.read_text(errors="replace").splitlines(keepends=True)[-lines:])
        if lines
        else ""
    )


def client_logs(root: Path, *, follow=False, lines=50):
    return _logs(Config.load(Path(root)).state_dir / "daemon.log", follow=follow, lines=lines)


def client_run(root: Path):
    return subprocess.run(
        [sys.executable, "-m", "slack_conductor", "--root", str(Path(root).resolve()), "client"],
        check=False,
    ).returncode


def server_run(root: Path):
    return subprocess.run(
        [sys.executable, "-m", "slack_conductor", "--root", str(Path(root).resolve()), "server"],
        check=False,
    ).returncode


def _managed_server(root):
    root = Path(root).resolve()
    if (
        sys.platform != "linux"
        or root != Path(os.environ.get("SC_SERVER_CONFIG_ROOT", "/data/config")).resolve()
    ):
        raise RuntimeError(
            "Server service controls require the local supervised /data/config server; use 'server run' for a foreground server"
        )
    if not _supervisor_running():
        raise RuntimeError("Server supervisor is not running on this host")
    return root


def _supervisor_running():
    proc = Path("/proc")
    if not proc.is_dir():
        return False
    for process in proc.iterdir():
        if not process.name.isdigit():
            continue
        try:
            command = (process / "cmdline").read_bytes().split(b"\0")
        except (OSError, PermissionError):
            continue
        if any(arg == b"/app/scripts/server_entrypoint.py" for arg in command):
            return True
    return False


def server_start(root: Path):
    hold = _managed_server(root) / "HOLD"
    hold.unlink(missing_ok=True)
    return "Server supervisor released; starting"


def server_stop(root: Path):
    hold = _managed_server(root) / "HOLD"
    hold.touch(mode=0o600, exist_ok=True)
    hold.chmod(0o600)
    return "Server supervisor held; stopping"


def server_restart(root: Path):
    root = _managed_server(root)
    server_stop(root)
    lock_path = Config.load(root).state_dir / "daemon.lock"
    deadline = time.monotonic() + 40
    while lock_path.exists():
        with lock_path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Server did not stop; HOLD remains in place") from None
                time.sleep(0.25)
                continue
            finally:
                try:
                    fcntl.flock(lock, fcntl.LOCK_UN)
                except OSError:
                    pass
        break
    server_start(root)
    return "Server supervisor restarted"


def server_status(root: Path):
    try:
        root = _managed_server(root)
    except RuntimeError:
        return "Server not managed on this host; use 'server run'"
    if (root / "HOLD").exists():
        return "Server supervisor held"
    lock_path = Config.load(root).state_dir / "daemon.lock"
    if lock_path.exists():
        with lock_path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return "Server running; run 'doctor' to check application readiness"
    return "Server supervisor active; server waiting or starting"


def server_logs(root: Path, *, follow=False, lines=50):
    return _logs(Config.load(Path(root)).state_dir / "daemon.log", follow=follow, lines=lines)
