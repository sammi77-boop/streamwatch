"""Schedule image → entries (PRD F-3, X-1–X-5, I-4). Discord-free; the readers are passed in so tests can fake them."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional

from .db import Database, Entry, Streamer
from .schedule import CheckedEntry, ReadEntry, best_match, cross_check, title_similarity, to_utc
from .tracker import Event, Rescheduled, Updated

log = logging.getLogger("streamwatch.pipeline")

PAST_GRACE = timedelta(hours=3)
TOO_FAR = timedelta(days=21)
RESCHEDULE_TOLERANCE = timedelta(minutes=1)

# (model, images [(bytes, media_type)], streamer_name, post_date, post_text) -> (is_schedule, entries)
ReadFn = Callable[..., Awaitable[tuple[bool, list[ReadEntry]]]]
OcrFn = Callable[[bytes], Awaitable[str]]


@dataclass
class NewImageEntry:
    entry: Entry


@dataclass
class NeedsReview:
    entry: Entry
    issues: list[str]


@dataclass
class ImageResult:
    is_schedule: bool = True
    events: list = field(default_factory=list)
    skipped_past: int = 0
    duplicates: int = 0
    used_second_opinion: bool = False

    def summary(self) -> str:
        if not self.is_schedule:
            return "That image doesn't look like a stream schedule, so nothing was added."
        new = sum(isinstance(e, NewImageEntry) for e in self.events)
        review = sum(isinstance(e, NeedsReview) for e in self.events)
        merged = sum(isinstance(e, (Updated, Rescheduled)) for e in self.events)
        parts = [f"**{new}** added to #schedule-images"]
        if merged:
            parts.append(f"**{merged}** matched streams already listed")
        if self.duplicates:
            parts.append(f"{self.duplicates} already listed")
        if review:
            parts.append(f"**{review}** need a quick check in #needs-review")
        if self.skipped_past:
            parts.append(f"{self.skipped_past} already over")
        note = " (double-checked with a second reader)" if self.used_second_opinion else ""
        return "📅 Read the schedule" + note + ": " + ", ".join(parts) + "."


async def process_image(db: Database, streamer: Streamer, images: list[tuple[bytes, str]], read: ReadFn,
                        ocr: Optional[OcrFn], first_model: str, second_model: str, post_date: datetime, now: datetime,
                        post_text: str = "", post_id: Optional[str] = None,
                        image_path: Optional[str] = None, schedule_id: Optional[int] = None,
                        review_tz: str = "UTC") -> ImageResult:
    """One schedule, possibly split across several images (e.g. a post with two halves).

    image_path is the saved (stitched) schedule image that cards show. review_tz (the owner's timezone) only
    places streams whose image printed no timezone, provisionally, on their review card."""
    result = ImageResult()
    is_schedule, entries = await read(first_model, images, streamer.name, post_date, post_text)
    if not is_schedule or not entries:
        result.is_schedule = is_schedule and bool(entries)
        return result

    # OCR cross-check is optional: with a strong reader it flagged correct stylised text more than real mistakes.
    ocr_text = "\n".join([await ocr(data) for data, _ in images]) if ocr else None
    checked = [cross_check(e, ocr_text) for e in entries]
    if any(not c.ok for c in checked):
        # X-3: disagreement → second opinion from the stronger model; keep whichever reading OCR agrees with more.
        result.used_second_opinion = True
        try:
            _, second = await read(second_model, images, streamer.name, post_date, post_text)
            checked_b = [cross_check(e, ocr_text) for e in second]
            checked = _merge_readings(checked, checked_b)
        except Exception as e:  # a failed second opinion just means the first reading goes to review
            log.warning("Second opinion failed: %s", e)

    for c in checked:
        await _place(db, streamer, c, now, post_id, image_path, schedule_id, review_tz, result)
    return result


def _merge_readings(first: list[CheckedEntry], second: list[CheckedEntry]) -> list[CheckedEntry]:
    """Per stream, keep whichever reader's version passes the checks (the first reader's if both or neither do)."""
    def same_stream(a: CheckedEntry, b: CheckedEntry) -> bool:
        key_a, key_b = a.read.slot_date or a.read.date, b.read.slot_date or b.read.date
        return key_a == key_b and title_similarity(a.read.title, b.read.title) >= 0.5

    merged = []
    for a in first:
        if a.ok:
            merged.append(a)
            continue
        better = next((b for b in second if same_stream(a, b) and b.ok), None)
        merged.append(better or a)
    return merged


async def _place(db: Database, streamer: Streamer, c: CheckedEntry, now: datetime, post_id: Optional[str],
                 image_path: Optional[str], schedule_id: Optional[int], review_tz: str,
                 result: ImageResult) -> None:
    issues = list(c.issues)
    start, tz_label = c.start_utc, c.tz_name
    if start is None:
        # No/unknown timezone or unreadable time: place it provisionally in the owner's timezone for the review
        # card, where Edit's timezone dropdown fixes it.
        start, _, more = to_utc(ReadEntry(c.read.date, c.read.start_time, review_tz, c.read.title))
        tz_label = None
        if start is None:
            log.info("Dropped unreadable entry %s", c.read)
            return
        issues += [i for i in more if i not in issues]
    if start < now - PAST_GRACE:
        result.skipped_past += 1
        return
    if start > now + TOO_FAR:
        issues.append("more than 3 weeks away")

    existing = best_match(start, c.read.title, db.entries_for_streamer(streamer.id, ("confirmed", "pending_review")))
    if existing is not None:
        if not issues and existing.source == "waiting_room":
            # The waiting room already has the exact time; just note it's also on the schedule image.
            entry = db.update_entry(existing.id, source="both", is_collab=existing.is_collab or c.read.is_collab,
                                    partners=existing.partners or c.read.partners,
                                    image_path=existing.image_path or image_path)
            result.events.append(Updated(entry))
        elif not issues and existing.source == "image" and existing.status == "confirmed" \
                and abs(existing.start_utc - start) > RESCHEDULE_TOLERANCE:
            # A newer schedule moved an image-only stream.
            entry = db.update_entry(existing.id, start_utc=start, previous_start=existing.start_utc,
                                    title=c.read.title, orig_tz=tz_label, image_path=image_path or existing.image_path)
            result.events.append(Rescheduled(entry, existing.start_utc))
        else:
            result.duplicates += 1
        return

    status = "confirmed" if not issues else "pending_review"
    entry = db.create_entry(streamer.id, start, c.read.title, "image", is_collab=c.read.is_collab,
                            partners=c.read.partners, status=status, orig_tz=tz_label,
                            confidence=c.read.confidence, post_id=post_id, image_path=image_path,
                            schedule_id=schedule_id, subtitle=c.read.subtitle)
    result.events.append(NewImageEntry(entry) if not issues else NeedsReview(entry, issues))
