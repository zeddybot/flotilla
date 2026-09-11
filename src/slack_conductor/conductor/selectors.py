import re


def model_identity(label):
    """Accept the pre-0.85 names retained in configuration and saved threads."""
    if label in {"5.6 Sol", "5.6 Terra", "5.6 Luna"}:
        return f"GPT-{label}"
    return label


def model_picker_label(title):
    """Conductor 0.85 appends the effort to the selected model's button label."""
    label = str(title or "").removeprefix("Change agent (").removesuffix(")")
    return label.split(" · ", 1)[0]


def model_menu_label(title):
    """Separate model identity from badges and effort without selecting new chats."""
    label = str(title or "").split("\n", 1)[0]
    label = re.sub(r" (?:low|medium|high|xhigh|max|ultracode)$", "", label, flags=re.IGNORECASE)
    return label.removesuffix(" NEW")


def is_queued_prompt(value, prompt):
    """Conductor flattens the multiline editor value in its queue labels."""
    label = " ".join(str(value or "").split())
    return " ".join(prompt.split()) in label and label.endswith(" Edit Delete Steer")
