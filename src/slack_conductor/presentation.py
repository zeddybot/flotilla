"""Every Slack message has a live status header and optional stable content."""

import html

from .markdown import markdown_chunks

STATES = {
    "creating": ("🏗️", "Creating workspace"),
    "starting": ("🚀", "Starting"),
    "thinking": ("🧠", "Thinking"),
    "working": ("⚙️", "Working"),
    "waiting": ("⏳", "Waiting"),
    "queued": ("🕓", "Queued"),
    "done": ("✅", "Done"),
    "attention": ("🙋", "Needs attention"),
    "unconfirmed": ("⚠️", "Needs attention"),
    "failed": ("❌", "Failed"),
    "reconnecting": ("🔄", "Reconnecting"),
    "archiving": ("📦", "Archiving"),
    "archived": ("📦", "Archived"),
}


def pages(phase, content="", detail="", note=""):
    emoji, label = STATES[phase]
    detail = html.escape(" ".join(str(detail).split())[:120])
    header = f"{emoji} *{label}*" + (f" · {detail}" if detail else "")
    body = content + (f"\n\nNote: {note}" if note else "")
    # Reserve room for the header, including escaped characters and continuation.
    parts = markdown_chunks(body.strip(), limit=3900 - len(header) - 20) if body.strip() else [""]
    return [
        header + (" · continued" if i else "") + (f"\n\n{part}" if part else "")
        for i, part in enumerate(parts)
    ]


def render(phase, content="", detail="", note=""):
    return pages(phase, content, detail, note)[0]


def blocks(text):
    """Keep the status in Slack syntax; let Slack render the agent's Markdown."""
    header, _, body = text.partition("\n\n")
    output = [{"type": "section", "text": {"type": "mrkdwn", "text": header}}]
    if body:
        output.append({"type": "markdown", "text": body})
    return output


def live(turn, note=""):
    phase, detail = turn.phase, turn.detail
    if turn.tasks:
        count = f"{len(turn.tasks)} background job" + ("s" if len(turn.tasks) != 1 else "")
        if turn.result_seq:
            phase = "waiting"
            detail = (
                next(iter(turn.tasks.values())) + " · " if len(turn.tasks) == 1 else ""
            ) + count
        else:
            detail = (detail + " · " if detail else "") + count
    return render(phase, turn.content, detail, note)
