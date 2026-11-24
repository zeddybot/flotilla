import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from types import SimpleNamespace

import pytest
from slack_sdk.errors import SlackApiError

from slack_conductor.conductor.journal import Unconfirmed
from slack_conductor.config import Config
from slack_conductor.headless.runtime import HeadlessRuntime
from slack_conductor.pairing import PairingStore
from slack_conductor.protocol import parse
from slack_conductor.remote_journal import Receipts
from slack_conductor.slack_app import Bridge
from slack_conductor.slack_routing import Routing
from slack_conductor.store import Store
from tests.support import Slack


def endpoint(config):
    sends, mutations = [], []
    return SimpleNamespace(
        config=config,
        backend=config.runtime_adapter,
        sent=sends,
        mutations=mutations,
        db=SimpleNamespace(cursor=lambda sid: 0, pickup=lambda sid, prompt: None),
        create_session=lambda key, options: {"id": "w", "active_session_id": "s"},
        validate=lambda wid, sid: {"id": wid, "active_session_id": sid},
        send=lambda *args: sends.append(args) or "",
        archive=lambda *args, **kwargs: mutations.append(("archive", args, kwargs)),
        probe=lambda: {
            "capabilities": dict.fromkeys(
                ("create", "queue", "steer", "interrupt", "close_session", "respond"), True
            )
        },
        rpc=lambda *args, **kwargs: mutations.append((args, kwargs)),
    )


def setup(path):
    config = Config(state_dir=path, pairing_allowed_users=["U1", "U2"])
    registry = PairingStore(path / "pairing.sqlite")
    legacy = endpoint(config)
    legacy.pairing, legacy.client_id = registry, config.client_id
    endpoints = {}
    legacy.for_client = endpoints.__getitem__
    store = Store(path / "bridge.sqlite")
    slack = Slack()
    slack.call = lambda *args, **kwargs: {"permalink": "https://slack.test/message"}
    bridge = Bridge(config, store, legacy, slack, "B", "U1", "T1", routed=True)
    return bridge, endpoints


def register(bridge, endpoints, user, code, name="daytona", backend="conductor"):
    digest = hashlib.sha256(code.encode()).hexdigest()
    registry = bridge.conductor.pairing
    registry.register(code, digest, name, backend)
    profile = registry.claim(code, "T1", user)
    state_dir = bridge.config.state_dir / "clients" / profile.id
    state_dir.mkdir(parents=True)
    endpoints[profile.id] = endpoint(
        replace(bridge.config, state_dir=state_dir, runtime_adapter=backend)
    )
    return profile.id


def event(user, ts, text, *, thread=None, direct=False):
    message = {
        "user": user,
        "ts": ts,
        "channel": "D1" if direct else "C1",
        "channel_type": "im" if direct else "channel",
        "type": "message" if direct else "app_mention",
        "text": text,
    }
    if thread:
        message["thread_ts"] = thread
    return {"team_id": "T1", "event": message}


def connector_dm(ts, text, *, formatted=False):
    attribution = "*Sent using* <@UAPP>"
    visible = f"`{text}`" if formatted else text
    body = event("U2", ts, f"{visible} {attribution}", direct=True)
    element = {"type": "text", "text": text}
    if formatted:
        element["style"] = {"code": True}
    body["event"]["blocks"] = [
        {
            "type": "rich_text",
            "block_id": "body",
            "elements": [{"type": "rich_text_section", "elements": [element]}],
        },
        {
            "type": "context",
            "block_id": "attribution",
            "elements": [{"type": "mrkdwn", "text": attribution, "verbatim": False}],
        },
    ]
    return body


@pytest.mark.parametrize("message_shape", ["connector", "code", "plain"])
def test_direct_profile_commands_with_connector_attribution(tmp_path, message_shape):
    bridge, _ = setup(tmp_path)

    def for_client(client_id):
        state_dir = tmp_path / "clients" / client_id
        state_dir.mkdir(parents=True)
        return endpoint(replace(bridge.config, state_dir=state_dir))

    bridge.conductor.for_client = for_client
    pairs = bridge.conductor.pairing
    code = "a" * 32
    pairs.register(code, hashlib.sha256(code.encode()).hexdigest(), "daytona", "conductor")
    router = Routing(bridge)

    def command(ts, text):
        body = (
            event("U2", ts, text, direct=True)
            if message_shape == "plain"
            else connector_dm(ts, text, formatted=message_shape == "code")
        )
        assert not router.receive(body)
        assert bridge.store.next_event() is None
        return bridge.slack.posts[-1][2]

    assert command("1", f"connect {code}").startswith("Connected `daytona`")
    assert "`daytona` (conductor)" in command("2", "profiles")
    assert command("3", "default daytona") == "`daytona` is your default for new threads."
    assert command("4", "disconnect daytona") == "Disconnected `daytona`."
    assert pairs.profiles("T1", "U2") == []


