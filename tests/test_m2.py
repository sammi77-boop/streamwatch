"""M2 tests: waiting rooms, go-live and surprise detection. Run with:  python3 -m unittest discover tests"""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from streamwatch.db import Database
from streamwatch.tracker import (Cancelled, NewWaitingRoom, Rescheduled, SurpriseLive, WentLive, apply_update,
                                 is_free_chat)
from streamwatch.youtube import VideoInfo, parse_rss_video_ids, parse_video_item

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


def video(vid="v1", broadcast="upcoming", start=NOW + timedelta(hours=8), actual=None, title="Silent Hill pt 3",
          premiere=False):
    return VideoInfo(vid, "UC1", title, broadcast, start, actual, premiere)


class TrackerTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = Database(self.path)
        self.streamer, _ = self.db.add_streamer("UC1", "@vanta", "Vantacrow", "America/Los_Angeles")

    def tearDown(self):
        self.db.conn.close()
        os.remove(self.path)

    def update(self, vid, info, alerts=True):
        return apply_update(self.db, self.streamer, vid, info, NOW, alerts_enabled=alerts)

    def test_new_waiting_room_creates_one_entry(self):
        events = self.update("v1", video())
        self.assertEqual(len(events), 1)
        self.assertIsInstance(events[0], NewWaitingRoom)
        self.assertEqual(events[0].entry.start_utc, NOW + timedelta(hours=8))
        self.assertEqual(self.update("v1", video()), [])  # unchanged → nothing
        self.assertEqual(self.db.video_ids_to_poll(), ["v1"])

    def test_picked_stream_going_live_alerts_once(self):
        entry = self.update("v1", video())[0].entry
        self.db.set_picked(entry.id, True)
        events = self.update("v1", video(broadcast="live", actual=NOW))
        self.assertEqual(len(events), 1)
        self.assertIsInstance(events[0], WentLive)
        self.assertEqual(self.update("v1", video(broadcast="live", actual=NOW)), [])  # L-3

    def test_unpicked_scheduled_stream_does_not_alert(self):
        self.update("v1", video())
        self.assertEqual(self.update("v1", video(broadcast="live", actual=NOW)), [])

    def test_unannounced_stream_is_a_surprise_once(self):
        events = self.update("v9", video("v9", broadcast="live", start=None, actual=NOW))
        self.assertEqual(len(events), 1)
        self.assertIsInstance(events[0], SurpriseLive)
        self.assertEqual(self.update("v9", video("v9", broadcast="live", start=None, actual=NOW)), [])

    def test_first_scan_does_not_alert_for_streams_already_live(self):
        self.assertEqual(self.update("v9", video("v9", broadcast="live", actual=NOW), alerts=False), [])
        self.assertEqual(self.update("v9", video("v9", broadcast="live", actual=NOW)), [])

    def test_first_scan_still_posts_waiting_rooms(self):
        events = self.update("v1", video(), alerts=False)
        self.assertIsInstance(events[0], NewWaitingRoom)

    def test_reschedule(self):
        self.update("v1", video())
        events = self.update("v1", video(start=NOW + timedelta(hours=10)))
        self.assertIsInstance(events[0], Rescheduled)
        self.assertEqual(events[0].old_start, NOW + timedelta(hours=8))
        self.assertEqual(events[0].entry.start_utc, NOW + timedelta(hours=10))
        self.assertEqual(events[0].entry.previous_start, NOW + timedelta(hours=8))

    def test_small_time_jitter_is_not_a_reschedule(self):
        self.update("v1", video())
        self.assertEqual(self.update("v1", video(start=NOW + timedelta(hours=8, seconds=30))), [])

    def test_deleted_waiting_room_is_cancelled(self):
        self.update("v1", video())
        events = self.update("v1", None)
        self.assertIsInstance(events[0], Cancelled)
        self.assertEqual(events[0].entry.status, "cancelled")
        self.assertEqual(self.db.video_ids_to_poll(), [])
        self.assertEqual(self.update("v1", None), [])

    def test_waiting_room_turned_into_normal_video_is_cancelled(self):
        self.update("v1", video())
        events = self.update("v1", video(broadcast="none", start=None))
        self.assertIsInstance(events[0], Cancelled)

    def test_finished_stream_is_not_cancelled(self):
        entry = self.update("v1", video())[0].entry
        self.db.set_picked(entry.id, True)
        self.update("v1", video(broadcast="live", actual=NOW))
        self.assertEqual(self.update("v1", video(broadcast="none", actual=NOW)), [])
        self.assertEqual(self.db.get_entry(entry.id).status, "confirmed")
        self.assertEqual(self.db.video_ids_to_poll(), [])

    def test_free_chat_and_premieres_are_ignored(self):
        self.assertEqual(self.update("fc", video("fc", title="【フリーチャット】free chat")), [])
        self.assertEqual(self.update("far", video("far", start=NOW + timedelta(days=700))), [])
        self.assertEqual(self.update("pm", video("pm", premiere=True)), [])
        self.assertEqual(self.update("fc", video("fc", broadcast="live", title="【フリーチャット】")), [])
        self.assertEqual(self.db.video_ids_to_poll(), [])

    def test_old_vods_from_rss_are_ignored(self):
        self.assertEqual(self.update("old", video("old", broadcast="none", start=None)), [])
        self.assertEqual(self.db.video_ids_to_poll(), [])

    def test_free_chat_detection(self):
        self.assertTrue(is_free_chat(video(title="FREE CHAT ROOM"), NOW))
        self.assertTrue(is_free_chat(video(title="Free-chat"), NOW))
        self.assertFalse(is_free_chat(video(title="Chatting with chat!"), NOW))
        self.assertFalse(is_free_chat(video(start=NOW + timedelta(days=29)), NOW))

    def test_pick_and_unpick(self):
        entry = self.update("v1", video())[0].entry
        self.assertFalse(entry.picked)
        self.db.set_picked(entry.id, True)
        self.db.set_picked(entry.id, True)  # double tap is harmless
        self.assertTrue(self.db.get_entry(entry.id).picked)
        self.db.set_picked(entry.id, False)
        self.assertFalse(self.db.get_entry(entry.id).picked)


