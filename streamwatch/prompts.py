"""Instructions for reading schedule images, shared by every model provider (Claude, Gemini)."""

from __future__ import annotations

from datetime import datetime
from typing import Optional


SYSTEM = """You read livestream schedule images posted by YouTube streamers and VTubers.
Return one entry per scheduled stream. Rules:
- Skip days marked off, rest, no stream, TBA/TBD without a time, or crossed out.
- Dates: resolve to real calendar dates. Schedules are usually for the week the post was made in or the week after;
  use the post date and any dates printed on the image. Weekday-only schedules start on or after the post date.
- Times: if a JST row is printed, ALWAYS use the JST row: start_time is its time (24-hour HH:MM),
  timezone_as_written is "JST", and date is the day it happens in Japan. Apply its day marker ("07:00 (FRI)" means
  Friday). When the columns are dated in another timezone (e.g. US), a JST time is often the NEXT day even without
  a marker, so work the JST date out from the other row. Never pair the JST time with another row's date.
  Example: under "15th of October, THU": JST 07:00 (FRI) / EDT 6:00 PM / PDT 3:00 PM → date 2026-10-16,
  start_time 07:00, timezone_as_written JST, check_time {EDT, 2026-10-15, 18:00}.
  If no JST row is printed, use the row whose day matches the column (no day marker), usually the streamer's own
  timezone. If no timezone is printed anywhere, use null.
- Two times on one row separated by "|" (e.g. "6 PM | 9 PM"):
  * if the slot shows two titles (e.g. "BACK N' FORTH" and "#FSFABLES | DAY 3"), they are TWO streams: the first
    title at the first time, the second title at the second time. Return two entries.
  * if the slot shows one title, it's one stream: use the first time.
  A range written with "-" or "~" (e.g. "8PM-11PM") is start and end of one stream.
- check_time: just ONE other printed row for the same stream (prefer the one matching the column, no day marker),
  with its own date and time. Don't read or list any further timezone rows. null if only one timezone is printed.
- slot_date is the date printed for that day/column (may differ from date when the JST row is the next day).
- A time past midnight that belongs to the previous day's slot (e.g. "Mon 1AM" after Sunday night) keeps the
  calendar date it actually happens on.
- title_as_written: the stream's topic as printed, in its original language. Don't invent titles.
- subtitle_as_written: the small tagline printed with that title, if any, as printed; otherwise null.
- Mark collabs (guests, "w/", "ft.", "collab", コラボ, partner avatars with names) and list partners as printed.
- confidence below 0.8 when text is blurry, stylised, partly hidden, or you had to guess the date or time."""


def _prompt(streamer: str, post_date: datetime, post_text: str, image_count: int,
            notes: Optional[str] = None) -> str:
    what = ("this schedule image" if image_count == 1 else
            f"these {image_count} images, which are parts of ONE schedule (read them together, in order)")
    owner_notes = (f"\nThe owner's notes on reading this streamer's schedules (these override the general rules):\n"
                   f"{notes.strip()}\n" if notes and notes.strip() else "")
    return (f"Streamer: {streamer}\n"
            f"Posted: {post_date:%A %Y-%m-%d}\n"
            f"Post text: {post_text.strip() or '(none)'}\n{owner_notes}\n"
            f"Read every stream in {what}.")
