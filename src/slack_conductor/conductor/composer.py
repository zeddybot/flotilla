def prepare_prompt(ax, prompt, confirm_target, read_value):
    """Insert real editor input once, preserving unrelated drafts and target guards."""
    expected = prompt.rstrip("\n")

    def editor():
        confirm_target()
        return ax.require(ax.composer(), role="AXTextArea")

    current = editor()
    draft = read_value(current)
    if draft.rstrip("\n") == expected:
        return
    if draft.strip() and not draft.startswith("Slack workspace "):
        raise RuntimeError("Conductor composer has an unsent draft; clear it before retrying")

    def unchanged():
        fresh = editor()
        if fresh != current or read_value(fresh) != draft:
            raise RuntimeError("Conductor composer changed while preparing text; action stopped")
        return fresh

    # AXValue can report success without producing an input event in the web editor.
    # Select only the verified draft, then insert text through the native input path.
    ax.set(current, "AXFocused", True)
    ax.wait(lambda: ax.focused(unchanged()), "Conductor composer could not be focused")
    selected = draft.rstrip("\n")  # WebKit adds a synthetic trailing newline to AXValue.
    ax.select_text(unchanged(), len(draft.encode("utf-16-le")) // 2)
    ax.wait(
        lambda: ax.selected_text(unchanged()).rstrip("\n") == selected,
        "Conductor composer selection was not confirmed",
    )
    if not ax.focused(unchanged()) or ax.selected_text(current).rstrip("\n") != selected:
        raise RuntimeError("Conductor composer focus or selection changed; action stopped")
    ax.type_text(prompt)
    # Input might already have arrived even if this confirmation times out. Never
    # replay native keystrokes automatically; the caller also submits only once.
    ax.wait(
        lambda: read_value(editor()).rstrip("\n") == expected,
        "Conductor composer value did not match prompt",
    )
