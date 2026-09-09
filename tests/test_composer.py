import pytest

from slack_conductor.conductor.composer import prepare_prompt


class Composer:
    def __init__(self, effects, draft=""):
        self.current = {"value": draft}
        self.effects = iter(effects)
        self.writes = []

    def composer(self):
        return self

    def require(self, root, role):
        assert root is self and role == "AXTextArea"
        return self.current

    def set(self, editor, key, value):
        assert editor is self.current and key == "AXValue"
        self.writes.append(value)
        # Simulate the mounted editor being replaced as the UI settles.
        self.current = {"value": next(self.effects)}

    def wait(self, predicate, message):
        if not predicate():
            raise RuntimeError(message)


def prepare(ax, confirm=lambda: None):
    prepare_prompt(ax, "the prompt", confirm, lambda e: e["value"])


def test_replaced_editor_is_read_without_rewriting():
    ax = Composer(["the prompt\n"])
    prepare(ax)
    assert ax.writes == ["the prompt"]


def test_empty_write_is_retried_before_submission():
    ax = Composer(["\n", "the prompt\n"])
    prepare(ax)
    assert ax.writes == ["the prompt", "the prompt"]


@pytest.mark.parametrize("draft", ["my unsent work", "the prompt plus an edit"])
def test_existing_draft_is_preserved(draft):
    ax = Composer([], draft=draft)
    with pytest.raises(RuntimeError, match="unsent draft"):
        prepare(ax)
    assert ax.writes == []
    assert ax.current["value"] == draft


def test_draft_that_appears_during_write_is_preserved():
    ax = Composer(["user typed this"])
    with pytest.raises(RuntimeError, match="unsent draft"):
        prepare(ax)
    assert len(ax.writes) == 1
    assert ax.current["value"] == "user typed this"


def test_existing_matching_prompt_is_not_rewritten():
    ax = Composer([], draft="the prompt\n")
    prepare(ax)
    assert ax.writes == []


def test_empty_retries_are_bounded():
    ax = Composer(["", "", ""])
    with pytest.raises(RuntimeError, match="did not match prompt"):
        prepare(ax)
    assert len(ax.writes) == 3


def test_target_change_stops_retry():
    ax = Composer([""])

    def confirm():
        if ax.writes:
            raise RuntimeError("workspace changed")

    with pytest.raises(RuntimeError, match="workspace changed"):
        prepare(ax, confirm)
    assert len(ax.writes) == 1


def test_creation_marker_can_be_replaced():
    ax = Composer(["the prompt"], draft="Slack workspace unique-id")
    prepare(ax)
    assert ax.writes == ["the prompt"]