class ParsingTest(unittest.TestCase):
    def test_rss(self):
        xml = """<?xml version="1.0"?><feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
                 xmlns="http://www.w3.org/2005/Atom"><entry><yt:videoId>abc</yt:videoId></entry>
                 <entry><yt:videoId>def</yt:videoId></entry></feed>"""
        self.assertEqual(parse_rss_video_ids(xml), ["abc", "def"])

    def test_videos_list_item(self):
        item = {"id": "5ajkvBpvdks",
                "snippet": {"channelId": "UC1", "title": "SILENT HILL", "liveBroadcastContent": "upcoming"},
                "liveStreamingDetails": {"scheduledStartTime": "2026-10-14T03:30:00Z"},
                "contentDetails": {"duration": "P0D"}}
        info = parse_video_item(item)
        self.assertEqual(info.broadcast, "upcoming")
        self.assertEqual(info.scheduled_start, datetime(2026, 10, 14, 3, 30, tzinfo=timezone.utc))
        self.assertFalse(info.is_premiere)
        item["contentDetails"]["duration"] = "PT3M12S"
        self.assertTrue(parse_video_item(item).is_premiere)


class MigrationTest(unittest.TestCase):
    def test_m1_database_gains_new_column(self):
        import sqlite3
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE streamers (id INTEGER PRIMARY KEY, channel_id TEXT NOT NULL UNIQUE, handle TEXT, "
                     "name TEXT NOT NULL, home_tz TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, "
                     "created_at TEXT NOT NULL)")
        conn.execute("INSERT INTO streamers VALUES (1, 'UC1', '@a', 'A', 'UTC', 1, '2026-10-14')")
        conn.commit()
        conn.close()
        db = Database(path)
        self.assertFalse(db.list_streamers()[0].bootstrapped)
        db.conn.close()
        os.remove(path)


if __name__ == "__main__":
    unittest.main()
