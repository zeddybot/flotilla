import json
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from slack_conductor.conductor.journal import Journal, Unconfirmed
from slack_conductor.conductor.transport import Transport


def test_pause_commits_before_ack_and_old_send_cannot_override_newer_stop(tmp_path):
    path = tmp_path / "journal.sqlite"
    journal = Journal(path)
    journal.pause("send:m", "s", False)
    stopped = journal.pause("stop", "s", True)
    restarted = Journal(path)
    assert restarted.pause_state("s") == stopped
    assert restarted.pause("send:m", "s", False) == stopped
    assert restarted.pause("resume", "s", False)["paused"] == 0
    with pytest.raises(ValueError, match="identity reused"):
        restarted.pause("stop", "s", False)


def test_fenced_ack_and_reconnect_reconcile_accepted_send_before_retry(tmp_path):
    journal = Journal(tmp_path / "journal.sqlite")
    body = {"op": "send", "sid": "s", "message_id": "m"}
    journal.enqueue("m", body)
    assert journal.claim("old")["body"] == body
    journal.reconnect("new", SimpleNamespace(accepted=lambda *_: True))
    assert journal.wait("m", 0.1) == ""
    assert not journal.complete("m", "old", {"value": "late response"})
    assert journal.claim("new") is None
    with pytest.raises(ValueError, match="different arguments"):
        journal.enqueue("m", {**body, "message_id": "different"})


def test_absent_command_only_retries_after_attachment_changes(tmp_path):
    journal = Journal(tmp_path / "journal.sqlite")
    journal.enqueue("m", {"op": "send", "sid": "s", "message_id": "m"})
    journal.claim("old")
    native = SimpleNamespace(accepted=lambda *_: False)
    journal.reconnect("old", native)
    assert journal.claim("old") is None
    journal.reconnect("new", native)
    assert journal.claim("new")["id"] == "m"
    assert not journal.complete("m", "old", {"value": "wrong"})
    assert journal.complete("m", "new", {"value": "ok"})


@pytest.mark.parametrize("operation", ["interrupt", "respond"])
def test_unconfirmed_control_does_not_replay_into_later_native_state(tmp_path, operation):
    journal = Journal(tmp_path / "journal.sqlite")
    journal.enqueue("control", {"op": operation, "sid": "s", "turn_id": "turn-1"})
    journal.claim("old")
    journal.reconnect("new", SimpleNamespace(accepted=lambda *_: False))
    assert journal.claim("new") is None
    assert not journal.complete("control", "old", {"value": "late"})
    with pytest.raises(Unconfirmed, match="inspect the native session"):
        journal.wait("control", 0.1)


def test_deferred_send_waits_for_explicit_resume(tmp_path):
    journal = Journal(tmp_path / "journal.sqlite")
    journal.enqueue("m", {"op": "send", "sid": "s", "message_id": "m"})
    journal.claim("old")
    journal.pause("stop", "s", True)
    journal.complete("m", "old", {"value": {"deferred": True}})
    journal.reconnect("new", SimpleNamespace(accepted=lambda *_: False))
    assert journal.claim("new") is None
    journal.pause("resume", "s", False)
    assert journal.claim("new")["id"] == "m"


@pytest.mark.parametrize("claimed", [False, True])
@pytest.mark.parametrize("scoped", [False, True])
def test_shutdown_never_replays_into_replacement_attachment(tmp_path, claimed, scoped):
    journal = Journal(tmp_path / "journal.sqlite")
    body = {"op": "detach"}
    if scoped:
        body["target_generation"] = "old"
    journal.enqueue("shutdown", body)
    if claimed:
        # Reproduce both old unscoped and current in-flight shutdown records.
        with journal.connect() as c:
            c.execute("UPDATE commands SET state='running',generation='old'")
    journal.enqueue("read", {"op": "repos"})
    # The new frontend can poll before reconnect reconciliation runs.
    assert journal.claim("new")["id"] == "read"
    assert journal.wait("shutdown", 0.1) is True
    journal.reconnect("new", SimpleNamespace(accepted=lambda *_: False))
    assert journal.claim("new") is None


def test_current_shutdown_and_disconnected_command_ack(tmp_path):
    journal = Journal(tmp_path / "journal.sqlite")
    journal.enqueue("shutdown", {"op": "detach", "target_generation": "current"})
    assert journal.claim("current")["id"] == "shutdown"
    assert journal.complete("shutdown", "current", {"value": True})
    journal.enqueue("read", {"op": "repos"})
    journal.claim("current")
    assert journal.complete("read", "current", {"retry": True})
    assert journal.claim("current")["id"] == "read"


def test_transport_rejects_stale_attachment_and_unknown_pause_target(tmp_path):
    transport = Transport(tmp_path, lambda epoch: epoch == "app")
    transport.start()
    old = transport.attach("app")
    current = transport.attach("app")
    try:
        for config, route, body, code in [
            (old, "heartbeat", {"ready": True}, 409),
            (current, "pause", {"sid": "unowned", "paused": True, "request": "1"}, 404),
        ]:
            req = Request(
                config["base"] + "/" + route,
                data=json.dumps({**body, "generation": config["generation"]}).encode(),
            )
            with pytest.raises(HTTPError) as error:
                urlopen(req)
            with error.value as response:
                assert response.code == code
        assert not transport.online()
    finally:
        transport.close()


def test_bootstrap_module_is_scoped_to_current_attachment(tmp_path):
    transport = Transport(tmp_path, lambda _: True)
    transport.start()
    try:
        config = transport.attach("app")
        transport.bootstrap = "console.log('fixed dispatcher');"
        url = config["base"] + "/bootstrap?generation=" + config["generation"]
        with urlopen(url) as response:
            assert response.headers["Content-Type"] == "application/javascript"
            assert response.read().decode() == transport.bootstrap
        transport.attach("new")
        with pytest.raises(HTTPError) as error:
            urlopen(url)
        with error.value as response:
            assert response.code == 409
        assert transport.bootstrap == ""
    finally:
        transport.close()