@pytest.mark.parametrize(
    ("workspace_enabled", "user", "member_fields", "allowed", "lookup"),
    [
        (True, "U3", {}, True, True),
        (False, "U3", {}, False, False),
        (True, "U3", {"team_id": "T2"}, False, True),
        (True, "U3", {"is_stranger": True}, False, True),
        (True, "U3", {"deleted": True}, False, True),
        (True, "U3", {"suspended": True}, False, True),
        (True, "U3", {"is_bot": True}, False, True),
        (True, "U3", {"is_workflow_bot": True}, False, True),
        (True, "W3", {"id": "U3", "enterprise_user": {"id": "W3", "teams": ["T1"]}}, True, True),
        (True, "U3", {"enterprise_user": {"id": "W3", "teams": ["T2"]}}, False, True),
        (True, "U3", {"id": "U4"}, False, True),
        (True, "U2", {"team_id": "T2"}, True, False),
        (False, "U1", {}, True, False),
    ],
)
def test_workspace_pairing_checks_active_membership(
    tmp_path, workspace_enabled, user, member_fields, allowed, lookup
):
    bridge, _ = setup(tmp_path)
    bridge.config.pairing_allow_workspace_members = workspace_enabled
    pairs = bridge.conductor.pairing
    code = "a" * 32
    pairs.register(code, hashlib.sha256(code.encode()).hexdigest(), "daytona", "conductor")
    lookups = []

    def users_info(method, **arguments):
        lookups.append((method, arguments))
        return {
            "ok": True,
            "user": {"id": user, "team_id": "T1", "is_bot": False, "deleted": False}
            | member_fields,
        }

    def for_client(client_id):
        state_dir = tmp_path / "clients" / client_id
        state_dir.mkdir(parents=True)
        return endpoint(replace(bridge.config, state_dir=state_dir))

    bridge.slack.call = users_info
    bridge.conductor.for_client = for_client
    assert not Routing(bridge).receive(event(user, "1", f"connect {code}", direct=True))
    assert lookups == ([("users_info", {"user": user})] if lookup else [])
    assert bool(pairs.profiles("T1", user)) is allowed
    assert bridge.store.next_event() is None
    assert code not in bridge.slack.posts[-1][2]


def test_workspace_lookup_failure_keeps_code_available_for_retry(tmp_path):
    bridge, _ = setup(tmp_path)
    bridge.config.pairing_allow_workspace_members = True
    pairs = bridge.conductor.pairing
    code = "a" * 32
    pairs.register(code, hashlib.sha256(code.encode()).hexdigest(), "daytona", "conductor")

    def users_info(*args, **kwargs):
        raise SlackApiError("Slack API failed", {"error": "missing_scope"})

    bridge.slack.call = users_info
    router = Routing(bridge)
    command = f"connect {code}"
    assert not router.receive(event("U3", "1", command, direct=True))
    assert pairs.profiles("T1", "U3") == []
    assert bridge.slack.posts[-1][2] == "Slack membership could not be verified. Try pairing again."
    assert bridge.store.next_event() is None
    assert pairs.claim(code, "T1", "U3").name == "daytona"


@pytest.mark.parametrize("invalid", ["team", "forged", "public", "history", "code"])
def test_workspace_pairing_checks_command_before_member_lookup(tmp_path, invalid):
    bridge, _ = setup(tmp_path)
    bridge.config.pairing_allow_workspace_members = True
    lookups = []
    bridge.slack.call = lambda *args, **kwargs: lookups.append((args, kwargs))
    command = f"connect {'a' * 32}"
    body = event("U3", "1", command, direct=True)
    if invalid == "team":
        body["team_id"] = "T2"
    elif invalid == "forged":
        body = connector_dm("1", command)
        body["event"]["user"] = "U3"
        body["event"]["text"] += " forged"
    elif invalid == "public":
        body = event("U3", "1", f"<@B> {command}")
    elif invalid == "code":
        body["event"]["text"] = "connect invalid"

    assert not Routing(bridge).receive(body, source="history" if invalid == "history" else "socket")
    assert lookups == []
    assert bridge.store.next_event() is None


