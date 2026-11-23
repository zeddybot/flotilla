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
from .intake_recovery import IntakeRecovery
from .monitoring import write_service_status
from .notifications import Notifications
from .poller import Poller
from .presentation import render
from .protocol import parse, prompt_header
from .remote_peer import SubmissionRejected
from .slack_io import SlackIO

log = logging.getLogger(__name__)


def rejection(body, team_id, addressed):
    event = body.get("event", {})
    return next(
        (
            name
            for rejected, name in (
                (body.get("team_id") != team_id, "wrong_team"),
                (not event.get("user"), "missing_identity"),
                (bool(event.get("bot_id")), "bot"),
                (event.get("subtype") not in (None, "file_share"), "unsupported_subtype"),
                (bool(event.get("hidden")), "hidden"),
                (event.get("type") not in {"app_mention", "message"}, "unsupported_type"),
                (not event.get("text"), "empty_text"),
                (not event.get("channel") or not event.get("ts"), "missing_identity"),
                (not addressed(event), "not_addressed"),
            )
            if rejected
        ),
        None,
    )


def addressed(event, bot_id):
    return (
        event.get("type") == "message" and event.get("channel_type") == "im"
    ) or f"<@{bot_id}>" in event.get("text", "")


class SocketModeHandler(BoltSocketModeHandler):
    def handle(self, client, req):
        # Record arrival before Bolt authorization/matching can discard an event.
        if req.type == "events_api":
            audit(req.payload, "received", "socket")
        super().handle(client, req)


