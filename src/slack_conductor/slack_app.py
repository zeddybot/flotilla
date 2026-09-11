import json
import logging
import time

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from .conductor.journal import Unconfirmed
from .connection_health import ConnectionHealth
from .notifications import Notifications
from .poller import Poller
from .presentation import render
from .protocol import parse, prompt_header
from .slack_io import SlackIO

log = logging.getLogger(__name__)


class Bridge:
    def __init__(self, config, store, conductor, slack, bot_id, owner_id, team_id):
        self.config, self.store, self.conductor, self.slack = config, store, conductor, slack
        self.bot_id, self.owner_id, self.team_id = bot_id, owner_id, team_id
        self.notifications = Notifications(store, slack, owner_id)
        self.poller = Poller(store, conductor.db, slack, notifications=self.notifications)

    def addressed(self, event):
        return (
            event.get("type") == "message" and event.get("channel_type") == "im"
        ) or f"<@{self.bot_id}>" in event.get("text", "")

    def receive(self, body):
        event = body.get("event", {})
        if body.get("team_id") == self.team_id and event.get("user") == self.bot_id:
            self.notifications.acknowledge(event)
            return
        if (
            body.get("team_id") != self.team_id
            or event.get("user") != self.owner_id
            or event.get("bot_id")
            or event.get("subtype")
            or event.get("hidden")
        ):
            return
        if event.get("type") not in {"app_mention", "message"} or not event.get("text"):
            return
        channel, ts = event.get("channel"), event.get("ts")
        if not channel or not ts:
            return
        if not self.addressed(event):
            return
        # app_mention + message.channels deliver the same message twice with
        # different event IDs. The channel/message identity is the dedupe key.
        self.store.accept(f"{self.team_id}:{channel}:{ts}", event)

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
                    workspace = self.conductor.new_workspace(key)
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


def run(config, store, conductor, creds, stop):
    app = App(token=creds["SLACK_BOT_TOKEN"], process_before_response=True)
    auth = app.client.auth_test()
    if auth["team_id"] != creds["SLACK_TEAM_ID"]:
        raise RuntimeError("Slack token belongs to a different workspace")
    owner = app.client.users_info(user=creds["OWNER_SLACK_USER_ID"])["user"]
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

    conductor.start(store.session_bindings())
    # Replaying an event reuses its status, workspace intent and native message ID.
    store.execute("UPDATE events SET state='pending' WHERE state='processing'")
    bridge.poller.reconcile()
    handler = SocketModeHandler(app, creds["SLACK_APP_TOKEN"])
    health = ConnectionHealth(handler.client, stop)
    health.start()
    try:
        handler.connect()
        log.info(
            "Conductor Slack bridge connected to %s; owner %s",
            auth["team"],
            creds["OWNER_SLACK_USER_ID"],
        )
        while not stop.is_set():
            started = time.monotonic()
            try:
                online = conductor.ready()
                envelope = store.next_event() if online else None
                if envelope:
                    bridge.process(envelope)
                bridge.poller.tick()
            except Unconfirmed:
                log.info("Conductor reconnect pending")
            stop.wait(max(0.05, 1 - (time.monotonic() - started)))
        if health.failed:
            raise ConnectionError("Socket Mode stalled; launchd will restart the daemon")
    finally:
        health.close()
        handler.close()
        conductor.close()