@pytest.mark.parametrize(
    "source, invalid",
    [
        ("socket", "extra_prose"),
        ("socket", "fallback_prose"),
        ("socket", "code_fallback_prose"),
        ("socket", "quoted"),
        ("socket", "forwarded"),
        ("socket", "public"),
        ("socket", "public_connector"),
        ("socket", "public_code_connector"),
        ("history", None),
    ],
)
def test_pairing_rejects_noncommands_and_nonlive_direct_messages(tmp_path, source, invalid):
    bridge, _ = setup(tmp_path)
    pairs = bridge.conductor.pairing
    code = "a" * 32
    pairs.register(code, hashlib.sha256(code.encode()).hexdigest(), "daytona", "conductor")
    text = f"connect {code}"
    body = connector_dm(
        "1",
        text + " extra prose" if invalid == "extra_prose" else text,
        formatted=invalid in ("code_fallback_prose", "public_code_connector"),
    )
    message = body["event"]
    if invalid in ("fallback_prose", "code_fallback_prose"):
        message["user"] = "U1"
        message["text"] += " extra prose"
    elif invalid == "quoted":
        message["blocks"][0]["elements"][0]["type"] = "rich_text_quote"
        message["text"] = text
    elif invalid == "forwarded":
        message["attachments"] = [{"is_msg_unfurl": True}]
        message["text"] = text
    elif invalid in ("public", "public_connector", "public_code_connector"):
        body = event("U1", "1", f"<@B> {text if invalid == 'public' else message['text']}")

    assert not Routing(bridge).receive(body, source=source)
    assert pairs.profiles("T1", "U2") == []
    assert pairs.profiles("T1", "U1") == []
    assert bridge.store.next_event() is None
    assert all(code not in post[2] for post in bridge.slack.posts)


def test_atomic_first_summoner_routes_to_one_client_and_survives_restart(tmp_path):
    bridge, endpoints = setup(tmp_path)
    one = register(bridge, endpoints, "U1", "1" * 32)
    two = register(bridge, endpoints, "U2", "2" * 32)
    router = Routing(bridge)
    barrier = Barrier(2)

    def summon(user, ts):
        barrier.wait()
        return router.receive(event(user, ts, "<@B> [profile:daytona] first", thread="1"))

    with ThreadPoolExecutor(2) as pool:
        tasks = [pool.submit(summon, user, ts) for user, ts in [("U1", "2"), ("U2", "3")]]
        assert sorted(task.result() for task in tasks) == [False, True]

    route = bridge.store.route("T1", "C1", "1")
    winner = route["client_id"]
    assert winner in {one, two}
    loser = two if winner == one else one
    reboot = Routing(bridge)
    reboot.restore()
    target = reboot.to_bridge(winner)
    assert reboot.to_bridge(loser).store.next_event() is None
    target.process(target.store.next_event())
    guest = "U2" if route["owner_id"] == "U1" else "U1"
    assert reboot.receive(event(guest, "4", "<@B> [queue] follow up later", thread="1"))
    target.process(target.store.next_event())
    assert len(endpoints[winner].sent) == 2
    assert f"sender=<@{guest}>" in endpoints[winner].sent[-1][2]
    assert endpoints[winner].sent[-1][3]["send_mode"] == "queue"
    assert reboot.receive(event(guest, "5", "<@B> change course now", thread="1"))
    target.process(target.store.next_event())
    assert endpoints[winner].sent[-1][3]["send_mode"] == "steer"

    for ts, control in enumerate(["stop", "close", "questions", "deny:id", "new", "verbose"], 6):
        assert reboot.receive(event(guest, str(ts), f"<@B> [{control}] text", thread="1"))
        target.process(target.store.next_event())
    assert not endpoints[winner].mutations
    assert len(endpoints[winner].sent) == 3
    assert not endpoints[loser].sent
    assert all(row[0] == guest for row in bridge.slack.posts[-6:])


