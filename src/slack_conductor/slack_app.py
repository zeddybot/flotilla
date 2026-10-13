import json
import logging
import threading
import time
from contextlib import ExitStack

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler as BoltSocketModeHandler

from .conductor.journal import Unconfirmed
from .connection_health import ConnectionHealth
from .intake import audit
from .monitoring import write_service_status
from .notifications import Notifications
from .poller import Poller
from .presentation import render
from .protocol import parse, prompt_header
from .slack_io import SlackIO

log = logging.getLogger(__name__)


class SocketModeHandler(BoltSocketModeHandler):
    def handle(self, client, req):
        # Record arrival before Bolt authorization/matching can discard an event.
        if req.type == "events_api":
            audit(req.payload, "received", "socket")
        super().handle(client, req)


class Bridge:
    def __init__(self, config, store, conductor, slack, bot_id, owner_id, team_id):
        self.config, self.store, self.conductor, self.slack = config, store, conductor, slack
        self.bot_id, self.owner_id, self.team_id = bot_id, owner_id, team_id
        self.notifications = Notifications(store, slack, owner_id)
        self.poller = Poller(store, conductor.db, slack, notifications=self.notifications)
        if hasattr(conductor, "make_uploads"):
            self.poller.uploads = conductor.make_uploads(store, slack, self.poller.clock)

    def addressed(self, event):
        return (
            event.get("type") == "message" and event.get("channel_type") == "im"
        ) or f"<@{self.bot_id}>" in event.get("text", "")

    def receive(self, body, *, source="socket"):
        event = body.get("event", {})
        if body.get("team_id") == self.team_id and event.get("user") == self.bot_id:
            self.notifications.acknowledge(event)
            return False
        reason = next(
            (
                name
                for rejected, name in (
                    (body.get("team_id") != self.team_id, "wrong_team"),
                    (event.get("user") != self.owner_id, "not_owner"),
                    (bool(event.get("bot_id")), "bot"),
                    (event.get("subtype") not in (None, "file_share"), "unsupported_subtype"),
                    (bool(event.get("hidden")), "hidden"),
                    (event.get("type") not in {"app_mention", "message"}, "unsupported_type"),
                    (not event.get("text"), "empty_text"),
                )
                if rejected
            ),
            None,
        )
        channel, ts = event.get("channel"), event.get("ts")
        reason = reason or ("missing_identity" if not channel or not ts else None)
        reason = reason or ("not_addressed" if not self.addressed(event) else None)
        if reason:
            audit(body, reason, source)
            return False
        # app_mention + message.channels deliver the same message twice with
        # different event IDs. The channel/message identity is the dedupe key.
        try:
            accepted = self.store.accept(f"{self.team_id}:{channel}:{ts}", event)
        except Exception as exc:
            audit(body, "persist_failed", source, exc)
            raise
        audit(body, "accepted" if accepted else "duplicate", source)
        return accepted

    def process(self, envelope):
        key = envelope["key"]
        event = json.loads(envelope["payload"])
        # Apply the same rule to events persisted before an upgrade/restart.
        if not self.addressed(event):
            self.store.execute("UPDATE events SET state='ignored' WHERE key=?", (key,))
            return
        completed = self.store.rows(
            "SELECT id FROM statuses WHERE event_key=? AND state IN ('complete','superseded')",
            (key,),
        )
        if completed:
            self.store.execute("UPDATE events SET state='done' WHERE key=?", (key,))
            return
        channel, thread_ts = event["channel"], event.get("thread_ts", event["ts"])
        self.store.execute("UPDATE events SET state='processing' WHERE key=?", (key,))
        status_id = None
        try:
            notice = self.notifications.reply_target(channel, thread_ts)
            if notice:
                self.slack.post(
                    channel,
                    thread_ts,
                    render(
                        "attention",
                        f"Please follow up in the [original thread]({notice['permalink']}) "
                        "and mention @Conductor there. This DM is a notification.",
                    ),
                    f"notice-reply:{key}",
                )
                self.store.execute("UPDATE events SET state='done' WHERE key=?", (key,))
                return
            thread = self.store.thread(channel, thread_ts)
            previous = json.loads(thread["options"]) if thread else None
            text, options, command = parse(event["text"], self.bot_id, self.config, previous)
            if thread and thread["state"] == "archived" and command not in {"new", "close"}:
                raise ValueError("workspace archived; use [new]")
            if command == "close":
                if not thread:
                    raise ValueError("This thread has no workspace")
                status_id = self.store.add_status(key, channel, thread_ts, "", options)
                status_ts = self.store.status(status_id)["status_ts"] or self.slack.post(
                    channel, thread_ts, render("archiving"), f"status:{key}"
                )
                self.store.update(status_id, status_ts=status_ts)
                self.conductor.archive(thread["workspace_id"], thread["session_id"])
                self.store.thread_state(thread, "archived")
                self.poller.finish(self.store.status(status_id), "", "archived")
            else:
                if not text:
                    raise ValueError("Include a prompt after the mention or bracket tokens")
                permalink = self.slack.call(
                    "chat_getPermalink", channel=channel, message_ts=event["ts"]
                )["permalink"]
                prompt = prompt_header(channel, thread_ts, event["ts"], permalink, text)
                status_id = self.store.add_status(key, channel, thread_ts, prompt, options)
                status_ts = self.store.status(status_id)["status_ts"] or self.slack.post(
                    channel,
                    thread_ts,
                    render(
                        "creating" if not thread or command == "new" else "starting",
                        detail=options["model"],
                    ),
                    f"status:{key}",
                )
                self.store.update(status_id, status_ts=status_ts)
                saved = self.store.status(status_id)
                options = json.loads(saved["options"])
                prompt = saved["prompt"]
                if saved["workspace_id"]:
                    workspace = self.conductor.validate(saved["workspace_id"], saved["session_id"])
                elif not thread or command == "new":
                    workspace = (
                        self.conductor.create_session(key, options)
                        if hasattr(self.conductor, "create_session")
                        else self.conductor.new_workspace(key)
                    )
                else:
                    workspace = self.conductor.validate(
                        thread["workspace_id"], thread["session_id"]
                    )
                sid = workspace["active_session_id"]
                self.store.bind(channel, thread_ts, workspace, options)
                self.store.update(
                    status_id,
                    workspace_id=workspace["id"],
                    session_id=sid,
                    cursor=saved["cursor"]
                    if saved["workspace_id"]
                    else self.conductor.db.cursor(sid),
                    state="waiting",
                )
                note = self.conductor.send(workspace["id"], sid, prompt, options)
                picked = self.conductor.db.pickup(sid, prompt)
                self.store.update(
                    status_id, note=note, latest="starting agent" if picked else "queued"
                )
            self.store.execute("UPDATE events SET state='done' WHERE key=?", (key,))
        except Unconfirmed:
            self.store.execute(
                "UPDATE events SET state='pending',retry_at=? WHERE key=?", (time.time() + 5, key)
            )
            if (
                status_id
                and self.store.status(status_id)["state"] == "preparing"
                and self.store.status(status_id)["status_ts"]
            ):
                self.slack.edit(
                    self.store.status(status_id),
                    render("reconnecting", "Your message is saved; waiting for Conductor."),
                )
        except Exception as exc:
            log.exception("Slack event action failed: %s", key)
            message = f"error: {exc}"
            if status_id:
                status = self.store.status(status_id)
                if status["state"] == "waiting":
                    # Sending may have succeeded before the native acknowledgement arrived.
                    # Keep polling for pickup; never send the prompt twice.
                    self.store.update(
                        status_id, note=message, latest="send unconfirmed; checking Conductor"
                    )
                    self.slack.edit(status, render("unconfirmed", message, "send unconfirmed"))
                elif status["status_ts"]:
                    self.poller.finish(status, message, "attention")
                else:
                    self.store.update(status_id, state="failed")
            else:
                self.slack.post(channel, thread_ts, render("attention", message), f"error:{key}")
            self.store.execute("UPDATE events SET state='failed' WHERE key=?", (key,))


