import pytest

from slack_conductor.runtime_log import CursorExpired, EventLog


def test_replay_survives_restart_and_is_bounded(tmp_path):
    log = EventLog(tmp_path / "events.sqlite")
    for n in range(6):
        log.append("session", {"event": {"kind": "result", "text": str(n)}})
    other = EventLog(log.path)
    page = other.page("session", limit=2)
    assert [r["seq"] for r in page["events"]] == [1, 2]
    assert page["cursor"] == 2 and page["has_more"]
    assert other.page("session", after=4)["has_more"] is False
    assert other.page("other")["events"] == []


def test_native_cursors_are_preserved_and_reimport_is_idempotent(tmp_path):
    log = EventLog(tmp_path / "events.sqlite")
    row = {"id": "native-id", "event": {"kind": "started"}}
    log.append("s", row, seq=87)
    log.append("s", row, seq=87)
    assert len(log.page("s")["events"]) == 1
    assert log.cursor("s") == 87
    assert log.append("s", row)["seq"] == 88


def test_expired_cursor_requires_explicit_resync_and_sequence_never_rewinds(tmp_path):
    log = EventLog(tmp_path / "events.sqlite")
    log.append("s", {"event": {}}, seq=50)
    log.snapshot("s", {"status": "idle"})
    log.prune("s", 50)
    with pytest.raises(CursorExpired, match="resume after 50"):
        log.page("s")
    assert log.snapshot("s")["value"] == {"status": "idle"}
    assert log.page("s", after=50)["events"] == []
    assert log.append("s", {"event": {}})["seq"] == 51


@pytest.mark.parametrize("after,limit", [(-1, 1), (True, 1), (0, 0), (0, 501)])
def test_invalid_replay_bounds(tmp_path, after, limit):
    with pytest.raises(ValueError):
        EventLog(tmp_path / "events.sqlite").page("s", after, limit)