@pytest.mark.parametrize("legacy", [False, True])
def test_close_uses_saved_event_identity_across_retry(tmp_path, legacy):
    bridge, _ = setup(tmp_path)
    store = bridge.store
    store.bind("C1", "1", {"id": "w", "active_session_id": "s"}, {})
    attempts = []

    def archive(wid, sid, *, command_id=None):
        assert (wid, sid) == ("w", "s")
        attempts.append(command_id)
        if len(attempts) == 1:
            raise Unconfirmed("conductor returned no acknowledgement")
        if len(attempts) == 2:
            raise RuntimeError("safe archive failed")

    bridge.conductor.archive = archive
    assert bridge.receive(event("U1", "2", "<@B> [close]", thread="1"))
    envelope = store.next_event()
    if legacy:
        store.add_status(envelope["key"], "C1", "1", "", {})
    bridge.process(envelope)
    bridge.process(envelope)
    assert bridge.receive(event("U1", "3", "<@B> [close]", thread="1"))
    bridge.process(store.next_event())
    original_key = None if legacy else "T1:C1:2"
    assert attempts == [original_key, original_key, "T1:C1:3"]
    assert store.thread("C1", "1")["state"] == "archived"


def test_replayed_close_does_not_archive_rebound_thread(tmp_path):
    bridge, _ = setup(tmp_path)
    store = bridge.store
    store.bind("C1", "1", {"id": "w", "active_session_id": "s"}, {})
    attempts = []

    def archive(wid, sid, *, command_id=None):
        attempts.append((wid, sid, command_id))
        raise Unconfirmed("conductor returned no acknowledgement")

    bridge.conductor.archive = archive
    assert bridge.receive(event("U1", "2", "<@B> [close]", thread="1"))
    envelope = store.next_event()
    bridge.process(envelope)
    store.bind("C1", "1", {"id": "w2", "active_session_id": "s2"}, {})
    bridge.process(envelope)

    assert attempts == [("w", "s", "T1:C1:2")]
    thread = store.thread("C1", "1")
    assert (thread["workspace_id"], thread["session_id"], thread["state"]) == (
        "w2",
        "s2",
        "running",
    )


def test_legacy_mac_is_default_and_existing_queue_stays_after_switch(tmp_path):
    bridge, endpoints = setup(tmp_path)
    old = event("U1", "1", "<@B> earlier")
    bridge.store.accept("T1:C1:1", old["event"])
    profile = register(bridge, endpoints, "U1", "1" * 32)
    router = Routing(bridge)
    assert router.resolve("U1", None) == router.legacy_id
    router.receive(event("U1", "2", "profiles", direct=True))
    assert "`mac` (conductor) · default" in bridge.slack.posts[-1][2]
    router.receive(event("U1", "3", "default daytona", direct=True))
    reboot = Routing(bridge)
    assert reboot.resolve("U1", None) == profile
    assert reboot.receive(event("U1", "4", "<@B> continue", thread="1"))
    assert reboot.receive(event("U1", "5", "<@B> begin"))
    assert reboot.receive(event("U1", "6", "<@B> [profile:mac] check"))
    assert [bridge.store.route("T1", "C1", ts)["client_id"] for ts in ("1", "5", "6")] == [
        router.legacy_id,
        profile,
        router.legacy_id,
    ]
    assert bridge.store.next_event()["key"] == "T1:C1:1"
    router.receive(event("U1", "7", "default mac", direct=True))
    assert reboot.resolve("U1", None) == router.legacy_id