class Link:
    """Socket Mode connection, replaced in-process when it stalls."""

    # In-process replacement once failed for hours until the daemon was restarted,
    # and a restart is the recovery that is proven. It is cheap now that the next
    # daemon adopts the frontend, so it follows a short outage.
    OUTAGE_LIMIT = 180

    def __init__(self, app, token, stop):
        self.app, self.token, self.stop = app, token, stop
        self.handler = self.health = None
        self.retry_at, self.delay = 0, 1
        self.down_since = None
        self.escalated = False

    def open(self):
        self.handler = SocketModeHandler(self.app, self.token)
        self.health = ConnectionHealth(self.handler.client, self.stop)
        self.health.start()
        self.handler.connect()

    def close(self):
        health, handler = self.health, self.handler
        self.health = self.handler = None
        if health:
            health.close()
            self.escalated = self.escalated or health.escalated
        if handler:
            # SDK shutdown joins its own workers; a wedged one must not wedge recovery.
            closing = threading.Thread(target=handler.close, name="slack-close", daemon=True)
            closing.start()
            closing.join(5)

    def connected(self):
        now = time.monotonic()
        if not self.health and now >= self.retry_at and not self.stop.is_set():
            self.retry_at, self.delay = now + self.delay, min(self.delay * 2, 60)
            try:
                self.open()
            except Exception as exc:  # noqa: BLE001 - any failed attempt is retried
                # Error text can contain authenticated URLs; only log its type here.
                log.warning("Slack connection attempt failed (%s)", type(exc).__name__)
                self.close()
        if self.health:
            self.health.check()
            if not self.health.failed:
                if self.health.stable():
                    self.delay, self.down_since = 1, None
                return True
            self.close()
        if self.down_since is None:
            self.down_since = now
        if now - self.down_since >= self.OUTAGE_LIMIT and not self.stop.is_set():
            log.error("Restarting Slack daemon: no stable connection for %ss", self.OUTAGE_LIMIT)
            self.escalated = True
            self.stop.set()
        return False


