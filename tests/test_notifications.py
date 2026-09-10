from types import SimpleNamespace

from slack_conductor.notifications import Notifications
from slack_conductor.store import Store


def fixture(tmp_path):
    store = Store(tmp_path / "bridge.sqlite")
    store.accept("dm", {"user": "U1", "channel": "D1", "channel_type": "im"})
    sid = store.add_status("event", "C1", "1", "[slack] header\n\nfix <@U2> bug", {})
    store.update(sid, state="complete", status_ts="2")
    messages, calls = [], []

    def call(method, **kwargs):
        calls.append((method, kwargs))
        if method == "chat_getPermalink":
            return {"permalink": "https://slack.test/reply"}
        if method == "conversations_history":
            return {"messages": messages}
        assert method == "chat_postMessage"
        messages.append(kwargs | {"ts": str(len(messages) + 10)})
        return {"channel": "D1", "ts": messages[-1]["ts"]}

    return store, sid, messages, calls, SimpleNamespace(call=call)


def test_completion_dm_is_fresh_linked_and_not_repeated_after_restart(tmp_path):
    store, sid, messages, _calls, slack = fixture(tmp_path)
    notice = Notifications(store, slack, "U1")
    notice.observe(store.status(sid), "done")
    notice.tick()
    restarted = Notifications(Store(store.path), slack, "U1")
    restarted.observe(store.status(sid), "done")
    restarted.tick()
    assert len(messages) == 1
    message = messages[0]
    assert message["channel"] == "D1" and "thread_ts" not in message
    assert "fix &lt;@U2&gt; bug" in message["text"]
    assert "<https://slack.test/reply|View reply>" in message["text"]
    assert message["blocks"][0]["text"]["type"] == "plain_text"


def test_lost_ack_reconciles_history_without_posting_again(tmp_path):
    store, sid, messages, _calls, slack = fixture(tmp_path)
    call = slack.call

    def lose_ack(method, **kwargs):
        response = call(method, **kwargs)
        if method == "chat_postMessage":
            raise TimeoutError("response lost after Slack accepted message")
        return response

    clock = [100.0]
    notice = Notifications(store, SimpleNamespace(call=lose_ack), "U1", clock=lambda: clock[0])
    notice.observe(store.status(sid), "done")
    notice.tick()
    assert store.rows("SELECT state FROM notifications")[0]["state"] == "sending"
    clock[0] += 60
    Notifications(Store(store.path), slack, "U1", clock=lambda: clock[0]).tick()
    assert len(messages) == 1
    assert store.rows("SELECT state,dm_ts FROM notifications") == [{"state": "sent", "dm_ts": "10"}]


def test_resolved_attention_is_cancelled_and_new_episode_not_suppressed(tmp_path):
    store, sid, messages, _calls, slack = fixture(tmp_path)
    notice = Notifications(store, slack, "U1")
    store.update(sid, state="active")
    notice.observe(store.status(sid), "attention")
    notice.observe(store.status(sid), "working")
    notice.observe(store.status(sid), "attention")
    notice.tick()
    assert not messages
    notice.tick()
    assert len(messages) == 1
    notice.observe(store.status(sid), "attention")
    notice.tick()
    assert len(messages) == 1
    notice.observe(store.status(sid), "working")
    notice.observe(store.status(sid), "attention")
    notice.tick()
    assert len(messages) == 2


def test_superseded_and_historical_results_do_not_notify(tmp_path):
    store, sid, messages, _calls, slack = fixture(tmp_path)
    notice = Notifications(store, slack, "U1")
    notice.tick()  # Existing completed rows aren't a backlog of new notifications.
    assert not messages
    notice.observe(store.status(sid), "done")
    store.update(sid, state="superseded")
    notice.tick()
    assert not messages
