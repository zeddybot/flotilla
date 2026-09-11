from slack_conductor.conductor import process


def test_process_probe_refreshes_after_app_exits_without_appkit_notifications(monkeypatch):
    monkeypatch.setattr(process, "_cached", "11:old")
    monkeypatch.setattr(process, "_checked", 99)
    monkeypatch.setattr(process.time, "monotonic", lambda: 100)

    def kill(pid, signal):
        if pid == 11:
            raise ProcessLookupError()

    monkeypatch.setattr(process.os, "kill", kill)
    commands = []

    def output(args, **kwargs):
        commands.append(args)
        return " 22 Thu Sep 10 21:53:00 2026 /Applications/Conductor.app/Contents/MacOS/conductor\n"

    monkeypatch.setattr(process.subprocess, "check_output", output)
    assert process.incarnation() == "22:Thu Sep 10 21:53:00 2026"
    assert commands == [["/bin/ps", "-axo", "pid=,lstart=,comm="]]


def test_process_identity_changes_when_pid_is_reused(monkeypatch):
    monkeypatch.setattr(process, "_cached", "22:old")
    monkeypatch.setattr(process, "_checked", 0)
    monkeypatch.setattr(process.os, "kill", lambda *_: None)
    monkeypatch.setattr(
        process.subprocess,
        "check_output",
        lambda *a, **kw: (
            "22 Fri Sep 11 01:00:00 2026 /Applications/Conductor.app/Contents/MacOS/conductor\n"
        ),
    )
    assert process.incarnation() == "22:Fri Sep 11 01:00:00 2026"


def inspector_module(monkeypatch):
    import importlib.util
    import sys
    from pathlib import Path
    from types import SimpleNamespace

    for name in ("ApplicationServices", "Quartz"):
        monkeypatch.setitem(sys.modules, name, SimpleNamespace())
    spec = importlib.util.spec_from_file_location(
        "slack_conductor.conductor._test_inspector",
        Path(process.__file__).with_name("inspector.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_console_search_reaches_prompt_before_large_log(monkeypatch, tmp_path):
    module = inspector_module(monkeypatch)
    prompt = {"AXRole": "AXTextArea", "AXDescription": "Console prompt"}
    rows = [{"AXChildren": [{"AXRole": "AXStaticText"}]} for _ in range(7000)]
    root = {"AXChildren": [{"AXChildren": rows}, {"AXChildren": [prompt]}]}
    monkeypatch.setattr(module, "attr", lambda node, key: node.get(key))
    ax = module.Inspector(tmp_path)
    assert ax.find(root, role="AXTextArea", description="Console prompt") is prompt
    prompt["AXChildren"] = [{"AXRole": "AXStaticText", "AXValue": "unsent command"}]
    assert module.Inspector.draft(prompt) == "unsent command"


def test_offscreen_ax_window_still_switches_and_restores_aerospace(monkeypatch, tmp_path):
    from types import SimpleNamespace

    module = inspector_module(monkeypatch)
    ax = module.Inspector(tmp_path)
    monkeypatch.setattr(ax, "connect", lambda: None)
    monkeypatch.setattr(ax, "window", lambda: object())
    monkeypatch.setattr(module.shutil, "which", lambda _: "aerospace")
    commands, focused = [], ["5"]

    def run(args, **kwargs):
        commands.append(args)
        if args[1] == "workspace":
            focused[0] = args[2]
        return SimpleNamespace(returncode=0, stdout="1\n")

    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(module.subprocess, "check_output", lambda *a, **kw: focused[0])
    with ax.reachable():
        assert focused[0] == "1"
    assert focused[0] == "5"
    assert commands[0][1:4] == ["list-windows", "--monitor", "all"]


def test_new_app_does_not_inherit_failed_attachment_backoff(monkeypatch, tmp_path):
    import sys
    from types import SimpleNamespace

    from slack_conductor.conductor import attachment

    now, attempts = [0], []
    monkeypatch.setitem(
        sys.modules,
        "slack_conductor.conductor.inspector",
        SimpleNamespace(Inspector=SimpleNamespace(focused_pid=lambda: 999)),
    )
    monkeypatch.setattr(attachment.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(attachment, "incarnation", lambda: "1:old" if now[0] == 0 else "2:new")

    def fail(*args):
        attempts.append(now[0])
        raise RuntimeError("not ready")

    monkeypatch.setattr(attachment, "attach", fail)
    worker = attachment.Attachment(SimpleNamespace(online=lambda: False), tmp_path)
    worker.stop = SimpleNamespace(
        is_set=lambda: now[0] >= 3,
        wait=lambda seconds: now.__setitem__(0, now[0] + seconds),
    )
    worker.run()
    assert attempts == [0, 1]
