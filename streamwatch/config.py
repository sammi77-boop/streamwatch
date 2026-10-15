"""Settings come from environment variables (or a .env file next to the project). Secrets never go in code."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

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
    db_path: str
    google_key_path: Path
    google_calendar_id: Optional[str]
    anthropic_api_key: Optional[str] = None
    images_dir: Path = ROOT / "schedule_images"
    image_reader: str = "gemini"  # "gemini" (Agent Platform, on the GCP credit) or "claude" (Anthropic credit)
    owner_tz: str = "America/Los_Angeles"  # for text that can't use Discord's local-time stamps (dropdown labels)
    websub_enabled: bool = False        # parked: see PRD §10 Notes. WEBSUB_ENABLED=1 turns instant alerts on
    websub_port: int = 8080
    websub_host: Optional[str] = None   # public IP/hostname for the push callback; found automatically on GCP


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
        db_path=os.environ.get("DB_PATH", str(ROOT / "streamwatch.db")),
        # Service account key; the bot's robot account (decision 10).
        google_key_path=Path(os.environ.get("GOOGLE_KEY_PATH", str(ROOT / "google_service_account.json"))),
        # C-4: the one calendar stream events go to, shared with the robot account.
        google_calendar_id=os.environ.get("GOOGLE_CALENDAR_ID") or None,
        # M4: schedule image reading. Without it, /drop explains how to turn it on.
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
        websub_enabled=os.environ.get("WEBSUB_ENABLED", "").lower() in ("1", "true", "yes"),
        owner_tz=os.environ.get("OWNER_TZ", "America/Los_Angeles"),
        image_reader=os.environ.get("IMAGE_READER", "gemini").lower(),
        websub_port=int(os.environ.get("WEBSUB_PORT", "8080")),
        websub_host=os.environ.get("WEBSUB_HOST") or None,
    )
