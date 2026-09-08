def model_menu_label(title):
    """Ignore Conductor's NEW badge while preserving model/context labels."""
    return str(title or "").split("\n", 1)[0].removesuffix(" NEW")


def is_queued_prompt(value, prompt):
    """Conductor flattens the multiline editor value in its queue labels."""
    label = " ".join(str(value or "").split())
    return " ".join(prompt.split()) in label and label.endswith(" Edit Delete Steer")
