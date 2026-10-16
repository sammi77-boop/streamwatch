"""Turns YouTube video updates into events (PRD I-3, I-4a, L-1–L-3, C-3). No Discord code here."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Union

from .collab import detect_collab
from .db import Database, Entry, Streamer
from .schedule import best_match
from .youtube import VideoInfo

FREE_CHAT_RE = re.compile(r"free\s*-?\s*chat|フリーチャット|フリチャ", re.IGNORECASE)
FREE_CHAT_HORIZON = timedelta(days=30)
RESCHEDULE_TOLERANCE = timedelta(minutes=1)
SHORT_NOTICE = timedelta(hours=1)  # L-2c: a waiting room first seen closer to its start than this is a surprise


@dataclass
class NewWaitingRoom:
    entry: Entry


@dataclass
class Rescheduled:
    entry: Entry
    old_start: datetime
    was_tentative: bool = False  # a tentative reading corrected: I'm told even if it isn't picked


@dataclass
class Updated:
    """Title or collab partners changed; refresh the card and calendar event."""
    entry: Entry


@dataclass
class Cancelled:
    entry: Entry


@dataclass
class WentLive:
    entry: Entry
    video: VideoInfo


@dataclass
class LiveChanged:
    """A listed stream started or ended: its card or table line is redrawn (no ping)."""
    entry: Entry


@dataclass
class SurpriseLive:
    video: VideoInfo
    short_notice: bool = False  # announced, but by a waiting room less than an hour ahead


Event = Union[NewWaitingRoom, Rescheduled, Updated, Cancelled, WentLive, SurpriseLive, LiveChanged]


def is_free_chat(info: VideoInfo, now: datetime) -> bool:
    """I-4a: permanent free-chat rooms have far-future dates or say so in the title."""
    if FREE_CHAT_RE.search(info.title):
        return True
    return info.scheduled_start is not None and info.scheduled_start - now > FREE_CHAT_HORIZON


def apply_update(db: Database, streamer: Streamer, video_id: str, info: Optional[VideoInfo], now: datetime,
                 alerts_enabled: bool = True) -> list[Event]:
    """Store the latest state of one video and return what changed.

    info is None when YouTube no longer returns the video (deleted or made private).
    alerts_enabled is False during a streamer's first scan, so streams already live at /follow don't alert.
    """
    previous = db.get_video(video_id)
    entry = db.entry_for_video(video_id)

    if info is None:
        if previous and previous.broadcast in ("upcoming", "live"):
            db.set_video_broadcast(video_id, "gone")
            if entry and entry.status != "cancelled" and previous.broadcast == "upcoming":
                return [Cancelled(db.update_entry(entry.id, status="cancelled"))]
        return []

    ignored = info.is_premiere or is_free_chat(info, now) or (previous is None and info.broadcast == "none")
    if previous and previous.ignored:
        ignored = True  # once ignored, always ignored (a free chat stays a free chat)
    db.save_video(video_id, streamer.id, info.title, info.broadcast, info.scheduled_start, info.actual_start, ignored)
    if ignored:
        return []

    events: list[Event] = []
    if info.broadcast == "upcoming" and info.scheduled_start:
        is_collab, partners = detect_collab(info.title, info.description, streamer.handle)
        details = {"title": info.title, "is_collab": is_collab, "partners": partners}
        if entry is None:
            image_entry = best_match(info.scheduled_start, info.title,
                                     db.entries_for_streamer(streamer.id, unlinked_only=True))
            if image_entry is not None:
                # I-4: the stream from the schedule image now has a waiting room; its exact time and title win.
                old = image_entry.start_utc
                entry = db.update_entry(image_entry.id, video_id=video_id, source="both",
                                        start_utc=info.scheduled_start, title=info.title,
                                        is_collab=is_collab or image_entry.is_collab,
                                        partners=partners or image_entry.partners, tentative=False)
                if abs(old - info.scheduled_start) > RESCHEDULE_TOLERANCE:
                    entry = db.update_entry(entry.id, previous_start=old)
                    events.append(Rescheduled(entry, old, was_tentative=image_entry.tentative))
                else:
                    events.append(Updated(entry))
            else:
                # L-2c: not on any schedule and announced less than an hour ahead. Not during a first scan,
                # when every waiting room is new to the bot however long it has existed.
                short_notice = alerts_enabled and info.scheduled_start - now < SHORT_NOTICE
                entry = db.create_entry(streamer.id, info.scheduled_start, info.title, "waiting_room", video_id,
                                        is_collab, partners, short_notice=short_notice)
                events.append(NewWaitingRoom(entry))
        elif entry.status == "cancelled":
            entry = db.update_entry(entry.id, status="confirmed", start_utc=info.scheduled_start, **details)
            events.append(NewWaitingRoom(entry))
        elif abs(entry.start_utc - info.scheduled_start) > RESCHEDULE_TOLERANCE:
            old = entry.start_utc
            entry = db.update_entry(entry.id, start_utc=info.scheduled_start, previous_start=old, **details)
            events.append(Rescheduled(entry, old))
        elif (entry.title, entry.is_collab, entry.partners) != (info.title, is_collab, partners):
            events.append(Updated(db.update_entry(entry.id, **details)))

    elif info.broadcast == "live":
        if entry is None:
            # L-2a: a stream from a schedule image that never had a waiting room.
            started = info.actual_start or now
            entry = best_match(started, info.title, db.entries_for_streamer(streamer.id, unlinked_only=True))
            if entry is not None:
                entry = db.update_entry(entry.id, video_id=video_id, source="both", tentative=False)
        if entry is not None:
            if previous is None or previous.broadcast != "live":
                events.append(LiveChanged(entry))  # card shows LIVE NOW, picked or not
            if entry.picked:
                if db.record_alert(video_id, "picked"):
                    events.append(WentLive(entry, info))
            elif entry.short_notice and entry.source == "waiting_room" and alerts_enabled:
                if db.record_alert(video_id, "surprise"):
                    events.append(SurpriseLive(info, short_notice=True))  # L-2c
            # L-2: scheduled ahead but not picked → no alert.
        elif alerts_enabled and (previous is None or previous.broadcast != "live"):
            # No entry means it was never seen as a waiting room, so nobody scheduled it.
            if db.record_alert(video_id, "surprise"):
                events.append(SurpriseLive(info))

    elif info.broadcast == "none" and previous and previous.broadcast == "live" and entry is not None:
        events.append(LiveChanged(entry))  # the stream ended

    elif info.broadcast == "none" and previous and previous.broadcast == "upcoming" and info.actual_start is None:
        # The waiting room was turned back into a normal video without ever going live.
        if entry and entry.status != "cancelled":
            events.append(Cancelled(db.update_entry(entry.id, status="cancelled")))

    return events
