import json
from types import SimpleNamespace

from slack_conductor.config import Config
from slack_conductor.poller import Poller
from slack_conductor.presentation import render
from slack_conductor.protocol import chunks, parse
from slack_conductor.slack_app import Bridge
from slack_conductor.store import Store
from tests.support import fixture, message, result, status


def test_tokens_are_leading_case_insensitive_and_sticky():
    text, options, command = parse(
        "<@B> [ASTRA][MAX][fast][queue] fix &lt;x&gt; [close]", "B", Config()
    )
    assert text == "fix <x> [close]"
    assert options["model"] == "GPT-6 Astra" and options["effort"] == "max" and options["fast"]
    assert command is None
    assert parse("next", "B", Config(), options)[1] == options
    assert parse("[unknown] test", "B", Config())[0] == "[unknown] test"


def test_default_steering_and_explicit_queue_override():
    config = Config()
    _, defaults, _ = parse("start", "B", config)
    assert defaults["send_mode"] == "steer"
    assert defaults["model"] == "Fable 5.1"
    _, queued, _ = parse("[queue] next", "B", config, defaults)
    assert queued["send_mode"] == "queue"
    assert parse("next again", "B", config, queued)[1]["send_mode"] == "queue"
    assert parse("[steer] change course", "B", config, queued)[1]["send_mode"] == "steer"
    for token, label in {
        "astra": "GPT-6 Astra",
        "sol": "GPT-5.6 Sol",
        "terra": "GPT-5.6 Terra",
        "luna": "GPT-5.6 Luna",
    }.items():
        assert parse(f"[{token}] go", "B", config)[1]["model"] == label


def test_chunking_preserves_normal_code_blocks_and_bounds_giant_blocks():
    code = "```python\nprint('hello')\n```"
    parts = chunks("a" * 3880 + "\n" + code)
    assert "".join(parts) == "a" * 3880 + "\n" + code
    assert code in parts
    giant = chunks("```" + "x" * 10000 + "```")
    assert all(len(p) <= 3900 and p.startswith("```") and p.endswith("```") for p in giant)


def test_owner_team_bot_gate_and_overlapping_slack_events(tmp_path):
    store, conn, db, slack = fixture(tmp_path)
    bridge = Bridge(Config(), store, SimpleNamespace(db=db), slack, "B", "U1", "T1")
    event = {"type": "app_mention", "channel": "C1", "ts": "1", "user": "U1", "text": "<@B> go"}
    for changes, team in [
        ({"user": "U2"}, "T1"),
        ({"bot_id": "bot"}, "T1"),
        ({}, "T2"),
        ({"subtype": "message_changed"}, "T1"),
    ]:
        bridge.receive({"team_id": team, "event": event | changes})
    assert store.next_event() is None
    bridge.receive({"team_id": "T1", "event": event})
    bridge.receive(
        {"team_id": "T1", "event": event | {"type": "message", "channel_type": "channel"}}
    )
    assert len(store.rows("SELECT * FROM events")) == 1
    bridge.receive(
        {"team_id": "T1", "event": event | {"type": "message", "text": "unrelated", "ts": "2"}}
    )
    assert len(store.rows("SELECT * FROM events")) == 1
    store.bind("C1", "1", {"id": "w", "active_session_id": "s"}, {})
    bridge.receive(
        {
            "team_id": "T1",
            "event": event | {"type": "message", "user": "U2", "thread_ts": "1", "ts": "3"},
        }
    )
    assert len(store.rows("SELECT * FROM events")) == 1
    conn.close()


def test_channel_followups_require_mention_and_reuse_mapped_session(tmp_path):
    store, conn, db, slack = fixture(tmp_path)
    workspace = {"id": "w", "active_session_id": "s"}
    config = Config()
    options = parse("initial task", "B", config)[1]
    store.bind("C1", "1", workspace, options)
    sent, validated = [], []

    def validate(wid, sid):
        validated.append((wid, sid))
        return workspace

    conductor = SimpleNamespace(
        db=db, validate=validate, send=lambda *args: sent.append(args) or ""
    )
    slack.call = lambda *args, **kwargs: {"permalink": "https://slack.test/message"}
    bridge = Bridge(config, store, conductor, slack, "B", "U1", "T1")
    event = {
        "type": "message",
        "channel_type": "channel",
        "channel": "C1",
        "thread_ts": "1",
        "ts": "2",
        "user": "U1",
        "text": "talking to Lucas",
    }
    bridge.receive({"team_id": "T1", "event": event})
    assert store.next_event() is None
    event["text"] = "<@B> [queue] continue investigating"
    bridge.receive({"team_id": "T1", "event": event})
    bridge.receive({"team_id": "T1", "event": event | {"type": "app_mention"}})
    assert len(store.rows("SELECT * FROM events")) == 1
    bridge.process(store.next_event())
    assert validated == [("w", "s")]
    assert sent[0][:2] == ("w", "s")
    assert sent[0][2].endswith("continue investigating")
    assert sent[0][3]["send_mode"] == "queue"
    assert store.thread("C1", "1")["session_id"] == "s"
    conn.close()


def test_pending_unmentioned_channel_event_is_ignored_but_dm_is_accepted(tmp_path):
    store, conn, db, slack = fixture(tmp_path)
    bridge = Bridge(Config(), store, SimpleNamespace(db=db), slack, "B", "U1", "T1")
    event = {
        "type": "message",
        "channel_type": "channel",
        "channel": "C1",
        "thread_ts": "1",
        "ts": "2",
        "user": "U1",
        "text": "talking to Lucas",
    }
    store.accept("old-event", event)
    bridge.process(store.next_event())
    assert store.rows("SELECT state FROM events")[0]["state"] == "ignored"
    assert not slack.posts and not store.rows("SELECT * FROM statuses")
    bridge.receive({"team_id": "T1", "event": event | {"channel_type": "im", "channel": "D1"}})
    assert store.next_event() is not None
    conn.close()


