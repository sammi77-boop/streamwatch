"""Pure input parsing for slash commands, kept separate so it can be unit-tested without Discord."""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CHANNEL_URL_EXAMPLE = "https://www.youtube.com/@handle  or  https://www.youtube.com/channel/UC…"

_URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.|m\.)?youtube\.com/"
    r"(?:(?P<handle>@[\w.\-]+)|channel/(?P<channel_id>UC[\w\-]{22}))"
    r"(?:[/?#].*)?$",
    re.IGNORECASE,
)


def parse_channel_url(text: str) -> Optional[dict]:
    """Return {'handle': '@x'} or {'channel_id': 'UC…'} for a valid channel URL, else None (F-1)."""
    match = _URL_RE.match(text.strip())
    if not match:
        return None
    if match.group("handle"):
        return {"handle": match.group("handle")}
    return {"channel_id": match.group("channel_id")}


def parse_duration(text: str) -> Optional[timedelta]:
    """Parse '30m', '12h', '3d', '1w' or combos like '1d12h'. Returns None if invalid."""
    text = text.strip().lower().replace(" ", "")
    parts = re.findall(r"(\d+)([mhdw])", text)
    if not parts or "".join(n + u for n, u in parts) != text:
        return None
    units = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
    delta = timedelta()
    for number, unit in parts:
        delta += timedelta(**{units[unit]: int(number)})
    return delta if delta > timedelta(0) else None


COMMON_TIMEZONES = [
    "America/Los_Angeles", "America/Denver", "America/Chicago", "America/New_York", "America/Toronto",
    "America/Sao_Paulo", "Europe/London", "Europe/Berlin", "Europe/Paris", "Asia/Tokyo", "Asia/Seoul",
    "Asia/Taipei", "Asia/Shanghai", "Asia/Singapore", "Asia/Manila", "Australia/Sydney", "Pacific/Auckland",
]


def suggest_timezones(text: str, limit: int = 25) -> list[str]:
    """Autocomplete for timezone options: common zones first, then any IANA zone containing the text."""
    from zoneinfo import available_timezones

    query = text.strip().lower().replace(" ", "_")
    if not query:
        return COMMON_TIMEZONES[:limit]
    common = [tz for tz in COMMON_TIMEZONES if query in tz.lower()]
    rest = sorted((tz for tz in available_timezones()
                   if "/" in tz and query in tz.lower() and tz not in common and not tz.startswith("Etc/")),
                  key=lambda tz: (not tz.lower().rsplit("/", 1)[-1].startswith(query), tz))
    return (common + rest)[:limit]


def valid_timezone(name: str) -> bool:
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False
