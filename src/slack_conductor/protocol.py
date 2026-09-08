import html
import re

EFFORTS = {"low", "medium", "high", "xhigh", "max", "ultracode"}


def parse(text, bot_id, config, previous=None):
    text = re.sub(rf"^\s*<@{re.escape(bot_id)}>\s*", "", text)
    options = {
        "model": config.model,
        "effort": config.effort,
        "fast": False,
        "send_mode": config.send_mode,
        "verbose": False,
    }
    if previous:
        options.update({key: previous[key] for key in options})
    command = None
    while match := re.match(r"^\s*\[([^\]]+)\]\s*", text):
        token = match[1].lower()
        if token in config.models:
            options["model"] = config.models[token]
        elif token in EFFORTS:
            options["effort"] = token
        elif token in {"fast", "verbose"}:
            options[token] = True
        elif token in {"queue", "steer"}:
            options["send_mode"] = token
        elif token in {"new", "close"}:
            if command and command != token:
                raise ValueError("Use either [new] or [close], not both")
            command = token
        else:
            break  # Unknown bracket text belongs to the prompt.
        text = text[match.end() :]
    return html.unescape(text).strip(), options, command


def prompt_header(channel, thread_ts, message_ts, permalink, text):
    return (
        f"[slack] channel={channel} thread_ts={thread_ts} message_ts={message_ts} permalink={permalink}\n"
        "You already have Slack tools; read the thread or channel yourself if you need more context.\n"
        "The bridge relays your final reply to this thread. Do not post to Slack yourself.\n\n"
        + text
    )


def chunks(text, limit=3900):
    """Keep whole fenced blocks together; reopen only blocks exceeding Slack's limit."""
    pieces = re.split(r"(```[\s\S]*?```)", text)
    output, current = [], ""
    for piece in pieces:
        if not piece:
            continue
        if len(current) + len(piece) <= limit:
            current += piece
            continue
        if current:
            output.append(current)
            current = ""
        if len(piece) <= limit:
            current = piece
            continue
        fenced = piece.startswith("```") and piece.endswith("```")
        body = piece[3:-3] if fenced else piece
        budget = limit - 8 if fenced else limit
        while len(body) > budget:
            cut = body.rfind("\n", 0, budget + 1)
            cut = cut + 1 if cut > 0 else budget
            part, body = body[:cut], body[cut:]
            output.append("```" + part + "\n```" if fenced else part)
        current = "```" + body + "```" if fenced else body
    if current:
        output.append(current)
    return output or ["Agent finished without a text reply."]
