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
            # This runs under the attachment lock. Once discovered, inspect only
            # Conductor: a whole-machine scan can stall heartbeat ingress under load.
            pid = _cached.split(":", 1)[0] if _cached else None
            args = ["/bin/ps", "-p", pid, "-o"] if pid else ["/bin/ps", "-axo"]
            try:
                output = subprocess.check_output(
                    [*args, "pid=,lstart=,comm="], text=True, timeout=0.25 if pid else 3
                )
            except subprocess.CalledProcessError as exc:
                if pid and exc.returncode == 1:
                    # It exited between kill(0) and ps. Rediscover on the next call.
                    _cached, _checked = None, 0.0
                    return None
                raise
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
            # A slow successful probe must still get a full cache interval.
            _checked = time.monotonic()
        return _cached
