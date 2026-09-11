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
        "The bridge relays your final reply to this thread. Do not post to Slack yourself.\n"
        "To share files, include ordinary Markdown links anywhere in your final reply: "
        "[label](path/to/file) or ![caption](path/to/image.png). The bridge uploads local files "
        "after all background work finishes; image syntax displays PNG/JPEG/GIF previews. "
        "Relative paths resolve against this workspace; absolute paths also work. "
        "Only link files you intend to share with everyone in this Slack conversation. "
        "Links with source line numbers (file.py:42 or file.py#L42) remain code references. "
        "Keep files available until delivery. Maximum 10 files, 25 MiB each.\n\n" + text
    )
