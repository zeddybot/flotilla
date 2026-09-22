import subprocess
from pathlib import Path


def test_actual_frontend_pause_interception_and_recovery_order():
    subprocess.run(
        ["node", str(Path(__file__).with_name("native_frontend.mjs"))], check=True, timeout=15
    )


def test_actual_frontend_survives_stalled_heartbeats_but_yields_when_superseded():
    subprocess.run(
        ["node", str(Path(__file__).with_name("native_heartbeat.mjs"))], check=True, timeout=60
    )
