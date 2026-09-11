import subprocess
import time
import uuid
from pathlib import Path
from urllib.parse import quote

from .ax import Accessibility, attr, walk
from .composer import prepare_prompt
from .db import ConductorDB
from .selectors import is_queued_prompt, model_identity, model_menu_label, model_picker_label

EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultracode")


class Conductor:
    def __init__(self, config):
        self.config = config
        self.db = ConductorDB(config.conductor_db)
        self.ax = Accessibility(config.state_dir)

    def validate(self, wid, sid=None):
        workspace = self.db.workspace(wid)
        path = Path(workspace.get("workspace_path") or "/").resolve()
        if not path.is_relative_to(self.config.workspace_root.resolve()):
            raise RuntimeError("Refusing workspace outside configured repository workspace root")
        if workspace["state"] == "archived":
            raise RuntimeError("workspace archived; use [new]")
        if sid and workspace["active_session_id"] != sid:
            raise RuntimeError(
                "Active Conductor session changed; select the original session or use [new]"
            )
        return workspace

    def navigate(self, wid, sid):
        self.validate(wid, sid)
        self.ax.press(self.ax.require(role="AXLink", url_suffix=f"/workspace/{wid}"))
        self.ax.wait(
            lambda: self.ax.find(role="AXGroup", description="composer"),
            "Conductor UI changed: composer",
        )
        # AX navigation is asynchronous; don't type into the previous workspace.
        self.ax.wait(
            lambda: any(
                f"/workspace/{wid}" in str(attr(e, "AXURL") or "")
                and attr(e, "AXRole") == "AXWebArea"
                for e in walk(self.ax.window())
            ),
            "Conductor workspace navigation not confirmed",
        )

    def confirm_target(self, wid):
        if not any(
            attr(e, "AXRole") == "AXWebArea" and f"/workspace/{wid}" in str(attr(e, "AXURL") or "")
            for e in walk(self.ax.window())
        ):
            raise RuntimeError("Conductor navigation changed during send; action stopped")

    def new_workspace(self):
        before = self.db.workspace_ids()
        marker = f"Slack workspace {uuid.uuid4()}"
        url = f"conductor://prompt={quote(marker, safe='')}&path={quote(str(self.config.repo), safe='')}"
        subprocess.run(["/usr/bin/open", url], check=True, timeout=5)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            rows = self.db.new_workspaces(before)
            candidates = [
                r
                for r in rows
                if r.get("workspace_path")
                and Path(r["workspace_path"])
                .resolve()
                .is_relative_to(self.config.workspace_root.resolve())
            ]
            if len(candidates) > 1:
                raise RuntimeError("Concurrent workspace creation; refusing ambiguous workspace")
            if (
                candidates
                and candidates[0]["active_session_id"]
                and candidates[0]["state"] == "ready"
            ):
                row = candidates[0]
                with self.ax.reachable():
                    self.navigate(row["id"], row["active_session_id"])
                    editor = self.ax.require(self.ax.composer(), role="AXTextArea")
                    self.ax.wait(
                        lambda editor=editor: marker in str(attr(editor, "AXValue") or ""),
                        "Workspace creation marker not confirmed",
                    )
                return row
            time.sleep(0.2)
        raise RuntimeError("workspace creation failed; no retry was attempted")

    def options(self, sid, options):
        warnings = []
        model = model_identity(options["model"])
        picker = self.ax.require(self.ax.composer(), role="AXPopUpButton", prefix="Change agent (")
        if model_identity(model_picker_label(attr(picker, "AXTitle"))) != model:
            self.ax.press(picker)
            time.sleep(0.15)

            def menu_item(label):
                return next(
                    (
                        e
                        for e in walk(self.ax.window())
                        if attr(e, "AXRole") == "AXMenuItem"
                        and model_identity(model_menu_label(attr(e, "AXTitle")))
                        == model_identity(label)
                    ),
                    None,
                )

            item = menu_item(model)
            if item is None:
                # Select current model to close this menu without changing it.
                current = model_picker_label(attr(picker, "AXTitle"))
                item = menu_item(current)
                warnings.append(f"requested model {model} unavailable")
            if item is not None:
                chosen = model_menu_label(attr(item, "AXTitle"))
                self.ax.press(item)
                self.ax.wait(
                    lambda: self.ax.find(role="AXMenuItem") is None,
                    "Conductor model menu did not close",
                )

                def selection_settled():
                    button = self.ax.find(
                        self.ax.composer(), role="AXPopUpButton", prefix="Change agent ("
                    )
                    return (
                        button is not None and model_picker_label(attr(button, "AXTitle")) == chosen
                    )

                try:
                    self.ax.wait(
                        selection_settled,
                        "Conductor model selection did not settle",
                        3,
                    )
                except RuntimeError:
                    warnings.append("model selection did not settle")
            else:
                raise RuntimeError("Conductor UI changed: cannot close model menu safely")
        for _ in range(8):
            session = self.db.session(sid)
            key = (
                "codex_thinking_level"
                if session["agent_type"] == "codex"
                else "claude_effort_level"
            )
            if session.get(key) == options["effort"]:
                break
            button = next(
                (
                    e
                    for e in walk(self.ax.composer())
                    if attr(e, "AXRole") == "AXButton"
                    and str(attr(e, "AXTitle") or "").lower() in EFFORTS
                ),
                None,
            )
            if button is None:
                break
            self.ax.press(button)
            time.sleep(0.15)
        session = self.db.session(sid)
        if bool(session["fast_mode"]) != bool(options["fast"]):
            button = self.ax.find(self.ax.composer(), role="AXButton", title="Fast")
            if button is not None:
                self.ax.press(button)
                time.sleep(0.15)
        actual = self.db.session(sid)
        actual_effort = actual.get(
            "codex_thinking_level" if actual["agent_type"] == "codex" else "claude_effort_level"
        )
        picker = self.ax.require(self.ax.composer(), role="AXPopUpButton", prefix="Change agent (")
        label = model_picker_label(attr(picker, "AXTitle"))
        if (
            model_identity(label) != model
            or actual_effort != options["effort"]
            or bool(actual["fast_mode"]) != bool(options["fast"])
        ):
            warnings.append(
                f"model: {label}, effort: {actual_effort}, fast: {bool(actual['fast_mode'])}"
            )
        return "; ".join(warnings)

    def send(self, wid, sid, prompt, options):
        with self.ax.reachable():
            self.navigate(wid, sid)
            warning = self.options(sid, options)
            prepare_prompt(
                self.ax,
                prompt,
                lambda: (self.validate(wid, sid), self.confirm_target(wid)),
                lambda editor: str(attr(editor, "AXValue") or ""),
            )
            busy = self.db.session(sid)["status"] == "working"
            self.validate(wid, sid)
            self.confirm_target(wid)
            editor = self.ax.require(self.ax.composer(), role="AXTextArea")
            if str(attr(editor, "AXValue") or "").rstrip("\n") != prompt.rstrip("\n"):
                raise RuntimeError("Conductor composer changed before send; action stopped")
            if busy and options["send_mode"] != self.db.send_mode():
                self.ax.cmd_enter(editor)
            else:
                self.ax.press(
                    self.ax.require(self.ax.composer(), role="AXButton", prefix="Send message")
                )
            self.ax.wait(
                lambda: (
                    self.db.pickup(sid, prompt)
                    or any(
                        any(
                            is_queued_prompt(attr(e, key), prompt) for key in ("AXValue", "AXTitle")
                        )
                        for e in walk(self.ax.window())
                    )
                ),
                "Send not confirmed; check Conductor before retrying",
                8,
            )
            return warning

    def archive(self, wid, sid):
        with self.ax.reachable():
            self.validate(wid, sid)
            link = self.ax.require(role="AXLink", url_suffix=f"/workspace/{wid}")
            self.ax.press(link, "AXShowMenu")
            item = self.ax.wait(
                lambda: self.ax.find(role="AXMenuItem", prefix="Archive"),
                "Conductor UI changed: Archive menu",
            )
            self.ax.press(item)
            confirm = self.ax.wait(
                lambda: (
                    self.ax.find(role="AXButton", prefix="Archive workspace")
                    or self.ax.find(role="AXButton", prefix="Stop agents and archive")
                ),
                "Conductor UI changed: archive confirmation",
            )
            self.ax.press(confirm)
            self.ax.wait(
                lambda: self.db.workspace(wid)["state"] == "archived", "Archive not confirmed", 10
            )
            return (self.db.workspace(wid).get("archive_commit") or "")[:10]
