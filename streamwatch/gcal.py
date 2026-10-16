"""Google Calendar REST calls (PRD §5.5).

Signs in as a service account (a robot Google account) that the owner shared ONE calendar with,
so the bot can only touch that calendar and the access never expires (decision 10).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import aiohttp

from .gauth import GoogleAuthError, ServiceAccountToken
from .gauth import token_claims as _token_claims

API = "https://www.googleapis.com/calendar/v3"
from .gauth import TOKEN_URL  # noqa: E402,F401  (re-exported for tests)
SCOPE = "https://www.googleapis.com/auth/calendar.events"
EVENT_LENGTH = timedelta(hours=2)  # decision 3: fixed 2-hour block


class CalendarError(Exception):
    pass


class CalendarAuthError(CalendarError):
    """The service account key is missing, deleted or disabled."""


def token_claims(client_email: str, now: int) -> dict:
    return _token_claims(client_email, SCOPE, now)


class GoogleCalendar:
    def __init__(self, session: aiohttp.ClientSession, key_path: Path, calendar_id: Optional[str]):
        self.session = session
        self.key_path = key_path
        self.calendar_id = calendar_id
        self._auth = ServiceAccountToken(key_path, SCOPE)

    @property
    def connected(self) -> bool:
        return self.key_path.exists() and bool(self.calendar_id)

    @property
    def robot_email(self) -> Optional[str]:
        """The service account address the calendar must be shared with."""
        try:
            return json.loads(self.key_path.read_text())["client_email"]
        except (OSError, ValueError, KeyError):
            return None

    async def _token(self) -> str:
        try:
            return await self._auth.get(self.session)
        except GoogleAuthError as e:
            raise CalendarAuthError(str(e)) from e

    async def _call(self, method: str, path: str, *, params: dict = None, json_body: dict = None) -> Optional[dict]:
        headers = {"Authorization": f"Bearer {await self._token()}"}
        async with self.session.request(method, API + path, params=params, json=json_body, headers=headers,
                                        timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status == 204:
                return None
            body = await resp.json(content_type=None)
            if resp.status == 401:
                self._auth.invalidate()
                raise CalendarAuthError(body.get("error", {}).get("message", "unauthorized"))
            if resp.status >= 400:
                message = body.get("error", {}).get("message", body)
                if resp.status in (403, 404) and "/events/" not in path:
                    message = (f"can't write to calendar {self.calendar_id}. Share it with {self.robot_email} "
                               "and allow 'Make changes to events'")
                raise CalendarError(f"{resp.status}: {message}")
            return body

    async def create_event(self, calendar_id: str, title: str, start: datetime, description: str) -> str:
        body = await self._call("POST", f"/calendars/{quote(calendar_id)}/events", json_body={
            "summary": title, "description": description,
            "start": {"dateTime": start.isoformat()}, "end": {"dateTime": (start + EVENT_LENGTH).isoformat()},
        })
        return body["id"]

    async def move_event(self, calendar_id: str, event_id: str, start: datetime, title: str,
                         description: Optional[str] = None) -> None:
        """Update time and title (and description if given) of an existing event."""
        body = {"description": description} if description is not None else {}
        await self._call("PATCH", f"/calendars/{quote(calendar_id)}/events/{quote(event_id)}", json_body={
            **body, "summary": title,
            "start": {"dateTime": start.isoformat()}, "end": {"dateTime": (start + EVENT_LENGTH).isoformat()},
        })

    async def delete_event(self, calendar_id: str, event_id: str) -> None:
        try:
            await self._call("DELETE", f"/calendars/{quote(calendar_id)}/events/{quote(event_id)}")
        except CalendarError as e:
            if not str(e).startswith(("404", "410")):  # already gone is fine
                raise

    async def timezone(self) -> str:
        """T-1: the shared calendar's timezone (events.list returns it; no extra permission needed)."""
        body = await self._call("GET", f"/calendars/{quote(self.calendar_id)}/events", params={"maxResults": 1})
        return body["timeZone"]


def event_title(streamer_name: str, stream_title: str, is_collab: bool = False, tentative: bool = False) -> str:
    """C-1: '💜 {Streamer}: {title}', with a 🤝 prefix for collabs and '(tentative)' until confirmed."""
    return f"{'🤝 ' if is_collab else ''}💜 {streamer_name}: {stream_title}" + (" (tentative)" if tentative else "")


def event_description(video_id: Optional[str], partner_names: list[str] = ()) -> str:
    lines = []
    if video_id:
        lines.append(f"Watch: https://www.youtube.com/watch?v={video_id}")
    if partner_names:
        lines.append("Collab with: " + ", ".join(partner_names))
    lines.append("Added by StreamWatch.")
    return "\n".join(lines)
