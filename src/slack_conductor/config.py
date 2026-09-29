import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv


@dataclass
class Config:
    mode: str = "client"
    runtime_adapter: str = "conductor"
    engine: str = "claude"
    claude_binary: str = "claude"
    codex_binary: str = "codex"
    engine_model: str = ""
    engine_effort: str = ""
    engine_permission_policy: str = "read-only"
    server_url: str = ""
    server_host: str = "127.0.0.1"
    server_port: int = 8765
    client_id: str = "mac"
    remote_token_file: Path | None = None
    repo: Path = Path("/Users/sameer/Documents/work/prodex/ttd")
    workspace_root: Path = Path.home() / "conductor/workspaces/ttd"
    conductor_db: Path = Path.home() / "Library/Application Support/com.conductor.app/conductor.db"
    state_dir: Path = Path.home() / "Library/Application Support/slack-conductor"
    model: str = "Fable 5.1"
    effort: str = "high"
    send_mode: str = "steer"
    models: dict = field(
        default_factory=lambda: {
            "opus": "Opus 5.5",
            "sonnet": "Sonnet 5 1M",
            "haiku": "Haiku 4.5",
            "fable": "Fable 5.1",
            "astra": "GPT-6 Astra",
            "sol": "GPT-6 Sol",
            "terra": "GPT-5.6 Terra",
            "luna": "GPT-6 Luna",
            "alpha": "Union Alpha Free",
        }
    )

    @classmethod
    def load(cls, root: Path):
        path = root / "config.toml"
        data = tomllib.loads(path.read_text()) if path.exists() else {}
        for key in ("repo", "workspace_root", "conductor_db", "state_dir", "remote_token_file"):
            if key in data:
                path_value = Path(data[key]).expanduser()
                if key == "remote_token_file" and not path_value.is_absolute():
                    path_value = root / path_value
                data[key] = path_value.resolve()
        cfg = cls(**data)
        if cfg.send_mode not in {"queue", "steer"}:
            raise ValueError("send_mode must be queue or steer")
        if cfg.mode not in {"server", "client"}:
            raise ValueError("mode must be server or client")
        if cfg.runtime_adapter not in {"conductor", "headless"}:
            raise ValueError("runtime_adapter must be conductor or headless")
        if cfg.engine not in {"claude", "codex"}:
            raise ValueError("engine must be claude or codex")
        if cfg.engine_permission_policy not in {"read-only", "workspace-write"}:
            raise ValueError("engine_permission_policy must be read-only or workspace-write")
        if not 1 <= cfg.server_port <= 65535:
            raise ValueError("server_port must be between 1 and 65535")
        if not cfg.client_id:
            raise ValueError("client_id must not be empty")
        cfg.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        return cfg


def remote_credentials(config: Config) -> str:
    """Read the private shared secret used by the remote server and Mac client."""
    if config.mode not in {"server", "client"}:
        raise ValueError("remote credentials require server or client mode")
    if config.mode == "client":
        url = urlsplit(config.server_url)
        localhost = url.hostname in {"localhost", "127.0.0.1", "::1"}
        if url.scheme != "wss" and not (url.scheme == "ws" and localhost):
            raise ValueError("server_url must use wss (ws is allowed only for localhost)")
        if not url.netloc or url.username or url.password or url.fragment:
            raise ValueError("server_url must be a websocket URL without credentials or fragment")
    if config.remote_token_file is None:
        raise RuntimeError("remote_token_file is required")
    path = config.remote_token_file
    if not path.is_file() or path.stat().st_mode & 0o077:
        raise RuntimeError("remote_token_file must exist with permissions 600")
    token = path.read_text().strip()
    if len(token) < 32:
        raise RuntimeError("remote_token_file must contain at least 32 characters")
    return token


def credentials(root: Path):
    env = root / ".env"
    if env.exists():
        if env.stat().st_mode & 0o077:
            raise RuntimeError(".env must have permissions 600 (chmod 600 .env)")
        load_dotenv(env, override=False)
    patterns = {
        "SLACK_BOT_TOKEN": r"xoxb-.+",
        "SLACK_APP_TOKEN": r"xapp-.+",
        "OWNER_SLACK_USER_ID": r"[UW][A-Z0-9]+",
        "SLACK_TEAM_ID": r"T[A-Z0-9]+",
    }
    values = {key: os.environ.get(key, "") for key in patterns}
    for key, pattern in patterns.items():
        if not re.fullmatch(pattern, values[key]):
            raise RuntimeError(f"Missing or invalid {key} in .env")
    return values
