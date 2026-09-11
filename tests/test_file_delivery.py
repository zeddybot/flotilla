from pathlib import Path
from types import SimpleNamespace

from slack_conductor.file_links import references
from slack_conductor.poller import Poller
from slack_conductor.presentation import blocks, pages
from slack_conductor.slack_io import SlackIO
from slack_conductor.store import Store
from tests.support import event, message, result, status


def test_final_steered_reply_owns_uploads_and_restart_does_not_repeat_them(tmp_path, bridge_state):
    store, conn, db, slack = bridge_state
    conn.execute("ALTER TABLE workspaces ADD COLUMN workspace_path")
    conn.execute("UPDATE workspaces SET workspace_path=?", (str(tmp_path),))
    conn.commit()
    (tmp_path / "result.csv").write_text("verified")
    first = status(store, "first", "watch")
    message(conn, "user", "watch", "turn")
    event(conn, "task_started", task_id="deploy")
    result(conn, "[Old interim file](must-not-upload.csv)", "turn")
    clock = [100]
    poller = Poller(store, db, slack, clock=lambda: clock[0], settle_seconds=0)
    poller.tick()
    assert store.status(first)["attachments"] == "{}"
    second = status(store, "second", "stop", cursor=db.cursor("s"))
    message(conn, "user", "stop", "stop")
    event(conn, "task_notification", task_id="deploy", status="stopped", turn="stop")
    result(conn, "Stopped. [Results](result.csv)", "stop")
    calls, writes, sent = [], [], []

    def call(method, **kwargs):
        calls.append(method)
        if method == "files_getUploadURLExternal":
            return {"file_id": "F1", "upload_url": "https://files.slack.com/upload/test"}
        if method == "files_completeUploadExternal":
            return {}
        return {"file": {"permalink": "https://slack.test/result", "mimetype": "text/csv"}}

    def edit(row, text, **kwargs):
        writes.append((row["status_ts"], text, kwargs))

    slack.call, slack.edit = call, edit
    poller.uploads.send_bytes = lambda url, path: sent.append(Path(path).read_text())
    poller.tick()  # Handoff and allocate the upload.
    assert store.status(first)["state"] == "superseded"
    assert slack.deletes == ["status-1"]
    assert "Sending" in writes[-1][1]
    poller.tick()  # Upload bytes, then restart.
    poller = Poller(Store(store.path), db, slack, clock=lambda: clock[0], settle_seconds=0)
    poller.reconcile()
    for _ in range(3):
        poller.tick()
    assert sent == ["verified"]
    assert calls.count("files_getUploadURLExternal") == 1
    assert calls.count("files_completeUploadExternal") == 1
    assert writes[-1] == (
        "status-2",
        "✅ *Done*\n\nStopped. [Results](https://slack.test/result)",
        {"images": {}, "file_ids": ["F1"]},
    )
    assert store.status(second)["state"] == "complete"
    assert store.status(first)["attachments"] == "{}"


def test_slack_edit_attaches_files_and_renders_images_between_markdown():
    calls = []
    slack = SlackIO(SimpleNamespace(chat_update=lambda **kwargs: calls.append(kwargs)))
    slack.edit(
        {"channel": "D1", "status_ts": "1"},
        "✅ *Done*\n\nBefore. ![Chart](https://slack.test/chart) After.",
        file_ids=["F1"],
        images={"https://slack.test/chart": "F1"},
    )
    assert calls[0]["file_ids"] == ["F1"]
    assert [b["type"] for b in calls[0]["blocks"]] == ["section", "markdown", "image", "markdown"]
    assert calls[0]["blocks"][2]["alt_text"] == "Chart"


def test_many_image_references_and_long_paragraphs_preserve_previews_when_split():
    text = "Before. " + "![Chart](https://slack.test/chart) Some more prose. " * 40 + "After."
    output = pages("done", text)
    assert len(output) > 1
    rendered = [
        blocks(p, {"https://slack.test/chart": "F1"}, status_header=i == 0)
        for i, p in enumerate(output)
    ]
    assert all(len(page) <= 25 for page in rendered)
    assert sum(b["type"] == "image" for page in rendered for b in page) == 40
    assert all(len(p) <= 3900 for p in output)
    assert sum(len([r for r in references(p) if r["image"]]) for p in output) == 40
