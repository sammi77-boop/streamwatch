"""M4 tests: timezones, OCR cross-check, matching, and the image pipeline with fake readers."""

import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone

from streamwatch.db import Database
from streamwatch.pipeline import NeedsReview, NewImageEntry, process_image
from streamwatch.schedule import (ReadEntry, best_match, cross_check, date_in_text, resolve_timezone, times_in_text,
                                  title_similarity, to_utc)
from streamwatch.tracker import Rescheduled, SurpriseLive, Updated, WentLive, apply_update
from streamwatch.youtube import VideoInfo

# Tuesday 13 Oct 2026, 18:00 UTC
NOW = datetime(2026, 10, 13, 18, 0, tzinfo=timezone.utc)
OCR = """WEEKLY SCHEDULE
MON 12 OFF
TUE 13 7PM PT SILENT HILL TOWNFALL
WED 14 OFF
THU 15 5:30 PM PT Karaoke w/ Juno
FRI 16 9PM PT  Minecraft hardcore"""


def entry(day, time, title, tz="PT", conf=0.95, collab=False):
    return ReadEntry(day, time, tz, title, collab, [], conf)


class TimezoneTest(unittest.TestCase):
    def test_abbreviations_offsets_and_names(self):
        self.assertEqual(resolve_timezone("PT", "Asia/Tokyo")[1], "PT")
        self.assertEqual(str(resolve_timezone("JST", "UTC")[0]), "Asia/Tokyo")
        self.assertEqual(resolve_timezone("GMT+9", "UTC")[0].utcoffset(None), timedelta(hours=9))
        self.assertEqual(resolve_timezone("UTC-07:00", "UTC")[0].utcoffset(None), timedelta(hours=-7))
        self.assertEqual(str(resolve_timezone(None, "Asia/Tokyo")[0]), "Asia/Tokyo")  # X-4 fallback
        self.assertIsNotNone(resolve_timezone("Mars Time", "UTC")[2])

    def test_to_utc_handles_daylight_saving(self):
        start, label, issues = to_utc(entry("2026-10-13", "19:00", "x"), "UTC")
        self.assertEqual(start, datetime(2026, 10, 14, 2, 0, tzinfo=timezone.utc))  # PDT = UTC-7
        start, _, _ = to_utc(entry("2026-12-01", "19:00", "x"), "UTC")
        self.assertEqual(start, datetime(2026, 12, 2, 3, 0, tzinfo=timezone.utc))  # PST = UTC-8


class CrossCheckTest(unittest.TestCase):
    def test_times_in_text(self):
        found = times_in_text("7PM 5:30 PM 21:00 8：00 20時 9時30分 12am")
        for hhmm in ["19:00", "17:30", "21:00", "08:00", "20:00", "20:00", "09:30", "21:30", "00:00"]:
            h, m = map(int, hhmm.split(":"))
            self.assertIn(h * 60 + m, found, hhmm)
        self.assertEqual(times_in_text("MON 12 TUE 13"), set())  # bare numbers are dates, not times

    def test_date_in_text(self):
        self.assertTrue(date_in_text(date(2026, 10, 15), "THU 15"))
        self.assertTrue(date_in_text(date(2026, 10, 15), "Thursday Karaoke"))
        self.assertTrue(date_in_text(date(2026, 10, 15), "10/15（木）"))
        self.assertFalse(date_in_text(date(2026, 10, 15), "MON 12 TUE 13"))

    def test_agreement_and_disagreement(self):
        self.assertTrue(cross_check(entry("2026-10-15", "17:30", "Karaoke"), OCR, "UTC").ok)
        bad = cross_check(entry("2026-10-15", "18:30", "Karaoke"), OCR, "UTC")
        self.assertIn("time 18:30 not found by OCR", bad.issues)
        low = cross_check(entry("2026-10-15", "17:30", "Karaoke", conf=0.5), OCR, "UTC")
        self.assertIn("low confidence (50%)", low.issues)


