import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


@dataclass
class Config:
    repo: Path = Path("/Users/sameer/Documents/work/prodex/ttd")
    workspace_root: Path = Path.home() / "conductor/workspaces/ttd"
    conductor_db: Path = Path.home() / "Library/Application Support/com.conductor.app/conductor.db"
    state_dir: Path = Path.home() / "Library/Application Support/slack-conductor"
    model: str = "Fable 5.1"
    effort: str = "high"
    send_mode: str = "steer"
    models: dict = field(
        default_factory=lambda: {
            "opus": "Opus 5",
            "sonnet": "Sonnet 5 1M",
            "haiku": "Haiku 4.5",
            "fable": "Fable 5.1",
            "astra": "GPT-6 Astra",
            "sol": "5.6 Sol",
            "terra": "5.6 Terra",
            "luna": "5.6 Luna",
        }
    )

    @classmethod
    def load(cls, root: Path):
        path = root / "config.toml"
        data = tomllib.loads(path.read_text()) if path.exists() else {}
        for key in ("repo", "workspace_root", "conductor_db", "state_dir"):
            if key in data:
                data[key] = Path(data[key]).expanduser().resolve()
        cfg = cls(**data)
        if cfg.send_mode not in {"queue", "steer"}:
            raise ValueError("send_mode must be queue or steer")
        cfg.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        return cfg


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
