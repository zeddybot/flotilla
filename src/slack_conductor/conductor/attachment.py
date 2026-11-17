"""Brief Inspector bootstrap; all agent operations use native services afterward."""

import hashlib
import json
import logging
import subprocess
import threading
import time
from pathlib import Path

from .process import incarnation

log = logging.getLogger(__name__)

BOOTSTRAP_PREFIX = "/* Flotilla native bridge */"
# How long the frontend keeps intercepting without an acknowledged heartbeat before
# it rests. Injected into its config so both ends share one number.
FRONTEND_RELEASE = 30
# Re-attaching foregrounds Conductor, so it is reserved for a frontend that is gone.
# One that is only wedged or resting finds the daemon again within this grace.
REATTACH_GRACE = FRONTEND_RELEASE
# A tick slower than this means the machine stalled, and the frontend with it.
STALL = 3
# Stalls restart the grace. Past this much silence the bootstrap goes ahead anyway.
SILENCE_LIMIT = 120
# A working bridge outranks keeping focus, so a failed bootstrap is retried on this
# schedule whatever app is in front. Nobody is watching a locked screen, and
# selecting Conductor gives the focus away already: both retry at the first delay.
RETRY_DELAYS = (60, 120, 300)


def frontend_version():
    source = Path(__file__).with_name("frontend.js").read_bytes()
    return hashlib.sha256(source).hexdigest()


def frontend_config(config):
    return {**config, "release": FRONTEND_RELEASE * 1000}


def bootstrap_snippet(source):
    # DevTools can execute console input directly, but Conductor's CSP blocks eval.
    return BOOTSTRAP_PREFIX + "\nwindow.__flotilla?.detach();\n" + source


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
    return "the console bootstrap did not report back"


def attach(transport):
    from .inspector import Inspector

    ax = Inspector()
    front = Inspector.focused_pid()
    if front is None:
        log.warning("Foreground app unknown; Conductor will stay in front after the bootstrap")
    log.info("Attaching Conductor frontend; previous app PID %s", front)
    try:
        _attach(transport, ax)
    finally:
        restore(Inspector, front, ax.pid)


def restore(inspector, front, conductor):
    # Runs on a machine slow enough to have needed a bootstrap. Raising here would
    # leave Conductor in front and report a bootstrap that worked as failed.
    if front and conductor and front != conductor:
        for _ in range(3):
            try:
                inspector.activate(front, only_if=conductor, timeout=15)
                break
            except subprocess.TimeoutExpired:
                log.warning("Handing focus back to PID %s timed out", front)
    log.info("Conductor bootstrap finished; foreground app PID %s", inspector.focused_pid())


def retry_delay(inspector, failures):
    try:
        unattended = inspector.unattended()
    except Exception:  # noqa: BLE001 - an unreadable session must not stop the retries
        unattended = False
    return RETRY_DELAYS[0 if unattended else min(failures, len(RETRY_DELAYS)) - 1]


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
        prompt = ax.console_prompt()
        opened = prompt is None
        try:
            if opened:
                ax.press(menu)
                time.sleep(0.5)
            prompt = ax.console_prompt()
            if prompt is None:
                tab = ax.console_tab()
                if tab:
                    ax.press(tab)
                prompt = ax.wait(
                    ax.console_prompt,
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
            ax.set(prompt, "AXFocused", True)
            if draft:
                # Replacing a previous attempt's snippet means selecting it first.
                # Refuse unless the prompt really holds focus, or the select-all
                # would overwrite whatever else in Conductor does.
                if not attr(prompt, "AXFocused"):
                    raise RuntimeError("Inspector console prompt did not take focus")
                ax.select_all()
            ax.type_text(bootstrap_snippet(transport.bootstrap))
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
            if opened and ax.console_prompt() is not None:
                ax.close_console()


class Attachment:
    def __init__(self, transport):
        self.transport = transport
        self.stop = threading.Event()
        self.worker = threading.Thread(target=self.run, daemon=True)

    def start(self):
        self.worker.start()

    def run(self):
        state = {
            "failures": 0,
            "app": None,
            "retry_at": 0,
            "attempted_at": 0,
            "offline_since": None,
            "silent_since": None,
            "established": False,
            "attached": False,
            "waited": 0,
        }
        while not self.stop.is_set():
            try:
                self.tick(state)
            except Exception:
                # Nothing restarts this worker, so one failed probe must not end it.
                log.exception("Conductor attachment check failed")
                self.stop.wait(1)

    def due(self, state, current, inspector):
        now = time.monotonic()
        if not state["failures"] or now >= state["retry_at"]:
            return True
        return bool(
            now >= state["attempted_at"] + RETRY_DELAYS[0]
            and current
            and inspector.focused_pid() == int(current.split(":", 1)[0])
        )

    def tick(self, state):
        from .inspector import Inspector

        started = time.monotonic()
        current = incarnation()
        online = self.transport.online()
        stalled = max(state["waited"], time.monotonic() - started) > STALL
        if current != state["app"]:
            state.update(failures=0, app=current, retry_at=0)
            # The previous daemon's frontend earns the same grace as one that
            # served this daemon: it only needs time to find the address again.
            state["established"] = self.transport.expects(current)
            if state["established"]:
                log.info("Waiting for the previous daemon's Conductor frontend")
        if online:
            if state["established"] and not state["attached"]:
                log.info("Conductor native frontend adopted")
            state.update(
                failures=0, offline_since=None, silent_since=None, established=True, attached=True
            )
        else:
            now = time.monotonic()
            if state["silent_since"] is None:
                state["silent_since"] = now
            # Silence during a stall says nothing about the frontend, so the
            # grace only counts time the daemon was running to hear from it.
            if state["offline_since"] is None or stalled:
                state["offline_since"] = now
            # Only a frontend that was already serving earns the grace period;
            # a cold start or a fresh app still attaches on the first tick.
            settled = (
                not state["established"]
                or now - state["offline_since"] >= REATTACH_GRACE
                or now - state["silent_since"] >= SILENCE_LIMIT
            )
            if settled and self.due(state, current, Inspector):
                try:
                    attach(self.transport)
                    log.info("Conductor native frontend attached")
                    state.update(failures=0, attached=True)
                except Exception as exc:
                    state["failures"] += 1
                    state["attempted_at"] = time.monotonic()
                    state["retry_at"] = state["attempted_at"] + retry_delay(
                        Inspector, state["failures"]
                    )
                    log.warning("Conductor attachment pending: %s", exc, exc_info=True)
        # Keep observing process changes during backoff so a new app launch
        # or crash does not inherit the old frontend's retry delay.
        before = time.monotonic()
        self.stop.wait(1)
        state["waited"] = time.monotonic() - before

    def close(self):
        self.stop.set()
        self.worker.join(timeout=30)
