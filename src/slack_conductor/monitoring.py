"""Read-only local health snapshots and persistent incident transitions."""

import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .config import remote_credentials

STALL_SECONDS = 600


def _check(state, detail, **data):
    return {"state": state, "detail": detail, **data}


def _read_db(path, queries):
    if not path.is_file():
        return None
    uri = path.resolve().as_uri() + "?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=1) as conn:
            conn.execute("PRAGMA query_only=ON")
            return [conn.execute(sql).fetchone() for sql in queries]
    except (sqlite3.DatabaseError, OSError):
        return None


def _pending_ids(state_dir):
    """Hash durable IDs so private prompts and paths never enter monitor state."""
    pending = set()
    for filename, table, where, prefix in (
        ("remote.sqlite", "calls", "response IS NULL", "receipt"),
        ("bridge.sqlite", "statuses", "state='delivering'", "delivery"),
    ):
        path = state_dir / filename
        if not path.is_file():
            continue
        try:
            with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1) as conn:
                conn.execute("PRAGMA query_only=ON")
                for (identity,) in conn.execute(
                    f"SELECT id FROM {table} WHERE {where} ORDER BY id LIMIT 256"
                ):
                    digest = hashlib.sha256(str(identity).encode()).hexdigest()
                    pending.add(f"{prefix}:{digest}")
        except (sqlite3.DatabaseError, OSError):
            continue
    return pending


