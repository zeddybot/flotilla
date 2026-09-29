"""Configuration discovery and validation for the Flotilla command line."""

import json
import os
import tempfile
import tomllib
from pathlib import Path

from .config import Config, remote_credentials


class CLIError(Exception):
    """A user-facing command failure with a stable machine-readable code."""

    def __init__(self, message: str, code: str = "command_error"):
        super().__init__(message)
        self.code = code


def discover_root(explicit: Path | None = None) -> Path:
    """Select a config directory without silently loading Config's personal defaults."""
    if explicit is not None:
        root = explicit.expanduser().resolve()
        if root.is_file():
            if root.name != "config.toml":
                raise CLIError("--root must name a config directory or config.toml", "config_error")
            return root.parent
        return root
    override = os.environ.get("FLOTILLA_CONFIG_DIR")
    if override:
        return Path(override).expanduser().resolve()
    candidates = (
        Path.cwd(),
        Path.home() / ".config/flotilla",
        Path.home() / "Library/Application Support/slack-conductor/runtime",
    )
    for root in candidates:
        if (root / "config.toml").is_file():
            return root.resolve()
    raise CLIError("No config.toml found; use --root or FLOTILLA_CONFIG_DIR", "config_missing")


def load_config(root: Path) -> Config:
    path = root / "config.toml"
    if not path.is_file():
        raise CLIError(f"Config file does not exist: {path}", "config_missing")
    try:
        tomllib.loads(path.read_text())
        config = Config.load(root)
    except (OSError, ValueError, TypeError, tomllib.TOMLDecodeError) as exc:
        raise CLIError(f"Invalid config: {exc}", "config_error") from exc
    return config


def validate_config(root: Path) -> dict:
    config = load_config(root)
    try:
        remote_credentials(config)
    except (OSError, RuntimeError, ValueError) as exc:
        raise CLIError(f"Invalid credentials: {exc}", "credential_error") from exc
    return {
        "valid": True,
        "credentials_valid": True,
        "path": str(root / "config.toml"),
        "mode": config.mode,
        "state_dir": str(config.state_dir),
    }


def init_config(root: Path, server_url: str, token_file: Path, repo: Path, workspace_root: Path):
    """Create a single-owner client config from explicit, already held credentials."""
    root = root.expanduser().resolve()
    token_file = token_file.expanduser().resolve()
    repo = repo.expanduser().resolve()
    workspace_root = workspace_root.expanduser().resolve()
    if not repo.is_dir():
        raise CLIError(f"Repository does not exist: {repo}", "config_error")
    candidate = Config(mode="client", server_url=server_url, remote_token_file=token_file)
    try:
        remote_credentials(candidate)
    except (OSError, RuntimeError, ValueError) as exc:
        raise CLIError(f"Invalid credentials: {exc}", "credential_error") from exc
    target = root / "config.toml"
    if target.exists():
        raise CLIError(f"Config already exists: {target}", "config_exists")
    if root.exists() and not root.is_dir():
        raise CLIError(f"Config root is not a directory: {root}", "config_error")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    values = {
        "mode": "client",
        "server_url": server_url,
        "remote_token_file": str(token_file),
        "repo": str(repo),
        "workspace_root": str(workspace_root),
    }
    content = "".join(f"{key} = {json.dumps(value)}\n" for key, value in values.items())
    pending = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=root, prefix=".config-", delete=False
        ) as file:
            pending = Path(file.name)
            pending.chmod(0o600)
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        try:
            os.link(pending, target)
        except FileExistsError as exc:
            raise CLIError(f"Config already exists: {target}", "config_exists") from exc
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)
    return {"created": True, "path": str(target), "mode": "client"}
