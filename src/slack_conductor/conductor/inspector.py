import shutil
import subprocess
import time
from collections import deque
from contextlib import contextmanager

import ApplicationServices as AX
import Quartz

from .process import incarnation


def attr(element, name):
    err, value = AX.AXUIElementCopyAttributeValue(element, name, None)
    return value if err == 0 else None


def walk(root):
    todo = deque([(root, 0)])
    count = 0
    while todo and count < 12000:
        element, depth = todo.popleft()
        count += 1
        yield element
        if depth < 60:
            todo.extend((child, depth + 1) for child in (attr(element, "AXChildren") or []))


def matches(element, role=None, title=None, description=None):
    return (
        (role is None or attr(element, "AXRole") == role)
        and (title is None or attr(element, "AXTitle") == title)
        and (description is None or attr(element, "AXDescription") == description)
    )


class Inspector:
    def __init__(self):
        self.pid = None
        self.app = None

    @staticmethod
    def focused_pid():
        # A fresh short-lived process avoids AppKit's stale application cache in
        # the daemon. Reading NSWorkspace does not send Apple events to other apps.
        try:
            result = subprocess.run(
                [
                    "/usr/bin/osascript",
                    "-l",
                    "JavaScript",
                    "-e",
                    "ObjC.import('AppKit'); $.NSWorkspace.sharedWorkspace.frontmostApplication.processIdentifier;",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return None
        value = result.stdout.strip()
        return int(value) if result.returncode == 0 and value.isdigit() else None

    @staticmethod
    def unattended():
        # The lock key is absent, not false, while the screen is unlocked.
        session = Quartz.CGSessionCopyCurrentDictionary()
        return bool(
            session is None
            or session.get("CGSSessionScreenIsLocked")
            or not session.get("kCGSSessionOnConsoleKey")
            or Quartz.CGDisplayIsAsleep(Quartz.CGMainDisplayID())
        )

    @staticmethod
    def activate(pid, only_if=None, timeout=5):
        condition = (
            f"$.NSWorkspace.sharedWorkspace.frontmostApplication.processIdentifier == {int(only_if)}"
            if only_if
            else "true"
        )
        subprocess.run(
            [
                "/usr/bin/osascript",
                "-l",
                "JavaScript",
                "-e",
                (
                    "ObjC.import('AppKit'); "
                    f"if ({condition}) {{ const app = $.NSRunningApplication.runningApplicationWithProcessIdentifier({int(pid)}); "
                    "if (app) app.activateWithOptions(2); }"
                ),
            ],
            capture_output=True,
            timeout=timeout,
            check=False,
        )

    @staticmethod
    def draft(prompt):
        # Web Inspector's CodeMirror prompt exposes its text through children,
        # while AXValue itself can be empty even with an unsent command.
        return str(attr(prompt, "AXValue") or "") or "".join(
            str(attr(e, "AXValue") or "")
            for e in walk(prompt)
            if attr(e, "AXRole") == "AXStaticText"
        )

    def connect(self):
        if not AX.AXIsProcessTrusted():
            raise RuntimeError(
                "Accessibility permission required for the daemon Python interpreter"
            )
        current = incarnation()
        if not current:
            subprocess.run(["/usr/bin/open", "-a", "Conductor"], check=True, timeout=5)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not current:
                time.sleep(0.2)
                current = incarnation()
        if not current:
            raise RuntimeError("Conductor not reachable")
        self.pid = int(current.split(":", 1)[0])
        self.app = AX.AXUIElementCreateApplication(self.pid)

    def window(self):
        windows = attr(self.app, "AXWindows") or []
        return windows[0] if windows else attr(self.app, "AXMainWindow")

    @contextmanager
    def reachable(self):
        self.connect()
        switched = False
        original_space = target_space = None
        aerospace = shutil.which("aerospace")
        try:
            # AeroSpace can expose an AXWindow while parking it offscreen. Its
            # existence does not prove that Web Inspector can receive Return.
            if aerospace:
                result = subprocess.run(
                    [
                        aerospace,
                        "list-windows",
                        "--monitor",
                        "all",
                        "--app-bundle-id",
                        "com.conductor.app",
                        "--format",
                        "%{workspace}",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                spaces = result.stdout.splitlines() if result.returncode == 0 else []
                if spaces:
                    original_space = subprocess.check_output(
                        [aerospace, "list-workspaces", "--focused"], text=True, timeout=5
                    ).strip()
                    target_space = spaces[0]
                    if target_space != original_space:
                        subprocess.run(
                            [aerospace, "workspace", target_space], check=True, timeout=5
                        )
                        switched = True
            if self.window() is None:
                if not switched:
                    subprocess.run(["/usr/bin/open", "-a", "Conductor"], check=True, timeout=5)
                self.wait(lambda: self.window(), "Conductor not reachable", 5)
            yield
        finally:
            if switched and original_space:
                current_space = subprocess.check_output(
                    [aerospace, "list-workspaces", "--focused"], text=True, timeout=5
                ).strip()
                if current_space == target_space:
                    subprocess.run([aerospace, "workspace", original_space], timeout=5, check=False)

    def find(self, root=None, **selector):
        root = root if root is not None else self.window()
        if root is None:
            return None
        return next((e for e in walk(root) if matches(e, **selector)), None)

    def console_prompt(self):
        return self.find(role="AXTextArea", title="Console prompt")

    def console_tab(self):
        return self.find(role="AXRadioButton", title="Console")

    def close_console(self):
        devtools = self.find(role="AXWebArea", title="DevTools")
        if devtools is not None:
            close = self.find(root=devtools, role="AXButton", title="Close")
            if close is not None:
                self.press(close)

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
        raise RuntimeError(message() if callable(message) else message)

    def select_all(self):
        for down in (True, False):
            event = Quartz.CGEventCreateKeyboardEvent(None, 0, down)
            Quartz.CGEventSetFlags(event, Quartz.kCGEventFlagMaskCommand)
            Quartz.CGEventPostToPid(self.pid, event)

    def type_text(self, text):
        # A Unicode text event carries embedded newlines without pressing Return,
        # and targets Conductor directly without touching the system clipboard.
        for down in (True, False):
            event = Quartz.CGEventCreateKeyboardEvent(None, 0, down)
            Quartz.CGEventSetFlags(event, 0)
            if down:
                Quartz.CGEventKeyboardSetUnicodeString(
                    event, len(text.encode("utf-16-le")) // 2, text
                )
            Quartz.CGEventPostToPid(self.pid, event)
