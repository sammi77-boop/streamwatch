"""Turns YouTube video updates into events (PRD I-3, I-4a, L-1–L-3, C-3). No Discord code here."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Union

from .db import Database, Entry, Streamer
from .youtube import VideoInfo

FREE_CHAT_RE = re.compile(r"free\s*-?\s*chat|フリーチャット|フリチャ", re.IGNORECASE)
FREE_CHAT_HORIZON = timedelta(days=30)
RESCHEDULE_TOLERANCE = timedelta(minutes=1)


@dataclass
class NewWaitingRoom:
    entry: Entry


@dataclass
class Rescheduled:
    entry: Entry
    old_start: datetime


@dataclass
class Cancelled:
    entry: Entry


@dataclass
class WentLive:
    entry: Entry
    video: VideoInfo


@dataclass
class SurpriseLive:
    video: VideoInfo


Event = Union[NewWaitingRoom, Rescheduled, Cancelled, WentLive, SurpriseLive]


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
        if entry is None:
            entry = db.create_entry(streamer.id, info.scheduled_start, info.title, "waiting_room", video_id)
            events.append(NewWaitingRoom(entry))
        elif entry.status == "cancelled":
            entry = db.update_entry(entry.id, status="confirmed", start_utc=info.scheduled_start, title=info.title)
            events.append(NewWaitingRoom(entry))
        elif abs(entry.start_utc - info.scheduled_start) > RESCHEDULE_TOLERANCE:
            old = entry.start_utc
            entry = db.update_entry(entry.id, start_utc=info.scheduled_start, previous_start=old, title=info.title)
            events.append(Rescheduled(entry, old))
        elif entry.title != info.title:
            db.update_entry(entry.id, title=info.title)

    elif info.broadcast == "live":
        if entry is not None:
            if entry.picked and db.record_alert(video_id, "picked"):
                events.append(WentLive(entry, info))
            # L-2: scheduled but not picked → no alert.
        elif alerts_enabled and (previous is None or previous.broadcast != "live"):
            # No entry means it was never seen as a waiting room, so nobody scheduled it.
            if db.record_alert(video_id, "surprise"):
                events.append(SurpriseLive(info))

    elif info.broadcast == "none" and previous and previous.broadcast == "upcoming" and info.actual_start is None:
        # The waiting room was turned back into a normal video without ever going live.
        if entry and entry.status != "cancelled":
            events.append(Cancelled(db.update_entry(entry.id, status="cancelled")))

    return events
