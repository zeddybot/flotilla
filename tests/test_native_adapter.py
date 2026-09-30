import itertools
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

from slack_conductor.conductor.journal import Journal, Unconfirmed
from slack_conductor.conductor.models import configuration
from slack_conductor.conductor.native import Conductor, NativeDB, identity
from slack_conductor.config import Config


def fixture(tmp_path):
    path = tmp_path / "conductor.db"
    with closing(sqlite3.connect(path)) as c, c:
        c.executescript("""
            CREATE TABLE repos(id,root_path);
            CREATE TABLE workspaces(local_id,id,repository_id,workspace_path,state,active_session_id);
            CREATE TABLE sessions(id,workspace_id,model,status);
            CREATE TABLE session_messages(id,session_id,role,content);
            CREATE TABLE session_messages_outbox(message_id,session_id);
        """)
        c.execute("INSERT INTO repos VALUES (?,?)", ("r", str(tmp_path)))
        c.execute(
            "INSERT INTO workspaces VALUES (?,?,?,?,?,?)",
            ("w", "remote-w", "r", str(tmp_path / "workspaces/w"), "ready", "selected-other"),
        )
        c.execute("INSERT INTO sessions VALUES (?,?,?,?)", ("s", "w", "gpt-6-luna", "idle"))
        c.execute(
            "INSERT INTO sessions VALUES (?,?,?,?)", ("foreign", "elsewhere", "fable-5-1", "idle")
        )
    transport = SimpleNamespace(
        bindings={},
        session=lambda _: {"live": {"status": "working"}, "incarnation": "app"},
        journal=Journal(tmp_path / "journal.sqlite"),
    )
    adapter = Conductor.__new__(Conductor)
    adapter.config = Config(
        repo=tmp_path, workspace_root=tmp_path / "workspaces", conductor_db=path
    )
    adapter.transport = transport
    adapter.db = NativeDB(path, transport)
    adapter.generation = None
    adapter.watches = {}
    return adapter, path


def test_failed_watch_is_isolated_retried_and_reset_on_reattachment(tmp_path, monkeypatch):
    adapter, path = fixture(tmp_path)
    clock = [100.0]
    monkeypatch.setattr("slack_conductor.conductor.native.time.monotonic", lambda: clock[0])
    transport = adapter.transport
    transport.online = lambda: True
    transport.attachment = {"generation": "first"}
    transport.journal = Journal(tmp_path / "journal.sqlite")
    transport.bindings = {"s": "w", "healthy": "w", "missing": "gone", "closed": "archived"}
    with closing(sqlite3.connect(path)) as c, c:
        c.execute("INSERT INTO workspaces(local_id,state) VALUES ('archived','archived')")
    calls = []
    failing = {"s"}

    def call(body, *, key, timeout):
        calls.append((body["sid"], key))
        transport.journal.enqueue(key, body)
        command = transport.journal.claim(transport.attachment["generation"])
        if command:
            transport.journal.complete(
                command["id"],
                transport.attachment["generation"],
                {"error": "Conductor target changed"}
                if body["sid"] in failing
                else {"value": True},
            )
        return transport.journal.wait(key, timeout)

    transport.call = call
    assert adapter.ready() is True
    assert adapter.watches["healthy"].ready
    assert not adapter.watches["s"].ready
    assert not adapter.watches["missing"].ready
    assert transport.bindings == {"s": "w", "healthy": "w", "missing": "gone"}
    assert [sid for sid, _ in calls] == ["s", "healthy"]
    first_key = calls[0][1]
    clock[0] += 29
    adapter.ready()
    assert len(calls) == 2  # no tight retry loop, including missing DB records
    failing.clear()
    clock[0] += 1
    adapter.ready()
    assert adapter.watches["s"].ready
    assert calls[-1][1] != first_key  # must not replay the journal's cached error
    transport.attachment["generation"] = "second"
    adapter.ready()
    assert [sid for sid, _ in calls[-2:]] == ["s", "healthy"]
    assert all("second" in key for _, key in calls[-2:])


def test_unconfirmed_watch_keeps_its_command_identity(tmp_path, monkeypatch):
    adapter, _ = fixture(tmp_path)
    clock = [100.0]
    monkeypatch.setattr("slack_conductor.conductor.native.time.monotonic", lambda: clock[0])
    transport = adapter.transport
    transport.online = lambda: True
    transport.attachment = {"generation": "first"}
    transport.journal = Journal(tmp_path / "journal.sqlite")
    transport.bindings = {"s": "w"}
    keys = []

    def call(body, *, key, timeout):
        transport.journal.enqueue(key, body)
        keys.append(key)
        if len(keys) == 1:
            raise Unconfirmed("acknowledgement delayed")
        command = transport.journal.claim("first")
        transport.journal.complete(command["id"], "first", {"value": True})
        return transport.journal.wait(key, timeout)

    transport.call = call
    assert adapter.ready() is True
    assert not adapter.watches["s"].ready
    clock[0] += 1
    assert adapter.ready() is True
    assert adapter.watches["s"].ready
    assert keys[0] == keys[1]
    with transport.journal.connect() as c:
        assert c.execute("SELECT count(*) FROM commands").fetchone()[0] == 1


