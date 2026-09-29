"""Supervise a headless client on Daytona without holding Slack credentials."""

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path


def main():
    from slack_conductor.config import Config, remote_credentials

    os.umask(0o077)
    root = Path(os.environ.get("FLOTILLA_WORKER_CONFIG_ROOT", "/data/config"))
    stopped = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stopped.set())
    delay = 1
    while not stopped.is_set():
        if not (root / "config.toml").is_file() or (root / "HOLD").exists():
            stopped.wait(1)
            continue
        config = Config.load(root)
        if config.mode != "client" or config.runtime_adapter != "headless":
            raise ValueError("Worker requires mode=client and runtime_adapter=headless")
        remote_credentials(config)
        started = time.monotonic()
        child = subprocess.Popen(
            [sys.executable, "-m", "slack_conductor", "--root", str(root), "client"]
        )
        while child.poll() is None:
            if stopped.wait(1) or (root / "HOLD").exists():
                child.terminate()
                try:
                    child.wait(timeout=35)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
                break
        if time.monotonic() - started > 60:
            delay = 1
        stopped.wait(delay)
        delay = min(delay * 2, 30)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
