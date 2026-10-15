"""M5: read schedule images from YouTube community posts (the channel's Posts tab).

There's no API or RSS feed for community posts, so this reads the Posts tab page like a browser does (M0 showed
YouTube serves it to the VM). Only posts whose TEXT looks like a schedule are read by Gemini (option A): no
image is sent to the model just to ask "is this a schedule?".
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Optional

import aiohttp

from .youtube import BROWSER_HEADERS, YouTubeError

POSTS_URL = "https://www.youtube.com/channel/{}/posts"
_INITIAL_DATA = re.compile(r"var ytInitialData = (\{.*?\});</script>", re.S)

# Option A: words that mark a post as a schedule. Weekday names alone aren't enough (lots of posts mention a day).
SCHEDULE_WORDS = re.compile(
    r"schedule|sched\b|timetable|this week|next week|week of|weekly|stream plan|"
    r"スケジュール|予定|今週|来週|週間|配信表|行程|時間表|直播表|班表",
    re.IGNORECASE)
_WEEKDAY = re.compile(r"\b(mon|tue|wed|thu|fri|sat|sun)[a-z]*\b|[月火水木金土日]曜", re.IGNORECASE)


@dataclass
class Post:
    post_id: str
    text: str
    image_urls: list[str] = field(default_factory=list)
    published_text: str = ""        # e.g. "9 days ago"
    members_only: bool = False


def looks_like_schedule(text: str) -> bool:
    """A schedule word, or at least four different weekday names (a schedule written out in the caption)."""
    if SCHEDULE_WORDS.search(text):
        return True
    return len({m.group(0)[:3].lower() for m in _WEEKDAY.finditer(text)}) >= 4


def _find(node, key: str, out: list) -> list:
    if isinstance(node, dict):
        if key in node:
            out.append(node[key])
        for value in node.values():
            _find(value, key, out)
    elif isinstance(node, list):
        for value in node:
            _find(value, key, out)
    return out


def full_size(url: str) -> str:
    """Post image links carry a size suffix ("=s900-c-..."); ask for the original so small print stays readable."""
    return re.sub(r"=s\d+[^/]*$", "=s0", url)


def _images(post: dict) -> list[str]:
    attachment = post.get("backstageAttachment") or {}
    renderers = []
    if "backstageImageRenderer" in attachment:
        renderers = [attachment["backstageImageRenderer"]]
    elif "postMultiImageRenderer" in attachment:
        renderers = [i.get("backstageImageRenderer", {}) for i in attachment["postMultiImageRenderer"].get("images", [])]
    urls = []
    for r in renderers:
        thumbnails = (r.get("image") or {}).get("thumbnails") or []
        if thumbnails:
            urls.append(full_size(max(thumbnails, key=lambda t: t.get("width", 0))["url"]))
    return urls


def parse_posts(html: str) -> list[Post]:
    """Posts on a Posts tab page, newest first. Raises YouTubeError if the page no longer has the expected data."""
    match = _INITIAL_DATA.search(html)
    if not match:
        raise YouTubeError("Posts page has no ytInitialData (YouTube may have changed the page)")
    data = json.loads(match.group(1))
    posts, seen = [], set()
    for raw in _find(data, "backstagePostRenderer", []) + _find(data, "postRenderer", []):
        post_id = raw.get("postId")
        if not post_id or post_id in seen:
            continue
        seen.add(post_id)
        posts.append(Post(
            post_id=post_id,
            text="".join(run.get("text", "") for run in (raw.get("contentText") or {}).get("runs", [])),
            image_urls=_images(raw),
            published_text="".join(run.get("text", "") for run in (raw.get("publishedTimeText") or {}).get("runs", [])),
            members_only="sponsorsOnlyBadge" in json.dumps(raw),
        ))
    return posts


_AGE = re.compile(r"(\d+)\s*(second|minute|hour|day|week|month|year)s?\s+ago", re.IGNORECASE)
_UNIT = {"second": timedelta(seconds=1), "minute": timedelta(minutes=1), "hour": timedelta(hours=1),
         "day": timedelta(days=1), "week": timedelta(weeks=1), "month": timedelta(days=30), "year": timedelta(days=365)}


def post_age(published_text: str) -> Optional[timedelta]:
    """'9 days ago (edited)' → 9 days. None if the text isn't understood."""
    match = _AGE.search(published_text)
    if not match:
        return timedelta(0) if "just now" in published_text.lower() else None
    return int(match.group(1)) * _UNIT[match.group(2).lower()]


async def fetch_posts(session: aiohttp.ClientSession, channel_id: str) -> list[Post]:
    async with session.get(POSTS_URL.format(channel_id), headers=BROWSER_HEADERS,
                           timeout=aiohttp.ClientTimeout(total=30)) as resp:
        if resp.status != 200:
            raise YouTubeError(f"Posts page HTTP {resp.status}")
        return parse_posts(await resp.text())


async def download_image(session: aiohttp.ClientSession, url: str) -> tuple[bytes, str]:
    async with session.get(url, headers=BROWSER_HEADERS, timeout=aiohttp.ClientTimeout(total=30)) as resp:
        if resp.status != 200:
            raise YouTubeError(f"post image HTTP {resp.status}")
        media_type = (resp.headers.get("Content-Type") or "image/jpeg").split(";")[0]
        return await resp.read(), media_type if media_type.startswith("image/") else "image/jpeg"