def test_binding_ignores_selected_chat_but_rejects_foreign_session(tmp_path):
    adapter, _ = fixture(tmp_path)
    assert adapter.validate("w", "s")["id"] == "w"
    assert adapter.validate("w", "s")["active_session_id"] == "s"
    with pytest.raises(RuntimeError, match="does not belong"):
        adapter.validate("w", "foreign")
    adapter.config.workspace_root = tmp_path / "another-repo"
    with pytest.raises(RuntimeError, match="outside configured"):
        adapter.validate("w", "s")


def test_live_status_overrides_idle_and_missing_heartbeat_is_reconnecting(tmp_path):
    adapter, _ = fixture(tmp_path)
    assert adapter.db.session("s")["status"] == "working"
    adapter.transport.session = lambda _: None
    assert adapter.db.session("s")["status"] == "reconnecting"


def test_send_reuses_native_identity_after_ack_loss(tmp_path):
    adapter, path = fixture(tmp_path)
    calls = []

    def call(body, **kwargs):
        calls.append(body)
        if body["op"] == "send":
            with closing(sqlite3.connect(path)) as c, c:
                c.execute(
                    "INSERT INTO session_messages VALUES (?,?,?,?)",
                    (body["message_id"], "s", "user", body["prompt"]),
                )

    adapter.transport.call = call
    options = {"model": "GPT-6 Luna", "effort": "high", "fast": False, "send_mode": "steer"}
    adapter.send("w", "s", "unique Slack message", options)
    adapter.send("w", "s", "unique Slack message", options)
    assert [c["op"] for c in calls] == ["configure", "send"]
    assert calls[-1]["message_id"] == identity("s:unique Slack message")


def test_recovered_creation_uses_original_workspace_and_session(tmp_path):
    adapter, path = fixture(tmp_path)
    created = []

    def call(body, **kwargs):
        wid = body["wid"]
        if body["op"] == "create":
            assert body["args"]["localWorkspaceId"] == wid
            assert "workspaceId" not in body["args"]
        with closing(sqlite3.connect(path)) as c, c:
            if not c.execute("SELECT 1 FROM workspaces WHERE local_id=?", (wid,)).fetchone():
                created.append(wid)
                c.execute(
                    "INSERT INTO workspaces VALUES (?,?,?,?,?,?)",
                    (wid, "remote-" + wid, "r", str(tmp_path / "workspaces" / wid), "ready", None),
                )
                c.execute(
                    "INSERT INTO sessions VALUES (?,?,?,?)", ("created", wid, "fable-5-1", "idle")
                )

    adapter.transport.call = call
    first = adapter.new_workspace("slack:event")
    second = adapter.new_workspace("slack:event")
    assert first == second
    assert created == [identity("workspace:slack:event")]
    assert second["active_session_id"] == "created"


@pytest.mark.parametrize(
    "returned_workspace",
    [{"id": {"localId": "w", "serverId": "remote-w"}}, {"localId": "w", "id": "remote-w"}],
)
def test_creation_recovers_returned_identity_from_durable_receipt(
    tmp_path, monkeypatch, returned_workspace
):
    adapter, _ = fixture(tmp_path)
    monkeypatch.setattr("slack_conductor.conductor.native.time.sleep", lambda _: None)
    monkeypatch.setattr(
        "slack_conductor.conductor.native.time.monotonic", itertools.count().__next__
    )
    journal = adapter.transport.journal
    creations = []

    def call(body, *, key):
        journal.enqueue(key, body)
        command = journal.claim("generation")
        if command:
            assert body["op"] == "create"
            creations.append(body["wid"])
            journal.complete(key, "generation", {"value": {"workspace": returned_workspace}})
        return journal.wait(key)

    adapter.transport.call = call
    first = adapter.new_workspace("saved-event")
    second = adapter.new_workspace("saved-event")
    assert first == second
    assert first["id"] == "w" and first["active_session_id"] == "s"
    assert creations == [identity("workspace:saved-event")]


@pytest.mark.parametrize(
    ("alias", "expected", "agent"),
    [
        ("fable", "fable-5-1", "claude"),
        ("opus", "opus-5-5-1m", "claude"),
        ("sonnet", "sonnet-5-5-1m", "claude"),
        ("haiku", "haiku-4-5", "claude"),
        ("astra", "gpt-6-astra", "codex"),
        ("sol", "gpt-6.1-sol", "codex"),
        ("terra", "gpt-5.6-terra", "codex"),
        ("luna", "gpt-6-luna", "codex"),
        ("alpha", "opencode:opencode/union-alpha", "acp"),
    ],
)
def test_configured_aliases_select_the_expected_engine_and_model(alias, expected, agent):
    from slack_conductor.protocol import parse

    _, options, _ = parse(f"[{alias}] hello", "bot", Config())
    config, warning = configuration(options, "Fable 5.1")
    assert not warning
    assert config["model"] == expected
    assert config["agent"] == agent


