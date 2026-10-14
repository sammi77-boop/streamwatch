"""Settings come from environment variables (or a .env file next to the project). Secrets never go in code."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class Config:
    discord_token: str
    guild_id: int
    owner_id: int
    youtube_api_key: str
    default_tz: str
    db_path: str


def load_config() -> Config:
    _load_dotenv(ROOT / ".env")
    missing = [k for k in ("DISCORD_TOKEN", "GUILD_ID", "OWNER_ID", "YOUTUBE_API_KEY") if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"Missing settings in .env: {', '.join(missing)} (see .env.example)")
    return Config(
        discord_token=os.environ["DISCORD_TOKEN"],
        guild_id=int(os.environ["GUILD_ID"]),
        owner_id=int(os.environ["OWNER_ID"]),
        youtube_api_key=os.environ["YOUTUBE_API_KEY"],
        default_tz=os.environ.get("DEFAULT_STREAMER_TZ", "America/Los_Angeles"),
        db_path=os.environ.get("DB_PATH", str(ROOT / "streamwatch.db")),
    )
