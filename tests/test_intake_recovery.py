import json
import logging
import threading
from types import SimpleNamespace

from slack_sdk.errors import SlackApiError
from slack_sdk.web import SlackResponse

from slack_conductor.config import Config
from slack_conductor.intake_recovery import IntakeRecovery
from slack_conductor.monitoring import report
from slack_conductor.slack_app import Bridge


def setup(bridge_state, pages):
    store, _, db, slack = bridge_state
    config = Config(mode="server", state_dir=store.path.parent)
    bridge = Bridge(config, store, SimpleNamespace(db=db), slack, "B", "U1", "T1")
    store.accept("bootstrap", {"user": "U1", "channel": "D1", "channel_type": "im"})
    store.execute("UPDATE events SET state='done' WHERE key='bootstrap'")
    calls = []

    def history(**kwargs):
        calls.append(kwargs)
        page = pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return page

    client = SimpleNamespace(
        conversations_history=history,
    )
    recovery = IntakeRecovery(
        bridge, "unused", threading.Event(), client=client, clock=lambda: 1000
    )
    return store, bridge, recovery, calls


def message(ts="950.0", **extra):
    return {"type": "message", "user": "U1", "ts": ts, "text": "private task", **extra}


def test_missing_dm_recovered_once_across_pages_socket_race_and_restart(bridge_state, caplog):
    original = message(files=[{"url_private": "https://private.test/image"}])
    pages = [
        {"messages": [original], "response_metadata": {"next_cursor": "page2"}},
        {"messages": [message("940.0")]},
    ]
    store, bridge, recovery, calls = setup(bridge_state, pages)
    with caplog.at_level(logging.INFO):
        assert recovery.tick() == 60
        assert not bridge.receive(
            {
                "team_id": "T1",
                "event": {
                    **original,
                    "channel": "D1",
                    "channel_type": "im",
                },
            }
        )
    rows = store.rows("SELECT * FROM events WHERE key!='bootstrap' ORDER BY rowid")
    assert [r["key"] for r in rows] == ["T1:D1:940.0", "T1:D1:950.0"]
    assert json.loads(rows[1]["payload"])["files"] == original["files"]
    assert calls[1]["cursor"] == "page2" and calls[0]["oldest"] == "700"
    assert "private task" not in caplog.text and "private.test" not in caplog.text
    assert '"source": "history"' in caplog.text
    assert json.loads(recovery.path.read_text())["recovered"] == 2
    # A new worker and overlapping history cannot cause another dispatch.
    _, _, restarted, _ = setup(bridge_state, [{"messages": [original, message("940.0")]}])
    restarted.tick()
    assert len(store.rows("SELECT * FROM events WHERE key!='bootstrap'")) == 2
    assert json.loads(restarted.path.read_text())["recovered"] == 0


def test_recovery_does_not_replay_old_edited_foreign_or_thread_messages(bridge_state):
    pages = [
        {
            "messages": [
                message("600"),
                message("995"),
                message("910", user="OTHER"),
                message("920", edited={"ts": "990"}),
                message("930", thread_ts="800"),
                message("940", subtype="message_deleted"),
                message("950", bot_id="bot"),
            ]
        }
    ]
    store, _, recovery, _ = setup(bridge_state, pages)
    recovery.tick()
    assert store.next_event() is None


def test_history_failure_is_visible_and_next_pass_recovers(bridge_state, monkeypatch, caplog):
    store, bridge, recovery, _ = setup(
        bridge_state,
        [
            TimeoutError("secret url"),
            {"messages": [message()]},
        ],
    )
    monkeypatch.setattr("slack_conductor.monitoring.time.time", lambda: 1000)
    recovery.tick()
    assert report(bridge.config)["checks"]["intake_recovery"]["state"] == "warning"
    assert "secret url" not in caplog.text
    recovery.tick()
    assert report(bridge.config)["checks"]["intake_recovery"]["state"] == "ok"
    assert store.next_event() is not None
    monkeypatch.setattr("slack_conductor.monitoring.time.time", lambda: 1200)
    assert report(bridge.config)["checks"]["intake_recovery"]["state"] == "warning"


def test_rate_limit_delays_next_scan_and_does_not_sleep_delivery_loop(bridge_state):
    response = SlackResponse(
        client=None,
        http_verb="GET",
        api_url="",
        req_args={},
        data={"ok": False, "error": "ratelimited"},
        headers={"Retry-After": "240"},
        status_code=429,
    )
    _, _, recovery, _ = setup(bridge_state, [SlackApiError("rate limited", response)])
    assert recovery.tick() == 240


def test_incomplete_history_does_not_dispatch_and_stop_prevents_enqueue(bridge_state):
    store, _, recovery, _ = setup(bridge_state, [{"messages": [message()], "has_more": True}])
    recovery.tick()
    assert store.next_event() is None
    assert not json.loads(recovery.path.read_text())["ready"]
    recovery.stop.set()
    assert recovery.scan() == 0


def test_worker_stops_and_joins(bridge_state):
    _, _, recovery, _ = setup(bridge_state, [{"messages": []}])
    recovery.start()
    recovery.close()
    assert not recovery.worker.is_alive()


def test_unknown_dm_waits_for_trusted_owner_event(bridge_state):
    store, bridge, recovery, calls = setup(bridge_state, [{"messages": [message()]}])
    store.execute("DELETE FROM events WHERE key='bootstrap'")
    recovery.tick()
    assert not calls
    assert json.loads(recovery.path.read_text())["error_type"] == "OwnerDMUnknown"
    bridge.receive(
        {
            "team_id": "T1",
            "event": {
                **message(),
                "channel": "D1",
                "channel_type": "im",
            },
        }
    )
    recovery.tick()
    assert json.loads(recovery.path.read_text())["ready"]
    assert len(store.rows("SELECT * FROM events")) == 1


