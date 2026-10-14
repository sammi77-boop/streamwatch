"""WebSub push: signature check, feed parsing, verification and dispatch (with a real local HTTP server)."""

import hashlib
import hmac
import unittest

import aiohttp

from streamwatch.websub import WebSubServer, callback_url, parse_push, signature_ok, topic_for

FEED = b"""<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns="http://www.w3.org/2005/Atom">
 <entry><id>yt:video:yrDHM1QCmjM</id><yt:videoId>yrDHM1QCmjM</yt:videoId>
  <yt:channelId>UCmpEFsiD67ZOjn2CCJoo1mw</yt:channelId><title>DEVOUR</title></entry>
</feed>"""


def sign(secret, body, algorithm="sha1"):
    return f"{algorithm}=" + hmac.new(secret.encode(), body, getattr(hashlib, algorithm)).hexdigest()


class PureTest(unittest.TestCase):
    def test_signature(self):
        self.assertTrue(signature_ok("s3cret", FEED, sign("s3cret", FEED)))
        self.assertTrue(signature_ok("s3cret", FEED, sign("s3cret", FEED, "sha256")))
        self.assertFalse(signature_ok("s3cret", FEED, sign("wrong", FEED)))
        self.assertFalse(signature_ok("s3cret", FEED + b"x", sign("s3cret", FEED)))
        self.assertFalse(signature_ok("s3cret", FEED, None))
        self.assertFalse(signature_ok("s3cret", FEED, "md5=abc"))

    def test_parse_push(self):
        self.assertEqual(parse_push(FEED), [("UCmpEFsiD67ZOjn2CCJoo1mw", "yrDHM1QCmjM")])
        deleted = b"""<feed xmlns:at="http://purl.org/atompub/tombstones/1.0" xmlns="http://www.w3.org/2005/Atom">
                      <at:deleted-entry ref="yt:video:abc" when="2026-10-14T00:00:00+00:00"/></feed>"""
        self.assertEqual(parse_push(deleted), [])

    def test_urls(self):
        self.assertEqual(topic_for("UC1"), "https://www.youtube.com/xml/feeds/videos.xml?channel_id=UC1")
        self.assertEqual(callback_url("1.2.3.4", 8080, "tok"), "http://1.2.3.4:8080/websub/tok")


class ServerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.received = []
        self.followed = {"UCmpEFsiD67ZOjn2CCJoo1mw"}

        async def on_video(channel_id, video_id):
            self.received.append((channel_id, video_id))
        self.server = WebSubServer("tok", "s3cret", lambda: self.followed, on_video)
        await self.server.start(18765)
        self.base = "http://127.0.0.1:18765/websub/tok"
        self.session = aiohttp.ClientSession()

    async def asyncTearDown(self):
        await self.session.close()
        await self.server.stop()

    async def test_verification_only_for_followed_channels(self):
        q = {"hub.mode": "subscribe", "hub.challenge": "abc123",
             "hub.topic": topic_for("UCmpEFsiD67ZOjn2CCJoo1mw")}
        async with self.session.get(self.base, params=q) as r:
            self.assertEqual((r.status, await r.text()), (200, "abc123"))
        q["hub.topic"] = topic_for("UCsomeoneelse")
        async with self.session.get(self.base, params=q) as r:
            self.assertEqual(r.status, 404)
        q["hub.mode"] = "unsubscribe"  # unfollowed channel → unsubscribe is confirmed
        async with self.session.get(self.base, params=q) as r:
            self.assertEqual((r.status, await r.text()), (200, "abc123"))

    async def test_signed_push_is_dispatched_unsigned_is_ignored(self):
        async with self.session.post(self.base, data=FEED, headers={"X-Hub-Signature": sign("s3cret", FEED)}) as r:
            self.assertEqual(r.status, 202)
        self.assertEqual(self.received, [("UCmpEFsiD67ZOjn2CCJoo1mw", "yrDHM1QCmjM")])
        async with self.session.post(self.base, data=FEED, headers={"X-Hub-Signature": sign("guess", FEED)}) as r:
            self.assertEqual(r.status, 202)
        async with self.session.post(self.base, data=FEED) as r:
            self.assertEqual(r.status, 202)
        self.assertEqual(len(self.received), 1)

    async def test_wrong_path_is_not_found(self):
        async with self.session.post("http://127.0.0.1:18765/websub/guess", data=FEED) as r:
            self.assertEqual(r.status, 404)


if __name__ == "__main__":
    unittest.main()
