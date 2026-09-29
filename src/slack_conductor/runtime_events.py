"""Presentation hints from normalized runtime events, never engine transcripts."""


def classify(row):
    event = row.get("event", {})
    if event.get("nested") or event.get("foreign"):
        return []
    kind = event.get("kind")
    if kind == "result":
        text = event.get("text") or "Agent finished without a text reply."
        return [("result", ("error: " if event.get("error") else "") + text)]
    if kind == "started":
        return [("status", "starting agent")]
    if kind == "job_started":
        return [("status", event.get("description") or "working")]
    out = []
    for part in event.get("parts", []):
        if part["kind"] == "activity":
            out.append(("status", part["phase"]))
        elif part["kind"] == "tool":
            out.append(("tool", part["summary"]))
    return out