class SlotDateTest(unittest.TestCase):
    # Rosco's real schedule: under "15th of October, THU": JST 07:00 (FRI) / EDT 6:00 PM / PDT 3:00 PM
    OCR = "15TH OF OCTOBER, THU FIRST STAGE FABLES | DAY 6 JST 07:00 (FRI) EDT 6:00 PM PDT 3:00 PM"

    def test_correct_reading_passes(self):
        good = ReadEntry("2026-10-15", "18:00", "EDT", "FIRST STAGE FABLES | DAY 6", slot_date="2026-10-15")
        checked = cross_check(good, self.OCR, "America/Los_Angeles")
        self.assertTrue(checked.ok, checked.issues)
        self.assertEqual(checked.start_utc, datetime(2026, 10, 15, 22, 0, tzinfo=timezone.utc))  # Thu 3 PM PDT

    def test_mixed_timezone_rows_go_to_review(self):
        # The bug: JST's time with the column's date → one day early
        bad = ReadEntry("2026-10-15", "07:00", "JST", "FIRST STAGE FABLES | DAY 6", slot_date="2026-10-15")
        self.assertTrue(cross_check(bad, self.OCR, "America/Los_Angeles").ok)  # OCR alone can't catch it...
        bad.other_times = [("2026-10-15", "15:00", "PDT"), ("2026-10-15", "18:00", "EDT")]
        self.assertIn("timezones disagree", " ".join(cross_check(bad, self.OCR, "America/Los_Angeles").issues))
        good = ReadEntry("2026-10-15", "18:00", "EDT", "DAY 6", slot_date="2026-10-15",
                         other_times=[("2026-10-16", "07:00", "JST"), ("2026-10-15", "15:00", "PDT")])
        self.assertTrue(cross_check(good, self.OCR, "America/Los_Angeles").ok)  # all three rows are one moment
        shifted = ReadEntry("2026-10-16", "07:00", "JST", "FIRST STAGE FABLES | DAY 6", slot_date="2026-10-15")
        issues = cross_check(shifted, self.OCR, "America/Los_Angeles").issues
        self.assertIn("differs from the day it's listed under", " ".join(issues))  # ...but the slot check does


class RealRoscoReadingTest(unittest.TestCase):
    """Readings Sonnet and Opus actually returned for Rosco's week-5 schedule (dry run, 2026-10-14)."""

    def test_one_misread_row_does_not_flag_a_correct_stream(self):
        day5 = ReadEntry("2026-10-14", "18:00", "EDT", "LOVEBYTE.EXE #FSFABLES | DAY 5", slot_date="2026-10-14",
                         other_times=[("2026-10-14", "07:00", "JST"), ("2026-10-14", "15:00", "PDT")])
        self.assertTrue(cross_check(day5, "14TH OF OCTOBER, WED JST 07:00 | 14:00 EDT 6 PM | 1 AM PDT 3 PM | 10 PM",
                                    "America/New_York").ok)

    def test_tilted_box_passes_if_any_agreeing_row_is_found(self):
        d9 = ReadEntry("2026-10-18", "20:00", "EDT", "FIRST STAGE FABLES | D9", slot_date="2026-10-18",
                       other_times=[("2026-10-19", "09:00", "JST"), ("2026-10-18", "17:00", "PDT")])
        ocr = "18TH OF OCTOBER, SUN FIRST STAGE FABLES D9 JST 09:00 (M) PDT 5:00 PM EDT 8:0O PN"  # 8 PM garbled
        self.assertTrue(cross_check(d9, ocr, "America/New_York").ok)

    def test_main_time_against_all_other_rows_is_still_caught(self):
        bug = ReadEntry("2026-10-15", "07:00", "JST", "DAY 6", slot_date="2026-10-15",
                        other_times=[("2026-10-15", "18:00", "EDT"), ("2026-10-15", "15:00", "PDT")])
        self.assertIn("timezones disagree",
                      " ".join(cross_check(bug, "THU 15 JST 07:00 (FRI) EDT 6:00 PM PDT 3:00 PM", "UTC").issues))


