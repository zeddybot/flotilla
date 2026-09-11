"""Slack status replies have a live header; overflow replies contain only content."""

import html

from .file_links import image_blocks
from .markdown import markdown_chunks

STATES = {
    "creating": ("🏗️", "Creating workspace"),
    "starting": ("🚀", "Starting"),
    "thinking": ("🧠", "Thinking"),
    "working": ("⚙️", "Working"),
    "waiting": ("⏳", "Waiting"),
    "queued": ("🕓", "Queued"),
    "sending": ("📤", "Sending"),
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
    # Reserve room for the first page's header, including escaped characters.
    parts = markdown_chunks(body.strip(), limit=3900 - len(header) - 20) if body.strip() else [""]
    return [
        (header + (f"\n\n{part}" if part else "")) if i == 0 else part
        for i, part in enumerate(parts)
    ]


def render(phase, content="", detail="", note=""):
    return pages(phase, content, detail, note)[0]


def blocks(text, images=None, *, status_header=True):
    """Keep the status in Slack syntax; let Slack render the agent's Markdown."""
    output, body = [], text
    if status_header:
        header, _, body = text.partition("\n\n")
        output.append({"type": "section", "text": {"type": "mrkdwn", "text": header}})
    if body:
        output.extend(
            image_blocks(body, images) if images else [{"type": "markdown", "text": body}]
        )
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
