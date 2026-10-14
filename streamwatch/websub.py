"""Instant waiting-room detection: YouTube pushes channel updates via WebSub (PubSubHubbub).

The bot subscribes each followed channel at Google's hub; when a streamer publishes or updates a video
(including creating a waiting room), the hub POSTs the feed entry to our callback within about a minute.
Polling (RSS every 15 min) stays on as the backup. No quota is used until a pushed video is looked up.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Awaitable, Callable, Optional
from urllib.parse import quote
from xml.etree import ElementTree

import aiohttp
from aiohttp import web

log = logging.getLogger("streamwatch.websub")

HUB = "https://pubsubhubbub.appspot.com/subscribe"
TOPIC = "https://www.youtube.com/xml/feeds/videos.xml?channel_id={}"
LEASE_SECONDS = 5 * 24 * 3600  # YouTube's hub grants about 5 days; we renew every 2
_ATOM = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
METADATA_IP = ("http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/"
               "access-configs/0/external-ip")


def topic_for(channel_id: str) -> str:
    return TOPIC.format(channel_id)


def signature_ok(secret: str, body: bytes, header: Optional[str]) -> bool:
    """The hub signs each push with HMAC(secret, body) in X-Hub-Signature: 'sha1=<hex>'."""
    if not header or "=" not in header:
        return False
    algorithm, received = header.split("=", 1)
    if algorithm not in ("sha1", "sha256"):
        return False
    expected = hmac.new(secret.encode(), body, getattr(hashlib, algorithm)).hexdigest()
    return hmac.compare_digest(expected, received)


def parse_push(body: bytes) -> list[tuple[str, str]]:
    """(channel_id, video_id) for each published or updated video in a pushed feed. Deletions are ignored."""
    root = ElementTree.fromstring(body)
    found = []
    for entry in root.findall("a:entry", _ATOM):
        video_id = entry.findtext("yt:videoId", namespaces=_ATOM)
        channel_id = entry.findtext("yt:channelId", namespaces=_ATOM)
        if video_id and channel_id:
            found.append((channel_id, video_id))
    return found


async def public_ip(session: aiohttp.ClientSession) -> Optional[str]:
    """The VM's current external IP from Google's metadata server (it can change after a VM stop/start)."""
    try:
        async with session.get(METADATA_IP, headers={"Metadata-Flavor": "Google"},
                               timeout=aiohttp.ClientTimeout(total=5)) as resp:
            return (await resp.text()).strip() if resp.status == 200 else None
    except (aiohttp.ClientError, TimeoutError):
        return None


async def request(session: aiohttp.ClientSession, callback: str, secret: str, channel_id: str,
                  subscribe: bool = True) -> bool:
    data = {"hub.callback": callback, "hub.topic": topic_for(channel_id), "hub.verify": "async",
            "hub.mode": "subscribe" if subscribe else "unsubscribe", "hub.secret": secret,
            "hub.lease_seconds": str(LEASE_SECONDS)}
    async with session.post(HUB, data=data, timeout=aiohttp.ClientTimeout(total=15)) as resp:
        if resp.status not in (202, 204):
            log.warning("Hub refused %s for %s: %s %s", data["hub.mode"], channel_id, resp.status,
                        (await resp.text())[:200])
            return False
        return True


class WebSubServer:
    """Answers the hub's verification GETs and receives pushed POSTs on /websub/<token>."""

    def __init__(self, token: str, secret: str, known_channels: Callable[[], set[str]],
                 on_video: Callable[[str, str], Awaitable[None]]):
        self.path = f"/websub/{token}"
        self.secret = secret
        self.known_channels = known_channels
        self.on_video = on_video
        self.runner: Optional[web.AppRunner] = None

    async def start(self, port: int) -> None:
        app = web.Application()
        app.router.add_get(self.path, self.verify)
        app.router.add_post(self.path, self.receive)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, "0.0.0.0", port).start()
        log.info("WebSub listening on port %s", port)

    async def stop(self) -> None:
        if self.runner:
            await self.runner.cleanup()

    async def verify(self, request: web.Request) -> web.Response:
        topic, challenge = request.query.get("hub.topic", ""), request.query.get("hub.challenge", "")
        mode = request.query.get("hub.mode")
        channel_id = topic.rsplit("channel_id=", 1)[-1]
        wanted = channel_id in self.known_channels()
        # Confirm subscriptions only for followed channels, and unsubscriptions only for unfollowed ones.
        if challenge and ((mode == "subscribe" and wanted) or (mode == "unsubscribe" and not wanted)):
            log.info("Hub verified %s for %s", mode, channel_id)
            return web.Response(text=challenge)
        return web.Response(status=404)

    async def receive(self, request: web.Request) -> web.Response:
        body = await request.read()
        if not signature_ok(self.secret, body, request.headers.get("X-Hub-Signature")):
            log.warning("Rejected push with a bad signature")
            return web.Response(status=202)  # don't tell a stranger anything
        try:
            pushed = parse_push(body)
        except ElementTree.ParseError:
            return web.Response(status=202)
        known = self.known_channels()
        for channel_id, video_id in pushed:
            if channel_id in known:
                log.info("Push: %s published/updated %s", channel_id, video_id)
                await self.on_video(channel_id, video_id)
        return web.Response(status=202)


def callback_url(ip: str, port: int, token: str) -> str:
    return f"http://{ip}:{port}/websub/{quote(token)}"
