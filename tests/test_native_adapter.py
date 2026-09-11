import sqlite3
from types import SimpleNamespace

import pytest

from slack_conductor.conductor.models import configuration
from slack_conductor.conductor.native import Conductor, NativeDB, identity
from slack_conductor.config import Config


def fixture(tmp_path):
    path = tmp_path / "conductor.db"
    with sqlite3.connect(path) as c:
        c.executescript("""
            CREATE TABLE repos(id,root_path);
            CREATE TABLE workspaces(id,repository_id,workspace_path,state,active_session_id);
            CREATE TABLE sessions(id,workspace_id,model,status);
            CREATE TABLE session_messages(id,session_id,role,content);
            CREATE TABLE session_messages_outbox(message_id,session_id);
        """)
        c.execute("INSERT INTO repos VALUES (?,?)", ("r", str(tmp_path)))
        c.execute(
            "INSERT INTO workspaces VALUES (?,?,?,?,?)",
            ("w", "r", str(tmp_path / "workspaces/w"), "ready", "selected-other"),
        )
        c.execute("INSERT INTO sessions VALUES (?,?,?,?)", ("s", "w", "gpt-5.6-luna", "idle"))
        c.execute(
            "INSERT INTO sessions VALUES (?,?,?,?)", ("foreign", "elsewhere", "fable-5-1", "idle")
        )
    transport = SimpleNamespace(
        bindings={}, session=lambda _: {"live": {"status": "working"}, "incarnation": "app"}
    )
    adapter = Conductor.__new__(Conductor)
    adapter.config = Config(
        repo=tmp_path, workspace_root=tmp_path / "workspaces", conductor_db=path
    )
    adapter.transport = transport
    adapter.db = NativeDB(path, transport)
    return adapter, path


def test_binding_ignores_selected_chat_but_rejects_foreign_session(tmp_path):
    adapter, _ = fixture(tmp_path)
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
            with sqlite3.connect(path) as c:
                c.execute(
                    "INSERT INTO session_messages VALUES (?,?,?,?)",
                    (body["message_id"], "s", "user", body["prompt"]),
                )

    adapter.transport.call = call
    options = {"model": "GPT-5.6 Luna", "effort": "high", "fast": False, "send_mode": "steer"}
    adapter.send("w", "s", "unique Slack message", options)
    adapter.send("w", "s", "unique Slack message", options)
    assert [c["op"] for c in calls] == ["configure", "send"]
    assert calls[-1]["message_id"] == identity("s:unique Slack message")


def test_recovered_creation_uses_original_workspace_and_session(tmp_path):
    adapter, path = fixture(tmp_path)
    created = []

    def call(body, **kwargs):
        wid = body["wid"]
        with sqlite3.connect(path) as c:
            if not c.execute("SELECT 1 FROM workspaces WHERE id=?", (wid,)).fetchone():
                created.append(wid)
                c.execute(
                    "INSERT INTO workspaces VALUES (?,?,?,?,?)",
                    (wid, "r", str(tmp_path / "workspaces" / wid), "ready", None),
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
    ("alias", "expected", "agent"),
    [
        ("fable", "fable-5-1", "claude"),
        ("opus", "opus-5-1m", "claude"),
        ("sonnet", "sonnet-5-1m", "claude"),
        ("haiku", "haiku-4-5", "claude"),
        ("astra", "gpt-6-astra", "codex"),
        ("sol", "gpt-5.6-sol", "codex"),
        ("terra", "gpt-5.6-terra", "codex"),
        ("luna", "gpt-5.6-luna", "codex"),
    ],
)
def test_configured_aliases_select_the_expected_engine_and_model(alias, expected, agent):
    from slack_conductor.protocol import parse

    _, options, _ = parse(f"[{alias}] hello", "bot", Config())
    config, warning = configuration(options, "Fable 5.1")
    assert not warning
    assert config["model"] == expected
    assert config["agent"] == agent


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
    with sqlite3.connect(path) as connection:
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
    assert "session model remains gpt-5.6-luna" in note