def test_dm_sessions_lists_owned_current_sessions_across_profiles(tmp_path, monkeypatch):
    bridge, endpoints = setup(tmp_path)
    daytona = register(bridge, endpoints, "U1", "1" * 32)
    register(bridge, endpoints, "U2", "2" * 32, name="private")

    def permalink(channel, ts):
        return f"https://workspace.slack.com/archives/{channel}/p{ts.replace('.', '')}"

    def slack_call(method, *, channel, message_ts):
        assert method == "chat_getPermalink"
        return {"permalink": permalink(channel, message_ts)}

    bridge.slack.call = slack_call

    def create(key, options):
        ts = key.rsplit(":", 1)[-1]
        return {"id": f"w-{ts}", "active_session_id": f"s-{ts}"}

    for runtime in [bridge.conductor, *endpoints.values()]:
        runtime.create_session = create

    # This prompt predates the team-scoped routing table.
    legacy = event("U1", "1.000001", "<@B> Legacy prompt")
    assert bridge.receive(legacy)
    legacy_envelope = bridge.store.next_event()
    bridge.process(legacy_envelope)
    bridge.store.thread_state(bridge.store.thread("C1", "1.000001"), "idle")
    bridge.store.execute(
        "UPDATE statuses SET state='complete' WHERE event_key=?", (legacy_envelope["key"],)
    )

    router = Routing(bridge)

    def process(user, ts, text, *, thread=None):
        assert router.receive(event(user, ts, f"<@B> {text}", thread=thread))
        route = bridge.store.route("T1", "C1", thread or ts)
        client = router.to_bridge(route["client_id"])
        client.process(client.store.next_event())

    process("U1", "2.000001", "[profile:daytona] Previous generation")
    process("U1", "2.000002", "[new] [opus] First current prompt & <unsafe>", thread="2.000001")
    process("U1", "2.000003", "Active follow-up", thread="2.000001")
    process("U1", "2.000004", "Queued follow-up", thread="2.000001")
    active_store = router.to_bridge(daytona).store
    active_store.execute("UPDATE statuses SET state='complete' WHERE event_key='T1:C1:2.000002'")
    active_store.execute("UPDATE statuses SET state='active' WHERE event_key='T1:C1:2.000003'")
    process("U1", "3.000001", "[profile:daytona] Closed prompt")
    process("U1", "3.000002", "[close]", thread="3.000001")
    process("U2", "4.000001", "Someone else's prompt")
    process("U1", "4.000002", "Collaborator follow-up", thread="4.000001")

    def fail_runtime(*args, **kwargs):
        pytest.fail("Session listing called the runtime")

    # Simulate a disconnected runtime with bridge state still on the server.
    for runtime in [bridge.conductor, *endpoints.values()]:
        for method in ("probe", "create_session", "validate", "send", "rpc", "archive"):
            monkeypatch.setattr(runtime, method, fail_runtime)
    bridge.slack.posts.clear()
    assert not router.receive(event("U1", "5.000001", "sessions", direct=True))

    ((destination, thread, text, _),) = bridge.slack.posts
    assert (destination, thread) == ("U1", None)
    assert text.count(permalink("C1", "2.000001")) == 1
    assert text.count(permalink("C1", "1.000001")) == 1
    assert bridge.config.model in text and bridge.config.models["opus"] in text
    assert "daytona" in text and "mac" in text
    assert "First current prompt &amp; &lt;unsafe&gt;" in text
    assert "Legacy prompt" in text
    assert "idle" in text.lower() and "running" in text.lower()
    assert "offline" in text.lower()
    for hidden in (
        "Previous generation",
        "Active follow-up",
        "Queued follow-up",
        "Closed prompt",
        "Someone else's prompt",
        "Collaborator follow-up",
        permalink("C1", "4.000001"),
    ):
        assert hidden not in text
    assert bridge.store.route("T1", "D1", "5.000001") is None
    assert all(client.store.next_event() is None for client in router.bridges.values())


def test_dm_sessions_empty_inbox_does_not_create_a_route(tmp_path):
    bridge, _ = setup(tmp_path)
    router = Routing(bridge)
    assert not router.receive(event("U1", "1.000000", "sessions", direct=True), source="history")
    assert bridge.slack.posts == []
    assert bridge.store.route("T1", "D1", "1.000000") is None
    assert not router.receive(event("U2", "1.000001", "sessions", direct=True))

    ((destination, thread, text, _),) = bridge.slack.posts
    assert (destination, thread) == ("U2", None)
    assert text == "You have no active Slack sessions."
    assert bridge.store.route("T1", "D1", "1.000001") is None
    assert bridge.store.next_event() is None


