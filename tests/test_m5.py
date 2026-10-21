"""M5 tests: reading schedule images from YouTube community posts (option A: text clues only)."""

import json
import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from streamwatch.community import full_size, looks_like_schedule, parse_posts, post_age, worth_reading


def page(posts):
    data = {"contents": {"tabs": [{"items": [{"backstagePostThreadRenderer": {"post": {"backstagePostRenderer": p}}}
                                             for p in posts]}]}}
    return f"<script>var ytInitialData = {json.dumps(data)};</script>"


def post(post_id, text, n_images=1, when="9 days ago", members=False):
    images = [{"backstageImageRenderer": {"image": {"thumbnails": [
        {"url": f"https://yt3.ggpht.com/{post_id}{i}=s288-c", "width": 288},
        {"url": f"https://yt3.ggpht.com/{post_id}{i}=s900-c-fcrop64=1,0-rw-nd-v1", "width": 900}]}}}
        for i in range(n_images)]
    attachment = ({"postMultiImageRenderer": {"images": images}} if n_images > 1 else images[0] if images else {})
    p = {"postId": post_id, "contentText": {"runs": [{"text": text}]}, "backstageAttachment": attachment,
         "publishedTimeText": {"runs": [{"text": when}]}}
    if members:
        p["sponsorsOnlyBadge"] = {"x": 1}
    return p


class ParseTest(unittest.TestCase):
    def test_real_caption_styles(self):
        self.assertTrue(looks_like_schedule("🕹️ Schedule for October 5th - October 11th 🩻"))   # Rosco
        self.assertTrue(looks_like_schedule("Schedule for the week🎸🌙\nSomething is happening..."))  # Lucien
        self.assertTrue(looks_like_schedule("今週の配信予定です！"))
        self.assertTrue(looks_like_schedule("Mon: Minecraft\nTue: off\nWed: karaoke\nFri: collab"))
        self.assertFalse(looks_like_schedule("Its a me"))                                       # Vantacrow art
        self.assertFalse(looks_like_schedule("🎁久我レオ2026年誕生日グッズ🎁"))                    # Kuga merch

    def test_short_captions_are_read_too(self):
        # Vantacrow posts schedules with one-line jokes and no schedule words
        self.assertTrue(worth_reading("I'm your Huckleberry"))
        self.assertTrue(worth_reading("SORRY FOR JUST UPLOADING THIS HERE NOW RAHHHH"))
        self.assertTrue(worth_reading(""))
        long_letter = "Hey Crew, my life has been really hectic lately with lots of new responsibilities " * 2
        self.assertFalse(worth_reading(long_letter))
        self.assertFalse(looks_like_schedule("See you Monday!"))

    def test_parse_posts(self):
        posts = parse_posts(page([post("A", "Schedule!", 2), post("B", "merch", 1, "1 month ago", members=True),
                                  post("C", "text only", 0)]))
        self.assertEqual([p.post_id for p in posts], ["A", "B", "C"])
        self.assertEqual(posts[0].image_urls, ["https://yt3.ggpht.com/A0=s0", "https://yt3.ggpht.com/A1=s0"])
        self.assertTrue(posts[1].members_only)
        self.assertEqual(posts[2].image_urls, [])
        with self.assertRaises(Exception):
            parse_posts("<html>no data</html>")

    def test_age_and_full_size(self):
        self.assertEqual(post_age("9 days ago"), timedelta(days=9))
        self.assertEqual(post_age("3 weeks ago (edited)"), timedelta(weeks=3))
        self.assertEqual(post_age("1 hour ago"), timedelta(hours=1))
        self.assertIsNone(post_age("yesterday-ish"))
        self.assertEqual(full_size("https://yt3.ggpht.com/abc=s900-c-fcrop64=1,0-rw-nd-v1"), "https://yt3.ggpht.com/abc=s0")


class ReadPostsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import streamwatch.bot as botmod
        from streamwatch.bot import StreamWatch
        from streamwatch.config import Config
        self.botmod = botmod
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.bot = StreamWatch(Config("x", 1, 42, "k", self.path, Path("/nonexistent"), None))
        self.streamer, _ = self.bot.db.add_streamer("UCr", "@rosco", "Rosco")
        self.read = []

        async def fake_read_image(streamer, images, post_text="", post_date=None, post_id=None, source_url=None):
            self.read.append((post_id, len(images), source_url))

            class Result:
                def summary(self):
                    return "ok"
            return Result()
        self.bot.read_image = fake_read_image
        self.posts = []

        async def fetch(session, channel_id):
            return self.posts

        async def download(session, url):
            return b"img", "image/jpeg"
        self.originals = (botmod.community.fetch_posts, botmod.community.download_image)
        botmod.community.fetch_posts, botmod.community.download_image = fetch, download

    async def asyncTearDown(self):
        self.botmod.community.fetch_posts, self.botmod.community.download_image = self.originals
        self.bot.db.conn.close()
        os.remove(self.path)

    async def test_only_new_recent_schedule_posts_are_read_once(self):
        self.posts = parse_posts(page([
            post("new", "🕹️ Schedule for October 12th - 18th", 2, "2 days ago"),
            post("short", "Its a me", 2, "3 days ago"),           # short caption: read too (decision 24)
            post("letter", "Hey crew, life has been hectic lately with lots of new responsibilities, so streams "
                           "will be lighter for a while, thank you all for understanding", 1, "4 days ago"),
            post("old", "Schedule for the week", 1, "9 days ago"),   # first scan: older than a week
            post("members", "Schedule (members)", 1, "1 day ago", members=True),
        ]))
        self.assertEqual(await self.bot.read_posts_of(self.streamer), 2)
        self.assertEqual(self.read, [("short", 2, "https://www.youtube.com/post/short"),
                                     ("new", 2, "https://www.youtube.com/post/new")])
        self.assertEqual(await self.bot.read_posts_of(self.streamer), 0)  # nothing is read twice
        # After the first scan, a newly posted schedule up to 2 weeks old is read
        self.posts = parse_posts(page([post("next", "Weekly schedule!", 1, "10 days ago")])) + self.posts
        self.assertEqual(await self.bot.read_posts_of(self.streamer), 1)


if __name__ == "__main__":
    unittest.main()
