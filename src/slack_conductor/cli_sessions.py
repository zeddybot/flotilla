"""Session commands through the local, versioned runtime adapter."""

import time
import uuid
from pathlib import Path

from .cli_config import CLIError


def runtime_for_cli(config):
    """Construct the native runtime only when a session command needs it."""
    from .runtime_adapter import local_runtime

    return local_runtime(config)


def _client(config):
    if config.mode != "client":
        raise CLIError("Session commands require a client configuration", "wrong_mode")
    return runtime_for_cli(config)


def _id():
    return str(uuid.uuid4())


def _derived(command_id, kind):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"flotilla-cli:{command_id}:{kind}"))


def _capability(runtime, name):
    probe = runtime.probe()
    if not probe.get("capabilities", {}).get(name, False):
        raise CLIError(f"This client does not support session {name}", "unsupported_capability")


def list_sessions(config):
    return {"sessions": _client(config).sessions()}


def show_session(config, session_id):
    return _client(config).describe(session_id)


def _options(config, send_mode=None):
    return {"send_mode": send_mode or config.send_mode}


def new_session(config, prompt, repo=None, send_mode=None, command_id=None):
    runtime = _client(config)
    selected_repo = Path(repo).expanduser().resolve() if repo else config.repo.resolve()
    if selected_repo != config.repo.resolve():
        raise CLIError("Repository must match config.toml", "invalid_repository")
    create_id = command_id or _id()
    created = runtime.create(
        create_id,
        {"repo": str(selected_repo), "model": config.model, "effort": config.effort, "fast": False},
    )
    submit_id = _derived(create_id, "submit")
    sent = runtime.submit(
        submit_id,
        created["session_id"],
        _derived(create_id, "message"),
        _derived(create_id, "run"),
        prompt,
        _options(config, send_mode),
    )
    return {**created, "delivery": sent}


def send(config, session_id, prompt, send_mode=None, command_id=None):
    runtime = _client(config)
    runtime.describe(session_id)
    submit_id = command_id or _id()
    sent = runtime.submit(
        submit_id,
        session_id,
        _derived(submit_id, "message"),
        _derived(submit_id, "run"),
        prompt,
        _options(config, send_mode),
    )
    return {"session_id": session_id, "delivery": sent}


def tail(config, session_id, *, follow=False, after=0):
    runtime = _client(config)
    runtime.describe(session_id)
    cursor = after
    while True:
        observation = runtime.observe(session_id, after=cursor)
        yield from observation.get("events", [])
        next_cursor = observation.get("cursor", cursor)
        if next_cursor is not None:
            cursor = next_cursor
        if not follow:
            return
        time.sleep(1)


def stop(config, session_id, command_id=None):
    runtime = _client(config)
    runtime.describe(session_id)
    _capability(runtime, "interrupt")
    return runtime.interrupt(command_id or _id(), session_id)


def open_session(config, session_id, command_id=None):
    runtime = _client(config)
    runtime.describe(session_id)
    _capability(runtime, "reveal")
    return runtime.reveal(command_id or _id(), session_id)


def archive(config, session_id, command_id=None):
    runtime = _client(config)
    runtime.describe(session_id)
    return runtime.close_session(command_id or _id(), session_id)
