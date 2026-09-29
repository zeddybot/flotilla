"""Exercise process termination in disposable daemons, never the running bridge."""

import subprocess
import sys
import textwrap

import pytest

from slack_conductor.store import Store


@pytest.mark.parametrize("scenario", ["blocked", "leaked_worker", "clean", "slow_running"])
def test_daemon_shutdown_deadline_and_durable_events(tmp_path, scenario):
    script = textwrap.dedent("""
        import functools
        import sys
        import threading
        import time
        from pathlib import Path
        from types import SimpleNamespace
        from slack_conductor import service

        state = Path(sys.argv[1])
        scenario = sys.argv[2]
        service.credentials = lambda _: {}
        service.enforce_shutdown = functools.partial(service.enforce_shutdown, timeout=0.2)

        def run(config, store, conductor, creds, stop):
            store.execute("INSERT INTO events(key,payload) VALUES ('saved','{}')")
            if scenario == "blocked":
                stop.set()
                # A process exit during a write must preserve the committed event
                # and roll back incomplete work when the next daemon opens SQLite.
                with store.connect() as conn:
                    conn.execute("INSERT INTO events(key,payload) VALUES ('partial','{}')")
                    threading.Event().wait()
            elif scenario == "leaked_worker":
                threading.Thread(target=threading.Event().wait, daemon=False).start()
            elif scenario == "slow_running":
                # The grace period starts at shutdown, not at daemon startup.
                time.sleep(0.4)

        service.run = run
        service.server(state, SimpleNamespace(state_dir=state), None)
    """)
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), scenario],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    expected = 1 if scenario in {"blocked", "leaked_worker"} else 0
    assert result.returncode == expected, result.stderr
    assert not result.stderr, result.stderr
    store = Store(tmp_path / "bridge.sqlite")
    assert [row["key"] for row in store.rows("SELECT key FROM events")] == ["saved"]
