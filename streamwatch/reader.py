"""Reader A: Claude reads a schedule image into structured entries (PRD X-1).

Sonnet 5.5 reads every image; Opus 5.5 only re-reads images whose result disagrees with the OCR
(cost decision, see memory/PRD §6). Server-side fallback is on so a rare safety decline is retried.
"""

from __future__ import annotations

import base64
import logging
from datetime import datetime
from typing import Optional

import anthropic
from pydantic import BaseModel, Field

from .schedule import ReadEntry

log = logging.getLogger("streamwatch.reader")

FIRST_READER = "claude-sonnet-5-5"
SECOND_OPINION = "claude-opus-5-5"


class _TimeRow(BaseModel):
    timezone: str = Field(description="Timezone label as printed, e.g. JST")
    date: str = Field(description="YYYY-MM-DD in that timezone (apply day markers like (FRI) or +1)")
    time: str = Field(description="HH:MM 24-hour, as printed for that timezone")


class _Entry(BaseModel):
    slot_date: Optional[str] = Field(
        description="YYYY-MM-DD of the day/column this stream is listed under, as printed; null if none printed")
    date: str = Field(description="YYYY-MM-DD the stream starts on, in timezone_as_written")
    start_time: str = Field(description="HH:MM in 24-hour time, in the timezone written for this stream")
    timezone_as_written: Optional[str] = Field(
        description="The timezone label used for start_time exactly as printed (e.g. PT, JST, GMT+9), or null")
    title_as_written: str = Field(description="What the stream is, as printed (game, activity or title)")
    is_collab: bool
    collab_partners: list[str] = Field(description="Partner names or @handles if shown")
    confidence: float = Field(description="0.0-1.0: how sure you are of this entry's date and time")
    other_times: list[_TimeRow] = Field(
        description="The same stream's time in every OTHER timezone printed for it (empty if only one)")


class _Reading(BaseModel):
    is_schedule: bool = Field(description="True if the image is a stream schedule")
    entries: list[_Entry]


SYSTEM = """You read livestream schedule images posted by YouTube streamers and VTubers.
Return one entry per scheduled stream. Rules:
- Skip days marked off, rest, no stream, TBA/TBD without a time, or crossed out.
- Dates: resolve to real calendar dates. Schedules are usually for the week the post was made in or the week after;
  use the post date and any dates printed on the image. Weekday-only schedules start on or after the post date.
- Times: when several timezones are printed for a stream, use the one whose day matches the day/column the stream
  is listed under: the row WITHOUT a day marker such as "(FRI)", "(M)", "+1" or "next day". That is usually the
  streamer's own timezone. Give start_time in 24-hour HH:MM in that timezone and its label in timezone_as_written.
  Example: under "15th of October, THU": JST 07:00 (FRI) / EDT 6:00 PM / PDT 3:00 PM → date 2026-10-15,
  start_time 18:00, timezone_as_written EDT (the first row without a marker). Never pair one timezone's time with
  another timezone's date. If no timezone is printed anywhere, use null.
- Two times on one row separated by "|" (e.g. "6 PM | 9 PM"):
  * if the slot shows two titles (e.g. "BACK N' FORTH" and "#FSFABLES | DAY 3"), they are TWO streams: the first
    title at the first time, the second title at the second time. Return two entries.
  * if the slot shows one title, it's one stream: use the first time.
  A range written with "-" or "~" (e.g. "8PM-11PM") is start and end of one stream.
- other_times: list every other timezone row printed for that stream, each with its own date in that timezone
  (apply its day marker: "07:00 (FRI)" under a THU column means Friday's date).
- slot_date is the date printed for that day/column. date must be the day the stream starts in
  timezone_as_written; it normally equals slot_date.
- A time past midnight that belongs to the previous day's slot (e.g. "Mon 1AM" after Sunday night) keeps the
  calendar date it actually happens on.
- title_as_written: the stream's topic as printed, in its original language. Don't invent titles.
- Mark collabs (guests, "w/", "ft.", "collab", コラボ, partner avatars with names) and list partners as printed.
- confidence below 0.8 when text is blurry, stylised, partly hidden, or you had to guess the date or time."""


def _prompt(streamer: str, home_tz: str, post_date: datetime, post_text: str, image_count: int,
            notes: Optional[str] = None) -> str:
    what = ("this schedule image" if image_count == 1 else
            f"these {image_count} images, which are parts of ONE schedule (read them together, in order)")
    owner_notes = (f"\nThe owner's notes on reading this streamer's schedules (these override the general rules):\n"
                   f"{notes.strip()}\n" if notes and notes.strip() else "")
    return (f"Streamer: {streamer} (usually streams in {home_tz})\n"
            f"Posted: {post_date:%A %Y-%m-%d}\n"
            f"Post text: {post_text.strip() or '(none)'}\n{owner_notes}\n"
            f"Read every stream in {what}.")


class RefusedError(Exception):
    pass


async def read_schedule(client: anthropic.AsyncAnthropic, model: str, images: list[tuple[bytes, str]],
                        streamer: str, home_tz: str, post_date: datetime, post_text: str = "",
                        notes: Optional[str] = None) -> tuple[bool, list[ReadEntry]]:
    """Read one schedule that may be split over several images [(bytes, media_type)]. Returns (is_schedule, entries)."""
    image_blocks = [{"type": "image", "source": {"type": "base64", "media_type": media_type,
                                                 "data": base64.standard_b64encode(data).decode()}}
                    for data, media_type in images]
    response = await client.messages.parse(
        model=model,
        max_tokens=16000,
        system=SYSTEM,
        messages=[{"role": "user", "content": [
            *image_blocks,
            {"type": "text", "text": _prompt(streamer, home_tz, post_date, post_text, len(images), notes)},
        ]}],
        output_format=_Reading,
        output_config={"effort": "high" if model == SECOND_OPINION else "medium"},
        # Retry a safety decline on Anthropic's recommended fallback model, server-side.
        extra_headers={"anthropic-beta": "server-side-fallback-2026-07-01"},
        extra_body={"fallbacks": "default"},
    )
    log.info("%s read image: %s in / %s out tokens, stop=%s", model, response.usage.input_tokens,
             response.usage.output_tokens, response.stop_reason)
    if response.stop_reason == "refusal":
        raise RefusedError(f"{model} declined to read this image")
    reading = response.parsed_output
    if reading is None:
        raise RefusedError(f"{model} returned no readable result (stop={response.stop_reason})")
    return reading.is_schedule, [
        ReadEntry(e.date, e.start_time, e.timezone_as_written, e.title_as_written.strip(), e.is_collab,
                  e.collab_partners, max(0.0, min(1.0, e.confidence)), e.slot_date,
                  [(r.date, r.time, r.timezone) for r in e.other_times])
        for e in reading.entries
    ]
