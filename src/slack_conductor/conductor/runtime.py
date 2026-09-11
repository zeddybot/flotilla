"""Fresh session observations shared by the daemon and its local CLI."""

import time

HEARTBEAT_MAX_AGE = 5


def fresh(timestamp, now):
    return (
        isinstance(timestamp, (int, float))
        and not isinstance(timestamp, bool)
        and 0 <= now - timestamp < HEARTBEAT_MAX_AGE
    )


def session_snapshot(snapshot, sid, *, now=None):
    """Missing, stale or unready observations never imply an idle agent."""
    now = time.time() if now is None else now
    if not snapshot.get("ready") or not snapshot.get("incarnation"):
        return None
    sessions = snapshot.get("sessions")
    value = sessions.get(sid) if isinstance(sessions, dict) else None
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("live"), dict)
        or not fresh(value.get("at"), now)
    ):
        return None
    return {**value, "incarnation": snapshot["incarnation"]}
