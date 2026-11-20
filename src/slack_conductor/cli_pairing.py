"""Enroll a locally generated client bearer by confirming a code in Slack."""

import json
import os
import secrets
import tempfile
from hashlib import sha256
from pathlib import Path

from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

from .cli_config import CLIError, load_config
from .config import remote_credentials, validate_server_url
from .pairing import PROFILE_NAME


def _write_private(path: Path, content: str):
    """Publish a complete private file without overwriting an existing enrollment."""
    pending = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as file:
            pending = Path(file.name)
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.link(pending, path)
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)


def _read_pending(path):
    if path.stat().st_mode & 0o077:
        raise CLIError("Pairing state must have permissions 600", "credential_error")
    return json.loads(path.read_text())


def _exchange(server_url, path, headers):
    endpoint = validate_server_url(server_url)._replace(path=path, query="").geturl()
    try:
        with connect(
            endpoint,
            additional_headers=headers,
            open_timeout=10,
            close_timeout=2,
            max_size=4096,
        ) as peer:
            result = json.loads(peer.recv(timeout=10))
    except (OSError, TimeoutError, WebSocketException, ValueError) as exc:
        raise CLIError("Could not reach the server's pairing endpoint", "pairing_failed") from exc
    if not isinstance(result, dict):
        raise CLIError("Invalid response from the pairing endpoint", "pairing_failed")
    return result


def begin_pairing(root: Path, args):
    pending = root / ".client-pairing.json"
    if (root / "config.toml").exists() or (root / "client.token").exists():
        raise CLIError(
            "This config directory already has client credentials. Use a different --root "
            "or run client connect --check to finish an existing pairing.",
            "config_exists",
        )
    url = args.server_url or args.server
    if args.server_url and args.server:
        raise CLIError("Provide the server URL once", "usage_error")
    if pending.exists():
        enrollment = _read_pending(pending)
        if url and _client_url(url) != enrollment["config"]["server_url"]:
            raise CLIError("A pairing with a different server is already pending", "config_exists")
        if any(
            value is not None and value != enrollment["config"].get(key)
            for key, value in {
                "profile": args.profile,
                "runtime_adapter": args.runtime_adapter,
                "engine": args.engine,
                "engine_permission_policy": args.engine_permission_policy,
                "repo": str(args.repo.expanduser().resolve()) if args.repo else None,
                "workspace_root": (
                    str(args.workspace_root.expanduser().resolve()) if args.workspace_root else None
                ),
                "state_dir": str(args.state_dir.expanduser().resolve()) if args.state_dir else None,
            }.items()
        ):
            raise CLIError("A pairing with different client settings is pending", "config_exists")
    else:
        if not url:
            raise CLIError("Provide the server URL", "usage_error")
        profile = args.profile or ("daytona" if args.runtime_adapter == "headless" else "mac")
        if not PROFILE_NAME.fullmatch(profile):
            raise CLIError(
                "Profile names must be 1–32 lowercase letters, digits, underscores or hyphens "
                "and start with a letter or digit",
                "usage_error",
            )
        repo = (args.repo or Path.cwd()).expanduser().resolve()
        if not repo.is_dir():
            raise CLIError(f"Repository directory does not exist: {repo}", "config_error")
        backend = args.runtime_adapter or "conductor"
        workspace_root = args.workspace_root or (
            Path.home() / "conductor" / "workspaces" / repo.name
            if backend == "conductor"
            else root / "workspaces"
        )
        config = {
            "mode": "client",
            "server_url": _client_url(url),
            "profile": profile,
            "runtime_adapter": backend,
            "repo": str(repo),
            "workspace_root": str(workspace_root.expanduser().resolve()),
            "state_dir": str((args.state_dir or root / "state").expanduser().resolve()),
            "engine": args.engine or "claude",
            "engine_permission_policy": args.engine_permission_policy or "read-only",
        }
        enrollment = {
            "config": config,
            "code": secrets.token_hex(16),
            "token": secrets.token_urlsafe(32),
        }
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            _write_private(pending, json.dumps(enrollment))
        except FileExistsError as exc:
            raise CLIError("Pairing is already pending in this directory", "config_exists") from exc

    config = enrollment["config"]
    result = _exchange(
        config["server_url"],
        "/pair",
        {
            "X-Flotilla-Pair-Code": enrollment["code"],
            "X-Flotilla-Token-SHA256": sha256(enrollment["token"].encode()).hexdigest(),
            "X-Flotilla-Profile": config["profile"],
            "X-Flotilla-Backend": config["runtime_adapter"],
        },
    )
    if result.get("ok") is not True:
        raise CLIError(
            "The server declined registration. Run client connect --check if you have sent "
            "the code to the bot.",
            "pairing_failed",
        )
    return {
        "paired": False,
        "slack_dm": "connect " + enrollment["code"],
        "expires_in_minutes": 10,
        "next_step": "Send the Slack DM to the bot, then run client connect --check.",
    }


def _client_url(value):
    url = validate_server_url(value)
    if url.path not in {"", "/", "/client"} or url.query:
        raise CLIError("Server URL must point to the server or its /client path", "usage_error")
    return url._replace(path="/client").geturl()


def finish_pairing(root: Path):
    pending = root / ".client-pairing.json"
    target = root / "config.toml"
    if target.is_file():
        config = load_config(root)
        if config.mode != "client":
            raise CLIError("The config directory is not a client", "config_error")
        token = remote_credentials(config)
        profile_id = _confirmed_profile(config.server_url, token)
        if pending.exists() and secrets.compare_digest(_read_pending(pending)["token"], token):
            pending.unlink(missing_ok=True)
    elif pending.exists():
        enrollment = _read_pending(pending)
        token = enrollment["token"]
        config = enrollment["config"].copy()
        profile_id = _confirmed_profile(config["server_url"], token)
        config.pop("profile")
        config["client_id"] = profile_id
        config["remote_token_file"] = "client.token"
        _publish(root / "client.token", token + "\n")
        _publish(target, "".join(f"{key} = {json.dumps(value)}\n" for key, value in config.items()))
        pending.unlink(missing_ok=True)
    else:
        raise CLIError(
            "No pending pairing. Run client connect with the server URL first.", "config_missing"
        )
    return {"paired": True, "profile_id": profile_id, "path": str(target)}


def _publish(path, content):
    try:
        _write_private(path, content)
    except FileExistsError:
        if path.stat().st_mode & 0o077 or path.read_text() != content:
            raise CLIError(f"A different config already exists: {path}", "config_exists") from None


def _confirmed_profile(server_url, token):
    result = _exchange(server_url, "/pair/check", {"Authorization": "Bearer " + token})
    profile_id = result.get("profile_id")
    if result.get("ok") is not True or not isinstance(profile_id, str):
        raise CLIError(
            "The server has not confirmed this pairing. Send the code to the bot and try again.",
            "pairing_unconfirmed",
        )
    return profile_id
