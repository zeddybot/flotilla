import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from types import SimpleNamespace

import pytest

from slack_conductor.conductor.journal import Unconfirmed
from slack_conductor.config import Config
from slack_conductor.headless.runtime import HeadlessRuntime
from slack_conductor.pairing import PairingStore
from slack_conductor.protocol import parse
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
        archive=lambda *args: mutations.append(("archive", args)),
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
    assert reboot.receive(event(guest, "4", "<@B> follow up", thread="1"))
    target.process(target.store.next_event())
    assert len(endpoints[winner].sent) == 2
    assert f"sender=<@{guest}>" in endpoints[winner].sent[-1][2]
    assert endpoints[winner].sent[-1][3]["send_mode"] == "queue"
    assert parse("", "B", bridge.config)[1]["send_mode"] == "steer"

    for ts, control in enumerate(["stop", "close", "questions", "deny:id", "new", "verbose"], 5):
        assert reboot.receive(event(guest, str(ts), f"<@B> [{control}] text", thread="1"))
        target.process(target.store.next_event())
    assert not endpoints[winner].mutations
    assert len(endpoints[winner].sent) == 2
    assert not endpoints[loser].sent
    assert all(row[0] == guest for row in bridge.slack.posts[-6:])


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


def test_first_headless_prompt_uses_client_model_settings(tmp_path, monkeypatch):
    bridge, endpoints = setup(tmp_path)
    client_id = register(bridge, endpoints, "U2", "1" * 32, backend="headless")
    peer = endpoints[client_id]
    runtime = HeadlessRuntime(
        replace(peer.config, workspace_root=tmp_path, claude_binary="true", engine_model="sonnet")
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
    assert router.receive(event("U2", "1", "<@B> [profile:daytona] first headless prompt"))
    target = router.to_bridge(client_id)
    target.process(target.store.next_event())
    assert runtime.sessions()[0]["configuration"]["requested"]["model"] == "sonnet"
    assert peer.sent == [{"send_mode": "queue", "verbose": False}]


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
