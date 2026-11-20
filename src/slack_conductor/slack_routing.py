"""Resolve Slack people and immutable thread routes before enqueueing prompts."""

import re
import threading
from dataclasses import replace

from .intake import audit
from .pairing import PAIR_CODE, PROFILE_NAME
from .protocol import parse, profile_selector
from .slack_app import Bridge, addressed, rejection
from .store import Store

CONNECT_GUIDE = (
    "To connect a client, follow the "
    "<https://github.com/zeddybot/flotilla/blob/main/docs/connect-client.md|setup guide>, "
    "run `flotilla client connect`, then DM me `connect <code>`."
)
LEGACY_NAME = "mac"
CONNECTOR_ATTRIBUTION = re.compile(r"\*Sent using\* <@[UW][A-Z0-9]+>")


def direct_command_text(event, fallback):
    """Verify a plain DM body and any Slack connector attribution in its fallback."""
    if event.get("type") != "message" or event.get("attachments") or event.get("files"):
        return None
    if not event.get("blocks"):
        return fallback
    try:
        body, *context = event["blocks"]
        (section,) = body["elements"]
        (element,) = section["elements"]
        if (body["type"], section["type"], element["type"]) != (
            "rich_text",
            "rich_text_section",
            "text",
        ):
            return None
        style = element.get("style", {})
        if style not in ({}, {"code": True}):
            return None
        text = element["text"].strip()
        attributions = []
        for block in context:
            (attribution,) = block["elements"]
            if (
                block["type"] != "context"
                or attribution["type"] != "mrkdwn"
                or not CONNECTOR_ATTRIBUTION.fullmatch(attribution["text"])
            ):
                return None
            attributions.append(attribution["text"])
    except (AttributeError, KeyError, TypeError, ValueError):
        return None

    rendered = f"`{text}`" if style else text
    for candidate in (text, rendered):
        if not fallback.startswith(candidate):
            continue
        tail = fallback[len(candidate) :]
        if (not attributions and not tail) or (
            attributions and tail[:1].isspace() and tail.split() == " ".join(attributions).split()
        ):
            return text
    return None


def contains_pairing_command(text):
    parts = text.removeprefix("`").split(maxsplit=2)
    return (
        len(parts) >= 2
        and parts[0].lower() == "connect"
        and PAIR_CODE.fullmatch(parts[1].removesuffix("`")) is not None
    )


