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
        self.events[event_id] = {"calendar": calendar_id, "title": title, "start": start, "description": description}
        return event_id

    async def move_event(self, calendar_id, event_id, start, title, description=None):
        self.events[event_id].update(start=start, title=title, description=description)

    async def delete_event(self, calendar_id, event_id):
        if self.fail_with:
            raise self.fail_with
        self.events.pop(event_id, None)


class CalendarFlowTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        config = Config("x", 1, 42, "k", self.path, Path("/nonexistent"),
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

    async def test_collab_found_later_updates_calendar_event(self):
        await self.bot.pick(self.entry)
        self.db.save_handle_names({"@dubstep_ch": "Dubstep 【NIJISANJI EN】"})
        info = VideoInfo("v1", "UC1", "Silent Hill pt 3", "upcoming", START, None, False,
                         "Collab w/\n@DubsteP_ch\n\nmerch")
        events = apply_update(self.db, self.streamer, "v1", info, NOW)
        self.assertEqual(type(events[0]).__name__, "Updated")
        await self.bot.sync_calendar_change(events[0].entry)
        event = self.bot.calendar.events[self.db.get_entry(self.entry.id).gcal_event_id]
        self.assertEqual(event["title"], "🤝 💜 Vantacrow: Silent Hill pt 3")
        self.assertIn("Collab with: Dubstep", event["description"])

    async def test_edit_image_entry_moves_calendar_event(self):
        image = self.db.create_entry(self.streamer.id, START, "Day 6", "image", orig_tz="EDT")
        await self.bot.pick(image)
        later = START + timedelta(days=1)

        async def no_card(entry, streamer):
            pass
        self.bot.refresh_card = no_card
        await self.bot.edit_entry(image.id, later, "FIRST STAGE FABLES | DAY 6", "EDT")
        fixed = self.db.get_entry(image.id)
        self.assertEqual((fixed.start_utc, fixed.previous_start, fixed.title), (later, START,
                                                                               "FIRST STAGE FABLES | DAY 6"))
        self.assertEqual(self.bot.calendar.events[fixed.gcal_event_id]["start"], later)

    async def test_delete_entry_removes_calendar_event(self):
        image = self.db.create_entry(self.streamer.id, START, "Day 6", "image", orig_tz="EDT")
        await self.bot.pick(image)
        self.assertIsNone(await self.bot.delete_entry(image.id))
        self.assertEqual(self.bot.calendar.events, {})
        gone = self.db.get_entry(image.id)
        self.assertEqual((gone.status, gone.picked), ("discarded", False))
        self.assertNotIn(gone.id, [e.id for e in self.db.entries_for_streamer(self.streamer.id)])

    async def test_event_title(self):
        self.assertEqual(event_title("Kuga Leo", "DEVOUR"), "💜 Kuga Leo: DEVOUR")
        self.assertEqual(event_title("Kuga Leo", "DEVOUR", is_collab=True), "🤝 💜 Kuga Leo: DEVOUR")


if __name__ == "__main__":
    unittest.main()


class ScheduleTableTest(unittest.IsolatedAsyncioTestCase):
    """One table per dropped schedule, with the calendar dropdown syncing picks."""

    async def asyncSetUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        config = Config("x", 1, 42, "k", self.path, Path("/nonexistent"), "cal@group")
        self.bot = StreamWatch(config)
        self.bot.calendar = FakeCalendar()

        async def fake_send(channel_name, **kwargs):
            pass
        self.bot.send = fake_send
        self.db = self.bot.db
        self.streamer, _ = self.db.add_streamer("UCr", "@rosco", "Rosco", "America/New_York")
        self.schedule_id = self.db.create_schedule(self.streamer.id, None)
        mk = lambda days, title, sub, **kw: self.db.create_entry(
            self.streamer.id, START + timedelta(days=days), title, "image", schedule_id=self.schedule_id,
            subtitle=sub, orig_tz="JST", **kw)
        self.day6 = mk(1, "FIRST STAGE FABLES | DAY 6", "READY TO ROCK AND ROLLO!")
        self.day7 = mk(2, "FIRST STAGE FABLES | DAY 7", None, is_collab=True, partners=["Juno", "Millie"])
        self.d9 = mk(4, "FIRST STAGE FABLES | D9", "TICK-TOCK", status="pending_review")

    async def asyncTearDown(self):
        self.db.conn.close()
        os.remove(self.path)

    async def test_dropdown_adds_and_removes_calendar_events(self):
        self.assertEqual(await self.bot.set_schedule_picks(self.schedule_id, {self.day6.id, self.day7.id}), [])
        self.assertEqual(len(self.bot.calendar.events), 2)
        self.assertTrue(self.db.get_entry(self.day7.id).picked)
        await self.bot.set_schedule_picks(self.schedule_id, {self.day7.id})  # untick Day 6
        self.assertFalse(self.db.get_entry(self.day6.id).picked)
        self.assertEqual(len(self.bot.calendar.events), 1)
        await self.bot.set_schedule_picks(self.schedule_id, {self.d9.id})  # pending entries can't be picked
        self.assertFalse(self.db.get_entry(self.d9.id).picked)

    async def test_table_lines(self):
        from streamwatch.ui import schedule_embed, schedule_view
        self.db.set_picked(self.day6.id, True)
        entries = self.db.schedule_entries(self.schedule_id)
        text = schedule_embed(self.streamer, entries, {self.day7.id: ["Juno", "Millie"]}, False).description
        self.assertIn("**FIRST STAGE FABLES | DAY 6** · *READY TO ROCK AND ROLLO!* 💜", text)
        self.assertIn("🤝 *with Juno, Millie*", text)
        self.assertIn("⏳ *check in #needs-review*", text)
        self.assertIn(f"<t:{int(START.timestamp()) + 86400}:F>", text)  # Discord shows it in local time
        self.assertNotIn("🤝", text.split("DAY 7")[0])  # only collabs get a partner line
        view = schedule_view(self.schedule_id, entries, "America/Los_Angeles")
        placeholders = [c.item.placeholder for c in view.children]
        self.assertEqual(len(placeholders), 3)
        calendar = view.children[0].item
        self.assertEqual(len(calendar.options), 2)  # pending D9 isn't offered yet
        self.assertEqual([o.default for o in calendar.options], [True, False])
        self.assertTrue(calendar.options[0].label.startswith("Thu 15 Oct 1:00 PM"))  # 20:00 UTC in PDT
