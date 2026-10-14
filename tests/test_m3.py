"""M3 tests: picking adds/removes calendar events and changes keep them in sync. Uses a fake calendar."""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from streamwatch.bot import StreamWatch
from streamwatch.config import Config
from streamwatch.gcal import SCOPE, TOKEN_URL, CalendarAuthError, GoogleCalendar, event_title, token_claims
from streamwatch.tracker import apply_update
from streamwatch.youtube import VideoInfo

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
START = NOW + timedelta(hours=8)


class FakeCalendar:
    def __init__(self):
        self.events = {}
        self.fail_with = None
        self.connected = True
        self.calendar_id = "streams@group.calendar.google.com"

    async def create_event(self, calendar_id, title, start, description):
        if self.fail_with:
            raise self.fail_with
        event_id = f"ev{len(self.events) + 1}"
        self.events[event_id] = {"calendar": calendar_id, "title": title, "start": start}
        return event_id

    async def move_event(self, calendar_id, event_id, start, title):
        self.events[event_id].update(start=start, title=title)

    async def delete_event(self, calendar_id, event_id):
        if self.fail_with:
            raise self.fail_with
        self.events.pop(event_id, None)


class CalendarFlowTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        config = Config("x", 1, 42, "k", "America/Los_Angeles", self.path, Path("/nonexistent"),
                        "streams@group.calendar.google.com")
        self.bot = StreamWatch(config)
        self.bot.calendar = FakeCalendar()
        self.sent = []

        async def fake_send(channel_name, **kwargs):
            self.sent.append((channel_name, kwargs.get("content")))
        self.bot.send = fake_send
        self.db = self.bot.db
        self.streamer, _ = self.db.add_streamer("UC1", "@vanta", "Vantacrow", "America/Los_Angeles")
        self.entry = apply_update(self.db, self.streamer, "v1",
                                  VideoInfo("v1", "UC1", "Silent Hill pt 3", "upcoming", START, None, False), NOW)[0].entry

    async def asyncTearDown(self):
        self.db.conn.close()
        os.remove(self.path)

    async def test_pick_creates_event_in_configured_calendar(self):
        self.assertIsNone(await self.bot.pick(self.entry))
        entry = self.db.get_entry(self.entry.id)
        self.assertTrue(entry.picked)
        self.assertEqual(entry.calendar_id, "streams@group.calendar.google.com")
        event = self.bot.calendar.events[entry.gcal_event_id]
        self.assertEqual(event["title"], "💜 Vantacrow: Silent Hill pt 3")
        self.assertEqual(event["start"], START)

    async def test_double_pick_does_not_duplicate_event(self):
        await self.bot.pick(self.entry)
        await self.bot.pick(self.db.get_entry(self.entry.id))
        self.assertEqual(len(self.bot.calendar.events), 1)

    async def test_unpick_deletes_event(self):
        await self.bot.pick(self.entry)
        self.assertIsNone(await self.bot.unpick(self.db.get_entry(self.entry.id)))
        self.assertEqual(self.bot.calendar.events, {})
        self.assertFalse(self.db.get_entry(self.entry.id).picked)

    async def test_reschedule_moves_event(self):
        await self.bot.pick(self.entry)
        later = START + timedelta(hours=2)
        events = apply_update(self.db, self.streamer, "v1",
                              VideoInfo("v1", "UC1", "Silent Hill pt 3", "upcoming", later, None, False), NOW)
        await self.bot.sync_calendar_change(events[0].entry)
        entry = self.db.get_entry(self.entry.id)
        self.assertEqual(self.bot.calendar.events[entry.gcal_event_id]["start"], later)

    async def test_cancel_deletes_event(self):
        await self.bot.pick(self.entry)
        events = apply_update(self.db, self.streamer, "v1", None, NOW)
        await self.bot.sync_calendar_change(events[0].entry)
        self.assertEqual(self.bot.calendar.events, {})
        self.assertIsNone(self.db.get_entry(self.entry.id).gcal_event_id)

    async def test_calendar_not_connected_still_picks_for_alerts(self):
        self.bot.calendar.connected = False
        problem = await self.bot.pick(self.entry)
        self.assertIn("isn't connected", problem)
        self.assertTrue(self.db.get_entry(self.entry.id).picked)

    async def test_auth_failure_warns_once_in_bot_health(self):
        self.bot.calendar.fail_with = CalendarAuthError("invalid_grant")
        problem = await self.bot.pick(self.entry)
        self.assertIn("access stopped working", problem)
        self.assertTrue(self.db.get_entry(self.entry.id).picked)  # alerts still work
        await self.bot.pick(self.db.get_entry(self.entry.id))
        self.assertEqual([c for c, _ in self.sent], ["bot-health"])

    async def test_service_account_token_claims(self):
        claims = token_claims("bot@proj.iam.gserviceaccount.com", 1000)
        self.assertEqual(claims, {"iss": "bot@proj.iam.gserviceaccount.com", "scope": SCOPE, "aud": TOKEN_URL,
                                  "iat": 1000, "exp": 4600})
        self.assertEqual(SCOPE, "https://www.googleapis.com/auth/calendar.events")  # events only, nothing else

    async def test_not_connected_without_calendar_id_or_key(self):
        self.assertFalse(GoogleCalendar(None, Path("/nonexistent"), "cal@x").connected)
        self.assertFalse(GoogleCalendar(None, Path(__file__), None).connected)
        self.assertTrue(GoogleCalendar(None, Path(__file__), "cal@x").connected)

    async def test_event_title(self):
        self.assertEqual(event_title("Kuga Leo", "DEVOUR"), "💜 Kuga Leo: DEVOUR")
        self.assertEqual(event_title("Kuga Leo", "DEVOUR", is_collab=True), "🤝 💜 Kuga Leo: DEVOUR")


if __name__ == "__main__":
    unittest.main()