class Routing:
    def __init__(self, bridge, *, on_bridge=None):
        self.legacy = bridge
        self.conductor, self.store, self.slack = bridge.conductor, bridge.store, bridge.slack
        self.team_id, self.bot_id, self.owner_id = bridge.team_id, bridge.bot_id, bridge.owner_id
        self.pairing = self.conductor.pairing
        self.legacy_id = self.conductor.client_id
        self.bridges = {self.legacy_id: bridge}
        self.on_bridge = on_bridge
        self.lock = threading.Lock()
        self.store.migrate_legacy_routes(self.team_id, self.owner_id, self.legacy_id)

    def to_bridge(self, client_id):
        with self.lock:
            if client_id not in self.bridges:
                profile = self.pairing.get(client_id)
                if profile is None or profile.team_id != self.team_id:
                    raise ValueError("The client linked to this thread is unavailable.")
                endpoint = self.conductor.for_client(client_id)
                config = endpoint.config
                if profile.backend == "headless":
                    config = replace(config, model="", effort="", send_mode="queue")
                bridge = Bridge(
                    config,
                    Store(config.state_dir / "bridge.sqlite"),
                    endpoint,
                    self.slack,
                    self.bot_id,
                    profile.user_id,
                    self.team_id,
                    routed=True,
                )
                if self.on_bridge:
                    self.on_bridge(bridge)
                self.bridges[client_id] = bridge
            return self.bridges[client_id]

    def restore(self):
        for profile in self.pairing.list_active(self.team_id):
            self.to_bridge(profile.id)

    def private(self, user, text, key):
        self.slack.post(user, None, text, f"private:{key}", status_header=False)

    def resolve(self, user, name):
        if user == self.owner_id:
            if name == LEGACY_NAME:
                return self.legacy_id
            if not name:
                preferred = self.store.legacy_default(self.team_id, self.legacy_id)
                return preferred if self.pairing.is_active(preferred) else self.legacy_id
        profile = (
            self.pairing.find(self.team_id, user, name)
            if name
            else self.pairing.get_default(self.team_id, user)
        )
        return profile.id if profile else None

    def dm_command(self, command, argument, user, key):
        if command == "connect":
            allowed = self.legacy.config.pairing_allowed_users
            if user != self.owner_id and user not in allowed:
                return "Ask the Flotilla server operator to enable pairing for your Slack account."
            profile = self.pairing.claim(
                argument,
                self.team_id,
                user,
                event_key=key,
                reserved_names=(LEGACY_NAME,) if user == self.owner_id else (),
            )
            self.to_bridge(profile.id)
            return f"Connected `{profile.name}` ({profile.backend}). Use `profiles` to see your clients."
        if command == "profiles":
            default = self.resolve(user, None)
            profiles = [
                (p.id, p.name, p.backend) for p in self.pairing.profiles(self.team_id, user)
            ]
            if user == self.owner_id:
                profiles.insert(0, (self.legacy_id, LEGACY_NAME, self.legacy.conductor.backend))
            return (
                "\n".join(
                    f"`{name}` ({backend})" + (" · default" if client_id == default else "")
                    for client_id, name, backend in profiles
                )
                or CONNECT_GUIDE
            )
        if command == "default":
            if user == self.owner_id:
                client_id = self.resolve(user, argument)
                if not client_id:
                    return "No connected profile with that name."
                self.store.set_legacy_default(self.team_id, client_id)
            else:
                self.pairing.set_default(self.team_id, user, argument)
            return f"`{argument}` is your default for new threads."
        if user == self.owner_id and argument == LEGACY_NAME:
            return "The original Mac profile is configured by the server operator."
        if self.pairing.revoke(self.team_id, user, argument):
            return f"Disconnected `{argument}`."
        return "No connected profile with that name."

    def receive(self, body, *, source="socket"):
        event = body.get("event", {})
        if body.get("team_id") == self.team_id and event.get("user") == self.bot_id:
            with self.lock:
                bridges = list(self.bridges.values())
            for bridge in bridges:
                bridge.notifications.acknowledge(event)
            return False
        reason = rejection(body, self.team_id, lambda event: addressed(event, self.bot_id))
        if reason:
            audit(body, reason, source)
            return False

        channel, ts, user = event["channel"], event["ts"], event["user"]
        thread_ts = event.get("thread_ts") or ts
        key = f"{self.team_id}:{channel}:{ts}"
        direct = event.get("channel_type") == "im"
        text = event["text"].strip().removeprefix(f"<@{self.bot_id}>").strip()
        verified_text = direct_command_text(event, text) if direct else None
        command, _, argument = (verified_text if verified_text is not None else text).partition(" ")
        command = command.lower()
        argument = argument.strip()
        is_command = direct and (
            command == "connect"
            or (command == "profiles" and not argument)
            or (command in {"default", "disconnect"} and PROFILE_NAME.fullmatch(argument))
        )
        if is_command or contains_pairing_command(text):
            # A pairing code must never enter the prompt queue, including when pasted in public.
            if source == "history":
                return False
            if not is_command or verified_text is None:
                self.private(user, CONNECT_GUIDE, key)
                return False
            try:
                result = self.dm_command(command, argument, user, key)
            except ValueError as exc:
                result = str(exc)
            self.private(user, result, key)
            audit(body, "profile_command", source)
            return False

        try:
            if direct and thread_ts != ts:
                with self.lock:
                    bridges = list(self.bridges.values())
                for bridge in bridges:
                    if bridge.notifications.reply_target(channel, thread_ts, actor=user):
                        accepted = bridge.store.accept(key, event)
                        audit(body, "accepted" if accepted else "duplicate", source)
                        return accepted

            if not self.pairing.profiles(self.team_id, user) and user != self.owner_id:
                if source != "history":
                    self.private(user, CONNECT_GUIDE, key)
                audit(body, "unregistered", source)
                return False

            name, event_text = profile_selector(event["text"], self.bot_id)
            if not addressed({**event, "text": event_text}, self.bot_id):
                raise ValueError("Place [profile:name] immediately after the mention.")
            route = self.store.route(self.team_id, channel, thread_ts)
            if route is None:
                client_id = self.resolve(user, name)
                if not client_id:
                    raise ValueError(
                        "Choose one of your connected profiles. DM me `profiles` to see them."
                    )
                route = {"owner_id": user, "client_id": client_id}
                bridge = self.to_bridge(route["client_id"])
                prompt, _, command = parse(event_text, self.bot_id, bridge.config)
                if not prompt or (command and command != "new"):
                    raise ValueError("Include a prompt to start a session.")
            elif name:
                if user != route["owner_id"] or self.resolve(user, name) != route["client_id"]:
                    raise ValueError("This thread is already linked to a client profile.")

            event = {**event, "text": event_text}
            while True:
                target = route["client_id"]
                if target != self.legacy_id and not self.pairing.is_active(target):
                    raise ValueError("The client linked to this thread is disconnected.")
                bridge = self.to_bridge(target)
                winning, accepted = self.store.accept_routed(
                    self.team_id, event, route, bridge.store
                )
                if winning == route:
                    break
                if name:
                    raise ValueError("This thread is already linked to a client profile.")
                route = winning
        except ValueError as exc:
            if source != "history":
                self.private(user, str(exc), key)
            audit(body, "routing_rejected", source)
            return False
        except Exception as exc:
            audit(body, "persist_failed", source, exc)
            raise
        audit(body, "accepted" if accepted else "duplicate", source)
        return accepted
