"""SQLite storage. One file, one connection, single user (PRD §6)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS streamers (
    id           INTEGER PRIMARY KEY,
    channel_id   TEXT NOT NULL UNIQUE,
    handle       TEXT,
    name         TEXT NOT NULL,
    home_tz      TEXT NOT NULL,
    active       INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL,
    bootstrapped INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- What YouTube says about each video we've seen in a feed (I-3).
CREATE TABLE IF NOT EXISTS videos (
    video_id        TEXT PRIMARY KEY,
    streamer_id     INTEGER NOT NULL REFERENCES streamers(id),
    title           TEXT NOT NULL,
    broadcast       TEXT NOT NULL,          -- upcoming | live | none | gone
    scheduled_start TEXT,
    actual_start    TEXT,
    ignored         INTEGER NOT NULL DEFAULT 0,  -- free chat, premiere, or VOD
    first_seen      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
-- One scheduled stream shown as a card. M2: from waiting rooms; M4 adds schedule images.
CREATE TABLE IF NOT EXISTS entries (
    id              INTEGER PRIMARY KEY,
    streamer_id     INTEGER NOT NULL REFERENCES streamers(id),
    start_utc       TEXT NOT NULL,
    title           TEXT NOT NULL,
    is_collab       INTEGER NOT NULL DEFAULT 0,
    partners_json   TEXT NOT NULL DEFAULT '[]',
    source          TEXT NOT NULL,          -- waiting_room | image | both
    post_id         TEXT,
    video_id        TEXT UNIQUE,
    confidence      REAL,
    status          TEXT NOT NULL DEFAULT 'confirmed',  -- pending_review | confirmed | cancelled
    previous_start  TEXT,
    card_channel_id INTEGER,
    card_message_id INTEGER,
    created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS picks (
    entry_id      INTEGER PRIMARY KEY REFERENCES entries(id),
    gcal_event_id TEXT,
    picked_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
    video_id TEXT PRIMARY KEY,
    kind     TEXT NOT NULL,                 -- picked | surprise
    sent_at  TEXT NOT NULL
);
"""

# Columns added after a table first shipped: (table, column, definition).
MIGRATIONS = [
    ("streamers", "bootstrapped", "INTEGER NOT NULL DEFAULT 0"),
]

PAUSED_FOREVER = "forever"


@dataclass
class Streamer:
    id: int
    channel_id: str
    handle: Optional[str]
    name: str
    home_tz: str
    active: bool
    bootstrapped: bool = False


@dataclass
class Video:
    video_id: str
    streamer_id: int
    title: str
    broadcast: str
    scheduled_start: Optional[datetime]
    actual_start: Optional[datetime]
    ignored: bool