class MatchingTest(unittest.TestCase):
    class C:
        def __init__(self, start, title):
            self.start_utc, self.title = start, title

    def test_similarity(self):
        self.assertGreater(title_similarity("SILENT HILL TOWNFALL",
                                            "THIS TOWN IS SILENT. I AM NOT.【SILENT HILL: TOWNFALL】"), 0.5)
        self.assertGreater(title_similarity("DEVOUR", "【DEVOUR】 個人的に世界一怖いゲーム"), 0.5)
        self.assertLess(title_similarity("Minecraft", "Karaoke night"), 0.34)

    def test_best_match(self):
        t = NOW
        a, b = self.C(t, "Minecraft hardcore"), self.C(t + timedelta(minutes=60), "Karaoke")
        self.assertIs(best_match(t + timedelta(minutes=50), "Karaoke!", [a, b]), b)
        self.assertIsNone(best_match(t + timedelta(hours=3), "Karaoke", [a, b]))
        lone = self.C(t, "【雑談】wednesday my dudes")
        self.assertIs(best_match(t + timedelta(minutes=20), "Just Chatting", [lone]), lone)
        self.assertIsNone(best_match(t + timedelta(minutes=80), "Just Chatting", [lone]))


class PipelineTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = Database(self.path)
        self.streamer, _ = self.db.add_streamer("UC1", "@vanta", "Vantacrow", "America/Los_Angeles")
        self.calls = []

    async def asyncTearDown(self):
        self.db.conn.close()
        os.remove(self.path)

    def reader(self, first, second=None):
        async def read(model, images, name, tz, post_date, post_text):
            self.images_seen = images
            self.calls.append(model)
            return True, (first if model == "A" or second is None else second)
        return read

    async def ocr(self, image):
        return OCR

    async def read_image(self, first, second=None):
        return await process_image(self.db, self.streamer, [(b"img", "image/png")], self.reader(first, second), self.ocr,
                                   "A", "B", NOW, NOW)

    async def test_clean_schedule_adds_cards_and_skips_past_days(self):
        result = await self.read_image([entry("2026-10-12", "19:00", "old stream"),
                                 entry("2026-10-13", "19:00", "SILENT HILL TOWNFALL"),
                                 entry("2026-10-15", "17:30", "Karaoke w/ Juno", collab=True),
                                 entry("2026-10-16", "21:00", "Minecraft hardcore")])
        self.assertEqual([type(e) for e in result.events], [NewImageEntry] * 3)
        self.assertEqual(result.skipped_past, 1)
        self.assertEqual(self.calls, ["A"])  # no second opinion needed
        karaoke = result.events[1].entry
        self.assertEqual(karaoke.start_utc, datetime(2026, 10, 16, 0, 30, tzinfo=timezone.utc))
        self.assertEqual((karaoke.source, karaoke.orig_tz, karaoke.is_collab), ("image", "PT", True))
        self.assertIn("added", result.summary())

    async def test_disagreement_uses_second_opinion_when_it_agrees(self):
        result = await self.read_image([entry("2026-10-16", "22:00", "Minecraft")], [entry("2026-10-16", "21:00", "Minecraft")])
        self.assertEqual(self.calls, ["A", "B"])
        self.assertIsInstance(result.events[0], NewImageEntry)
        self.assertEqual(result.events[0].entry.start_utc, datetime(2026, 10, 17, 4, 0, tzinfo=timezone.utc))

    async def test_persistent_disagreement_goes_to_review(self):
        result = await self.read_image([entry("2026-10-16", "22:00", "Minecraft")], [entry("2026-10-16", "23:00", "Minecraft")])
        self.assertIsInstance(result.events[0], NeedsReview)
        self.assertEqual(result.events[0].entry.status, "pending_review")
        self.assertIn("not found by OCR", result.events[0].issues[0])

    async def test_unknown_timezone_goes_to_review_with_provisional_time(self):
        result = await self.read_image([entry("2026-10-16", "21:00", "Minecraft", tz="Moon Time")],
                                [entry("2026-10-16", "21:00", "Minecraft", tz="Moon Time")])
        self.assertIsInstance(result.events[0], NeedsReview)
        self.assertIn("unknown timezone", " ".join(result.events[0].issues))

    async def test_same_image_twice_is_not_duplicated(self):
        await self.read_image([entry("2026-10-16", "21:00", "Minecraft hardcore")])
        again = await self.read_image([entry("2026-10-16", "21:00", "Minecraft hardcore")])
        self.assertEqual(again.events, [])
        self.assertEqual(again.duplicates, 1)

    async def test_image_matches_existing_waiting_room(self):
        info = VideoInfo("v1", "UC1", "THIS TOWN IS SILENT.【SILENT HILL: TOWNFALL】", "upcoming",
                         datetime(2026, 10, 14, 2, 0, tzinfo=timezone.utc), None, False)
        room = apply_update(self.db, self.streamer, "v1", info, NOW)[0].entry
        result = await self.read_image([entry("2026-10-13", "19:00", "SILENT HILL TOWNFALL")])
        self.assertIsInstance(result.events[0], Updated)
        self.assertEqual(result.events[0].entry.id, room.id)
        self.assertEqual(result.events[0].entry.source, "both")

    async def test_newer_schedule_moves_image_stream(self):
        await self.read_image([entry("2026-10-16", "21:00", "Minecraft hardcore")])
        result = await self.read_image([entry("2026-10-16", "22:00", "Minecraft hardcore")],
                                [entry("2026-10-16", "22:00", "Minecraft hardcore")])
        # 22:00 isn't in this OCR text, so an unconfirmed time never moves a listed stream
        self.assertEqual(result.duplicates, 1)

    async def test_newer_schedule_confirmed_by_ocr_moves_image_stream(self):
        first = (await self.read_image([entry("2026-10-16", "21:00", "Minecraft hardcore")])).events[0].entry

        async def new_ocr(image):
            return "FRI 16 10PM PT Minecraft hardcore"
        result = await process_image(self.db, self.streamer, [(b"i", "image/png")],
                                     self.reader([entry("2026-10-16", "22:00", "Minecraft hardcore")]), new_ocr,
                                     "A", "B", NOW, NOW)
        self.assertIsInstance(result.events[0], Rescheduled)
        self.assertEqual(result.events[0].entry.id, first.id)
        self.assertEqual(result.events[0].old_start, first.start_utc)

    async def test_waiting_room_later_links_to_image_entry(self):
        result = await self.read_image([entry("2026-10-16", "21:00", "Minecraft hardcore")])
        image_entry = result.events[0].entry
        info = VideoInfo("v2", "UC1", "【Minecraft】 HARDCORE day 1", "upcoming",
                         datetime(2026, 10, 17, 4, 15, tzinfo=timezone.utc), None, False)
        events = apply_update(self.db, self.streamer, "v2", info, NOW)
        self.assertIsInstance(events[0], Rescheduled)  # waiting room's exact time wins
        self.assertEqual(events[0].entry.id, image_entry.id)
        self.assertEqual((events[0].entry.video_id, events[0].entry.source), ("v2", "both"))

    async def test_picked_image_stream_going_live_alerts_not_surprise(self):
        result = await self.read_image([entry("2026-10-16", "21:00", "Minecraft hardcore")])
        self.db.set_picked(result.events[0].entry.id, True)
        live = VideoInfo("v3", "UC1", "hardcore minecraft!!", "live", None,
                         datetime(2026, 10, 17, 4, 5, tzinfo=timezone.utc), False)
        events = apply_update(self.db, self.streamer, "v3", live, NOW)
        self.assertIsInstance(events[0], WentLive)
        self.assertEqual(events[0].entry.id, result.events[0].entry.id)

    async def test_unrelated_live_stream_is_still_a_surprise(self):
        await self.read_image([entry("2026-10-16", "21:00", "Minecraft hardcore")])
        live = VideoInfo("v4", "UC1", "late night karaoke", "live", None,
                         datetime(2026, 10, 15, 6, 0, tzinfo=timezone.utc), False)
        self.assertIsInstance(apply_update(self.db, self.streamer, "v4", live, NOW)[0], SurpriseLive)

    async def test_timezone_suggestion_once(self):
        self.streamer = self.db.get_streamer(self.db.add_streamer("UC2", "@claude", "Claude", "America/Los_Angeles")[0].id)
        jst = [ReadEntry("2026-10-16", "21:00", "JST", "Minecraft", False, [], 0.95)]

        async def ocr(image):
            return "FRI 16 21:00 JST Minecraft"
        result = await process_image(self.db, self.streamer, [(b"i", "image/png")], self.reader(jst), ocr, "A", "B", NOW, NOW)
        self.assertEqual((result.suggestion.written, result.suggestion.iana), ("JST", "Asia/Tokyo"))
        again = await process_image(self.db, self.streamer, [(b"i", "image/png")], self.reader(jst), ocr, "A", "B", NOW, NOW)
        self.assertIsNone(again.suggestion)

    async def test_schedule_split_across_two_images_is_read_together(self):
        ocr_calls = []

        async def ocr(image):
            ocr_calls.append(image)
            return {b"top": "TUE 13 7PM PT SILENT HILL", b"bottom": "FRI 16 9PM PT Minecraft"}[image]
        result = await process_image(self.db, self.streamer, [(b"top", "image/png"), (b"bottom", "image/jpeg")],
                                     self.reader([entry("2026-10-13", "19:00", "SILENT HILL"),
                                                  entry("2026-10-16", "21:00", "Minecraft")]), ocr, "A", "B", NOW, NOW)
        self.assertEqual(self.calls, ["A"])  # one Claude request for both halves
        self.assertEqual(len(self.images_seen), 2)
        self.assertEqual(ocr_calls, [b"top", b"bottom"])  # OCR'd separately, checked against both
        self.assertEqual([type(e) for e in result.events], [NewImageEntry, NewImageEntry])

    async def test_second_opinion_is_merged_per_stream(self):
        first = [entry("2026-10-15", "17:30", "Karaoke"), entry("2026-10-16", "22:00", "Minecraft hardcore")]
        second = [entry("2026-10-15", "18:30", "Karaoke"), entry("2026-10-16", "21:00", "Minecraft hardcore")]
        result = await self.read_image(first, second)
        # Karaoke: first reader was right; Minecraft: second reader was right. Both become cards.
        self.assertEqual([type(e) for e in result.events], [NewImageEntry, NewImageEntry])
        self.assertEqual([e.entry.start_utc.hour for e in result.events], [0, 4])

    async def test_not_a_schedule(self):
        async def read(*args):
            return False, []
        result = await process_image(self.db, self.streamer, [(b"i", "image/png")], read, self.ocr, "A", "B", NOW, NOW)
        self.assertFalse(result.is_schedule)
        self.assertIn("doesn't look like", result.summary())


