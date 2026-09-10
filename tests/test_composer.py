import pytest

from slack_conductor.conductor.composer import prepare_prompt


class Composer:
    def __init__(self, draft="", *, result="the prompt\n"):
        self.current = {"value": draft}
        self.result = result
        self.writes = []
        self.selection = ""
        self.has_focus = False
        self.on_focus = lambda: None
        self.on_select = lambda: None

    def composer(self):
        return self

    def require(self, root, role):
        assert root is self and role == "AXTextArea"
        return self.current

    def set(self, editor, key, value):
        assert editor is self.current and key == "AXFocused" and value is True
        self.has_focus = True
        self.on_focus()

    def focused(self, editor):
        assert editor is self.current
        return self.has_focus

    def select_text(self, editor, length):
        assert editor is self.current
        self.selected_length = length
        self.selection = editor["value"].rstrip("\n")
        self.on_select()

    def selected_text(self, editor):
        assert editor is self.current
        return self.selection

    def type_text(self, value):
        self.writes.append(value)
        # Input can replace the mounted editor; confirmation must reacquire it.
        self.current = {"value": self.result}

    def wait(self, predicate, message):
        if not predicate():
            raise RuntimeError(message)


def prepare(ax, confirm=lambda: None):
    prepare_prompt(ax, "the prompt", confirm, lambda e: e["value"])


def test_native_input_survives_editor_replacement():
    ax = Composer()
    prepare(ax)
    assert ax.writes == ["the prompt"]
    assert ax.selected_length == 0


@pytest.mark.parametrize("draft", ["my unsent work", "the prompt plus an edit"])
def test_existing_draft_is_preserved(draft):
    ax = Composer(draft)
    with pytest.raises(RuntimeError, match="unsent draft"):
        prepare(ax)
    assert ax.writes == []
    assert ax.current["value"] == draft


@pytest.mark.parametrize("step", ["on_focus", "on_select"])
def test_draft_that_changes_during_preparation_is_preserved(step):
    ax = Composer()
    setattr(ax, step, lambda: ax.current.update(value="user typed this"))
    with pytest.raises(RuntimeError, match="composer changed"):
        prepare(ax)
    assert ax.writes == []
    assert ax.current["value"] == "user typed this"


def test_existing_matching_prompt_is_not_rewritten():
    ax = Composer("the prompt\n")
    prepare(ax)
    assert ax.writes == []
    assert not ax.has_focus


def test_unconfirmed_input_is_not_replayed():
    ax = Composer(result="")
    with pytest.raises(RuntimeError, match="did not match prompt"):
        prepare(ax)
    assert ax.writes == ["the prompt"]


def test_target_change_stops_before_native_input():
    ax = Composer()

    def confirm():
        if ax.has_focus:
            raise RuntimeError("workspace changed")

    with pytest.raises(RuntimeError, match="workspace changed"):
        prepare(ax, confirm)
    assert ax.writes == []


def test_creation_marker_uses_utf16_selection_and_is_replaced():
    marker = "Slack workspace ⛵👩🏽‍💻"
    ax = Composer(marker + "\n")
    prepare(ax)
    assert ax.selected_length == len((marker + "\n").encode("utf-16-le")) // 2
    assert ax.writes == ["the prompt"]


def test_multiline_unicode_is_one_text_event():
    prompt = "first line\n⛵️ café 👩🏽‍💻\nlast line"
    ax = Composer(result=prompt + "\n")
    prepare_prompt(ax, prompt, lambda: None, lambda e: e["value"])
    assert ax.writes == [prompt]


def test_selection_failure_does_not_type():
    ax = Composer("Slack workspace marker\n")
    ax.on_select = lambda: setattr(ax, "selection", "partial")
    with pytest.raises(RuntimeError, match="selection was not confirmed"):
        prepare(ax)
    assert ax.writes == []


def test_focus_loss_during_selection_does_not_type():
    ax = Composer()
    ax.on_select = lambda: setattr(ax, "has_focus", False)
    with pytest.raises(RuntimeError, match="focus or selection changed"):
        prepare(ax)
    assert ax.writes == []


def test_focus_failure_does_not_type():
    ax = Composer()
    ax.on_focus = lambda: setattr(ax, "has_focus", False)
    with pytest.raises(RuntimeError, match="could not be focused"):
        prepare(ax)
    assert ax.writes == []


def test_editor_replacement_before_input_stops():
    ax = Composer()
    ax.on_select = lambda: setattr(ax, "current", {"value": "new draft"})
    with pytest.raises(RuntimeError, match="composer changed"):
        prepare(ax)
    assert ax.writes == []
