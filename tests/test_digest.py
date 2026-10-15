"""M5 weekly digest: timing and contents."""

import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from streamwatch.db import Database
from streamwatch.digest import DigestLine, build_digest, digest_due, next_week

PT = ZoneInfo("America/Los_Angeles")


class TimingTest(unittest.TestCase):
    def test_due_sunday_evening_and_monday_morning_only(self):
        self.assertEqual(digest_due(datetime(2026, 10, 18, 20, 0, tzinfo=PT)), date(2026, 10, 19))   # Sun 8 PM
        self.assertEqual(digest_due(datetime(2026, 10, 18, 23, 59, tzinfo=PT)), date(2026, 10, 19))
        self.assertIsNone(digest_due(datetime(2026, 10, 18, 19, 59, tzinfo=PT)))                    # Sun 7:59 PM
        self.assertEqual(digest_due(datetime(2026, 10, 19, 9, 0, tzinfo=PT)), date(2026, 10, 19))   # missed: Mon AM
        self.assertIsNone(digest_due(datetime(2026, 10, 19, 13, 0, tzinfo=PT)))
        self.assertIsNone(digest_due(datetime(2026, 10, 21, 20, 0, tzinfo=PT)))                      # Wednesday

    def test_next_week_window(self):
        start, end = next_week(datetime(2026, 10, 18, 20, 0, tzinfo=PT))
        self.assertEqual((start.date(), end.date()), (date(2026, 10, 19), date(2026, 10, 26)))


class ContentTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = Database(self.path)
        self.s, _ = self.db.add_streamer("UC1", "@rosco", "Rosco Graves【FIRST STAGE PRODUCTION EN】")
        self.start = datetime(2026, 10, 19, tzinfo=PT)
        self.end = self.start + timedelta(days=7)

    def tearDown(self):
        self.db.conn.close()
        os.remove(self.path)

    def add(self, days, hour, title, **kw):
        return self.db.create_entry(self.s.id, (self.start + timedelta(days=days, hours=hour)).astimezone(timezone.utc),
                                    title, "image", **kw)

    def test_grouped_by_day_with_markers(self):
        day6 = self.add(3, 15, "FIRST STAGE FABLES | DAY 6", subtitle="READY TO ROCK AND ROLLO!")
        self.db.set_picked(day6.id, True)
        collab = self.add(0, 18, "Storebound", is_collab=True, partners=["Gale", "Nayu"])
        self.add(8, 15, "next week")                       # outside the window
        self.add(1, 15, "dropped", status="discarded")     # not confirmed
        entries = self.db.entries_between(self.start, self.end)
        self.assertEqual([e.title for e in entries], ["Storebound", "FIRST STAGE FABLES | DAY 6"])
        items = [DigestLine(e, self.s.name, ["Gale", "Nayu"] if e.is_collab else [], "https://discord.com/x")
                 for e in entries]
        msg = build_digest(items, self.start, self.end, "Your week ahead")
        self.assertIn("**2** streams · 1 collabs · 1 on your calendar", msg["content"])
        self.assertIn("Mon 19 Oct – Sun 25 Oct", msg["content"])
        self.assertEqual([e.title for e in msg["embeds"]], ["Monday 19 October", "Thursday 22 October"])
        thursday = msg["embeds"][1].description
        self.assertIn("Rosco Graves · **FIRST STAGE FABLES | DAY 6** · *READY TO ROCK AND ROLLO!* 💜", thursday)
        self.assertIn("[↗](https://discord.com/x)", thursday)
        self.assertIn("🤝 *with Gale, Nayu*", msg["embeds"][0].description)

    def test_empty_week(self):
        msg = build_digest([], self.start, self.end, "Your week ahead")
        self.assertIn("no streams listed yet", msg["content"])
        self.assertNotIn("embeds", msg)


if __name__ == "__main__":
    unittest.main()