if __name__ == "__main__":
    unittest.main()


class ScheduleImageTest(unittest.IsolatedAsyncioTestCase):
    async def test_image_entries_keep_the_schedule_image_and_cards_show_it(self):
        from streamwatch.ui import card_embed
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db = Database(path)
        streamer, _ = db.add_streamer("UC1", "@vanta", "Vantacrow", "America/Los_Angeles")

        async def read(model, images, name, tz, post_date, post_text):
            return True, [entry("2026-10-16", "21:00", "Minecraft hardcore")]

        async def ocr(image):
            return OCR
        result = await process_image(db, streamer, [(b"i", "image/png")], read, ocr, "A", "B", NOW, NOW,
                                     image_path="/data/schedule_images/1_x.jpg")
        card_entry = result.events[0].entry
        self.assertEqual(card_entry.image_path, "/data/schedule_images/1_x.jpg")
        self.assertEqual(card_embed(card_entry, streamer).thumbnail.url, "attachment://schedule.jpg")
        # Once a waiting room matches, the video thumbnail is shown instead
        linked = db.update_entry(card_entry.id, video_id="abc", source="both")
        self.assertIn("ytimg.com/vi/abc", card_embed(linked, streamer).thumbnail.url)
        db.conn.close()
        os.remove(path)

    def test_stitch_stacks_parts_at_one_width(self):
        import io
        from PIL import Image
        from streamwatch.ocr import stitch

        def png(w, h, color):
            out = io.BytesIO()
            Image.new("RGB", (w, h), color).save(out, format="PNG")
            return out.getvalue()
        combined = Image.open(io.BytesIO(stitch([png(800, 400, "red"), png(400, 400, "blue")])))
        self.assertEqual(combined.size, (800, 1200))  # second part scaled up to 800 wide → 800 tall
        self.assertEqual(combined.getpixel((10, 10))[0] > 200, True)      # top is red
        self.assertEqual(combined.getpixel((10, 1100))[2] > 200, True)    # bottom is blue