def test_dm_sessions_uses_saved_headless_default_model_without_runtime(tmp_path, monkeypatch):
    bridge, endpoints = setup(tmp_path)
    bridge.slack.call = lambda *args, **kwargs: {
        "permalink": "https://workspace.slack.com/archives/C1/p1000001"
    }
    client_id = register(bridge, endpoints, "U2", "1" * 32, backend="headless")
    peer = endpoints[client_id]
    peer.receipts = Receipts(peer.config.state_dir / "remote.sqlite")

    def create(key, options):
        assert options["model"] == ""
        command = f"runtime:create:{key}"
        peer.receipts.begin(command, "runtime.create", {"command_id": key, "spec": options})
        peer.receipts.finish(
            command,
            {"value": {"workspace_id": "w", "session_id": "s", "spec": {"model": "sonnet"}}},
        )
        return {"id": "w", "active_session_id": "s"}

    peer.create_session = create
    router = Routing(bridge)
    assert router.receive(event("U2", "1.000001", "<@B> Headless prompt"))
    target = router.to_bridge(client_id)
    target.process(target.store.next_event())
    assert peer.sent

    def fail_runtime(*args, **kwargs):
        pytest.fail("Headless sessions should use saved state while the client is offline")

    monkeypatch.setattr(peer, "rpc", fail_runtime)
    monkeypatch.setattr(peer, "probe", fail_runtime)
    bridge.slack.posts.clear()
    assert not router.receive(event("U2", "1.000002", "sessions", direct=True))
    ((destination, thread, text, _),) = bridge.slack.posts
    assert (destination, thread) == ("U2", None)
    assert "sonnet" in text and "Headless prompt" in text


@pytest.mark.parametrize("adapter", ["conductor", "headless"])
def test_dm_sessions_filters_and_updates_bindings_with_online_inventory(tmp_path, adapter):
    bridge, endpoints = setup(tmp_path)
    client_id = register(bridge, endpoints, "U2", "1" * 32, backend=adapter)
    router = Routing(bridge)
    client = router.to_bridge(client_id)
    for index, prompt in enumerate(
        ["Current thread", "Archived externally", "Wrong workspace", "Wrong session", "Missing"],
        1,
    ):
        ts = f"1.{index:06}"
        bridge.store.execute(
            "INSERT INTO slack_routes VALUES (?,?,?,?,?)", ("T1", "C1", ts, "U2", client_id)
        )
        client.store.bind(
            "C1", ts, {"id": f"w{index}", "active_session_id": f"s{index}"}, {"model": "Saved"}
        )
        status = client.store.add_status(f"T1:C1:{ts}", "C1", ts, prompt, {})
        client.store.update(
            status, workspace_id=f"w{index}", session_id=f"s{index}", state="active"
        )

    def summary(sid, wid, state):
        configuration = (
            {"model": "Live model"}
            if adapter == "conductor"
            else {"requested": {"model": "Requested"}, "effective": {"model": "Live model"}}
        )
        return {
            "adapter": adapter,
            "session_id": sid,
            "workspace_id": wid,
            "state": state,
            "activity": "idle",
            "configuration": configuration,
        }

    open_state = "ready" if adapter == "conductor" else "active"
    endpoints[client_id].session_inventory = lambda: [
        summary("s1", "w1", open_state),
        summary("s2", "w2", "archived"),
        summary("s3", "other-workspace", open_state),
        summary("other-session", "w4", open_state),
    ]
    assert not router.receive(event("U2", "2.000001", "sessions", direct=True))

    ((destination, thread, text, _),) = bridge.slack.posts
    assert (destination, thread) == ("U2", None)
    assert "Current thread" in text and "Live model" in text and "idle" in text.lower()
    for absent in ("Archived externally", "Wrong workspace", "Wrong session", "Missing", "Saved"):
        assert absent not in text
    assert "running" not in text.lower() and "offline" not in text.lower()


def test_dm_sessions_paginates_without_dropping_threads(tmp_path):
    bridge, _ = setup(tmp_path)
    router = Routing(bridge)
    thread_urls = []
    for index in range(36):
        ts, session = f"1.{index:06}", f"s-{index}"
        channel = f"C{index}"
        url = f"https://workspace.slack.com/archives/{channel}/p{ts.replace('.', '')}"
        thread_urls.append(url)
        bridge.store.execute(
            "INSERT INTO slack_routes VALUES (?,?,?,?,?)",
            ("T1", channel, ts, "U1", router.legacy_id),
        )
        bridge.store.bind(channel, ts, {"id": "w", "active_session_id": session}, {"model": "Opus"})
        status_id = bridge.store.add_status(
            f"T1:{channel}:{ts}", channel, ts, "Start this project " * 40, {}
        )
        bridge.store.update(
            status_id, workspace_id="w", session_id=session, state="waiting", latest="queued"
        )

    def slack_call(method, *, channel, message_ts):
        assert method == "chat_getPermalink"
        return {
            "permalink": f"https://workspace.slack.com/archives/{channel}/p{message_ts.replace('.', '')}"
        }

    bridge.slack.call = slack_call
    assert not router.receive(event("U1", "2.000001", "sessions", direct=True))
    posts = bridge.slack.posts
    assert len(posts) > 1
    assert len({key for _, _, _, key in posts}) == len(posts)
    for destination, thread, text, _ in posts:
        assert (destination, thread) == ("U1", None)
        assert len(text) <= 3500
        assert "queued" in text.lower()
    for url in thread_urls:
        assert sum(url in text for _, _, text, _ in posts) == 1


