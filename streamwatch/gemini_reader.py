"""Reader A on Gemini via Gemini Enterprise Agent Platform (formerly Vertex AI; billed to the Google Cloud project, so the free-trial credit covers it).

Same instructions and output as the Claude reader (prompts.py); Agent Platform enforces the JSON shape with a
response schema. Gemini 3.8 Flash reads every image: in a side-by-side test on a real schedule, 3.5 Flash-Lite
put a stream a day early (both rows consistently, so no check caught it) and mixed up subtitles, while 3.8 Flash
read all of it correctly. Both are paid from the Google Cloud credit. A second read uses the same model.
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import aiohttp

from .gauth import ServiceAccountToken
from .prompts import SYSTEM, _prompt
from .schedule import ReadEntry

log = logging.getLogger("streamwatch.gemini")

FIRST_READER = "gemini-3.8-flash"
SECOND_OPINION = "gemini-3.8-flash"
SCOPE = "https://www.googleapis.com/auth/cloud-platform"
URL = "https://aiplatform.googleapis.com/v1/projects/{project}/locations/global/publishers/google/models/{model}:generateContent"
# Same model on the Gemini API free tier (AI Studio key). Used by the eval only; Google may use free-tier inputs.
FREE_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

_TIME_ROW = {"type": "OBJECT", "nullable": True, "properties": {
    "timezone": {"type": "STRING", "description": "Timezone label as printed, e.g. EDT"},
    "date": {"type": "STRING", "description": "YYYY-MM-DD in that timezone"},
    "time": {"type": "STRING", "description": "HH:MM 24-hour in that timezone"},
}, "required": ["timezone", "date", "time"]}

RESPONSE_SCHEMA = {"type": "OBJECT", "properties": {
    "is_schedule": {"type": "BOOLEAN", "description": "True if the image is a stream schedule"},
    "entries": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "slot_date": {"type": "STRING", "nullable": True, "description": "YYYY-MM-DD of the day/column it's listed under"},
        "date": {"type": "STRING", "description": "YYYY-MM-DD the stream starts on, in timezone_as_written"},
        "start_time": {"type": "STRING", "description": "HH:MM 24-hour, in timezone_as_written"},
        "timezone_as_written": {"type": "STRING", "nullable": True, "description": "e.g. JST, PT, GMT+9, or null"},
        "title_as_written": {"type": "STRING"},
        "subtitle_as_written": {"type": "STRING", "nullable": True},
        "is_collab": {"type": "BOOLEAN"},
        "collab_partners": {"type": "ARRAY", "items": {"type": "STRING"}},
        "confidence": {"type": "NUMBER", "description": "0.0-1.0"},
        "check_time": _TIME_ROW,
    }, "required": ["date", "start_time", "title_as_written", "is_collab", "collab_partners", "confidence"]}},
}, "required": ["is_schedule", "entries"]}


class GeminiError(Exception):
    pass


class GeminiReader:
    def __init__(self, session: aiohttp.ClientSession, key_path: Path):
        self.session = session
        self.auth = ServiceAccountToken(key_path, SCOPE)
        self.project = self.auth.info()["project_id"]

    async def read_schedule(self, model: str, images: list[tuple[bytes, str]], streamer: str, post_date: datetime, post_text: str = "", notes: Optional[str] = None
                            ) -> tuple[bool, list[ReadEntry]]:
        return parse_response(await self.read_raw(model, images, streamer, post_date, post_text, notes), model)

    async def read_raw(self, model: str, images: list[tuple[bytes, str]], streamer: str, post_date: datetime,
                       post_text: str = "", notes: Optional[str] = None) -> dict:
        body = request_body(images, streamer, post_date, post_text, notes)
        headers = {"Authorization": f"Bearer {await self.auth.get(self.session)}"}
        return await _post(self.session, URL.format(project=self.project, model=model), body, headers, model)


class GeminiFreeReader:
    """The same model and request on the Gemini API free tier, with an AI Studio key (GEMINI_API_KEY)."""

    def __init__(self, session: aiohttp.ClientSession, api_key: str):
        self.session = session
        self.api_key = api_key

    async def read_raw(self, model: str, images: list[tuple[bytes, str]], streamer: str, post_date: datetime,
                       post_text: str = "", notes: Optional[str] = None) -> dict:
        body = request_body(images, streamer, post_date, post_text, notes)
        return await _post(self.session, FREE_URL.format(model=model), body, {"x-goog-api-key": self.api_key}, model)


def request_body(images: list[tuple[bytes, str]], streamer: str, post_date: datetime, post_text: str,
                 notes: Optional[str]) -> dict:
    parts = [{"inlineData": {"mimeType": media_type, "data": base64.standard_b64encode(data).decode()}}
             for data, media_type in images]
    parts.append({"text": _prompt(streamer, post_date, post_text, len(images), notes)})
    return {"systemInstruction": {"parts": [{"text": SYSTEM}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"responseMimeType": "application/json", "responseSchema": RESPONSE_SCHEMA}}


async def _post(session: aiohttp.ClientSession, url: str, body: dict, headers: dict, model: str) -> dict:
    async with session.post(url, json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=180)) as resp:
        data = await resp.json(content_type=None)
        if resp.status != 200:
            raise GeminiError(f"{model} HTTP {resp.status}: {data.get('error', {}).get('message', data)}")
    usage = data.get("usageMetadata", {})
    log.info("%s read image: %s in / %s out tokens", model, usage.get("promptTokenCount"),
             usage.get("candidatesTokenCount"))
    return data


def parse_response(data: dict, model: str = "gemini") -> tuple[bool, list[ReadEntry]]:
    candidates = data.get("candidates") or []
    if not candidates:
        raise GeminiError(f"{model} returned nothing ({data.get('promptFeedback', {}).get('blockReason', 'no reason')})")
    text = "".join(p.get("text", "") for p in candidates[0].get("content", {}).get("parts", []))
    try:
        reading = json.loads(text)
    except json.JSONDecodeError as e:
        raise GeminiError(f"{model} returned invalid JSON: {e}") from e
    entries = []
    for e in reading.get("entries", []):
        check = e.get("check_time")
        entries.append(ReadEntry(
            e["date"], e["start_time"], e.get("timezone_as_written"), e["title_as_written"].strip(),
            bool(e.get("is_collab")), e.get("collab_partners") or [],
            max(0.0, min(1.0, float(e.get("confidence", 0)))), e.get("slot_date"),
            [(check["date"], check["time"], check["timezone"])] if check else [],
            (e.get("subtitle_as_written") or "").strip() or None))
    return bool(reading.get("is_schedule")), entries
