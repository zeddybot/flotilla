"""Install a stable private runtime and a per-user macOS LaunchAgent."""

import argparse
import os
import plistlib
import shutil
import subprocess
import time
from pathlib import Path

from slack_conductor.config import Config

LABEL = "com.sameer.slack-conductor"


def unload(domain):
    service = f"{domain}/{LABEL}"
    subprocess.run(["launchctl", "bootout", service], check=False, capture_output=True)
    # bootout can return while launchd is still removing the old job. Reusing
    # the label immediately then fails bootstrap with an I/O error.
    deadline = time.monotonic() + 25
    while (
        subprocess.run(["launchctl", "print", service], capture_output=True, check=False).returncode
        == 0
    ):
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
            "daemon",
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    root = Path(__file__).resolve().parents[1]
    config = Config.load(root)
    state = config.state_dir
    domain = f"gui/{os.getuid()}"
    target = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    if args.uninstall:
        unload(domain)
        target.unlink(missing_ok=True)
        print("LaunchAgent removed; credentials, runtime, and thread history retained")
        return
    runtime = state / "runtime"
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv is required to install the runtime")
    if not args.prepare_only:
        unload(domain)
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    for filename in ("pyproject.toml", "uv.lock", ".python-version", "config.toml", ".env"):
        source = root / filename
        if source.exists():
            shutil.copyfile(source, runtime / filename)
            (runtime / filename).chmod(0o600)
    shutil.copytree(
        root / "src",
        runtime / "src",
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    subprocess.run([uv, "sync", "--frozen", "--no-dev", "--project", str(runtime)], check=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(plistlib.dumps(plist(runtime, state)))
    target.chmod(0o600)
    subprocess.run(["plutil", "-lint", str(target)], check=True)
    if args.prepare_only:
        print(f"Prepared runtime and LaunchAgent: {target}")
        return
    subprocess.run(["launchctl", "bootstrap", domain, str(target)], check=True)
    print(f"Installed {LABEL}; starts at login and restarts after failure")
    print(f"Runtime: {runtime}\nLogs: {state}")


if __name__ == "__main__":
    main()
