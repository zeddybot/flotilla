import json
import logging
import shutil
import subprocess
import time
from contextlib import contextmanager

import ApplicationServices as AX
import Quartz
from AppKit import NSWorkspace

log = logging.getLogger(__name__)


def attr(element, name):
    err, value = AX.AXUIElementCopyAttributeValue(element, name, None)
    return value if err == 0 else None


def walk(root):
    todo = [(root, 0)]
    count = 0
    while todo and count < 12000:
        element, depth = todo.pop()
        count += 1
        yield element
        if depth < 60:
            todo.extend((child, depth + 1) for child in reversed(attr(element, "AXChildren") or []))


def matches(element, role=None, title=None, prefix=None, description=None, url_suffix=None):
    return (
        (role is None or attr(element, "AXRole") == role)
        and (title is None or attr(element, "AXTitle") == title)
        and (prefix is None or str(attr(element, "AXTitle") or "").startswith(prefix))
        and (description is None or attr(element, "AXDescription") == description)
        and (url_suffix is None or str(attr(element, "AXURL") or "").endswith(url_suffix))
    )


class Accessibility:
    def __init__(self, state_dir):
        self.state_dir = state_dir
        self.pid = None
        self.app = None

    def connect(self):
        if not AX.AXIsProcessTrusted():
            raise RuntimeError(
                "Accessibility permission required for the daemon Python interpreter"
            )
        apps = [
            app
            for app in NSWorkspace.sharedWorkspace().runningApplications()
            if app.bundleIdentifier() == "com.conductor.app"
        ]
        if not apps:
            subprocess.run(["/usr/bin/open", "-a", "Conductor"], check=True, timeout=5)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not apps:
                time.sleep(0.2)
                apps = [
                    app
                    for app in NSWorkspace.sharedWorkspace().runningApplications()
                    if app.bundleIdentifier() == "com.conductor.app"
                ]
        if not apps:
            raise RuntimeError("Conductor not reachable")
        self.pid = apps[0].processIdentifier()
        self.app = AX.AXUIElementCreateApplication(self.pid)

    def window(self):
        windows = attr(self.app, "AXWindows") or []
        return windows[0] if windows else attr(self.app, "AXMainWindow")

    @contextmanager
    def reachable(self):
        self.connect()
        switched = False
        aerospace = shutil.which("aerospace")
        try:
            if self.window() is None:
                if aerospace:
                    result = subprocess.run(
                        [
                            aerospace,
                            "list-windows",
                            "--app-bundle-id",
                            "com.conductor.app",
                            "--format",
                            "%{workspace}",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=5,
                        check=True,
                    )
                    spaces = result.stdout.splitlines()
                    if spaces:
                        subprocess.run([aerospace, "workspace", spaces[0]], check=True, timeout=5)
                        switched = True
                if not switched:
                    subprocess.run(["/usr/bin/open", "-a", "Conductor"], check=True, timeout=5)
                self.wait(lambda: self.window(), "Conductor not reachable", 5)
            yield
        finally:
            if switched:
                subprocess.run([aerospace, "workspace-back-and-forth"], timeout=5, check=False)

    def find(self, root=None, **selector):
        root = root if root is not None else self.window()
        if root is None:
            return None
        return next((e for e in walk(root) if matches(e, **selector)), None)

    def require(self, root=None, **selector):
        found = self.find(root, **selector)
        if found is None:
            self.snapshot()
            raise RuntimeError(f"Conductor UI changed: {selector}")
        return found

    def snapshot(self):
        # Omit AXValue: it contains chat text and potentially secrets.
        rows = (
            [
                {
                    key: str(attr(e, key) or "")[:200]
                    for key in ("AXRole", "AXTitle", "AXDescription", "AXURL")
                }
                for e in walk(self.window())
            ]
            if self.window() is not None
            else []
        )
        path = self.state_dir / "last-ax-tree.json"
        path.write_text(json.dumps(rows, indent=2))
        path.chmod(0o600)
        log.error("Accessibility selector snapshot: %s", path)

    @staticmethod
    def press(element, action="AXPress"):
        error = AX.AXUIElementPerformAction(element, action)
        if error:
            raise RuntimeError(f"Accessibility action {action} failed ({error})")

    @staticmethod
    def set(element, name, value):
        error = AX.AXUIElementSetAttributeValue(element, name, value)
        if error:
            raise RuntimeError(f"Accessibility set {name} failed ({error})")

    @staticmethod
    def wait(predicate, message, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.15)
        raise RuntimeError(message)

    def cmd_enter(self, editor):
        self.set(editor, "AXFocused", True)
        for down in (True, False):
            event = Quartz.CGEventCreateKeyboardEvent(None, 36, down)
            Quartz.CGEventSetFlags(event, Quartz.kCGEventFlagMaskCommand)
            Quartz.CGEventPostToPid(self.pid, event)

    def composer(self):
        return self.require(role="AXGroup", description="composer")

    def check(self):
        with self.reachable():
            composer = self.composer()
            self.require(composer, role="AXTextArea")
            self.require(composer, role="AXPopUpButton", prefix="Change agent (")
            # The Send button is absent until the composer contains text.
            self.require(role="AXLink", url_suffix="")
