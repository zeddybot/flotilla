import json
from pathlib import Path

from slack_sdk.errors import SlackApiError

from slack_conductor.store import Store
from slack_conductor.uploads import Uploads
from tests.support import status


def test_snapshot_and_finalize_recovery_reuse_the_same_file_id(tmp_path, bridge_state):
    store, _, _, slack = bridge_state
    source = tmp_path / "report.csv"
    source.write_text("original")
    sid = status(store, "request", "report")
    store.update(sid, result="[report](report.csv) and [again](./report.csv)")
    row = store.status(sid)
    calls, sent, finished = [], [], []

    def call(method, **kwargs):
        calls.append(method)
        if method == "files_getUploadURLExternal":
            return {"file_id": "F1", "upload_url": "https://files.slack.com/upload/test"}
        if method == "files_completeUploadExternal":
            finished.append("F1")
            raise OSError("response lost after success")
        if method == "files_info":
            assert finished
            return {
                "file": {"id": "F1", "permalink": "https://slack.test/F1", "mimetype": "text/csv"}
            }
        raise AssertionError(method)

    slack.call = call
    now = [0]
    uploads = Uploads(
        store,
        slack,
        clock=lambda: now[0],
        send_bytes=lambda url, path: sent.append(Path(path).read_text()),
    )
    plan = uploads.prepare(row, tmp_path)
    assert len(plan["files"]) == 1
    source.unlink()
    assert Path(plan["files"][0]["snapshot"]).stat().st_mode & 0o077 == 0
    for _ in range(3):
        uploads.step(row, plan)
    assert plan["files"][0]["phase"] == "confirm"
    # Restart after Slack accepted finalization but the response was lost.
    now[0] = 100
    uploads = Uploads(Store(store.path), slack, clock=lambda: now[0])
    plan = json.loads(store.status(sid)["attachments"])
    assert not uploads.step(row, plan)
    assert uploads.step(row, plan)
    text, images, ids = uploads.result(plan)
    assert text == "[report](https://slack.test/F1) and [again](https://slack.test/F1)"
    assert not images and ids == ["F1"]
    assert sent == ["original"] and finished == ["F1"]
    assert calls.count("files_getUploadURLExternal") == 1
    uploads.cleanup(store.status(sid))
    assert not Path(plan["files"][0]["snapshot"]).exists()


def test_missing_and_nonregular_files_preserve_answer_without_uploads(tmp_path, bridge_state):
    store, _, _, slack = bridge_state
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "gone.pdf").write_bytes(b"another file with the same name")
    sid = status(store, "request", "report")
    store.update(sid, result="Answer. [missing](gone.pdf) [folder](.) [source](app.py:42)")
    uploads = Uploads(store, slack, clock=lambda: 0)
    plan = uploads.prepare(store.status(sid), tmp_path)
    assert uploads.step(store.status(sid), plan)
    text, _, ids = uploads.result(plan)
    assert text.startswith("Answer.") and text.count("file unavailable") == 2
    assert "full path from the workspace root" in text
    assert "[source](app.py:42)" in text and not ids


def test_upload_failure_is_bounded_and_does_not_hide_the_answer(tmp_path, bridge_state):
    store, _, _, slack = bridge_state
    (tmp_path / "a.txt").write_text("hello")
    sid = status(store, "request", "report")
    store.update(sid, result="Answer. [file](a.txt)")
    now = [0]
    uploads = Uploads(store, slack, clock=lambda: now[0])
    plan = uploads.prepare(store.status(sid), tmp_path)
    calls = []

    def offline(*args, **kwargs):
        calls.append(args)
        raise OSError("offline")

    slack.call = offline
    for _ in range(5):
        uploads.step(store.status(sid), plan)
        uploads.step(store.status(sid), plan)  # Backoff prevents a retry every poll.
        now[0] += 100
    assert len(calls) == 5
    assert uploads.step(store.status(sid), plan)
    assert "Answer." in uploads.result(plan)[0]
    assert "upload failed" in uploads.result(plan)[0]


def test_retry_finalization_when_slack_reports_unfinalized_file_as_deleted(bridge_state):
    store, _, _, slack = bridge_state
    sid = status(store, "request", "report")
    row = store.status(sid)
    item = {"name": "a.txt", "file_id": "F1", "phase": "confirm"}
    plan = {"files": [item]}
    clock = [0]
    calls = []

    def call(method, **kwargs):
        calls.append(method)
        if method == "files_info" and len(calls) == 1:
            raise SlackApiError("unfinalized", {"error": "file_deleted"})
        if method == "files_completeUploadExternal":
            return {}
        return {"file": {"permalink": "https://slack.test/F1", "mimetype": "text/plain"}}

    slack.call = call
    uploads = Uploads(store, slack, clock=lambda: clock[0])
    uploads.step(row, plan)
    assert item["phase"] == "complete"
    clock[0] = 100
    uploads.step(row, plan)
    uploads.step(row, plan)
    assert uploads.step(row, plan)
    assert calls == ["files_info", "files_completeUploadExternal", "files_info"]
