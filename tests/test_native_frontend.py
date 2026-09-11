import subprocess
from pathlib import Path


def test_actual_frontend_pause_interception_and_recovery_order():
    subprocess.run(
        ["node", str(Path(__file__).with_name("native_frontend.mjs"))], check=True, timeout=15
    )
