"""Fresh process identity without NSWorkspace's run-loop-dependent app cache."""

import os
import subprocess
import threading
import time

_lock = threading.Lock()
_cached = None
_checked = 0.0


def incarnation():
    global _cached, _checked
    with _lock:
        now = time.monotonic()
        if _cached:
            try:
                os.kill(int(_cached.split(":", 1)[0]), 0)
            except ProcessLookupError:
                _cached, _checked = None, 0.0
        if now - _checked >= 0.5:
            # comm contains the executable path, never agent arguments or tokens.
            output = subprocess.check_output(
                ["/bin/ps", "-axo", "pid=,lstart=,comm="], text=True, timeout=3
            )
            _cached = None
            for line in output.splitlines():
                fields = line.strip().split(None, 6)
                if len(fields) == 7 and fields[6].lower().endswith(
                    "/conductor.app/contents/macos/conductor"
                ):
                    _cached = fields[0] + ":" + " ".join(fields[1:6])
                    break
            _checked = now
        return _cached
