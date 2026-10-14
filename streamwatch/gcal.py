"""Google Calendar REST calls (PRD §5.5).

Signs in as a service account (a robot Google account) that the owner shared ONE calendar with,
so the bot can only touch that calendar and the access never expires (decision 10).
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import aiohttp
from google.auth import crypt, jwt

API = "https://www.googleapis.com/calendar/v3"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/calendar.events"
EVENT_LENGTH = timedelta(hours=2)  # decision 3: fixed 2-hour block


class CalendarError(Exception):
    pass


class CalendarAuthError(CalendarError):
    """The service account key is missing, deleted or disabled."""


def token_claims(client_email: str, now: int) -> dict:
    return {"iss": client_email, "scope": SCOPE, "aud": TOKEN_URL, "iat": now, "exp": now + 3600}


class GoogleCalendar:
    def __init__(self, session: aiohttp.ClientSession, key_path: Path, calendar_id: Optional[str]):
        self.session = session
        self.key_path = key_path
        self.calendar_id = calendar_id
        self._access_token: Optional[str] = None
        self._expires_at = 0.0

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
        if self._access_token and time.time() < self._expires_at - 60:
            return self._access_token
        if not self.key_path.exists():
            raise CalendarAuthError("service account key file is missing")
        info = json.loads(self.key_path.read_text())
        signer = crypt.RSASigner.from_service_account_info(info)
        assertion = jwt.encode(signer, token_claims(info["client_email"], int(time.time()))).decode()
        data = {"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion}
        async with self.session.post(TOKEN_URL, data=data, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            body = await resp.json(content_type=None)
        if "access_token" not in body:
            raise CalendarAuthError(body.get("error_description") or body.get("error") or str(body))
        self._access_token = body["access_token"]
        self._expires_at = time.time() + int(body.get("expires_in", 3600))
        return self._access_token

    async def _call(self, method: str, path: str, *, params: dict = None, json_body: dict = None) -> Optional[dict]:
        headers = {"Authorization": f"Bearer {await self._token()}"}
        async with self.session.request(method, API + path, params=params, json=json_body, headers=headers,
                                        timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status == 204:
                return None
            body = await resp.json(content_type=None)
            if resp.status == 401:
                self._access_token = None
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

    async def move_event(self, calendar_id: str, event_id: str, start: datetime, title: str) -> None:
        await self._call("PATCH", f"/calendars/{quote(calendar_id)}/events/{quote(event_id)}", json_body={
            "summary": title,
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


def event_title(streamer_name: str, stream_title: str, is_collab: bool = False) -> str:
    """C-1: '💜 {Streamer}: {title}', with a 🤝 prefix for collabs."""
    return f"{'🤝 ' if is_collab else ''}💜 {streamer_name}: {stream_title}"


def event_description(video_id: Optional[str]) -> str:
    lines = ["Added by StreamWatch."]
    if video_id:
        lines.insert(0, f"Watch: https://www.youtube.com/watch?v={video_id}")
    return "\n".join(lines)