def test_alpha_uses_provider_defaults_for_effort_and_rejects_fast_mode():
    from slack_conductor.protocol import parse

    text, options, _ = parse("[alpha][max][fast] investigate", "bot", Config())
    config, warning = configuration(options, "Fable 5.1")
    assert text == "investigate"
    assert config == {"agent": "acp", "model": "opencode:opencode/union-alpha", "fastMode": False}
    assert warning == "fast mode unavailable; using standard speed"


def test_unsupported_settings_keep_default_model_fallback():
    config, warning = configuration(
        {"model": "unavailable", "effort": "ultracode", "fast": True}, "Sonnet 5 1M"
    )
    assert config == {
        "model": "sonnet-5-1m",
        "agent": "claude",
        "thinkingLevel": "high",
        "fastMode": False,
    }
    assert "using Sonnet" in warning and "using high" in warning and "standard speed" in warning


def test_followup_cannot_reconfigure_session_while_first_prompt_is_in_outbox(tmp_path):
    adapter, path = fixture(tmp_path)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("INSERT INTO session_messages_outbox VALUES ('initial', 's')")
    calls = []
    adapter.transport.call = lambda body, **_: calls.append(body)
    note = adapter.send(
        "w",
        "s",
        "follow-up",
        {"model": "Fable 5.1", "effort": "high", "fast": False, "send_mode": "queue"},
    )
    assert [call["op"] for call in calls] == ["send"]
    assert calls[0]["mode"] == "queue"
    assert "session model remains gpt-6-luna" in note


def test_native_controls_keep_command_identity_and_session_target(tmp_path, monkeypatch):
    adapter, _ = fixture(tmp_path)
    adapter.db.latest_turn = lambda sid: "turn-1"
    calls = []
    adapter.transport.call = lambda body, **options: (
        calls.append((body, options))
        or (
            {"url": "conductor://workspace?id=w&session=s"}
            if body["op"] == "reveal"
            else {"ok": True}
        )
    )
    opened = []
    monkeypatch.setattr(
        "slack_conductor.conductor.native.subprocess.run",
        lambda command, **options: opened.append((command, options)),
    )
    assert adapter.reveal("w", "s", command_id="open-1") == {
        "url": "conductor://workspace?id=w&session=s"
    }
    assert opened[0][0] == ["/usr/bin/open", "conductor://workspace?id=w&session=s"]
    assert adapter.interrupt("w", "s", command_id="stop-1") == {"ok": True}
    assert adapter.respond(
        "w",
        "s",
        "approval-1",
        {"kind": "approval", "decision": "deny"},
        command_id="answer-1",
    ) == {"ok": True}
    assert adapter.questions("w", "s") == {"ok": True}
    assert [options["key"] for _, options in calls[:3]] == [
        "reveal:open-1",
        "interrupt:stop-1",
        "respond:answer-1",
    ]
    assert calls[3][0] == {"op": "questions", "wid": "w", "sid": "s"}
    assert calls[3][1]["key"].startswith("questions:")
    assert calls[1][0] == {"op": "interrupt", "wid": "w", "sid": "s", "turn_id": "turn-1"}
    assert calls[2][0]["question_id"] == "approval-1"
    with pytest.raises(RuntimeError, match="does not belong"):
        adapter.interrupt("w", "foreign", command_id="wrong")
    assert len(calls) == 4


def test_native_response_validation_and_idle_interrupt(tmp_path):
    adapter, _ = fixture(tmp_path)
    adapter.db.latest_turn = lambda sid: None
    adapter.transport.call = lambda *_args, **_kwargs: pytest.fail("no native call expected")
    assert adapter.interrupt("w", "s", command_id="idle") == {"state": "idle"}
    for response in [
        {"kind": "approval", "decision": "allow_edits_for_session"},
        {"kind": "question", "answers": []},
        {"kind": "plan", "approved": "yes"},
        {"kind": "codex_permission", "action": "grant", "scope": "session"},
        {"kind": "mcp_elicitation", "action": "accept", "content": "bad"},
    ]:
        with pytest.raises(ValueError):
            adapter.respond("w", "s", "q", response, command_id="invalid")


def test_interrupt_retry_keeps_original_turn_after_native_turn_changes(tmp_path):
    adapter, _ = fixture(tmp_path)
    turn = ["first"]
    adapter.db.latest_turn = lambda sid: turn[0]
    calls = []

    def call(body, *, key):
        adapter.transport.journal.enqueue(key, body)
        calls.append(body)
        return {"state": "requested"}

    adapter.transport.call = call
    adapter.interrupt("w", "s", command_id="stop")
    turn[0] = "later"
    adapter.interrupt("w", "s", command_id="stop")
    assert [body["turn_id"] for body in calls] == ["first", "first"]