class Bridge:
    def __init__(self, config, store, conductor, slack, bot_id, owner_id, team_id, *, routed=False):
        self.config, self.store, self.conductor, self.slack = config, store, conductor, slack
        self.bot_id, self.owner_id, self.team_id = bot_id, owner_id, team_id
        self.routed = routed
        self.notifications = Notifications(store, slack, owner_id)
        self.poller = Poller(store, conductor.db, slack, notifications=self.notifications)
        if hasattr(conductor, "make_uploads"):
            self.poller.uploads = conductor.make_uploads(store, slack, self.poller.clock)

    def addressed(self, event):
        return addressed(event, self.bot_id)

    def probe(self, capability):
        if not hasattr(self.conductor, "probe"):
            return {}
        probe = self.conductor.probe()
        if not probe.get("capabilities", {}).get(capability, False):
            raise ValueError(f"This profile does not support {capability}.")
        return probe

    def check_options(self, options, session_id=None):
        if not hasattr(self.conductor, "probe"):
            if options["send_mode"] is None:
                options["send_mode"] = "steer"
            return
        probe = self.conductor.probe()
        capabilities = probe.get("capabilities", {})
        configuration = probe.get("configuration", {})
        if probe.get("adapter") == "headless":
            if session_id:
                description = self.conductor.rpc("runtime.describe", {"session_id": session_id})
                capabilities = description.get("capabilities", capabilities)
            else:
                if "engine_capabilities" not in configuration and any(
                    options.get(key) for key in ("engine", "model", "effort")
                ):
                    raise ValueError(
                        "This headless client needs an upgrade to select an engine, model or effort."
                    )
                engine = options.get("engine") or configuration.get("default_engine")
                if engine and engine not in configuration.get("engines", (engine,)):
                    raise ValueError(f"This profile does not support the {engine} engine.")
                capabilities = configuration.get("engine_capabilities", {}).get(
                    engine, capabilities
                )
                efforts = configuration.get("engine_efforts", {}).get(engine)
                if efforts is not None and options["effort"] and options["effort"] not in efforts:
                    raise ValueError(f"The {engine} engine does not support this effort level.")
        if options["send_mode"] is None:
            options["send_mode"] = "steer" if capabilities.get("steer") else "queue"
        if not capabilities.get(options["send_mode"]):
            target = "session" if session_id and probe.get("adapter") == "headless" else "profile"
            raise ValueError(f"This {target} does not support {options['send_mode']}.")
        if session_id:
            return
        if not probe.get("capabilities", {}).get("create"):
            raise ValueError("This profile does not support create.")
        if "models" in configuration and options["model"] not in configuration["models"]:
            raise ValueError("This profile does not support the selected model.")
        if options["fast"] and not configuration.get("fast"):
            raise ValueError("This profile does not support fast mode.")

    def session_control(self, envelope, thread, command, text):
        action, _, question_id = command.partition(":")
        sid = thread["session_id"]
        key = "slack-control:" + envelope["key"]
        if action == "stop":
            self.probe("interrupt")
            self.conductor.rpc("runtime.interrupt", {"command_id": key, "session_id": sid}, key=key)
            return "Stop requested."
        probe = self.probe("respond")
        if envelope.get("control"):
            response = json.loads(envelope["control"])
        else:
            description = self.conductor.rpc("runtime.describe", {"session_id": sid})
            if description.get("questions_available") is False:
                raise ValueError("Questions are currently unavailable. Check Conductor.")
            questions = description.get("questions", [])
            if action == "questions":
                return (
                    "\n".join(
                        f"`{q['question_id']}` ({q['kind']}) — {q['prompt']}" for q in questions
                    )
                    or "There are no pending questions."
                )
            pending = next((q for q in questions if q["question_id"] == question_id), None)
            if not pending or pending["kind"] not in probe.get("response_kinds", []):
                raise ValueError(
                    "Use [questions] to find a supported pending question or approval."
                )
            kind = pending["kind"]
            if action == "answer" and kind == "question":
                answers = [answer.strip() for answer in text.split("\n") if answer.strip()]
                if not answers or len(answers) != len(pending.get("questions", [])):
                    raise ValueError("Include one answer per line after [answer:question-id].")
                response = {"kind": "question", "answers": answers}
            elif action in {"approve", "deny"} and kind == "approval":
                response = {
                    "kind": "approval",
                    "decision": "allow" if action == "approve" else "deny",
                }
            elif action in {"approve", "deny"} and kind == "plan":
                response = {"kind": "plan", "approved": action == "approve"}
            elif action == "deny" and kind in {"codex_permission", "mcp_elicitation"}:
                response = {"kind": kind, "action": "decline"}
            else:
                raise ValueError("Respond to this request in Conductor.")
            self.store.execute(
                "UPDATE events SET control=? WHERE key=?", (json.dumps(response), envelope["key"])
            )
        self.conductor.rpc(
            "runtime.respond",
            {
                "command_id": key,
                "session_id": sid,
                "question_id": question_id,
                "response": response,
            },
            key=key,
        )
        return "Response submitted."

    def receive(self, body, *, source="socket"):
        event = body.get("event", {})
        if body.get("team_id") == self.team_id and event.get("user") == self.bot_id:
            self.notifications.acknowledge(event)
            return False
        reason = rejection(body, self.team_id, self.addressed)
        if not reason and event.get("user") != self.owner_id:
            reason = "not_owner"
        channel, ts = event.get("channel"), event.get("ts")
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
        channel, thread_ts = event["channel"], event.get("thread_ts") or event["ts"]
        self.store.execute("UPDATE events SET state='processing' WHERE key=?", (key,))
        status_id = None
        private_error = self.routed and event.get("user") != self.owner_id
        try:
            notice = self.notifications.reply_target(channel, thread_ts, actor=event.get("user"))
            if notice:
                self.slack.post(
                    channel,
                    thread_ts,
                    render(
                        "attention",
                        f"Please follow up in the [original thread]({notice['permalink']}) "
                        f"and mention <@{self.bot_id}> there. This DM is a notification.",
                    ),
                    f"notice-reply:{key}",
                )
                self.store.execute("UPDATE events SET state='done' WHERE key=?", (key,))
                return
            thread = self.store.thread(channel, thread_ts)
            previous = json.loads(thread["options"]) if thread else None
            contributor = self.routed and event.get("user") != self.owner_id
            baseline = parse("", self.bot_id, self.config, previous)[1]
            text, options, command = parse(event["text"], self.bot_id, self.config, baseline)
            if contributor and (
                not thread
                or command
                or any(options[key] != baseline[key] for key in baseline if key != "send_mode")
            ):
                raise ValueError(
                    "Only the thread owner can start a session or change session settings."
                )
            if (
                self.routed
                and thread
                and command != "new"
                and any(
                    options.get(name) != baseline.get(name)
                    for name in ("engine", "model", "effort", "fast")
                )
            ):
                if self.config.runtime_adapter == "headless":
                    raise ValueError("Use [new] to change the engine, model or effort.")
                raise ValueError("Use [new] to change the model, effort or fast mode.")
            if thread and thread["state"] == "archived" and command not in {"new", "close"}:
                raise ValueError("workspace archived; use [new]")
            if command and command not in {"new", "close"}:
                private_error = True
                if not thread:
                    raise ValueError("This thread has no workspace")
                result = self.session_control(envelope, thread, command, text)
                self.slack.post(self.owner_id, None, result, f"control:{key}", status_header=False)
            elif command == "close":
                if not thread:
                    raise ValueError("This thread has no workspace")
                status_id = self.store.add_status(key, channel, thread_ts, "", options)
                status_ts = self.store.status(status_id)["status_ts"] or self.slack.post(
                    channel, thread_ts, render("archiving"), f"status:{key}"
                )
                self.store.update(status_id, status_ts=status_ts)
                if self.routed:
                    self.probe("close_session")
                self.conductor.archive(thread["workspace_id"], thread["session_id"])
                self.store.thread_state(thread, "archived")
                self.poller.finish(self.store.status(status_id), "", "archived")
            else:
                if not text:
                    raise ValueError("Include a prompt after the mention or bracket tokens")
                permalink = self.slack.call(
                    "chat_getPermalink", channel=channel, message_ts=event["ts"]
                )["permalink"]
                prompt = prompt_header(
                    channel, thread_ts, event["ts"], permalink, text, sender=event.get("user")
                )
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
                needs_default = options["send_mode"] is None
                if self.routed:
                    session_id = saved["session_id"]
                    if not session_id and thread and command != "new":
                        session_id = thread["session_id"]
                    self.check_options(options, session_id)
                elif needs_default:
                    options["send_mode"] = "steer"
                if needs_default:
                    self.store.update(status_id, options=json.dumps(options))
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
                session_options = previous if contributor else options
                self.store.bind(
                    channel,
                    thread_ts,
                    workspace,
                    {name: value for name, value in session_options.items() if name != "send_mode"},
                )
                self.store.update(
                    status_id,
                    workspace_id=workspace["id"],
                    session_id=sid,
                    cursor=saved["cursor"]
                    if saved["workspace_id"]
                    else self.conductor.db.cursor(sid),
                    state="waiting",
                )
                submit_options = options
                if self.routed and self.config.runtime_adapter == "headless":
                    submit_options = {key: options[key] for key in ("send_mode", "verbose")}
                note = self.conductor.send(workspace["id"], sid, prompt, submit_options)
                picked = self.conductor.db.pickup(sid, prompt)
                self.store.update(
                    status_id, note=note, latest="starting agent" if picked else "queued"
                )
            self.store.execute("UPDATE events SET state='done' WHERE key=?", (key,))
        except SubmissionRejected as exc:
            self.store.update(status_id, note=exc.note_prefix + str(exc))
            self.store.execute("UPDATE events SET state='failed' WHERE key=?", (key,))
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
                    render("reconnecting", "Your message is saved; waiting for the client."),
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
                if private_error:
                    channel, thread_ts = event["user"], None
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
        self.refresh_requested = threading.Event()

    def refresh(self):
        # Called by the history worker; only the service loop replaces sockets.
        self.refresh_requested.set()

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
        if self.refresh_requested.is_set() and not self.stop.is_set():
            self.refresh_requested.clear()
            log.warning("Replacing Slack connection: history recovered a missed owner message")
            self.close()
            self.retry_at = 0
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


