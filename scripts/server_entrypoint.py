"""Run the Daytona server and restart it after an unexpected process exit."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("SC_SERVER_CONFIG_ROOT", "/data/config"))
HOLD = ROOT / "HOLD"
STOP = False
CHILD = None


def stop(signum, _frame):
    global STOP
    STOP = True
    if CHILD is not None and CHILD.poll() is None:
        CHILD.send_signal(signum)


def ready():
    for name in ("config.toml", ".env", "client.token"):
        path = ROOT / name
        if not path.is_file():
            return False
        if name != "config.toml" and path.stat().st_mode & 0o077:
            raise RuntimeError(f"{name} must have permissions 600")
    return True


def main():
    global CHILD
    os.umask(0o077)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    backoff = 1
    while not STOP:
        if HOLD.exists():
            time.sleep(1)
            continue
        if not ready():
            print("Waiting for server configuration in /data/config", flush=True)
            time.sleep(2)
            continue
        started = time.monotonic()
        CHILD = subprocess.Popen(["sc", "--root", str(ROOT), "server"])
        while CHILD.poll() is None:
            if STOP or HOLD.exists():
                CHILD.terminate()
                try:
                    CHILD.wait(timeout=35)
                except subprocess.TimeoutExpired:
                    CHILD.kill()
                    CHILD.wait()
                break
            try:
                CHILD.wait(timeout=1)
            except subprocess.TimeoutExpired:
                continue
        code = CHILD.returncode
        CHILD = None
        if STOP:
            break
        if HOLD.exists():
            backoff = 1
            continue
        if time.monotonic() - started > 60:
            backoff = 1
        print(f"Server exited ({code}); restarting in {backoff}s", flush=True)
        time.sleep(backoff)
        backoff = min(backoff * 2, 30)
    return 0


if __name__ == "__main__":
    sys.exit(main())
