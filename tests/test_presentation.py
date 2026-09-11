import json
from types import SimpleNamespace

import pytest
from slack_sdk.errors import SlackApiError
from slack_sdk.web.slack_response import SlackResponse

from slack_conductor.lifecycle import Turn
from slack_conductor.poller import Poller
from slack_conductor.presentation import STATES, live, pages, render
from slack_conductor.slack_io import SlackIO
from tests.support import event, message, result, status


def test_headers_and_background_content_survive_state_changes():
    for phase, (emoji, label) in STATES.items():
        assert render(phase, "update", "detail") == f"{emoji} *{label}* · detail\n\nupdate"
    turn = Turn(
        tasks={"deploy": "preview deployment"},
        content="I'll verify after deployment.",
        result_seq=5,
    )
    assert (
        live(turn)
        == "⏳ *Waiting* · preview deployment · 1 background job\n\nI'll verify after deployment."
    )
    turn.result_seq, turn.phase, turn.detail = 0, "working", "verifying preview"
    assert live(turn).startswith("⚙️ *Working* · verifying preview · 1 background job")
    assert live(turn).endswith("I'll verify after deployment.")


def test_long_final_has_one_status_header_and_closed_code_fences():
    output = pages("done", "```python\n" + "print('hello')\n" * 1000 + "```", "<&>" * 100)
    assert len(output) > 1
    assert output[0].startswith("✅ *Done*")
    assert all(p.startswith("```python\n") for p in output[1:])
    assert all(len(p) <= 3900 for p in output)
    assert all(p.count("```") % 2 == 0 for p in output)
    assert all("```python\n" in p for p in output)


def test_markdown_body_survives_posts_edits_and_body_removal():
    body = (
        "# Findings\n\n**bold**, *italic*, ~~strike~~, `inline_code`.\n\n"
        "[link](https://example.com/path?q=a&b=c)\n\n"
        "> quote\n\n- [x] done\n- [ ] pending\n\n"
        "| Name | State |\n| --- | --- |\n| test | passed |\n\n"
        '```python\nprint("**literal** <tag> & value")\n```'
    )
    posts, edits = [], []
    slack = SlackIO(
        SimpleNamespace(
            chat_postMessage=lambda **kw: posts.append(kw) or {"ts": "2"},
            chat_update=lambda **kw: edits.append(kw),
        )
    )
    text = render("working", body)
    assert slack.post("C", "1", text, "key") == "2"
    status = {"channel": "C", "status_ts": "2"}
    slack.edit(status, render("done", body))
    for call in (posts[0], edits[0]):
        assert call["text"].endswith(body)  # Notification/accessibility fallback remains present.
        assert call["blocks"][0]["text"]["type"] == "mrkdwn"
        assert call["blocks"][1] == {"type": "markdown", "text": body}
    assert edits[0]["blocks"][0]["text"]["text"] == "✅ *Done*"
    slack.edit(status, render("archived"))
    assert len(edits[-1]["blocks"]) == 1  # Explicit replacement clears the previous body.


def test_long_code_continuations_keep_language_and_literal_contents():
    code = 'print("**literal** <tag> & value")\n' * 400
    output = pages("done", "```python\n" + code + "```", note="verified")
    blocks = []
    slack = SlackIO(
        SimpleNamespace(chat_postMessage=lambda **kw: blocks.extend(kw["blocks"]) or {"ts": "2"})
    )
    for i, text in enumerate(output):
        slack.post("C", "1", text, str(i), status_header=i == 0)
    assert sum(b["type"] == "section" for b in blocks) == 1
    bodies = [b["text"] for b in blocks if b["type"] == "markdown"]
    assert len(bodies) > 1
    assert sum(b.count('print("**literal** <tag> & value")') for b in bodies) == 400
    assert all(b.startswith("```python\n") for b in bodies if "```" in b)
    assert all(len(p) <= 3900 for p in output)