def test_unregistered_user_gets_private_guide_without_thread_claim(tmp_path):
    bridge, _ = setup(tmp_path)
    router = Routing(bridge)
    assert not router.receive(event("U2", "1", "<@B> private work"))
    assert bridge.store.route("T1", "C1", "1") is None
    assert bridge.store.next_event() is None
    ((destination, thread, text, _),) = bridge.slack.posts
    assert (destination, thread) == ("U2", None)
    assert "docs/connect-client.md" in text
    assert "private work" not in text


@pytest.mark.parametrize(
    ("default_mode", "selector", "expected_mode"),
    [("queue", "", "steer"), ("steer", "", "steer"), ("steer", "[queue]", "queue")],
)
def test_first_headless_prompt_uses_client_settings(
    tmp_path, monkeypatch, default_mode, selector, expected_mode
):
    bridge, endpoints = setup(tmp_path)
    client_id = register(bridge, endpoints, "U2", "1" * 32, backend="headless")
    peer = endpoints[client_id]
    runtime = HeadlessRuntime(
        replace(
            peer.config,
            workspace_root=tmp_path,
            claude_binary="true",
            engine_model="sonnet",
            send_mode=default_mode,
        )
    )
    monkeypatch.setattr(
        "slack_conductor.headless.runtime.subprocess.run",
        lambda argv, **kwargs: None,
    )

    def create(key, options):
        value = runtime.create(key, options)
        return {"id": value["workspace_id"], "active_session_id": value["session_id"]}

    def send(wid, sid, prompt, options):
        runtime.submit("submit", sid, "message", "run", prompt, options)
        peer.sent.append(options)
        return ""

    peer.probe = runtime.probe
    peer.create_session = create
    peer.send = send
    router = Routing(bridge)
    assert router.receive(event("U2", "1", f"<@B> [profile:daytona] {selector} first prompt"))
    target = router.to_bridge(client_id)
    target.process(target.store.next_event())
    assert runtime.sessions()[0]["configuration"]["requested"]["model"] == "sonnet"
    assert peer.sent == [{"send_mode": expected_mode, "verbose": False}]
    assert "send_mode" not in json.loads(target.store.thread("C1", "1")["options"])


@pytest.mark.parametrize(
    ("selectors", "engine", "model"),
    [
        ("[sol][codex]", "codex", "gpt-6.2-sol"),
        ("[astra]", "codex", "gpt-6.2-astra"),
        ("[claude][opus]", "claude", "claude-opus-5-5[1m]"),
        ("[sonnet]", "claude", "claude-sonnet-5-5[1m]"),
        ("[haiku]", "claude", "claude-haiku-5-5[1m]"),
        ("[fable]", "claude", "claude-fable-5-2"),
        ("[luna]", "codex", "gpt-6-luna"),
        ("[terra]", "codex", "gpt-6.1-terra"),
        ("[model:claude-sonnet-4-6]", "claude", "claude-sonnet-4-6"),
    ],
)
def test_headless_model_prefixes_use_native_values(selectors, engine, model):
    text, options, command = parse(
        f"[new]{selectors}[high] investigate", "B", Config(runtime_adapter="headless")
    )
    assert (text, command) == ("investigate", "new")
    assert (options["engine"], options["model"], options["effort"]) == (engine, model, "high")


def test_headless_conflicting_prefixes_are_rejected():
    config = Config(runtime_adapter="headless")
    with pytest.raises(ValueError, match="one engine"):
        parse("[codex][opus] investigate", "B", config)
    with pytest.raises(ValueError, match="one model"):
        parse("[sol][astra] investigate", "B", config)