@dataclass
class Entry:
    id: int
    streamer_id: int
    start_utc: datetime
    title: str
    source: str
    video_id: Optional[str]
    status: str
    previous_start: Optional[datetime]
    card_channel_id: Optional[int]
    card_message_id: Optional[int]
    picked: bool


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _dt(value: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(value) if value else None


class Database:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        for table, column, definition in MIGRATIONS:
            existing = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        self.conn.commit()

    # --- streamers (F-1, F-2) ---

    def add_streamer(self, channel_id: str, handle: Optional[str], name: str, home_tz: str) -> tuple[Streamer, bool]:
        """Insert or reactivate a streamer. Returns (streamer, created)."""
        existing = self.get_streamer_by_channel(channel_id)
        if existing and existing.active:
            return existing, False
        if existing:
            self.conn.execute(
                "UPDATE streamers SET active = 1, handle = ?, name = ?, home_tz = ?, bootstrapped = 0 WHERE id = ?",
                (handle, name, home_tz, existing.id),
            )
        else:
            self.conn.execute(
                "INSERT INTO streamers (channel_id, handle, name, home_tz, created_at) VALUES (?, ?, ?, ?, ?)",
                (channel_id, handle, name, home_tz, _now().isoformat()),
            )
        self.conn.commit()
        return self.get_streamer_by_channel(channel_id), True

    def get_streamer(self, streamer_id: int) -> Optional[Streamer]:
        row = self.conn.execute("SELECT * FROM streamers WHERE id = ?", (streamer_id,)).fetchone()
        return _to_streamer(row) if row else None

    def get_streamer_by_channel(self, channel_id: str) -> Optional[Streamer]:
        row = self.conn.execute("SELECT * FROM streamers WHERE channel_id = ?", (channel_id,)).fetchone()
        return _to_streamer(row) if row else None

    def find_active_streamer(self, query: str) -> Optional[Streamer]:
        """Match /unfollow input against name, @handle or channel ID (case-insensitive)."""
        q = query.strip().lstrip("@").lower()
        for s in self.list_streamers():
            if q in (s.name.lower(), (s.handle or "").lstrip("@").lower(), s.channel_id.lower()):
                return s
        return None

    def deactivate_streamer(self, streamer_id: int) -> None:
        self.conn.execute("UPDATE streamers SET active = 0 WHERE id = ?", (streamer_id,))
        self.conn.commit()

    def mark_bootstrapped(self, streamer_id: int) -> None:
        self.conn.execute("UPDATE streamers SET bootstrapped = 1 WHERE id = ?", (streamer_id,))
        self.conn.commit()

    def list_streamers(self) -> list[Streamer]:
        rows = self.conn.execute("SELECT * FROM streamers WHERE active = 1 ORDER BY name COLLATE NOCASE").fetchall()
        return [_to_streamer(r) for r in rows]

    # --- videos (I-3, L-1) ---

    def get_video(self, video_id: str) -> Optional[Video]:
        row = self.conn.execute("SELECT * FROM videos WHERE video_id = ?", (video_id,)).fetchone()
        return _to_video(row) if row else None

    def known_video_ids(self, video_ids: list[str]) -> set[str]:
        if not video_ids:
            return set()
        marks = ",".join("?" * len(video_ids))
        rows = self.conn.execute(f"SELECT video_id FROM videos WHERE video_id IN ({marks})", video_ids).fetchall()
        return {r["video_id"] for r in rows}

    def save_video(self, video_id: str, streamer_id: int, title: str, broadcast: str,
                   scheduled_start: Optional[datetime], actual_start: Optional[datetime], ignored: bool) -> None:
        now = _now().isoformat()
        self.conn.execute(
            """INSERT INTO videos (video_id, streamer_id, title, broadcast, scheduled_start, actual_start, ignored,
                                   first_seen, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(video_id) DO UPDATE SET title = excluded.title, broadcast = excluded.broadcast,
                   scheduled_start = excluded.scheduled_start, actual_start = excluded.actual_start,
                   ignored = excluded.ignored, updated_at = excluded.updated_at""",
            (video_id, streamer_id, title, broadcast, _iso(scheduled_start), _iso(actual_start), int(ignored),
             now, now),
        )
        self.conn.commit()

    def set_video_broadcast(self, video_id: str, broadcast: str) -> None:
        self.conn.execute("UPDATE videos SET broadcast = ?, updated_at = ? WHERE video_id = ?",
                          (broadcast, _now().isoformat(), video_id))
        self.conn.commit()

    def video_ids_to_poll(self) -> list[str]:
        """Upcoming or live videos of followed streamers that aren't ignored (L-1)."""
        rows = self.conn.execute(
            """SELECT v.video_id FROM videos v JOIN streamers s ON s.id = v.streamer_id
               WHERE s.active = 1 AND v.ignored = 0 AND v.broadcast IN ('upcoming', 'live')""").fetchall()
        return [r["video_id"] for r in rows]

    # --- entries and picks ---

    def create_entry(self, streamer_id: int, start_utc: datetime, title: str, source: str,
                     video_id: Optional[str] = None) -> Entry:
        cur = self.conn.execute(
            "INSERT INTO entries (streamer_id, start_utc, title, source, video_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (streamer_id, start_utc.isoformat(), title, source, video_id, _now().isoformat()),
        )
        self.conn.commit()
        return self.get_entry(cur.lastrowid)

    def get_entry(self, entry_id: int) -> Optional[Entry]:
        row = self.conn.execute(
            "SELECT e.*, p.entry_id IS NOT NULL AS picked FROM entries e LEFT JOIN picks p ON p.entry_id = e.id "
            "WHERE e.id = ?", (entry_id,)).fetchone()
        return _to_entry(row) if row else None

    def entry_for_video(self, video_id: str) -> Optional[Entry]:
        row = self.conn.execute("SELECT id FROM entries WHERE video_id = ?", (video_id,)).fetchone()
        return self.get_entry(row["id"]) if row else None

    def update_entry(self, entry_id: int, **fields) -> Entry:
        allowed = {"start_utc", "title", "status", "previous_start", "card_channel_id", "card_message_id"}
        assert set(fields) <= allowed, set(fields) - allowed
        values = [v.isoformat() if isinstance(v, datetime) else v for v in fields.values()]
        assignments = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE entries SET {assignments} WHERE id = ?", (*values, entry_id))
        self.conn.commit()
        return self.get_entry(entry_id)

    def set_picked(self, entry_id: int, picked: bool) -> None:
        if picked:
            self.conn.execute("INSERT OR IGNORE INTO picks (entry_id, picked_at) VALUES (?, ?)",
                              (entry_id, _now().isoformat()))
        else:
            self.conn.execute("DELETE FROM picks WHERE entry_id = ?", (entry_id,))
        self.conn.commit()

    # --- alerts (L-3: once per video) ---

    def record_alert(self, video_id: str, kind: str) -> bool:
        """Record an alert. Returns False if this video already had one."""
        cur = self.conn.execute("INSERT OR IGNORE INTO alerts (video_id, kind, sent_at) VALUES (?, ?, ?)",
                                (video_id, kind, _now().isoformat()))
        self.conn.commit()
        return cur.rowcount == 1

    # --- settings ---

    def get_setting(self, key: str) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: Optional[str]) -> None:
        if value is None:
            self.conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        else:
            self.conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
        self.conn.commit()

    # --- pause (F-2) ---

    def pause(self, until: Optional[datetime]) -> None:
        """Pause until a UTC time, or indefinitely when until is None."""
        self.set_setting("paused_until", until.isoformat() if until else PAUSED_FOREVER)

    def resume(self) -> None:
        self.set_setting("paused_until", None)

    def paused_until(self, now: Optional[datetime] = None) -> Optional[str]:
        """Return 'forever', an ISO time, or None when not paused. Expired pauses are cleared."""
        value = self.get_setting("paused_until")
        if value is None or value == PAUSED_FOREVER:
            return value
        if datetime.fromisoformat(value) <= (now or _now()):
            self.resume()
            return None
        return value

    def is_paused(self, now: Optional[datetime] = None) -> bool:
        return self.paused_until(now) is not None


def _to_streamer(row: sqlite3.Row) -> Streamer:
    return Streamer(row["id"], row["channel_id"], row["handle"], row["name"], row["home_tz"], bool(row["active"]),
                    bool(row["bootstrapped"]))


def _to_video(row: sqlite3.Row) -> Video:
    return Video(row["video_id"], row["streamer_id"], row["title"], row["broadcast"], _dt(row["scheduled_start"]),
                 _dt(row["actual_start"]), bool(row["ignored"]))


def _to_entry(row: sqlite3.Row) -> Entry:
    return Entry(row["id"], row["streamer_id"], _dt(row["start_utc"]), row["title"], row["source"], row["video_id"],
                 row["status"], _dt(row["previous_start"]), row["card_channel_id"], row["card_message_id"],
                 bool(row["picked"]))
