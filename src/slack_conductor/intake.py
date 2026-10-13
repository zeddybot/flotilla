"""Content-free diagnostics for Slack intake decisions."""

import json
import logging
import re

log = logging.getLogger(__name__)


def audit(body, decision, source, error=None):
    event = body.get("event", {})
    fields = {"decision": decision, "source": source}
    for key, value in {
        "event_id": body.get("event_id"),
        "team_id": body.get("team_id"),
        **{key: event.get(key) for key in ("type", "subtype", "channel", "ts", "user")},
    }.items():
        # Never put prompts, file URLs, credentials, or arbitrary payloads in logs.
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", value):
            fields[key] = value
    fields["file_count"] = len(event.get("files") or [])
    if error is not None:
        fields["error_type"] = type(error).__name__
    log.log(logging.ERROR if error else logging.INFO, "Slack intake %s", json.dumps(fields))