def test_headless_steering_uses_the_bound_sessions_capabilities(tmp_path):
    bridge, endpoints = setup(tmp_path)
    client_id = register(bridge, endpoints, "U2", "1" * 32, backend="headless")
    peer = endpoints[client_id]
    probe = peer.probe()
    peer.probe = lambda: {
        **probe,
        "adapter": "headless",
        "configuration": {
            "engines": ["claude", "codex"],
            "default_engine": "claude",
            "engine_capabilities": {
                "claude": {"queue": True, "steer": False},
                "codex": {"queue": True, "steer": True},
            },
        },
    }
    created = []
    descriptions = {
        "claude": {"capabilities": {"queue": True, "steer": False}},
        "codex": {"capabilities": {"queue": True, "steer": True}},
    }

    def create(key, options):
        created.append(options)
        engine = options["engine"] or "claude"
        return {"id": engine, "active_session_id": engine}

    peer.create_session = create
    peer.rpc = lambda method, args: descriptions[args["session_id"]]
    router = Routing(bridge)
    target = router.to_bridge(client_id)

    def process(ts, prompt):
        assert router.receive(event("U2", ts, f"<@B> {prompt}", thread="1" if ts != "1" else None))
        target.process(target.store.next_event())

    process("1", "initial prompt")
    process("2", "[steer] unsupported for this session")
    process("3", "[sol] use a different model without [new]")
    assert len(created) == len(peer.sent) == 1

    process("4", "[new][sol][xhigh][queue] start a Codex session")
    process("5", "steer by default in the new session")
    assert [item["engine"] for item in created] == ["", "codex"]
    assert created[1]["model"] == "gpt-6.2-sol"
    assert created[1]["effort"] == "xhigh"
    assert [send[-1]["send_mode"] for send in peer.sent] == ["queue", "queue", "steer"]
    assert peer.sent[-1][1] == "codex"


def test_old_headless_worker_accepts_queue_and_rejects_new_session_controls(tmp_path):
    bridge, endpoints = setup(tmp_path)
    client_id = register(bridge, endpoints, "U2", "1" * 32, backend="headless")
    peer = endpoints[client_id]
    peer.probe = lambda: {
        "adapter": "headless",
        "capabilities": {"create": True, "queue": True, "steer": False},
        "configuration": {"engines": ["claude", "codex"]},
    }
    peer.rpc = lambda method, args: {"engine": "claude"}
    created = []
    peer.create_session = lambda key, options: (
        created.append(options)
        or {
            "id": "w",
            "active_session_id": "s",
        }
    )
    router = Routing(bridge)
    target = router.to_bridge(client_id)

    def process(ts, prompt, thread=None):
        assert router.receive(event("U2", ts, f"<@B> {prompt}", thread=thread))
        target.process(target.store.next_event())

    process("1", "use the worker defaults")
    process("2", "ordinary follow-up", thread="1")
    process("3", "[steer] redirect", thread="1")
    process("4", "[new][codex][high] choose a new engine", thread="1")
    assert len(created) == 1
    assert [send[-1]["send_mode"] for send in peer.sent] == ["queue", "queue"]


def test_owner_approval_reuses_validated_response_after_unconfirmed_send(tmp_path):
    bridge, endpoints = setup(tmp_path)
    client_id = register(bridge, endpoints, "U2", "1" * 32)
    peer = endpoints[client_id]
    router = Routing(bridge)
    assert router.receive(event("U2", "1", "<@B> first"))
    target = router.to_bridge(client_id)
    target.process(target.store.next_event())
    base_probe = peer.probe
    peer.probe = lambda: {**base_probe(), "response_kinds": ["approval"]}
    operations = []

    def rpc(operation, arguments, **kwargs):
        operations.append((operation, arguments, kwargs))
        if operation == "runtime.describe":
            return {"questions": [{"question_id": "MixedCase", "kind": "approval"}]}
        if len(operations) == 2:
            raise Unconfirmed("lost response")
        return {"state": "answered"}

    peer.rpc = rpc
    assert router.receive(event("U2", "2", "<@B> [approve:MixedCase]", thread="1"))
    target.process(target.store.next_event())
    target.store.execute("UPDATE events SET retry_at=0 WHERE key='T1:C1:2'")
    target.process(target.store.next_event())
    assert [op[0] for op in operations] == [
        "runtime.describe",
        "runtime.respond",
        "runtime.respond",
    ]
    assert operations[1] == operations[2]
    assert operations[1][1]["response"] == {"kind": "approval", "decision": "allow"}
    assert operations[1][1]["question_id"] == "MixedCase"
    assert bridge.slack.posts[-1][:3] == ("U2", None, "Response submitted.")
