"""Pure input parsing for slash commands, kept separate so it can be unit-tested without Discord."""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Optional

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