def _prior_pending(state_dir):
    try:
        value = json.loads((state_dir / "monitoring.json").read_text())
        tracked = value.get("_tracked_pending", {})
        return tracked if isinstance(tracked, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _stalled_pending(state_dir, now):
    prior = _prior_pending(state_dir)
    current = _pending_ids(state_dir)
    stalled = {"receipt": 0, "delivery": 0}
    for identity in current:
        first = prior.get(identity)
        if isinstance(first, (int, float)) and 0 <= now - first >= STALL_SECONDS:
            stalled[identity.split(":", 1)[0]] += 1
    return stalled


def _db_check(rows, *, count_index=0, warning=False, name="items"):
    if rows is None:
        return _check("unknown", "database unavailable")
    count = int(rows[count_index][0])
    return _check("warning" if warning else "ok", f"{count} {name}", count=count)


def _lock_check(state_dir):
    path = state_dir / "daemon.lock"
    if not path.is_file():
        return _check("unknown", "service lock unavailable")
    try:
        with path.open("rb") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return _check("ok", "service owns lock")
            finally:
                try:
                    fcntl.flock(lock, fcntl.LOCK_UN)
                except OSError:
                    pass
    except OSError:
        return _check("unknown", "service lock unreadable")
    return _check("warning", "service lock is not held")


def _json_status(path, now, max_age, *, ready_key):
    try:
        value = json.loads(path.read_text())
        age = now - value["at"]
        if not isinstance(age, (int, float)) or age < 0:
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        return _check("unknown", "status unavailable")
    if age > max_age:
        return _check("warning", "status stale", age_seconds=round(age))
    if not value.get(ready_key):
        return _check("warning", "service not ready", age_seconds=round(age))
    return _check("ok", "ready", age_seconds=round(age))


def _server_health(config):
    try:
        token = remote_credentials(config)
        url = urlsplit(config.server_url)
        scheme = {"wss": "https", "ws": "http"}[url.scheme]
        endpoint = urlunsplit((scheme, url.netloc, "/health", "", ""))
        request = Request(endpoint, headers={"Authorization": "Bearer " + token})
        with urlopen(request, timeout=3) as response:
            ready = json.load(response).get("ready") is True
    except (OSError, ValueError, KeyError, RuntimeError, TimeoutError):
        return _check("warning", "server health unavailable")
    return _check("ok" if ready else "warning", "server ready" if ready else "server not ready")


def _server_checks(config, now, runtime_ready, slack_ready):
    state = config.state_dir
    stalled = _stalled_pending(state, now)
    checks = {
        "runtime": _check(
            "unknown" if runtime_ready is None else "ok" if runtime_ready else "warning",
            "runtime readiness unavailable"
            if runtime_ready is None
            else "runtime ready"
            if runtime_ready
            else "runtime not ready",
        ),
        "slack": _check(
            "unknown" if slack_ready is None else "ok" if slack_ready else "warning",
            "slack readiness unavailable"
            if slack_ready is None
            else "slack ready"
            if slack_ready
            else "slack not ready",
        ),
    }
    rows = _read_db(
        state / "bridge.sqlite",
        [
            "SELECT count(*), min(created) FROM events WHERE state='pending'",
            "SELECT count(*) FROM events WHERE state='failed' AND created>unixepoch('now')-86400",
            "SELECT count(*) FROM statuses WHERE state='delivering'",
            "SELECT count(*) FROM statuses WHERE state='failed' AND created>unixepoch('now')-86400",
            "SELECT count(*), coalesce(max(attempts),0) FROM notifications WHERE state IN ('pending','sending')",
            "SELECT count(*) FROM notifications WHERE state IN ('pending','sending') AND attempts>=5",
            "SELECT count(*), min(created) FROM statuses WHERE state IN ('waiting','active')",
        ],
    )
    if rows is None:
        for key in ("event_backlog", "delivery", "notifications", "active_work"):
            checks[key] = _check("unknown", "database unavailable")
    else:
        pending, oldest = rows[0]
        age = max(0, now - oldest) if oldest is not None else 0
        failed_events = rows[1][0]
        checks["event_backlog"] = _check(
            "warning" if age > 600 or pending > 100 or failed_events else "ok",
            f"{pending} pending events; {failed_events} failed events in the last day",
            count=pending,
            oldest_age_seconds=round(age),
            failed=failed_events,
        )
        delivering, failed_statuses = rows[2][0], rows[3][0]
        checks["delivery"] = _check(
            "warning" if delivering > 100 or failed_statuses or stalled["delivery"] else "ok",
            f"{delivering} deliveries in progress; {stalled['delivery']} stalled; "
            f"{failed_statuses} failed statuses in the last day",
            in_progress=delivering,
            stalled=stalled["delivery"],
            failed=failed_statuses,
        )
        notices, attempts = rows[4]
        checks["notifications"] = _check(
            "warning" if rows[5][0] else "ok",
            f"{notices} queued notifications; maximum attempts {attempts}",
            count=notices,
            max_attempts=attempts,
        )
        active, oldest_active = rows[6]
        checks["active_work"] = _check(
            "ok",
            f"{active} active or waiting turns",
            count=active,
            oldest_age_seconds=round(max(0, now - oldest_active)) if oldest_active else 0,
        )
    receipts = _read_db(
        state / "remote.sqlite", ["SELECT count(*) FROM calls WHERE response IS NULL"]
    )
    checks["receipts"] = (
        _check("unknown", "database unavailable")
        if receipts is None
        else _check(
            "warning" if receipts[0][0] > 100 or stalled["receipt"] else "ok",
            f"{receipts[0][0]} unresolved remote receipts; {stalled['receipt']} stalled",
            count=receipts[0][0],
            stalled=stalled["receipt"],
        )
    )
    cleanup = _read_db(
        state / "bridge.sqlite",
        ["SELECT count(*), coalesce(max(attempts),0) FROM remote_asset_cleanup"],
    )
    checks["asset_cleanup"] = (
        _check("unknown", "cleanup table unavailable")
        if cleanup is None
        else _check(
            "warning" if cleanup[0][1] >= 5 else "ok",
            f"{cleanup[0][0]} pending cleanups",
            count=cleanup[0][0],
            max_attempts=cleanup[0][1],
        )
    )
    try:
        usage = shutil.disk_usage(state)
        percent = round(100 * usage.used / usage.total, 1)
        checks["disk"] = _check(
            "warning" if percent >= 90 else "ok", f"disk {percent}% used", percent=percent
        )
    except (OSError, ZeroDivisionError):
        checks["disk"] = _check("unknown", "disk usage unavailable")
    # Upload files can be retained while a status is active; count only old
    # directories without a corresponding status ID.
    uploads = state / "uploads"
    if rows is None:
        checks["orphan_snapshots"] = _check("unknown", "database unavailable")
    elif not uploads.exists():
        checks["orphan_snapshots"] = _check("ok", "0 stale orphan snapshots", count=0)
    else:
        status_rows = _read_db(state / "bridge.sqlite", ["SELECT group_concat(id) FROM statuses"])
        known = set((status_rows[0][0] or "").split(",")) if status_rows else set()
        try:
            orphans = sum(
                1
                for item in uploads.iterdir()
                if item.is_dir() and item.name not in known and now - item.stat().st_mtime > 86400
            )
            checks["orphan_snapshots"] = _check(
                "warning" if orphans else "ok", f"{orphans} stale orphan snapshots", count=orphans
            )
        except OSError:
            checks["orphan_snapshots"] = _check("unknown", "snapshot directory unavailable")
    return checks


def report(config, root=None, *, runtime_ready=None, slack_ready=None):
    """Return a read-only health snapshot without including credentials or prompts."""
    now = time.time()
    if config.mode == "server":
        if runtime_ready is None or slack_ready is None:
            try:
                live = json.loads((config.state_dir / "service-status.json").read_text())
                if 0 <= now - live["at"] < 60:
                    runtime_ready = (
                        live.get("runtime_ready") if runtime_ready is None else runtime_ready
                    )
                    slack_ready = live.get("slack_ready") if slack_ready is None else slack_ready
            except (OSError, ValueError, KeyError, TypeError):
                pass
        checks = _server_checks(config, now, runtime_ready, slack_ready)
        checks["service"] = _lock_check(config.state_dir)
    else:
        checks = {
            "remote_link": _json_status(
                config.state_dir / "remote-status.json", now, 60, ready_key="connected"
            ),
            "native_attachment": _json_status(
                config.state_dir / "native-heartbeat.json", now, 5, ready_key="ready"
            ),
            "service": _lock_check(config.state_dir),
            "server_health": _server_health(config),
        }
    return {
        "ok": all(item["state"] == "ok" for item in checks.values()),
        "role": config.mode,
        "at": now,
        "checks": checks,
    }


class Monitor:
    """Persist snapshots and log warning/recovery transitions once each."""

    def __init__(self, config):
        self.state_dir = config.state_dir

    def check(self, snapshot):
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (self.state_dir / "monitoring.lock").open("a") as lock:
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            return self._check(snapshot)

    def _check(self, snapshot):
        path = self.state_dir / "monitoring.json"
        try:
            previous = json.loads(path.read_text())
        except (OSError, ValueError):
            previous = {}
        previous_at = previous.get("at")
        if isinstance(previous_at, (int, float)) and snapshot["at"] < previous_at:
            return {
                **{k: v for k, v in previous.items() if k != "_tracked_pending"},
                "incidents": [],
            }
        active = set(previous.get("active_incidents", []))
        incidents = []
        for key, current in snapshot["checks"].items():
            after = current["state"]
            if after in {"warning", "unknown"} and key not in active:
                incidents.append(
                    {
                        "check": key,
                        "transition": "warning",
                        "at": snapshot["at"],
                        "detail": current["detail"],
                    }
                )
                active.add(key)
            elif key in active and after == "ok":
                incidents.append(
                    {
                        "check": key,
                        "transition": "recovered",
                        "at": snapshot["at"],
                        "detail": current["detail"],
                    }
                )
                active.remove(key)
        result = {**snapshot, "incidents": incidents, "active_incidents": sorted(active)}
        tracked = _prior_pending(self.state_dir)
        now = snapshot["at"]
        result_on_disk = {
            **result,
            "_tracked_pending": {
                identity: tracked[identity]
                if isinstance(tracked.get(identity), (int, float)) and 0 <= now - tracked[identity]
                else now
                for identity in _pending_ids(self.state_dir)
            },
        }
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", dir=self.state_dir, delete=False) as stream:
            stream.write(json.dumps(result_on_disk, sort_keys=True) + "\n")
            temporary = stream.name
        os.replace(temporary, path)
        if incidents:
            log = self.state_dir / "monitoring.log"
            if log.exists() and log.stat().st_size > 1_000_000:
                rotated = self.state_dir / "monitoring.log.1"
                rotated.unlink(missing_ok=True)
                log.replace(rotated)
            with log.open("a") as stream:
                os.chmod(log, 0o600)
                for incident in incidents:
                    stream.write(json.dumps(incident, sort_keys=True) + "\n")
        return result


def write_service_status(state_dir, **values):
    path = state_dir / "service-status.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"at": time.time(), **values}))
    temporary.chmod(0o600)
    os.replace(temporary, path)


def watch(config, stop, interval=30):
    import logging

    monitor = Monitor(config)
    # Give a normal startup time to establish both links before logging incidents.
    while not stop.wait(interval):
        try:
            monitor.check(report(config))
        except Exception as exc:  # noqa: BLE001 - monitoring must not stop the service loop
            logging.getLogger(__name__).warning("Monitoring sample failed (%s)", type(exc).__name__)
