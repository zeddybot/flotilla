"""Deterministic supervisor checks without launching a client or sleeping."""

import importlib.util
import signal
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from slack_conductor import config as config_module


def _entrypoint():
    path = Path(__file__).resolve().parents[1] / "scripts/worker_entrypoint.py"
    spec = importlib.util.spec_from_file_location("worker_entrypoint", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Event:
    def __init__(self):
        self.stopped = False
        self.on_wait = None
        self.waits = []

    def set(self):
        self.stopped = True

    def is_set(self):
        return self.stopped

    def wait(self, seconds):
        self.waits.append(seconds)
        if self.on_wait:
            self.on_wait(seconds)
        return self.stopped


class Child:
    def __init__(self, *, timeout=False):
        self.running = True
        self.timeout = timeout
        self.terminated = 0
        self.killed = 0
        self.waits = []

    def poll(self):
        return None if self.running else 0

    def terminate(self):
        self.terminated += 1

    def kill(self):
        self.killed += 1
        self.running = False

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if timeout and self.timeout:
            raise subprocess.TimeoutExpired("fake-client", timeout)
        self.running = False
        return 0


def _configure(monkeypatch, module, root, event):
    handlers = {}
    monkeypatch.setenv("FLOTILLA_WORKER_CONFIG_ROOT", str(root))
    monkeypatch.setattr(module.threading, "Event", lambda: event)
    monkeypatch.setattr(
        module.signal, "signal", lambda number, handler: handlers.setdefault(number, handler)
    )
    monkeypatch.setattr(module.os, "umask", lambda mask: None)
    monkeypatch.setattr(
        config_module.Config,
        "load",
        classmethod(lambda cls, path: SimpleNamespace(mode="client", runtime_adapter="headless")),
    )
    validated = []
    monkeypatch.setattr(
        config_module, "remote_credentials", lambda config: validated.append(config)
    )
    return handlers, validated


def test_waits_for_config_without_starting_client(tmp_path, monkeypatch):
    module = _entrypoint()
    event = Event()
    event.on_wait = lambda _: event.set()
    _configure(monkeypatch, module, tmp_path, event)
    monkeypatch.setattr(module.subprocess, "Popen", lambda *_: pytest.fail("client started"))
    assert module.main() == 0
    assert event.waits == [1]


def test_signal_gracefully_stops_running_client(tmp_path, monkeypatch):
    module = _entrypoint()
    (tmp_path / "config.toml").touch()
    event = Event()
    handlers, validated = _configure(monkeypatch, module, tmp_path, event)
    child = Child()
    launches = []
    monkeypatch.setattr(module.subprocess, "Popen", lambda argv: launches.append(argv) or child)
    event.on_wait = lambda _: handlers[signal.SIGTERM](signal.SIGTERM, None)
    assert module.main() == 0
    assert len(validated) == 1
    assert launches == [
        [module.sys.executable, "-m", "slack_conductor", "--root", str(tmp_path), "client"]
    ]
    assert child.terminated == 1 and child.killed == 0
    assert child.waits == [35]


def test_hold_forces_stuck_client_then_prevents_restart(tmp_path, monkeypatch):
    module = _entrypoint()
    (tmp_path / "config.toml").touch()
    event = Event()
    _configure(monkeypatch, module, tmp_path, event)
    child = Child(timeout=True)
    launches = []
    monkeypatch.setattr(module.subprocess, "Popen", lambda argv: launches.append(argv) or child)

    def on_wait(seconds):
        if len(event.waits) == 1:
            (tmp_path / "HOLD").touch()
        else:
            event.set()

    event.on_wait = on_wait
    assert module.main() == 0
    assert len(launches) == 1
    assert child.terminated == 1 and child.killed == 1
    assert child.waits == [35, None]


def test_rejects_non_headless_client_config(tmp_path, monkeypatch):
    module = _entrypoint()
    (tmp_path / "config.toml").touch()
    event = Event()
    _configure(monkeypatch, module, tmp_path, event)
    monkeypatch.setattr(
        config_module.Config,
        "load",
        classmethod(lambda cls, path: SimpleNamespace(mode="server", runtime_adapter="conductor")),
    )
    with pytest.raises(ValueError, match="mode=client"):
        module.main()
