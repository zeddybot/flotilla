import html
import re

EFFORTS = {"low", "medium", "high", "xhigh", "max", "ultracode"}


def profile_selector(text, bot_id):
    prefix = re.match(rf"^\s*(?:<@{re.escape(bot_id)}>\s*)?", text)[0]
    match = re.match(r"\[profile:([^\]]*)\]\s*", text[len(prefix) :], re.IGNORECASE)
    if not match:
        return None, text
    name = match[1].strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", name):
        raise ValueError("Use [profile:name] at the start of your message.")
    return name, prefix + text[len(prefix) + match.end() :]


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
        options.update({key: previous[key] for key in options if key in previous})
    command = None
    while match := re.match(r"^\s*\[([^\]]+)\]\s*", text):
        raw_token = match[1]
        token = raw_token.lower()
        if token in config.models:
            options["model"] = config.models[token]
        elif token in EFFORTS:
            options["effort"] = token
        elif token in {"fast", "verbose"}:
            options[token] = True
        elif token in {"queue", "steer"}:
            options["send_mode"] = token
        elif token in {"new", "close", "stop", "questions"} or token.partition(":")[0] in {
            "approve",
            "deny",
            "answer",
        }:
            if command:
                raise ValueError("Choose one session command per message.")
            action, sep, question_id = raw_token.partition(":")
            command = action.lower() + sep + question_id
        else:
            break  # Unknown bracket text belongs to the prompt.
        text = text[match.end() :]
    return html.unescape(text).strip(), options, command


def prompt_header(channel, thread_ts, message_ts, permalink, text, *, sender=None):
    attribution = f" sender=<@{sender}>" if sender else ""
    return (
        f"[slack] channel={channel} thread_ts={thread_ts} message_ts={message_ts} permalink={permalink}{attribution}\n"
        "You already have Slack tools; read the thread or channel yourself if you need more context.\n"
        "The bridge relays your final reply to this thread. Do not post to Slack yourself.\n"
        "When your reply directly addresses someone or needs their action, @mention that "
        "person using Slack's <@USER_ID> syntax. Address the relevant people, not automatically "
        "the person who dispatched you. Resolve their IDs from the thread or Slack tools; "
        "do not guess IDs or tag unrelated people.\n"
        "To share files, include ordinary Markdown links anywhere in your final reply: "
        "[label](path/to/file) or ![caption](path/to/image.png). The bridge uploads local files "
        "after all background work finishes; image syntax displays PNG/JPEG/GIF previews. "
        "Relative paths resolve against this workspace; absolute paths also work. "
        "Only link files you intend to share with everyone in this Slack conversation. "
        "Links with source line numbers (file.py:42 or file.py#L42) remain code references. "
        "Keep files available until delivery. Maximum 10 files, 25 MiB each.\n\n" + text
    )