@pytest.mark.parametrize("method", ["chat_postMessage", "chat_update"])
@pytest.mark.parametrize("error", ["invalid_blocks", "channel_not_found"])
def test_rejected_markdown_falls_back_without_retrying_other_errors(method, error):
    calls = []

    def send(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            response = SlackResponse(
                client=None,
                http_verb="POST",
                api_url="https://slack.com/api/" + method,
                req_args={},
                data={"ok": False, "error": error},
                headers={},
                status_code=200,
            )
            raise SlackApiError("rejected", response)
        return {"ts": "2"}

    slack = SlackIO(SimpleNamespace(**{method: send}))
    text = render("done", "\n\n".join(f"# Heading {i}" for i in range(75)))

    def deliver():
        if method == "chat_postMessage":
            return slack.post("C", "1", text, "key")
        return slack.edit({"channel": "C", "status_ts": "2"}, text)

    if error != "invalid_blocks":
        with pytest.raises(SlackApiError):
            deliver()
        assert len(calls) == 1
    else:
        deliver()
        assert len(calls) == 2
        assert calls[1] == calls[0] | {"blocks": []}
        assert calls[1]["text"] == text


def test_steering_moves_content_below_followup_and_preserves_jobs_across_crash(bridge_state):
    store, conn, db, slack = bridge_state
    first = status(store, "e1", "first")
    message(conn, "user", "first", "turn")
    event(conn, "task_started", task_id="job", description="build preview")
    result(conn, "Waiting for build.", "turn")
    poller = Poller(store, db, slack, settle_seconds=0)
    poller.tick()
    second = status(store, "e2", "second", cursor=db.cursor("s"))
    message(conn, "user", "second", "turn")
    # Simulate interruption after the SQLite handoff, before Slack cleanup.
    store.handoff(second, first)
    store.update(second, state="active", turn_id="turn")
    assert json.loads(store.status(second)["discard_ts"]) == ["status-1"]
    poller = Poller(store, db, slack, settle_seconds=0)
    poller.reconcile()
    assert store.status(second)["status_ts"] == "status-2"
    assert set(Turn.load(store.status(second)["tracking"]).tasks) == {"job"}
    assert slack.deletes == ["status-1"]
    assert slack.edits[-1][0] == "status-2"
    assert "Waiting for build." in slack.edits[-1][1]
    assert "1 background job" in slack.edits[-1][1]
    event(conn, "task_notification", task_id="job", status="completed")
    result(conn, "Verified the steered change.", "turn")
    poller.tick()
    assert slack.edits[-1] == ("status-2", render("done", "Verified the steered change."))
    assert store.status(first)["state"] == "superseded"
    assert store.status(second)["state"] == "complete"


def test_repeated_steers_retry_cleanup_and_deliver_long_answer_at_latest_message(bridge_state):
    store, conn, db, slack = bridge_state
    first = status(store, "e1", "first")
    message(conn, "user", "first", "turn")
    event(conn, "task_started", task_id="job", description="preview")
    result(conn, "Waiting for preview.", "turn")
    poller = Poller(store, db, slack, settle_seconds=0)
    poller.tick()
    for key in ("second", "third"):
        new = status(store, key, key, cursor=db.cursor("s"))
        message(conn, "user", key, "turn")
        store.handoff(new, first)
        store.update(new, state="active", turn_id="turn")
        first = new
    assert json.loads(store.status(new)["discard_ts"]) == ["status-1", "status-2"]
    original_delete = slack.delete

    def interrupted_delete(status):
        # The replacement must contain the interim content before any deletion.
        assert slack.edits[-1][0] == "status-3"
        assert "Waiting for preview." in slack.edits[-1][1]
        if status["status_ts"] == "status-2":
            raise RuntimeError("Slack unavailable")
        original_delete(status)

    slack.delete = interrupted_delete
    poller.tick()
    assert store.status(new)["discard_ts"] != "[]"
    slack.delete = original_delete
    poller = Poller(store, db, slack, settle_seconds=0)
    poller.reconcile()
    assert store.status(new)["discard_ts"] == "[]"
    assert "status-3" not in slack.deletes
    event(conn, "task_notification", task_id="job", status="completed")
    answer = "Verified the preview.\n" * 250
    result(conn, answer, "turn")
    poller.tick()
    assert slack.edits[-1] == ("status-3", pages("done", answer)[0])
    assert [post[2] for post in slack.posts] == pages("done", answer)[1:]
    assert store.status(new)["state"] == "complete"


def test_error_result_renders_failed_and_preserves_partial_answer(bridge_state):
    store, conn, db, slack = bridge_state
    status(store, "e1", "task")
    message(conn, "user", "task", "turn")
    message(
        conn,
        "assistant",
        json.dumps(
            {"type": "result", "is_error": True, "result": "Tests failed; changes retained."}
        ),
        "turn",
    )
    Poller(store, db, slack, settle_seconds=0).tick()
    assert slack.edits[-1][1] == render("failed", "Tests failed; changes retained.")


def test_block_limit_continuations_resume_after_delivery_failure(bridge_state):
    store, conn, db, slack = bridge_state
    sid = status(store, "e1", "task")
    message(conn, "user", "task", "turn")
    answer = "\n\n".join(f"# Heading {i}" for i in range(75))
    result(conn, answer, "turn")
    original_post = slack.post

    def interrupted(*args, **kwargs):
        raise RuntimeError("Slack unavailable")

    slack.post = interrupted
    Poller(store, db, slack, settle_seconds=0).tick()
    assert store.status(sid)["delivered"] == 1
    assert store.status(sid)["state"] == "delivering"

    def resumed_post(*args, **kwargs):
        assert kwargs["status_header"] is False
        return original_post(*args, **kwargs)

    slack.post = resumed_post
    Poller(store, db, slack, settle_seconds=0).reconcile()
    assert len(slack.edits) == 1
    assert [p[2] for p in slack.posts] == pages("done", answer)[1:]
    assert store.status(sid)["state"] == "complete"
