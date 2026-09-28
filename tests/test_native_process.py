import pytest

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


def test_stalled_process_probe_keeps_live_identity_and_backs_off(monkeypatch):
    monkeypatch.setattr(process, "_cached", "22:Thu Sep 10 21:53:00 2026")
    monkeypatch.setattr(process, "_checked", 0)
    monkeypatch.setattr(process.time, "monotonic", lambda: 100)
    monkeypatch.setattr(process.os, "kill", lambda *_: None)
    probes = []

    def stall(args, **kwargs):
        probes.append(args)
        raise process.subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(process.subprocess, "check_output", stall)
    assert process.incarnation() == "22:Thu Sep 10 21:53:00 2026"
    assert process.incarnation() == "22:Thu Sep 10 21:53:00 2026"
    assert len(probes) == 1


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


def test_console_search_reaches_prompt_before_large_log(monkeypatch):
    module = inspector_module(monkeypatch)
    prompt = {"AXRole": "AXTextArea", "AXDescription": "Console prompt"}
    rows = [{"AXChildren": [{"AXRole": "AXStaticText"}]} for _ in range(7000)]
    root = {"AXChildren": [{"AXChildren": rows}, {"AXChildren": [prompt]}]}
    monkeypatch.setattr(module, "attr", lambda node, key: node.get(key))
    ax = module.Inspector()
    assert ax.find(root, role="AXTextArea", description="Console prompt") is prompt
    prompt["AXChildren"] = [{"AXRole": "AXStaticText", "AXValue": "unsent command"}]
    assert module.Inspector.draft(prompt) == "unsent command"


def test_offscreen_ax_window_still_switches_and_restores_aerospace(monkeypatch):
    from types import SimpleNamespace

    module = inspector_module(monkeypatch)
    ax = module.Inspector()
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


