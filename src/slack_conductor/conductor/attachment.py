"""Brief Inspector bootstrap; all agent operations use native services afterward."""

import hashlib
import json
import logging
import threading
import time
from pathlib import Path

from .process import incarnation

log = logging.getLogger(__name__)

BOOTSTRAP_PREFIX = "(()=>{const r=new XMLHttpRequest();"
# How long the frontend keeps retrying unacknowledged heartbeats before releasing
# itself. Injected into its config so both ends share one number.
FRONTEND_RELEASE = 30
# A stalled machine freezes the daemon and the frontend together, and re-attaching
# foregrounds Conductor to replace a frontend that was only wedged. online() already
# tolerates 5s of staleness before this grace starts, so waiting the full release
# window leaves the daemon behind the frontend's own deadline: a stall the frontend
# survives never costs a foreground bootstrap.
REATTACH_GRACE = FRONTEND_RELEASE


def frontend_version():
    source = Path(__file__).with_name("frontend.js").read_bytes()
    return hashlib.sha256(source).hexdigest()


def frontend_config(config):
    return {**config, "release": FRONTEND_RELEASE * 1000}


def bootstrap_snippet(url):
    return (
        BOOTSTRAP_PREFIX + "r.open('GET'," + json.dumps(url) + ",false);r.send();"
        "if(r.status!==200)throw Error('Attachment expired');"
        "window.__flotilla?.detach();eval(r.responseText)})();"
    )


def own_draft(draft):
    # CodeMirror reports the prompt as highlighted spans, so compare without spacing.
    return "".join(draft.split()).startswith("".join(BOOTSTRAP_PREFIX.split()))


def bootstrap_progress(transport):
    if transport.heartbeat.get("ready"):
        return "the frontend started, then its heartbeats stopped"
    if transport.heartbeat:
        return "the frontend started but Conductor's services did not initialize"
    if transport.served:
        return "the console fetched the frontend, which never reported back"
    return "the console never ran the bootstrap snippet"


def attach(transport):
    from .inspector import Inspector

    ax = Inspector()
    front = Inspector.focused_pid()
    log.info("Attaching Conductor frontend; previous app PID %s", front)
    try:
        _attach(transport, ax)
    finally:
        if front and ax.pid and front != ax.pid:
            Inspector.activate(front, only_if=ax.pid)
        log.info("Conductor bootstrap finished; foreground app PID %s", Inspector.focused_pid())


def _attach(transport, ax):
    import Quartz

    from .inspector import Inspector, attr, walk

    with ax.reachable():
        Inspector.activate(ax.pid)
        menu = next(
            (
                e
                for e in walk(attr(ax.app, "AXMenuBar"))
                if attr(e, "AXTitle") == "Open Debug Tools"
            ),
            None,
        )
        if menu is None:
            raise RuntimeError("Conductor Inspector unavailable")
        prompt = ax.find(role="AXTextArea", description="Console prompt")
        opened = prompt is None
        try:
            if opened:
                ax.press(menu)
                time.sleep(0.5)
            prompt = ax.find(role="AXTextArea", description="Console prompt")
            if prompt is None:
                tab = ax.find(role="AXRadioButton", title="Console") or ax.find(
                    role="AXButton", title="Console"
                )
                if tab:
                    ax.press(tab)
                prompt = ax.wait(
                    lambda: ax.find(role="AXTextArea", description="Console prompt"),
                    "Inspector console unavailable",
                    5,
                )
            draft = Inspector.draft(prompt).strip()
            if draft and not own_draft(draft):
                raise RuntimeError(
                    "Inspector console has a draft; clear it to allow attachment: "
                    + repr(draft[:80])
                )
            config = transport.attach(incarnation())
            transport.bootstrap = (
                Path(__file__)
                .with_name("frontend.js")
                .read_text()
                .replace("__CONFIG__", json.dumps(frontend_config(config)))
            )
            url = config["base"] + "/bootstrap?generation=" + config["generation"]
            ax.set(prompt, "AXFocused", True)
            if draft:
                # Replacing a previous attempt's snippet means selecting it first.
                # Refuse unless the prompt really holds focus, or the select-all
                # would overwrite whatever else in Conductor does.
                if not attr(prompt, "AXFocused"):
                    raise RuntimeError("Inspector console prompt did not take focus")
                ax.select_all()
            ax.type_text(bootstrap_snippet(url))
            time.sleep(0.2)
            for down in (True, False):
                event = Quartz.CGEventCreateKeyboardEvent(None, 36, down)
                Quartz.CGEventSetFlags(event, 0)
                Quartz.CGEventPostToPid(ax.pid, event)

            def ready():
                if transport.heartbeat.get("error"):
                    raise RuntimeError(
                        "Conductor native bootstrap: " + transport.heartbeat["error"]
                    )
                return transport.online()

            ax.wait(
                ready,
                lambda: (
                    "Conductor native services did not become ready: "
                    + bootstrap_progress(transport)
                ),
                20,
            )
        finally:
            if opened and ax.find(role="AXTextArea", description="Console prompt") is not None:
                ax.press(menu)


class Attachment:
    def __init__(self, transport):
        self.transport = transport
        self.stop = threading.Event()
        self.worker = threading.Thread(target=self.run, daemon=True)

    def start(self):
        self.worker.start()

    def run(self):
        from .inspector import Inspector

        failures, last_app, retry_at, offline_since, established = 0, None, 0, None, False
        deferred = attached = False
        while not self.stop.is_set():
            current = incarnation()
            if current != last_app:
                failures, last_app, retry_at = 0, current, 0
                # The previous daemon's frontend earns the same grace as one that
                # served this daemon: it only needs time to find the address again.
                established = self.transport.expects(current)
                if established:
                    log.info("Waiting for the previous daemon's Conductor frontend")
            if self.transport.online():
                if established and not attached:
                    log.info("Conductor native frontend adopted")
                failures, offline_since, established, attached = 0, None, True, True
            else:
                if offline_since is None:
                    offline_since = time.monotonic()
                # Only a frontend that was already serving earns the grace period;
                # a cold start or a fresh app still attaches on the first tick.
                settled = not established or time.monotonic() - offline_since >= REATTACH_GRACE
                if (
                    settled
                    and time.monotonic() >= retry_at
                    and (
                        not failures
                        or (current and Inspector.focused_pid() == int(current.split(":", 1)[0]))
                    )
                ):
                    # A locked screen cannot show the Inspector, and the activation
                    # requested behind it lands in front of the user on unlock.
                    if Inspector.unattended():
                        if not deferred:
                            log.info("Conductor attachment deferred until the screen is unlocked")
                        deferred = True
                        self.stop.wait(1)
                        continue
                    deferred = False
                    try:
                        attach(self.transport)
                        log.info("Conductor native frontend attached")
                        failures, attached = 0, True
                    except Exception as exc:
                        failures += 1
                        retry_at = time.monotonic() + 60
                        log.warning("Conductor attachment pending: %s", exc, exc_info=True)
            # After a failed bootstrap, retries cannot repeatedly foreground the
            # app: wait for the user to select it, and retry at most once a minute.
            # Keep observing process changes during backoff so a new app launch
            # or crash does not inherit the old frontend's retry delay.
            self.stop.wait(1)

    def close(self):
        self.stop.set()
        self.worker.join(timeout=30)
