"""Schedule-image logic with no API calls (PRD X-3, X-4, I-4): timezones, OCR cross-check, title matching."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo
from difflib import SequenceMatcher
from typing import Optional
from zoneinfo import ZoneInfo

MIN_CONFIDENCE = 0.8


@dataclass
class ReadEntry:
    """One stream as read from a schedule image (Reader A's output)."""
    date: str                       # YYYY-MM-DD
    start_time: str                 # HH:MM, 24h, in the timezone written on the image
    timezone_as_written: Optional[str]
    title: str
    is_collab: bool = False
    partners: list = field(default_factory=list)
    confidence: float = 1.0
    slot_date: Optional[str] = None  # the day/column it's listed under; differs from date when tz rows got mixed
    other_times: list = field(default_factory=list)  # [(date, time, tz)]: same stream in other printed timezones


@dataclass
class CheckedEntry:
    read: ReadEntry
    start_utc: Optional[datetime]
    tz_name: Optional[str]          # what the time was interpreted in, for showing the original time
    issues: list[str]

    @property
    def ok(self) -> bool:
        return not self.issues


# --- X-4: timezones ---

TZ_ABBREVIATIONS = {
    "PT": "America/Los_Angeles", "PST": "America/Los_Angeles", "PDT": "America/Los_Angeles",
    "MT": "America/Denver", "MST": "America/Denver", "MDT": "America/Denver",
    "CT": "America/Chicago", "CST": "America/Chicago", "CDT": "America/Chicago",
    "ET": "America/New_York", "EST": "America/New_York", "EDT": "America/New_York",
    "JST": "Asia/Tokyo", "KST": "Asia/Seoul", "SGT": "Asia/Singapore", "PHT": "Asia/Manila",
    "HKT": "Asia/Hong_Kong", "AEST": "Australia/Sydney", "AEDT": "Australia/Sydney",
    "BST": "Europe/London", "GMT": "Etc/UTC", "UTC": "Etc/UTC", "WIB": "Asia/Jakarta",
    "CET": "Europe/Berlin", "CEST": "Europe/Berlin", "NZST": "Pacific/Auckland", "NZDT": "Pacific/Auckland",
    "日本時間": "Asia/Tokyo",
}
_OFFSET_RE = re.compile(r"^(?:GMT|UTC)\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?$", re.IGNORECASE)


def resolve_timezone(written: Optional[str], home_tz: str) -> tuple[Optional[tzinfo], Optional[str], Optional[str]]:
    """X-4: written timezone → streamer's home timezone. Returns (tz, label, problem)."""
    if not written or not written.strip():
        return ZoneInfo(home_tz), home_tz, None
    text = unicodedata.normalize("NFKC", written).strip().upper().replace(".", "")
    if text in TZ_ABBREVIATIONS:
        return ZoneInfo(TZ_ABBREVIATIONS[text]), written.strip(), None
    match = _OFFSET_RE.match(text)
    if match:
        sign = 1 if match.group(1) == "+" else -1
        offset = timedelta(hours=int(match.group(2)), minutes=int(match.group(3) or 0)) * sign
        return timezone(offset), written.strip(), None
    try:
        return ZoneInfo(written.strip()), written.strip(), None
    except (KeyError, ValueError, OSError):
        return None, None, f"unknown timezone '{written}'"


def to_utc(entry: ReadEntry, home_tz: str) -> tuple[Optional[datetime], Optional[str], list[str]]:
    tz, label, problem = resolve_timezone(entry.timezone_as_written, home_tz)
    if problem:
        return None, None, [problem]
    try:
        day = date.fromisoformat(entry.date)
        hour, minute = (int(x) for x in entry.start_time.split(":"))
        local = datetime(day.year, day.month, day.day, hour % 24, minute, tzinfo=tz)
    except (ValueError, TypeError):
        return None, None, [f"couldn't understand date/time '{entry.date} {entry.start_time}'"]
    return local.astimezone(timezone.utc), label, []


# --- X-3: OCR cross-check ---

_TIME_RE = re.compile(
    r"(?<!\d)(\d{1,2})\s*(?:[:：.h]\s*(\d{2}))?\s*(a\.?m\.?|p\.?m\.?|時(?:\s*(\d{1,2})\s*分)?|半)?(?!\d)",
    re.IGNORECASE)
_WEEKDAYS = [("mon", "monday", "月"), ("tue", "tuesday", "火"), ("wed", "wednesday", "水"), ("thu", "thursday", "木"),
             ("fri", "friday", "金"), ("sat", "saturday", "土"), ("sun", "sunday", "日")]


def normalize_ocr(text: str) -> str:
    return unicodedata.normalize("NFKC", text).lower()


def times_in_text(text: str) -> set[int]:
    """Minutes-of-day for every time-looking token. '8:00' without am/pm counts as 08:00 and 20:00."""
    found: set[int] = set()
    for m in _TIME_RE.finditer(normalize_ocr(text)):
        hour, minutes, suffix = int(m.group(1)), m.group(2), (m.group(3) or "")
        if suffix.startswith("時"):
            minute = int(m.group(4) or 0)
        elif suffix == "半":
            minute = 30
        elif minutes is not None:
            minute = int(minutes)
        elif suffix:
            minute = 0
        else:
            continue  # a bare number is probably a date
        if minute > 59 or hour > 30:
            continue
        if suffix.startswith("p") and hour < 12:
            hour += 12
        elif suffix.startswith("a") and hour == 12:
            hour = 0
        if suffix.startswith(("a", "p")):
            found.add((hour % 24) * 60 + minute)
        else:
            found.add((hour % 24) * 60 + minute)
            if hour <= 12:
                found.add(((hour + 12) % 24) * 60 + minute)
    return found


def date_in_text(day: date, text: str) -> bool:
    """The day number (as a standalone number) or the weekday name appears in the OCR text."""
    norm = normalize_ocr(text)
    if re.search(rf"(?<!\d)0?{day.day}(?!\d)", norm):
        return True
    short, full, jp = _WEEKDAYS[day.weekday()]
    return bool(re.search(rf"\b{short}", norm)) or f"({jp})" in norm or f"（{jp}）" in norm or f"{jp}曜" in norm


def _minutes(hhmm: str) -> int:
    hour, minute = (int(x) for x in hhmm.split(":"))
    return (hour % 24) * 60 + minute


def cross_check(entry: ReadEntry, ocr_text: str, home_tz: str) -> CheckedEntry:
    start_utc, label, issues = to_utc(entry, home_tz)
    if start_utc is not None:
        # Same stream printed in other timezones: they must be the same moment as the main reading.
        agreeing, disagreeing = [], []
        for other_date, other_time, other_tz in entry.other_times:
            other, _, problems = to_utc(ReadEntry(other_date, other_time, other_tz, entry.title), home_tz)
            if other is not None:
                (agreeing if abs(other - start_utc) <= timedelta(minutes=5) else disagreeing).append(
                    (other_date, other_time, other_tz))
        if disagreeing and not agreeing:
            # e.g. "JST 07:00" paired with Thursday while EDT and PDT both say Thursday evening: a day off.
            d, t, tz = disagreeing[0]
            issues.append(f"timezones disagree: {entry.start_time} {entry.timezone_as_written} {entry.date} vs "
                          f"{t} {tz} {d}")
        # One odd row among agreeing ones is a misread of that row, not of the stream.

        found = times_in_text(ocr_text)
        if not any(_minutes(t) in found for t in [entry.start_time] + [t for _, t, _ in agreeing]):
            issues.append(f"time {entry.start_time} not found by OCR")
        if not date_in_text(date.fromisoformat(entry.date), ocr_text):
            issues.append(f"date {entry.date} not found by OCR")
    if entry.slot_date and entry.slot_date != entry.date and not (start_utc is not None and entry.other_times
                                                                  and not any("timezones disagree" in i for i in issues)):
        # A date off its column is fine when the check row confirms it (JST is often the next day); otherwise ask.
        issues.append(f"date {entry.date} differs from the day it's listed under ({entry.slot_date})")
    if entry.confidence < MIN_CONFIDENCE:
        issues.append(f"low confidence ({entry.confidence:.0%})")
    return CheckedEntry(entry, start_utc, label, issues)


# --- I-4: matching image entries to waiting rooms ---

def _tokens(title: str) -> set[str]:
    norm = re.sub(r"[【】\[\]()（）|｜!！?？:：#\-_/~、,.]", " ", unicodedata.normalize("NFKC", title).lower())
    words = {w for w in norm.split() if len(w) >= 3}
    cjk = re.sub(r"[\x00-⿿\s]", "", norm)  # CJK characters only
    words |= {cjk[i:i + 2] for i in range(len(cjk) - 1)}
    return words


def title_similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    overlap = len(ta & tb) / min(len(ta), len(tb)) if ta and tb else 0.0
    ratio = SequenceMatcher(None, a.lower(), b.lower()).ratio()
    return max(overlap, ratio)


MATCH_WINDOW = timedelta(minutes=90)
CLOSE_ENOUGH = timedelta(minutes=30)
SIMILAR = 0.34


def best_match(start: datetime, title: str, candidates: list) -> Optional[object]:
    """Pick the candidate (anything with .start_utc and .title) that is the same stream, or None.

    Within ±90 min and a similar title. A lone candidate within ±30 min counts even if the titles
    differ, since image titles are often just the game name while waiting rooms are long.
    """
    near = [c for c in candidates if abs(c.start_utc - start) <= MATCH_WINDOW]
    if not near:
        return None
    scored = sorted(near, key=lambda c: (-title_similarity(c.title, title), abs(c.start_utc - start)))
    top = scored[0]
    if title_similarity(top.title, title) >= SIMILAR:
        return top
    if len(near) == 1 and abs(top.start_utc - start) <= CLOSE_ENOUGH:
        return top
    return None