def identify(client, owner_id, stop):
    delay = 1
    while True:
        try:
            return client.auth_test(), client.users_info(user=owner_id)["user"]
        except OSError as exc:
            # Offline at login or on wake: exiting only relaunches into the same outage.
            # A rejected token is a SlackApiError and still stops the daemon.
            log.warning(
                "Slack unreachable at startup (%s); retrying in %ss", type(exc).__name__, delay
            )
        if stop.wait(delay):
            return None
        delay = min(delay * 2, 60)


def run(config, store, conductor, creds, stop):
    # Bolt's own verification calls auth.test once inside the constructor.
    app = App(
        token=creds["SLACK_BOT_TOKEN"],
        process_before_response=True,
        token_verification_enabled=False,
    )
    identity = identify(app.client, creds["OWNER_SLACK_USER_ID"], stop)
    if identity is None:
        return
    auth, owner = identity
    if auth["team_id"] != creds["SLACK_TEAM_ID"]:
        raise RuntimeError("Slack token belongs to a different workspace")
    if owner.get("is_bot") or owner.get("deleted"):
        raise RuntimeError("Configured Slack owner is not an active human user")
    bridge = Bridge(
        config,
        store,
        conductor,
        SlackIO(app.client),
        auth["user_id"],
        creds["OWNER_SLACK_USER_ID"],
        creds["SLACK_TEAM_ID"],
    )

    @app.event("app_mention")
    @app.event("message")
    def receive(body):
        bridge.receive(body)

    with ExitStack() as resources:
        conductor.start(store.session_bindings())
        resources.callback(conductor.close)
        # Replaying an event reuses its status, workspace intent and native message ID.
        store.execute("UPDATE events SET state='pending' WHERE state='processing'")
        bridge.poller.reconcile()
        link = Link(app, creds["SLACK_APP_TOKEN"], stop)
        resources.callback(link.close)
        announced = False
        while True:
            started = time.monotonic()
            connected = link.connected()
            conductor.slack_ready = connected
            try:
                write_service_status(
                    config.state_dir, runtime_ready=conductor.ready(), slack_ready=connected
                )
            except OSError:
                log.warning("Cannot persist service health snapshot")
            if stop.is_set():
                break
            if connected and not announced:
                announced = True
                log.info(
                    "Conductor Slack bridge connected to %s; owner %s",
                    auth["team"],
                    creds["OWNER_SLACK_USER_ID"],
                )
            try:
                online = conductor.ready()
                # Replies cannot be delivered while Slack is away; keep events queued.
                if connected:
                    envelope = (
                        store.next_event()
                        if online or getattr(conductor, "allow_offline", False)
                        else None
                    )
                    if envelope:
                        bridge.process(envelope)
                    bridge.poller.tick()
            except Unconfirmed:
                log.info("Conductor reconnect pending")
            stop.wait(max(0.05, 1 - (time.monotonic() - started)))
        link.close()
        if link.escalated:
            raise ConnectionError("Socket Mode stalled; launchd will restart the daemon")
