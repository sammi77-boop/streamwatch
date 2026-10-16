"""Eval step 1: collect schedule posts to test the image reader on (PRD §11). Free: public pages + ~2 quota units
per streamer.

For every followed streamer, saves the schedule posts on their Posts tab (same filter as the bot: images, not
members-only, schedule words in the text) under eval/data/<post_id>/: the images, meta.json, and youtube.json,
the streamer's recent live streams with YouTube's own start times (the automatic check on the answer key).

Run on the VM from ~/streamwatch:  .venv/bin/python -m eval.collect
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

from streamwatch import community
from streamwatch.config import load_config
from streamwatch.youtube import fetch_upload_ids, fetch_videos

DATA = Path(__file__).parent / "data"
PER_STREAMER = 6     # newest schedule posts per streamer


def followed(db_path: str) -> list[dict]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(
        "SELECT channel_id, name, reading_notes FROM streamers WHERE active = 1")]


async def youtube_streams(session: aiohttp.ClientSession, api_key: str, channel_id: str) -> list[dict]:
    """The channel's last 50 uploads that were live streams, with YouTube's scheduled and actual start times."""
    ids = await fetch_upload_ids(session, api_key, channel_id, limit=50)
    videos = await fetch_videos(session, api_key, ids)
    return [{"video_id": v.video_id, "title": v.title,
             "scheduled_start": v.scheduled_start.isoformat() if v.scheduled_start else None,
             "actual_start": v.actual_start.isoformat() if v.actual_start else None}
            for v in videos.values() if v.scheduled_start or v.actual_start]


async def main() -> None:
    config = load_config()
    now = datetime.now(timezone.utc)
    saved = 0
    async with aiohttp.ClientSession() as session:
        for s in followed(str(config.db_path)):
            try:
                posts = await community.fetch_posts(session, s["channel_id"])
                streams = await youtube_streams(session, config.youtube_api_key, s["channel_id"])
            except Exception as e:  # one streamer failing shouldn't stop the collection
                print(f"skip {s['name']}: {e}")
                continue
            picked = [p for p in posts if p.image_urls and not p.members_only
                      and community.looks_like_schedule(p.text)
                      and community.post_age(p.published_text) is not None][:PER_STREAMER]
            for post in picked:
                folder = DATA / post.post_id
                if (folder / "meta.json").exists():
                    continue
                folder.mkdir(parents=True, exist_ok=True)
                files = []
                for i, url in enumerate(post.image_urls[:3]):
                    data, media_type = await community.download_image(session, url)
                    name = f"image_{i + 1}.{media_type.split('/')[-1].replace('jpeg', 'jpg')}"
                    (folder / name).write_bytes(data)
                    files.append({"file": name, "media_type": media_type})
                post_date = now - community.post_age(post.published_text)
                (folder / "meta.json").write_text(json.dumps({
                    "post_id": post.post_id, "url": f"https://www.youtube.com/post/{post.post_id}",
                    "streamer": s["name"], "channel_id": s["channel_id"], "reading_notes": s["reading_notes"],
                    "post_text": post.text, "published_text": post.published_text,
                    "post_date": post_date.isoformat(), "images": files}, ensure_ascii=False, indent=1))
                (folder / "youtube.json").write_text(json.dumps(streams, ensure_ascii=False, indent=1))
                saved += 1
            print(f"{s['name']}: {len(picked)} schedule posts")
    print(f"saved {saved} new posts to {DATA}")


if __name__ == "__main__":
    asyncio.run(main())
