"""Fresh process identity without NSWorkspace's run-loop-dependent app cache."""

import logging
import os
import subprocess
import threading
import time

log = logging.getLogger(__name__)

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
            try:
                output = subprocess.check_output(
                    ["/bin/ps", "-axo", "pid=,lstart=,comm="], text=True, timeout=3
                )
            except subprocess.TimeoutExpired:
                # ps stalls under memory pressure; kill(0) above still vouches for the
                # cached pid, and raising here crashes the daemon or drops the attachment.
                log.warning("Process probe timed out; keeping identity %s", _cached)
                _checked = time.monotonic()
                return _cached
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
