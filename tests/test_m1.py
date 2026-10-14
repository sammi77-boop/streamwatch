"""M1 unit tests: parsing and storage. Run with:  python3 -m unittest discover tests"""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from streamwatch.db import PAUSED_FOREVER, Database
from streamwatch.parsing import parse_channel_url, parse_duration, suggest_timezones, valid_timezone


class ParseChannelUrlTest(unittest.TestCase):
    def test_handle_forms(self):
        for text in ("https://www.youtube.com/@roscograves", "www.youtube.com/@roscograves",
                     "youtube.com/@roscograves/", "https://m.youtube.com/@roscograves/streams",
                     "  https://www.youtube.com/@roscograves?si=abc  "):
            self.assertEqual(parse_channel_url(text), {"handle": "@roscograves"}, text)

    def test_handle_with_underscore_dot_dash(self):
        self.assertEqual(parse_channel_url("youtube.com/@kugaleo_Q"), {"handle": "@kugaleo_Q"})
        self.assertEqual(parse_channel_url("youtube.com/@a.b-c"), {"handle": "@a.b-c"})

    def test_channel_id(self):
        self.assertEqual(parse_channel_url("https://www.youtube.com/channel/UCX6OQ3DkcsbYNE6H8uQQuVA"),
                         {"channel_id": "UCX6OQ3DkcsbYNE6H8uQQuVA"})

    def test_rejects_non_channel_links(self):
        for text in ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "https://youtu.be/dQw4w9WgXcQ",
                     "https://x.com/@roscograves", "roscograves", "", "https://www.youtube.com/c/oldname",
                     "https://evil.com/youtube.com/@x"):
            self.assertIsNone(parse_channel_url(text), text)


class ParseDurationTest(unittest.TestCase):
    def test_units(self):
        self.assertEqual(parse_duration("30m"), timedelta(minutes=30))
        self.assertEqual(parse_duration("12h"), timedelta(hours=12))
        self.assertEqual(parse_duration("3d"), timedelta(days=3))
        self.assertEqual(parse_duration("1w"), timedelta(weeks=1))
        self.assertEqual(parse_duration("1d12h"), timedelta(days=1, hours=12))
        self.assertEqual(parse_duration(" 2D "), timedelta(days=2))

    def test_invalid(self):
        for text in ("", "3", "d", "3x", "abc", "3d junk", "0h", "-1d"):
            self.assertIsNone(parse_duration(text), text)


class TimezoneTest(unittest.TestCase):
    def test_timezones(self):
        self.assertTrue(valid_timezone("America/New_York"))
        self.assertTrue(valid_timezone("Asia/Taipei"))
        for bad in ("Mars/Olympus", "", "America", "../etc/passwd"):
            self.assertFalse(valid_timezone(bad), bad)


    def test_suggestions(self):
        self.assertEqual(suggest_timezones("")[0], "America/Los_Angeles")
        self.assertEqual(suggest_timezones("tok")[0], "Asia/Tokyo")
        self.assertEqual(suggest_timezones("vla")[0], "Asia/Vladivostok")
        self.assertIn("America/New_York", suggest_timezones("new york"))
        self.assertTrue(all(valid_timezone(tz) for tz in suggest_timezones("a")))
        self.assertLessEqual(len(suggest_timezones("a")), 25)
        self.assertEqual(suggest_timezones("zzzz"), [])


class DatabaseTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = Database(self.path)

    def tearDown(self):
        self.db.conn.close()
        os.remove(self.path)

    def test_follow_list_unfollow_refollow(self):
        s, created = self.db.add_streamer("UC1", "@rosco", "Rosco Graves", "America/New_York")
        self.assertTrue(created)
        _, created_again = self.db.add_streamer("UC1", "@rosco", "Rosco Graves", "America/New_York")
        self.assertFalse(created_again)
        self.db.add_streamer("UC2", "@kugaleo_Q", "Kuga Leo", "Asia/Tokyo")
        self.assertEqual([x.name for x in self.db.list_streamers()], ["Kuga Leo", "Rosco Graves"])

        self.assertEqual(self.db.find_active_streamer("@ROSCO").id, s.id)
        self.assertEqual(self.db.find_active_streamer("rosco graves").id, s.id)
        self.db.deactivate_streamer(s.id)
        self.assertEqual([x.name for x in self.db.list_streamers()], ["Kuga Leo"])
        self.assertIsNone(self.db.find_active_streamer("rosco"))

        again, created = self.db.add_streamer("UC1", "@rosco", "Rosco Graves", "Europe/London")
        self.assertTrue(created)
        self.assertEqual(again.id, s.id)
        self.assertEqual(again.home_tz, "Europe/London")

    def test_names_with_stray_spaces_still_match(self):
        s, _ = self.db.add_streamer("UCrosco", "@roscograves", "Rosco Graves【FIRST STAGE PRODUCTION EN】 ", "UTC")
        self.assertEqual(s.name, "Rosco Graves【FIRST STAGE PRODUCTION EN】")
        self.assertEqual(self.db.find_active_streamer("UCrosco").id, s.id)       # what autocomplete sends
        self.assertEqual(self.db.find_active_streamer("rosco graves【first stage production en】  ").id, s.id)
        self.db.conn.execute("UPDATE streamers SET name = name || '  ' WHERE id = ?", (s.id,))
        self.db.conn.commit()
        self.db.conn.close()
        self.db = Database(self.path)  # restart cleans up names saved before the fix
        self.assertEqual(self.db.get_streamer(s.id).name, "Rosco Graves【FIRST STAGE PRODUCTION EN】")

    def test_set_home_tz(self):
        s, _ = self.db.add_streamer("UC9", "@claudeclawmark", "Claude Clawmark", "America/Los_Angeles")
        self.db.set_home_tz(s.id, "Asia/Tokyo")
        self.assertEqual(self.db.find_active_streamer("@claudeclawmark").home_tz, "Asia/Tokyo")

    def test_pause_forever_and_resume(self):
        self.assertFalse(self.db.is_paused())
        self.db.pause(None)
        self.assertEqual(self.db.paused_until(), PAUSED_FOREVER)
        self.db.resume()
        self.assertFalse(self.db.is_paused())

    def test_timed_pause_expires(self):
        now = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
        self.db.pause(now + timedelta(days=3))
        self.assertTrue(self.db.is_paused(now + timedelta(days=2)))
        self.assertFalse(self.db.is_paused(now + timedelta(days=3, seconds=1)))
        self.assertIsNone(self.db.get_setting("paused_until"))

    def test_pause_survives_restart(self):
        self.db.pause(None)
        self.db.conn.close()
        self.db = Database(self.path)
        self.assertTrue(self.db.is_paused())


if __name__ == "__main__":
    unittest.main()
