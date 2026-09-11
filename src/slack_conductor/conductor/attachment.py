"""Brief Inspector bootstrap; all agent operations use native services afterward."""

import json
import logging
import threading
import time
from pathlib import Path

from .process import incarnation

log = logging.getLogger(__name__)


def attach(transport, state_dir):
    from .inspector import Inspector

    ax = Inspector(state_dir)
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
            if Inspector.draft(prompt).strip():
                raise RuntimeError("Inspector console has a draft; clear it to allow attachment")
            config = transport.attach(incarnation())
            transport.bootstrap = (
                Path(__file__)
                .with_name("frontend.js")
                .read_text()
                .replace("__CONFIG__", json.dumps(config))
            )
            url = config["base"] + "/bootstrap?generation=" + config["generation"]
            ax.set(prompt, "AXFocused", True)
            ax.type_text(
                "(()=>{const r=new XMLHttpRequest();r.open('GET',"
                + json.dumps(url)
                + ",false);r.send();if(r.status!==200)throw Error('Attachment expired');"
                "window.__flotilla?.detach();eval(r.responseText)})();"
            )
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

            ax.wait(ready, "Conductor native services did not become ready", 20)
        finally:
            if opened and ax.find(role="AXTextArea", description="Console prompt") is not None:
                ax.press(menu)


class Attachment:
    def __init__(self, transport, state_dir):
        self.transport, self.state_dir = transport, state_dir
        self.stop = threading.Event()
        self.worker = threading.Thread(target=self.run, daemon=True)

    def start(self):
        self.worker.start()

    def run(self):
        from .inspector import Inspector

        failures, last_app, retry_at = 0, None, 0
        while not self.stop.is_set():
            current = incarnation()
            if current != last_app:
                failures, last_app, retry_at = 0, current, 0
            if self.transport.online():
                failures = 0
            elif time.monotonic() >= retry_at and (
                not failures
                or (current and Inspector.focused_pid() == int(current.split(":", 1)[0]))
            ):
                try:
                    attach(self.transport, self.state_dir)
                    log.info("Conductor native frontend attached")
                    failures = 0
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
