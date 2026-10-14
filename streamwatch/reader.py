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
    subtitle_as_written: Optional[str] = Field(
        description="The short tagline printed with the title (e.g. 'YAP THEN WE RP!'), or null")
    is_collab: bool
    collab_partners: list[str] = Field(description="Partner names or @handles if shown")
    confidence: float = Field(description="0.0-1.0: how sure you are of this entry's date and time")
    check_time: Optional[_TimeRow] = Field(
        description="The same stream in ONE other printed timezone, used only to confirm the date; null if only one")


class _Reading(BaseModel):
    is_schedule: bool = Field(description="True if the image is a stream schedule")
    entries: list[_Entry]


from .prompts import SYSTEM, _prompt  # noqa: E402  (shared with the Gemini reader)


class RefusedError(Exception):
    pass


async def read_schedule(client: anthropic.AsyncAnthropic, model: str, images: list[tuple[bytes, str]],
                        streamer: str, post_date: datetime, post_text: str = "",
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
            {"type": "text", "text": _prompt(streamer, post_date, post_text, len(images), notes)},
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
                  [(e.check_time.date, e.check_time.time, e.check_time.timezone)] if e.check_time else [],
                  (e.subtitle_as_written or "").strip() or None)
        for e in reading.entries
    ]