def test_recent_threads_are_checked_each_pass_without_starving_old_threads(bridge_state):
    threads = tuple(str(n) for n in range(100, 140))
    _, _, recovery, _ = thread_setup(bridge_state, [], threads=threads)
    calls = []

    def replies(**kwargs):
        calls.append(kwargs["ts"])
        return {"messages": []}

    recovery.client.conversations_replies = replies
    for now in [1000, 1060, 1120]:
        recovery.clock = lambda now=now: now
        assert recovery.scan() == 0
    assert len(calls) == 60
    assert set(calls) == set(threads)
    # The latest thread remains in every batch even with a large backlog.
    assert all("139" in calls[start : start + 20] for start in [0, 20, 40])


def thread_setup(bridge_state, pages, threads=("100",)):
    store, bridge, recovery, _ = setup(bridge_state, [{"messages": []}] * 20)
    for thread in threads:
        store.add_status("root:" + thread, "D1", thread, "root", {})
    calls = []

    def replies(**kwargs):
        calls.append(kwargs)
        page = pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return page

    recovery.client.conversations_replies = replies
    return store, bridge, recovery, calls


def test_old_thread_reply_recovered_with_attachments_and_socket_deduplication(bridge_state):
    original = message("950", thread_ts="100", files=[{"id": "F1"}])
    store, bridge, recovery, calls = thread_setup(
        bridge_state,
        [
            {"messages": [message("100"), original], "response_metadata": {"next_cursor": "next"}},
            {"messages": [message("940", thread_ts="100")]},
            {"messages": [original]},
        ],
    )
    assert recovery.scan() == 2
    assert calls[0]["ts"] == "100" and calls[1]["cursor"] == "next"
    rows = store.rows("SELECT payload FROM events WHERE key='T1:D1:950'")
    assert json.loads(rows[0]["payload"])["files"] == [{"id": "F1"}]
    assert not bridge.receive(
        {
            "team_id": "T1",
            "event": {
                **original,
                "channel": "D1",
                "channel_type": "im",
            },
        }
    )
    recovery.clock = lambda: 1020
    assert recovery.scan() == 0


def test_thread_rotation_checkpoints_survive_restart_and_long_outage(bridge_state):
    store, _, recovery, calls = thread_setup(
        bridge_state,
        [
            {"messages": []},
            {"messages": [message("950", thread_ts="200")]},
        ],
        threads=("100", "200"),
    )
    recovery.THREADS_PER_SCAN = 1
    # Force a deterministic oldest-first order.
    recovery.checkpoints = {"100": 700, "200": 701}
    assert recovery.scan() == 0
    recovery.checkpoints = None  # Reload persisted state as a new daemon would.
    recovery.clock = lambda: 2000
    assert recovery.scan() == 1
    assert calls[-1]["ts"] == "200"
    assert store.rows("SELECT key FROM events WHERE key='T1:D1:950'")


def test_partial_thread_page_failure_retries_without_skipping_messages(bridge_state):
    original = message("950", thread_ts="100")
    store, _, recovery, _ = thread_setup(
        bridge_state,
        [
            {"messages": [original], "has_more": True},
            {"messages": [original]},
        ],
    )
    recovery.tick()
    assert recovery.checkpoints["100"] == 700
    assert not store.rows("SELECT key FROM events WHERE key='T1:D1:950'")
    recovery.clock = lambda: 2000
    recovery.tick()
    assert store.rows("SELECT key FROM events WHERE key='T1:D1:950'")
    assert recovery.checkpoints["100"] == 1990


def test_thread_recovery_filters_untrusted_edited_parent_and_wrong_thread(bridge_state):
    store, _, recovery, _ = thread_setup(
        bridge_state,
        [
            {
                "messages": [
                    message("900"),
                    message("910", thread_ts="100", user="OTHER"),
                    message("920", thread_ts="100", edited={"ts": "999"}),
                    message("930", thread_ts="different"),
                    message("940", thread_ts="100", bot_id="B"),
                    message("995", thread_ts="100"),
                    message("600", thread_ts="100"),
                ]
            }
        ],
    )
    assert recovery.scan() == 0
    assert store.next_event() is None


def test_failed_thread_does_not_block_other_threads(bridge_state):
    store, _, recovery, _ = thread_setup(
        bridge_state,
        [
            TimeoutError(),
            {"messages": [message("950", thread_ts="200")]},
        ],
        threads=("100", "200"),
    )
    recovery.checkpoints = {"100": 700, "200": 701}
    recovery.tick()
    assert recovery.checkpoints == {"100": 700, "200": 990}
    assert store.rows("SELECT key FROM events WHERE key='T1:D1:950'")
    assert not json.loads(recovery.path.read_text())["ready"]


def test_reply_exactly_on_previous_scan_boundary_is_not_lost(bridge_state):
    store, _, recovery, calls = thread_setup(
        bridge_state,
        [
            {"messages": []},
            {"messages": [message("990", thread_ts="100")]},
        ],
    )
    assert recovery.scan() == 0
    recovery.clock = lambda: 1060
    assert recovery.scan() == 1
    assert float(calls[-1]["oldest"]) < 990
    assert store.rows("SELECT key FROM events WHERE key='T1:D1:990'")
