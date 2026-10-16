"""M5: the weekly digest (decision 2: Sunday 20:00 local, covering the coming Monday–Sunday)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import discord

from .collab import short_channel_name
from .db import Entry

PURPLE = discord.Color.from_rgb(155, 89, 182)
GREY = discord.Color.from_rgb(128, 132, 142)


@dataclass
class DigestLine:
    entry: Entry
    streamer_name: str
    partner_names: list[str]
    jump_url: Optional[str]     # the stream's card or schedule table, where it can be added to the calendar


def next_week(now_local: datetime) -> tuple[datetime, datetime]:
    """The Monday 00:00 → next Monday 00:00 window after now_local (in now_local's timezone)."""
    monday = now_local.date() + timedelta(days=7 - now_local.weekday())
    start = datetime.combine(monday, time.min, tzinfo=now_local.tzinfo)
    return start, start + timedelta(days=7)


def digest_due(now_local: datetime) -> Optional[date]:
    """The Monday whose week should be sent now, or None. Sunday 20:00 onward; Monday morning catches a missed run."""
    if now_local.weekday() == 6 and now_local.hour >= 20:
        return now_local.date() + timedelta(days=1)
    if now_local.weekday() == 0 and now_local.hour < 12:
        return now_local.date()
    return None


def _line(item: DigestLine) -> str:
    e = item.entry
    text = f"**{discord.utils.format_dt(e.start_utc, 't')}** · {short_channel_name(item.streamer_name)} · **{e.title}**"
    if e.subtitle:
        text += f" · *{e.subtitle}*"
    if e.is_collab:
        text += " 🤝 " + (f"*with {', '.join(item.partner_names)}*" if item.partner_names else "*collab*")
    if e.picked:
        text += " 💜"
    if e.tentative:
        text += " ⏳"
    if item.jump_url:
        text += f" [↗]({item.jump_url})"
    return text


def build_digest(items: list[DigestLine], start: datetime, end: datetime, heading: str) -> dict:
    """Message kwargs: a summary line plus one embed per day that has streams (Discord allows 10 embeds)."""
    tz = start.tzinfo
    items = sorted(items, key=lambda i: i.entry.start_utc)
    picked = sum(i.entry.picked for i in items)
    collabs = sum(i.entry.is_collab for i in items)
    span = f"{start:%a %d %b} – {(end - timedelta(seconds=1)):%a %d %b}"
    if not items:
        return {"content": f"📅 **{heading}** ({span}): no streams listed yet. Waiting rooms usually appear closer "
                           "to the day, and new cards keep arriving in #waiting-rooms and #schedule-images."}
    content = (f"📅 **{heading}** ({span}) · **{len(items)}** streams · {collabs} collabs · {picked} on your calendar\n"
               "Tap ↗ to open a stream's card and add it to your calendar.")
    embeds, day_lines = [], {}
    for item in items:
        day_lines.setdefault(item.entry.start_utc.astimezone(tz).date(), []).append(_line(item))
    for day, lines in sorted(day_lines.items())[:10]:
        any_picked = any(i.entry.picked for i in items if i.entry.start_utc.astimezone(tz).date() == day)
        embeds.append(discord.Embed(title=f"{day:%A %d %B}", description="\n".join(lines)[:4096],
                                    color=PURPLE if any_picked else GREY))
    return {"content": content, "embeds": embeds}


def owner_now(tz_name: str, now_utc: datetime) -> datetime:
    return now_utc.astimezone(ZoneInfo(tz_name))
