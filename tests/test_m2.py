"""M2 tests: waiting rooms, go-live and surprise detection. Run with:  python3 -m unittest discover tests"""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from streamwatch.db import Database
from streamwatch.tracker import (Cancelled, LiveChanged, NewWaitingRoom, Rescheduled, SurpriseLive, WentLive,
                                 apply_update, is_free_chat)
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
        self.assertEqual([type(e) for e in events], [LiveChanged, WentLive])  # card redrawn + ping
        self.assertEqual(self.update("v1", video(broadcast="live", actual=NOW)), [])  # L-3

    def test_unpicked_scheduled_stream_does_not_alert(self):
        self.update("v1", video())
        # Only the card turns LIVE NOW; no ping (L-2)
        self.assertEqual([type(e) for e in self.update("v1", video(broadcast="live", actual=NOW))], [LiveChanged])

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

    def test_short_notice_waiting_room_is_a_surprise_once(self):
        # L-2c: Gale Galleon opened a waiting room ~20 min before going live; that should ping like a surprise.
        soon = NOW + timedelta(minutes=20)
        self.assertIsInstance(self.update("v1", video(start=soon))[0], NewWaitingRoom)
        events = self.update("v1", video(broadcast="live", start=soon, actual=soon))
        self.assertEqual([type(e) for e in events], [LiveChanged, SurpriseLive])
        self.assertTrue(events[1].short_notice)
        self.assertEqual(self.update("v1", video(broadcast="live", start=soon, actual=soon)), [])

    def test_waiting_room_an_hour_or_more_ahead_is_not_a_surprise(self):
        start = NOW + timedelta(minutes=61)
        self.update("v1", video(start=start))
        self.assertEqual([type(e) for e in self.update("v1", video(broadcast="live", start=start, actual=start))],
                         [LiveChanged])

    def test_short_notice_stream_i_picked_alerts_as_picked(self):
        entry = self.update("v1", video(start=NOW + timedelta(minutes=20)))[0].entry
        self.db.set_picked(entry.id, True)
        events = self.update("v1", video(broadcast="live", actual=NOW))
        self.assertEqual([type(e) for e in events], [LiveChanged, WentLive])

    def test_short_notice_rule_skips_a_streamers_first_scan(self):
        self.update("v1", video(start=NOW + timedelta(minutes=20)), alerts=False)
        self.assertEqual([type(e) for e in self.update("v1", video(broadcast="live", actual=NOW))], [LiveChanged])

    def test_short_notice_waiting_room_for_a_scheduled_stream_is_not_a_surprise(self):
        # Announced days ago on a schedule image; the late waiting room only fills in the link.
        soon = NOW + timedelta(minutes=20)
        self.db.create_entry(self.streamer.id, soon, "Silent Hill pt 3", "image")
        self.update("v1", video(start=soon))
        self.assertEqual([type(e) for e in self.update("v1", video(broadcast="live", start=soon, actual=soon))],
                         [LiveChanged])

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
        # Ending redraws the card as ended; it isn't a cancellation
        self.assertEqual([type(e) for e in self.update("v1", video(broadcast="none", actual=NOW))], [LiveChanged])
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


class LiveCardTest(unittest.TestCase):
    def test_live_and_ended_cards(self):
        from streamwatch.db import Entry, Streamer
        from streamwatch.ui import RED, card_embed, card_view
        entry = Entry(19, 1, NOW, "【#FSFables 03】IT'S TIME TO GO ON A HUNT", "waiting_room", "UOdSb9fsC8c", "confirmed",
                      None, None, None, False)
        malim = Streamer(1, "UC1", "@malim", "Malim Cendari", "UTC", True, True)
        live = card_embed(entry, malim, [], ("live", NOW))
        self.assertTrue(live.title.startswith("🔴 LIVE NOW · "))
        self.assertEqual(live.color, RED)                      # the card's stripe turns red
        self.assertEqual([b.label for b in card_view(entry, ("live", NOW)).children], ["▶ Watch"])
        ended = card_embed(entry, malim, [], ("ended", NOW))
        self.assertTrue(ended.title.startswith("Ended · "))
        self.assertEqual([b.label for b in card_view(entry, ("ended", NOW)).children], ["▶ Watch the replay"])
        upcoming = card_embed(entry, malim, [], None)          # before it starts: the normal card
        self.assertFalse(upcoming.title.startswith("🔴"))


class TentativeTest(unittest.TestCase):
    def test_tentative_entries_are_marked_and_asked_about_only_when_picked(self):
        from streamwatch.db import Entry, Streamer
        from streamwatch.gcal import event_title
        from streamwatch.ui import schedule_embed
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db = Database(path)
        s, _ = db.add_streamer("UC1", "@a", "Alpha")
        soon = NOW + timedelta(hours=2)
        unpicked = db.create_entry(s.id, soon, "Unpicked", "image", tentative=True)
        picked = db.create_entry(s.id, soon, "Picked", "image", tentative=True)
        db.set_picked(picked.id, True)
        sure = db.create_entry(s.id, soon, "Sure", "image")
        db.set_picked(sure.id, True)
        self.assertEqual([e.id for e in db.unconfirmed_picks(NOW, NOW + timedelta(hours=3))], [picked.id])
        db.update_entry(picked.id, review_message_id=1)  # asked once only
        self.assertEqual(db.unconfirmed_picks(NOW, NOW + timedelta(hours=3)), [])
        self.assertEqual(event_title("Alpha", "Picked", tentative=True), "💜 Alpha: Picked (tentative)")
        embed = schedule_embed(s, [db.get_entry(unpicked.id), db.get_entry(sure.id)], {}, False)
        self.assertIn("⏳ *tentative*", embed.description)
        self.assertIn("⏳ = tentative", embed.footer.text)
        db.conn.close()
        os.remove(path)


class CommandsTest(unittest.TestCase):
    def test_all_slash_commands_are_registered(self):
        # /drop was once deleted by accident along with another command; this keeps the list honest.
        import discord
        from discord import app_commands
        from streamwatch.bot import register_commands

        class Stub:
            tree = app_commands.CommandTree(discord.Client(intents=discord.Intents.none()))
        register_commands(Stub)
        self.assertEqual({c.name for c in Stub.tree.get_commands()},
                         {"follow", "unfollow", "drop", "reading_note", "timezone", "digest", "list", "pause",
                          "resume"})
