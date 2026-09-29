import os
import signal
import subprocess
import sys
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "server_entrypoint.py"


def wait_for(path, expected, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().count(expected) >= 1:
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {expected}")


def test_hold_stops_child_and_prevents_restart(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    for name in ("config.toml", ".env", "client.token"):
        file = config / name
        file.write_text("test")
        file.chmod(0o600)
    hold = config / "HOLD"
    hold.touch()

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_sc = bin_dir / "sc"
    fake_sc.write_text(
        f"#!{sys.executable}\n"
        "import os, signal, time\n"
        "def record(value):\n"
        "    with open(os.environ['SC_TEST_LOG'], 'a') as output:\n"
        "        output.write(value + '\\n')\n"
        "record('start')\n"
        "def stop(*_):\n"
        "    record('stop')\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "while True: time.sleep(0.1)\n"
    )
    fake_sc.chmod(0o755)
    log = tmp_path / "child.log"
    env = {
        **os.environ,
        "SC_SERVER_CONFIG_ROOT": str(config),
        "SC_TEST_LOG": str(log),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
    }
    supervisor = subprocess.Popen(
        [sys.executable, str(SCRIPT)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        time.sleep(0.2)
        assert not log.exists()
        hold.unlink()
        wait_for(log, "start")
        hold.touch()
        wait_for(log, "stop")
        time.sleep(1.2)
        assert log.read_text().count("start") == 1
        hold.unlink()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and log.read_text().count("start") < 2:
            time.sleep(0.05)
        assert log.read_text().count("start") == 2
    finally:
        supervisor.send_signal(signal.SIGTERM)
        supervisor.wait(timeout=5)
    assert supervisor.returncode == 0
    assert log.read_text().count("stop") == 2
