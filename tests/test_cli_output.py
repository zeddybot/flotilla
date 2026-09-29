"""Readable CLI output keeps structured machine output available separately."""

import json

from slack_conductor import cli, cli_output


def test_health_and_monitor_show_checks_and_incidents():
    report = {
        "ok": False,
        "role": "client",
        "at": 0,
        "checks": {
            "remote_link": {"state": "warning", "detail": "disconnected", "age_seconds": 7},
            "native_attachment": {"state": "ok", "detail": "ready"},
        },
        "incidents": [{"check": "remote_link", "transition": "warning", "detail": "disconnected"}],
        "active_incidents": ["remote_link"],
    }
    status = cli_output.render(report, "status")
    assert status.startswith("Client status: unhealthy\nChecked: 1970-01-01 00:00:00 UTC")
    assert "Remote link: warning — disconnected" in status
    assert "Age seconds: 7" in status
    monitor = cli_output.render(report, "monitor")
    assert "Incidents: 1" in monitor
    assert "remote_link: warning — disconnected" in monitor
    assert "Active incidents: remote_link" in monitor
    assert not any(mark in status for mark in ('"checks"', "{", "}"))


def test_sessions_and_events_are_readable_and_nested():
    listed = cli_output.render(
        {"sessions": [{"session_id": "abc", "state": "running", "spec": {"model": "astra"}}]},
        "session",
        "list",
    )
    assert "Sessions (1):" in listed
    assert "- abc (running)" in listed
    assert "Model: astra" in listed
    assert cli_output.render({"sessions": []}, "session", "list") == "No sessions."
    shown = cli_output.render(
        {"session_id": "abc", "events": [{"type": "text"}]}, "session", "show"
    )
    assert "Session id: abc" in shown
    assert "-" in shown and "Type: text" in shown
    event = cli_output.render({"type": "text", "cursor": 4, "content": "hello"}, "session", "tail")
    assert event == "text (cursor 4)\n  Content: hello"
    assert cli_output.render({"events": []}, "session", "tail") == "No events."


def test_untrusted_text_cannot_control_terminal():
    shown = cli_output.render({"message": "hello\x1b[31m\rworld\nnext\tline"})
    assert "\x1b" not in shown and "\r" not in shown and "\t" not in shown
    assert "\\1b[31m" in shown and "\\0d" in shown and "\\0a" in shown
    error = cli_output.error("bad\x1b[2J", "command_error")
    assert error == "flotilla: bad\\1b[2J"


def test_cli_default_is_readable_and_json_shape_is_unchanged(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "discover_root", lambda *_: tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda *_: object())
    report = {
        "ok": False,
        "role": "client",
        "checks": {"link": {"state": "warning", "detail": "down"}},
    }
    monkeypatch.setattr(cli, "_report", lambda *_: report)
    assert cli.main(["status"]) == 0
    assert capsys.readouterr().out == "Client status: unhealthy\n  Link: warning — down\n"
    assert cli.main(["doctor"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Doctor found unhealthy components\nClient doctor: unhealthy" in captured.err
    assert '"checks"' not in captured.err
    assert cli.main(["doctor", "--json"]) == 1
    machine = json.loads(capsys.readouterr().out)
    assert machine == {
        "ok": False,
        "error": {
            "code": "unhealthy",
            "message": "Doctor found unhealthy components: " + json.dumps(report),
        },
    }
