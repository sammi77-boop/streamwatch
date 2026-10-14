"""YouTube Data API calls. Only cheap endpoints; never search.list (PRD L-4)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from xml.etree import ElementTree

import aiohttp

API = "https://www.googleapis.com/youtube/v3"


class YouTubeError(Exception):
    pass


@dataclass
class ChannelInfo:
    channel_id: str
    title: str
    handle: Optional[str]


async def resolve_channel(session: aiohttp.ClientSession, api_key: str, *, handle: str = None,
                          channel_id: str = None) -> Optional[ChannelInfo]:
    """Look up a channel by @handle or UC… ID with channels.list (1 quota unit). None if not found."""
    params = {"part": "snippet", "key": api_key}
    if handle:
        params["forHandle"] = handle
    else:
        params["id"] = channel_id
    async with session.get(f"{API}/channels", params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
        data = await resp.json(content_type=None)
        if resp.status != 200:
            message = data.get("error", {}).get("message", f"HTTP {resp.status}")
            raise YouTubeError(message)
    items = data.get("items") or []
    if not items:
        return None
    snippet = items[0]["snippet"]
    return ChannelInfo(items[0]["id"], snippet["title"], snippet.get("customUrl"))


# --- M2: waiting rooms and go-live (I-3, L-1) ---

RSS_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
_ATOM = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}


@dataclass
class VideoInfo:
    video_id: str
    channel_id: str
    title: str
    broadcast: str                      # upcoming | live | none
    scheduled_start: Optional[datetime]
    actual_start: Optional[datetime]
    is_premiere: bool                   # premieres also report "upcoming"; they're out of scope


def parse_rss_video_ids(xml_text: str) -> list[str]:
    """Video IDs in a channel's uploads feed, newest first (last 15, incl. waiting rooms and live streams)."""
    root = ElementTree.fromstring(xml_text)
    return [e.findtext("yt:videoId", namespaces=_ATOM) for e in root.findall("a:entry", _ATOM)]


async def fetch_rss_video_ids(session: aiohttp.ClientSession, channel_id: str) -> list[str]:
    async with session.get(RSS_URL.format(channel_id), timeout=aiohttp.ClientTimeout(total=20)) as resp:
        if resp.status != 200:
            raise YouTubeError(f"RSS HTTP {resp.status} for {channel_id}")
        return parse_rss_video_ids(await resp.text())


def parse_video_item(item: dict) -> VideoInfo:
    snippet = item["snippet"]
    live = item.get("liveStreamingDetails") or {}
    duration = (item.get("contentDetails") or {}).get("duration", "P0D")
    broadcast = snippet.get("liveBroadcastContent", "none")
    return VideoInfo(
        video_id=item["id"],
        channel_id=snippet["channelId"],
        title=snippet["title"],
        broadcast=broadcast,
        scheduled_start=_parse_time(live.get("scheduledStartTime")),
        actual_start=_parse_time(live.get("actualStartTime")),
        # A live stream's duration is P0D until it ends; a premiere already has its real length.
        is_premiere=broadcast == "upcoming" and duration not in ("P0D", ""),
    )


async def fetch_videos(session: aiohttp.ClientSession, api_key: str, video_ids: list[str]) -> dict[str, VideoInfo]:
    """videos.list in batches of 50 (1 quota unit per batch). Missing IDs were deleted or made private."""
    found: dict[str, VideoInfo] = {}
    for i in range(0, len(video_ids), 50):
        params = {"part": "snippet,liveStreamingDetails,contentDetails", "id": ",".join(video_ids[i:i + 50]),
                  "key": api_key}
        async with session.get(f"{API}/videos", params=params, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200:
                raise YouTubeError(data.get("error", {}).get("message", f"HTTP {resp.status}"))
        for item in data.get("items", []):
            info = parse_video_item(item)
            found[info.video_id] = info
    return found


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