def test_restart_picks_up_queued_row_updated_below_cursor_and_ignores_ui_turn(tmp_path):
    store, conn, db, slack = fixture(tmp_path)
    message(conn, "user", "slack-prompt", None, sent=None)
    message(conn, "user", "typed-in-conductor", "ui-turn")
    result(conn, "private-ui-output", "ui-turn")
    sid = status(store, "event1", "slack-prompt", cursor=3)
    conn.execute(
        "UPDATE session_messages SET sent_at='now',turn_id='slack-turn' WHERE content='slack-prompt'"
    )
    conn.commit()
    result(conn, "slack-output", "slack-turn")
    restarted = Poller(Store(store.path), db, slack, settle_seconds=0)
    restarted.reconcile()
    assert store.status(sid)["state"] == "complete"
    assert slack.edits[-1] == ("status-1", render("done", "slack-output"))
    count = len(slack.edits)
    restarted.tick()
    assert len(slack.edits) == count
    conn.close()


def test_steering_handoff_only_posts_one_result(tmp_path):
    store, conn, db, slack = fixture(tmp_path)
    first, second = status(store, "e1", "first"), status(store, "e2", "second")
    message(conn, "user", "first", "turn")
    message(conn, "user", "second", "turn")
    result(conn, "done", "turn")
    Poller(store, db, slack, settle_seconds=0).tick()
    assert store.status(first)["state"] == "superseded"
    assert store.status(second)["state"] == "complete"
    assert slack.deletes == ["status-1"]
    assert store.status(second)["status_ts"] == "status-2"
    assert all(ts == "status-2" for ts, _ in slack.edits)
    assert slack.edits[-1] == ("status-2", render("done", "done"))
    conn.close()


def test_separate_queued_turns_both_deliver(tmp_path):
    store, conn, db, slack = fixture(tmp_path)
    status(store, "e1", "first")
    status(store, "e2", "second")
    message(conn, "user", "first", "t1")
    result(conn, "first-result", "t1")
    message(conn, "user", "second", "t2")
    result(conn, "second-result", "t2")
    Poller(store, db, slack, settle_seconds=0).tick()
    assert slack.edits == [
        ("status-1", render("done", "first-result")),
        ("status-2", render("done", "second-result")),
    ]
    assert not slack.deletes
    conn.close()


def test_slack_honors_retry_after_longer_than_a_minute(monkeypatch):
    from slack_sdk.errors import SlackApiError
    from slack_sdk.web.slack_response import SlackResponse

    from slack_conductor.slack_io import SlackIO

    response = SlackResponse(
        client=None,
        http_verb="POST",
        api_url="https://slack.com/api/chat.update",
        req_args={},
        data={"ok": False, "error": "ratelimited"},
        headers={"Retry-After": "90"},
        status_code=429,
    )
    calls, delays = [], []

    def update(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise SlackApiError("rate limited", response)
        return {"ok": True}

    monkeypatch.setattr("slack_conductor.slack_io.time.sleep", delays.append)
    assert SlackIO(SimpleNamespace(chat_update=update)).call("chat_update", text="working")["ok"]
    assert delays == [90]


def test_native_outbox_survives_idle_timeout_then_delivers_once(tmp_path):
    store, conn, db, slack = fixture(tmp_path)
    conn.execute(
        "CREATE TABLE session_messages_outbox(message_id,session_id,delivery_payload,state)"
    )
    conn.execute(
        "INSERT INTO session_messages_outbox VALUES ('queued-id','s',?,'ready')",
        (json.dumps({"message": "retained across reload"}),),
    )
    conn.commit()
    sid = status(store, "outbox", "retained across reload", send_mode="queue")
    store.update(sid, latest="queued")
    now = [100.0]
    poller = Poller(store, db, slack, clock=lambda: now[0], settle_seconds=0)
    poller.tick()
    now[0] += 90
    poller.tick()
    assert store.status(sid)["state"] == "waiting"
    assert not slack.posts
    assert all("idle without a matching reply" not in text for _, text in slack.edits)
    conn.execute("DELETE FROM session_messages_outbox")
    conn.commit()
    message(conn, "user", "retained across reload", "turn-after-reload")
    result(conn, "queue-recovered", "turn-after-reload")
    poller.tick()
    now[0] += 3
    poller.tick()
    assert store.status(sid)["state"] == "complete"
    assert store.status(sid)["result"] == "queue-recovered"
    delivered = len(slack.edits)
    poller.tick()
    assert len(slack.edits) == delivered


def test_pending_outbox_requires_matching_session_prompt_and_live_state(tmp_path):
    _, conn, db, _ = fixture(tmp_path)
    assert not db.pending("s", "target")
    conn.execute(
        "CREATE TABLE session_messages_outbox(message_id,session_id,delivery_payload,state)"
    )
    conn.executemany(
        "INSERT INTO session_messages_outbox VALUES (?,?,?,?)",
        [
            ("other-session", "other", '{"message":"target"}', "ready"),
            ("other-prompt", "s", '{"message":"different"}', "ready"),
            ("failed", "s", '{"message":"target"}', "failed"),
            ("bad-json", "s", "invalid", "ready"),
            ("wrong-type", "s", "[]", "ready"),
        ],
    )
    conn.commit()
    assert not db.pending("s", "target")
    message(conn, "user", "target", None, sent=None)
    assert db.pending("s", "target")
