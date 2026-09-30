import json
import sqlite3
import time
from contextlib import closing

from slack_conductor.config import Config
from slack_conductor.monitoring import Monitor, report


def test_missing_server_databases_are_unknown_and_not_created(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    snapshot = report(Config(mode="server", state_dir=state), runtime_ready=True, slack_ready=True)
    assert snapshot["checks"]["event_backlog"]["state"] == "unknown"
    assert snapshot["checks"]["receipts"]["state"] == "unknown"
    assert not snapshot["ok"]
    assert list(state.glob("*.sqlite")) == []


def test_old_active_turn_is_informational(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    with closing(sqlite3.connect(state / "bridge.sqlite")) as db, db:
        db.executescript(
            "CREATE TABLE events(state TEXT, created REAL);"
            "CREATE TABLE statuses(id INTEGER, state TEXT, created REAL);"
            "CREATE TABLE notifications(state TEXT, attempts INTEGER);"
            "CREATE TABLE remote_asset_cleanup(attempts INTEGER);"
        )
        db.execute("INSERT INTO statuses VALUES (1, 'active', ?)", (time.time() - 86400,))
    with closing(sqlite3.connect(state / "remote.sqlite")) as db, db:
        db.execute("CREATE TABLE calls(response TEXT)")
    snapshot = report(Config(mode="server", state_dir=state), runtime_ready=True, slack_ready=True)
    assert snapshot["checks"]["active_work"]["state"] == "ok"
    assert snapshot["checks"]["active_work"]["oldest_age_seconds"] >= 86000
    assert snapshot["checks"]["delivery"]["state"] == "ok"


def test_one_stalled_receipt_and_delivery_warn_then_recover(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    with closing(sqlite3.connect(state / "bridge.sqlite")) as db, db:
        db.executescript(
            "CREATE TABLE events(state TEXT, created REAL);"
            "CREATE TABLE statuses(id INTEGER, state TEXT, created REAL);"
            "CREATE TABLE notifications(state TEXT, attempts INTEGER);"
            "CREATE TABLE remote_asset_cleanup(attempts INTEGER);"
        )
        db.execute("INSERT INTO statuses VALUES (1, 'delivering', 1)")
        db.execute("INSERT INTO statuses VALUES (2, 'active', 1)")
    with closing(sqlite3.connect(state / "remote.sqlite")) as db, db:
        db.execute("CREATE TABLE calls(id TEXT, response TEXT)")
        db.execute("INSERT INTO calls VALUES ('private-receipt-id', NULL)")
    clock = [time.time()]
    monkeypatch.setattr("slack_conductor.monitoring.time.time", lambda: clock[0])
    config = Config(mode="server", state_dir=state)
    monitor = Monitor(config)

    first = monitor.check(report(config, runtime_ready=True, slack_ready=True))
    assert first["checks"]["receipts"]["state"] == "ok"
    assert first["checks"]["delivery"]["state"] == "ok"
    assert "private-receipt-id" not in (state / "monitoring.json").read_text()
    clock[0] += 601
    stalled = report(config, runtime_ready=True, slack_ready=True)
    assert stalled["checks"]["receipts"]["stalled"] == 1
    assert stalled["checks"]["delivery"]["stalled"] == 1
    assert stalled["checks"]["active_work"]["state"] == "ok"
    warning = monitor.check(stalled)
    assert {i["check"] for i in warning["incidents"]} == {"receipts", "delivery"}
    assert "private-receipt-id" not in (state / "monitoring.log").read_text()

    with closing(sqlite3.connect(state / "bridge.sqlite")) as db, db:
        db.execute("UPDATE statuses SET state='active' WHERE id=1")
    with closing(sqlite3.connect(state / "remote.sqlite")) as db, db:
        db.execute("UPDATE calls SET response='{}'")
    recovered = monitor.check(report(config, runtime_ready=True, slack_ready=True))
    assert {i["check"] for i in recovered["incidents"]} == {"receipts", "delivery"}
    assert {i["transition"] for i in recovered["incidents"]} == {"recovered"}
    assert json.loads((state / "monitoring.json").read_text())["_tracked_pending"] == {}


def test_new_receipt_does_not_inherit_previous_receipt_age(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    with closing(sqlite3.connect(state / "remote.sqlite")) as db, db:
        db.execute("CREATE TABLE calls(id TEXT PRIMARY KEY, response TEXT)")
        db.execute("INSERT INTO calls VALUES ('first', NULL)")
    clock = [time.time()]
    monkeypatch.setattr("slack_conductor.monitoring.time.time", lambda: clock[0])
    config = Config(mode="server", state_dir=state)
    monitor = Monitor(config)
    monitor.check(report(config, runtime_ready=True, slack_ready=True))
    clock[0] += 601
    with closing(sqlite3.connect(state / "remote.sqlite")) as db, db:
        db.execute("UPDATE calls SET response='{}' WHERE id='first'")
        db.execute("INSERT INTO calls VALUES ('second', NULL)")
    fresh = report(config, runtime_ready=True, slack_ready=True)
    assert fresh["checks"]["receipts"]["count"] == 1
    assert fresh["checks"]["receipts"]["stalled"] == 0
    monitor.check(fresh)
    clock[0] += 601
    assert (
        report(config, runtime_ready=True, slack_ready=True)["checks"]["receipts"]["stalled"] == 1
    )


def test_incident_dedup_survives_unknown_and_logs_recovery(tmp_path):
    config = Config(state_dir=tmp_path)
    monitor = Monitor(config)

    def sample(state):
        return {
            "ok": state == "ok",
            "role": "client",
            "at": time.time(),
            "checks": {"link": {"state": state, "detail": "link status"}},
        }

    assert [i["transition"] for i in monitor.check(sample("warning"))["incidents"]] == ["warning"]
    assert monitor.check(sample("warning"))["incidents"] == []
    assert monitor.check(sample("unknown"))["incidents"] == []
    assert monitor.check(sample("warning"))["incidents"] == []
    assert [i["transition"] for i in monitor.check(sample("ok"))["incidents"]] == ["recovered"]
    assert monitor.check(sample("ok"))["incidents"] == []
    assert [
        json.loads(line)["transition"]
        for line in (tmp_path / "monitoring.log").read_text().splitlines()
    ] == ["warning", "recovered"]
    assert (tmp_path / "monitoring.json").stat().st_mode & 0o077 == 0
    assert (tmp_path / "monitoring.log").stat().st_mode & 0o077 == 0


def test_stale_sample_cannot_reopen_recovered_incident(tmp_path):
    monitor = Monitor(Config(state_dir=tmp_path))

    def sample(at, state):
        return {
            "ok": state == "ok",
            "role": "client",
            "at": at,
            "checks": {"link": {"state": state, "detail": state}},
        }

    monitor.check(sample(100, "warning"))
    monitor.check(sample(200, "ok"))
    result = Monitor(Config(state_dir=tmp_path)).check(sample(150, "warning"))
    assert result["incidents"] == []
    assert result["active_incidents"] == []
    assert json.loads((tmp_path / "monitoring.json").read_text())["at"] == 200
    assert [
        json.loads(line)["transition"]
        for line in (tmp_path / "monitoring.log").read_text().splitlines()
    ] == ["warning", "recovered"]


def test_client_report_redacts_token_and_does_not_write_state(tmp_path, monkeypatch):
    token = tmp_path / "token"
    token.write_text("secret-token-with-at-least-thirty-two-chars")
    token.chmod(0o600)
    now = time.time()
    (tmp_path / "remote-status.json").write_text(json.dumps({"at": now, "connected": True}))
    (tmp_path / "native-heartbeat.json").write_text(json.dumps({"at": now, "ready": True}))
    monkeypatch.setattr("slack_conductor.monitoring.urlopen", lambda request, timeout: _Response())
    config = Config(
        mode="client",
        state_dir=tmp_path,
        server_url="wss://example.com/client",
        remote_token_file=token,
    )
    snapshot = report(config)
    serialized = json.dumps(snapshot)
    assert "secret-token" not in serialized
    assert "example.com" not in serialized
    assert snapshot["checks"]["server_health"]["state"] == "ok"
    assert not (tmp_path / "monitoring.json").exists()
    assert not list(tmp_path.glob("*.sqlite"))


def test_missing_client_token_reports_warning_without_exception(tmp_path):
    config = Config(mode="client", state_dir=tmp_path, server_url="wss://example.com/client")
    snapshot = report(config)
    assert snapshot["checks"]["server_health"] == {
        "state": "warning",
        "detail": "server health unavailable",
    }


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, *_):
        return b'{"ready": true}'
