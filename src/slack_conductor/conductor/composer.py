def prepare_prompt(ax, prompt, confirm_target, read_value):
    """Retry an empty UI write, never submission or replacement of a user's draft."""
    expected = prompt.rstrip("\n")

    def editor():
        confirm_target()
        return ax.require(ax.composer(), role="AXTextArea")

    for attempt in range(3):
        current = editor()
        draft = read_value(current)
        if (
            draft.strip()
            and draft.rstrip("\n") != expected
            and not (attempt == 0 and draft.startswith("Slack workspace "))
        ):
            raise RuntimeError("Conductor composer has an unsent draft; clear it before retrying")
        if draft.rstrip("\n") != expected:
            ax.set(current, "AXValue", prompt)
        try:
            # React can replace the text area during navigation or model selection.
            # Read the current editor instead of polling a stale AX element.
            ax.wait(
                lambda: read_value(editor()).rstrip("\n") == expected,
                "Conductor composer value did not match prompt",
            )
            return
        except RuntimeError:
            if attempt == 2:
                raise
