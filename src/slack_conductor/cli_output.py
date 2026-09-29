"""Terminal-safe, human-readable Flotilla command output."""

import json
import re
from datetime import UTC, datetime

_CONTROLS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def safe(value):
    """Keep untrusted runtime text from controlling a terminal."""
    return _CONTROLS.sub(lambda match: "\\" + format(ord(match.group()), "x").zfill(2), str(value))


def _label(key):
    return safe(str(key).replace("_", " ").capitalize())


def _scalar(value):
    if isinstance(value, bool):
        return "yes" if value else "no"
    if value is None:
        return "—"
    return safe(value)


def _lines(value, indent=0):
    """Render arbitrary nested data without leaking JSON syntax into text output."""
    prefix = " " * indent
    if isinstance(value, dict):
        if not value:
            return [prefix + "(none)"]
        lines = []
        for key, item in value.items():
            label = _label(key)
            if isinstance(item, (dict, list)):
                lines.append(f"{prefix}{label}:")
                lines.extend(_lines(item, indent + 2))
            else:
                lines.append(f"{prefix}{label}: {_scalar(item)}")
        return lines
    if isinstance(value, list):
        if not value:
            return [prefix + "(none)"]
        lines = []
        for item in value:
            if isinstance(item, (dict, list)):
                lines.append(prefix + "-")
                lines.extend(_lines(item, indent + 2))
            else:
                lines.append(f"{prefix}- {_scalar(item)}")
        return lines
    return [prefix + _scalar(value)]


def _time(value):
    if not isinstance(value, (int, float)):
        return _scalar(value)
    try:
        return datetime.fromtimestamp(value, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (OverflowError, OSError, ValueError):
        return _scalar(value)


def _health(value, command):
    role = _scalar(value.get("role", "service")).capitalize()
    state = "healthy" if value.get("ok") else "unhealthy"
    lines = [f"{role} {command}: {state}"]
    if "at" in value:
        lines.append(f"Checked: {_time(value['at'])}")
    for name, check in value.get("checks", {}).items():
        if not isinstance(check, dict):
            lines.append(f"  {_label(name)}: {_scalar(check)}")
            continue
        status = _scalar(check.get("state", "unknown"))
        detail = check.get("detail")
        line = f"  {_label(name)}: {status}"
        if detail is not None:
            line += f" — {_scalar(detail)}"
        lines.append(line)
        extra = {key: item for key, item in check.items() if key not in {"state", "detail"}}
        if extra:
            lines.extend(_lines(extra, 4))
    if command == "monitor":
        incidents = value.get("incidents", [])
        lines.append(f"Incidents: {len(incidents)}")
        for incident in incidents:
            if isinstance(incident, dict):
                name = _scalar(incident.get("check", "unknown"))
                transition = _scalar(incident.get("transition", "unknown"))
                detail = _scalar(incident.get("detail", ""))
                lines.append(f"  {name}: {transition} — {detail}")
            else:
                lines.append(f"  {_scalar(incident)}")
        active = value.get("active_incidents", [])
        lines.append("Active incidents: " + (", ".join(map(_scalar, active)) if active else "none"))
    return "\n".join(lines)


def _sessions(value):
    sessions = value.get("sessions", [])
    if not sessions:
        return "No sessions."
    lines = [f"Sessions ({len(sessions)}):"]
    for session in sessions:
        if not isinstance(session, dict):
            lines.append(f"  - {_scalar(session)}")
            continue
        identity = session.get("session_id", session.get("id", "unknown"))
        state = session.get("state", session.get("status"))
        title = f"  - {_scalar(identity)}"
        if state is not None:
            title += f" ({_scalar(state)})"
        lines.append(title)
        rest = {
            key: item
            for key, item in session.items()
            if key not in {"session_id", "id", "state", "status"}
        }
        if rest:
            lines.extend(_lines(rest, 4))
    return "\n".join(lines)


def _events(events):
    if not events:
        return "No events."
    lines = []
    for event in events:
        if not isinstance(event, dict):
            lines.append(_scalar(event))
            continue
        kind = _scalar(event.get("type", "event"))
        cursor = event.get("cursor")
        heading = kind + (f" (cursor {_scalar(cursor)})" if cursor is not None else "")
        lines.append(heading)
        rest = {key: item for key, item in event.items() if key not in {"type", "cursor"}}
        if rest:
            lines.extend(_lines(rest, 2))
    return "\n".join(lines)


def render(value, command=None, action=None):
    if value is None:
        return ""
    if isinstance(value, str):
        # Preserve log line breaks while neutralizing terminal control characters.
        return "\n".join(safe(line) for line in value.split("\n"))
    if isinstance(value, dict):
        if command in {"status", "doctor", "monitor"} and "checks" in value:
            return _health(value, command)
        if command == "session" and action == "list" and "sessions" in value:
            return _sessions(value)
        if command == "session" and action == "tail" and "events" in value:
            return _events(value["events"])
    if command == "session" and action == "tail":
        return _events([value])
    return "\n".join(_lines(value))


def error(message, code):
    if code == "unhealthy" and message.startswith("Doctor found unhealthy components: "):
        try:
            report = json.loads(message.split(": ", 1)[1])
        except (ValueError, TypeError):
            pass
        else:
            return "Doctor found unhealthy components\n" + render(report, "doctor")
    return "flotilla: " + safe(message)