def test_new_app_does_not_inherit_failed_attachment_backoff(monkeypatch):
    import sys
    from types import SimpleNamespace

    from slack_conductor.conductor import attachment

    now, attempts = [0], []
    monkeypatch.setitem(
        sys.modules,
        "slack_conductor.conductor.inspector",
        SimpleNamespace(
            Inspector=SimpleNamespace(focused_pid=lambda: 999, unattended=lambda: False)
        ),
    )
    monkeypatch.setattr(attachment.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(attachment, "incarnation", lambda: "1:old" if now[0] == 0 else "2:new")

    def fail(*args):
        attempts.append(now[0])
        raise RuntimeError("not ready")

    monkeypatch.setattr(attachment, "attach", fail)
    worker = attachment.Attachment(SimpleNamespace(online=lambda: False, expects=lambda _: False))
    worker.stop = SimpleNamespace(
        is_set=lambda: now[0] >= 3,
        wait=lambda seconds: now.__setitem__(0, now[0] + seconds),
    )
    worker.run()
    assert attempts == [0, 1]


def attachment_attempts(monkeypatch, online, unattended=lambda t: False, inherited=False):
    import sys
    from types import SimpleNamespace

    from slack_conductor.conductor import attachment

    now, attempts = [0], []
    monkeypatch.setitem(
        sys.modules,
        "slack_conductor.conductor.inspector",
        SimpleNamespace(
            Inspector=SimpleNamespace(focused_pid=lambda: 1, unattended=lambda: unattended(now[0]))
        ),
    )
    monkeypatch.setattr(attachment.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(attachment, "incarnation", lambda: "1:app")
    monkeypatch.setattr(attachment, "attach", lambda *args: attempts.append(now[0]))
    worker = attachment.Attachment(
        SimpleNamespace(online=lambda: online(now[0]), expects=lambda _: inherited)
    )
    worker.stop = SimpleNamespace(
        is_set=lambda: now[0] >= 10,
        wait=lambda seconds: now.__setitem__(0, now[0] + seconds),
    )
    worker.run()
    return attempts


def test_wedged_frontend_reattaches_only_after_the_grace_period(monkeypatch):
    from slack_conductor.conductor import attachment

    # Re-attaching before the frontend releases itself foregrounds Conductor to
    # replace a frontend that was only stalled: the focus-stealing loop this guards.
    assert attachment.REATTACH_GRACE >= attachment.FRONTEND_RELEASE
    monkeypatch.setattr(attachment, "REATTACH_GRACE", 3)
    # Serving until t=1, then silent: a stalled machine freezes the frontend and
    # the daemon together, so foregrounding the app has to wait out the recovery.
    assert attachment_attempts(monkeypatch, lambda t: t < 1)[0] == 4


def test_frontend_that_never_served_attaches_without_waiting(monkeypatch):
    assert attachment_attempts(monkeypatch, lambda t: False)[0] == 0


def test_restarted_daemon_adopts_the_frontend_it_inherited(monkeypatch, caplog):
    import logging

    from slack_conductor.conductor import attachment

    caplog.set_level(logging.INFO)
    monkeypatch.setattr(attachment, "REATTACH_GRACE", 5)
    # The frontend finds the daemon again two seconds in: no bootstrap at all.
    assert attachment_attempts(monkeypatch, lambda t: t >= 2, inherited=True) == []
    assert "Conductor native frontend adopted" in caplog.text
    # A frontend that died with its page is replaced once the grace has run out.
    assert attachment_attempts(monkeypatch, lambda t: False, inherited=True)[0] == 5


def test_locked_screen_defers_attachment_without_spending_a_retry(monkeypatch, caplog):
    import logging

    caplog.set_level(logging.INFO)
    attempts = attachment_attempts(monkeypatch, lambda t: False, unattended=lambda t: t < 4)
    assert attempts[0] == 4
    assert caplog.text.count("deferred until the screen is unlocked") == 1


def test_lock_state_is_read_from_the_login_session(monkeypatch):
    from types import SimpleNamespace

    module = inspector_module(monkeypatch)
    session = {"kCGSSessionOnConsoleKey": True}
    asleep = [0]
    monkeypatch.setattr(
        module,
        "Quartz",
        SimpleNamespace(
            CGSessionCopyCurrentDictionary=lambda: session,
            CGMainDisplayID=lambda: 1,
            CGDisplayIsAsleep=lambda _: asleep[0],
        ),
    )
    assert not module.Inspector.unattended()
    asleep[0] = 1
    assert module.Inspector.unattended()
    asleep[0] = 0
    session["CGSSessionScreenIsLocked"] = True
    assert module.Inspector.unattended()
    session = {"kCGSSessionOnConsoleKey": False}
    assert module.Inspector.unattended()
    session = None
    assert module.Inspector.unattended()


def test_failed_bootstrap_reports_how_far_it_got(tmp_path, monkeypatch):
    from http.client import HTTPConnection
    from urllib.parse import urlparse

    from slack_conductor.conductor import attachment
    from slack_conductor.conductor.transport import Transport

    module = inspector_module(monkeypatch)
    monkeypatch.setattr(module.time, "monotonic", iter([0, 0, 1]).__next__)
    with pytest.raises(RuntimeError, match="^computed late$"):
        module.Inspector.wait(lambda: False, lambda: "computed late", 1)

    transport = Transport(tmp_path, lambda _: True)
    transport.start()
    try:
        config = transport.attach("app")
        transport.bootstrap = "void 0"
        assert "never ran the bootstrap snippet" in attachment.bootstrap_progress(transport)
        url = urlparse(config["base"])
        connection = HTTPConnection(url.hostname, url.port, timeout=2)
        try:
            connection.request("GET", url.path + "/bootstrap?generation=" + config["generation"])
            assert connection.getresponse().read() == b"void 0"
        finally:
            connection.close()
        assert "never reported back" in attachment.bootstrap_progress(transport)
        transport.heartbeat = {"ready": False, "stage": "bootstrap"}
        assert "did not initialize" in attachment.bootstrap_progress(transport)
        transport.heartbeat = {"ready": True}
        assert "heartbeats stopped" in attachment.bootstrap_progress(transport)
        transport.attach("app")
        assert "never ran the bootstrap snippet" in attachment.bootstrap_progress(transport)
    finally:
        transport.close()


def test_own_bootstrap_draft_is_reclaimed_but_typed_input_is_not():
    from slack_conductor.conductor import attachment

    snippet = attachment.bootstrap_snippet("http://127.0.0.1:1/t/bootstrap?generation=g")
    assert attachment.own_draft(snippet)
    # Web Inspector reports the prompt as highlighted spans joined without spacing.
    assert attachment.own_draft(snippet.replace("const r", "const  r"))
    assert not attachment.own_draft("await workspace.getWorkspaces()")
    assert not attachment.own_draft("")


def test_release_window_reaches_the_frontend_under_the_name_it_reads():
    from pathlib import Path

    from slack_conductor.conductor import attachment

    source = Path(attachment.__file__).with_name("frontend.js").read_text()
    # Renaming either side leaves config.release undefined, which compares false and
    # would keep a frontend the daemon abandoned patched over the composer forever.
    assert "config.release" in source
    assert attachment.frontend_config({})["release"] == attachment.FRONTEND_RELEASE * 1000