def work_bridge(bridge, connected, stop):
    """A stalled client RPC must not hold another client's Slack work."""
    reconciled = False
    endpoint, store = bridge.conductor, bridge.store
    while not stop.is_set() and getattr(endpoint, "enabled", lambda: True)():
        started = time.monotonic()
        if connected.is_set():
            try:
                if not reconciled:
                    store.execute("UPDATE events SET state='pending' WHERE state='processing'")
                    bridge.poller.reconcile()
                    reconciled = True
                available = endpoint.ready() or getattr(endpoint, "allow_offline", False)
                if available and (envelope := store.next_event()):
                    try:
                        bridge.process(envelope)
                    except Exception:
                        store.execute(
                            "UPDATE events SET state='pending',retry_at=? "
                            "WHERE key=? AND state='processing'",
                            (time.time() + 5, envelope["key"]),
                        )
                        raise
                bridge.poller.tick()
            except Unconfirmed:
                log.info("Conductor reconnect pending")
            except Exception:
                log.exception("Slack client loop failed")
        stop.wait(max(0.05, 1 - (time.monotonic() - started)))


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
    multi_profile = hasattr(conductor, "pairing")
    bridge = Bridge(
        config,
        store,
        conductor,
        SlackIO(app.client),
        auth["user_id"],
        creds["OWNER_SLACK_USER_ID"],
        creds["SLACK_TEAM_ID"],
        routed=multi_profile,
    )
    router = bridge

    @app.event("app_mention")
    @app.event("message")
    def receive(body):
        router.receive(body)

    with ExitStack() as resources:
        conductor.start(store.session_bindings())
        resources.callback(conductor.close)
        link = Link(app, creds["SLACK_APP_TOKEN"], stop)
        resources.callback(link.close)
        connected = threading.Event()
        workers = []
        workers_lock = threading.Lock()

        def add_worker(active):
            recovery = IntakeRecovery(
                active,
                creds["SLACK_BOT_TOKEN"],
                stop,
                on_recovered=link.refresh,
                receive=router.receive,
            )
            worker = threading.Thread(
                target=work_bridge, args=(active, connected, stop), daemon=True
            )
            with workers_lock:
                if stop.is_set():
                    return
                recovery.start()
                try:
                    worker.start()
                except Exception:
                    recovery.close()
                    raise
                workers.append((active, worker, recovery))

        def stop_workers():
            stop.set()
            with workers_lock:
                stopping = list(workers)
            for _, _, recovery in stopping:
                recovery.close()
            for _, worker, _ in stopping:
                worker.join(timeout=0.25)

        resources.callback(stop_workers)
        if multi_profile:
            from .slack_routing import Routing

            router = Routing(bridge, on_bridge=add_worker)
            router.restore()
        add_worker(bridge)
        announced = False
        while True:
            online = link.connected()
            if online:
                connected.set()
            else:
                connected.clear()
            conductor.slack_ready = online
            with workers_lock:
                runtime_ready = any(active.conductor.ready() for active, _, _ in workers)
            try:
                write_service_status(
                    config.state_dir, runtime_ready=runtime_ready, slack_ready=online
                )
            except OSError:
                log.warning("Cannot persist service health snapshot")
            if stop.is_set():
                break
            if online and not announced:
                announced = True
                log.info(
                    "Conductor Slack bridge connected to %s; owner %s",
                    auth["team"],
                    creds["OWNER_SLACK_USER_ID"],
                )
            stop.wait(1)
        link.close()
        if link.escalated:
            raise ConnectionError("Socket Mode stalled; launchd will restart the daemon")
