"""Eval step 2: give the image reader an exam against the answer key (PRD §11).

Each schedule post in answers.json is read exactly as the bot reads it (same model, prompt, images, reading notes,
checks and second read), using the Gemini API free tier (GEMINI_API_KEY). Readings are saved next to the post, so
re-running only re-scores; pass --fresh to read again (e.g. after changing the prompt).

Per stream in the answer key:
  accepted + right time    passed the checks and was right (what we want)
  accepted + wrong time    SILENT ERROR: would have gone on my calendar wrong
  review + right time      unnecessary review: right, but I'd have been asked
  review + wrong time      caught: wrong, and the checks caught it
  missed                   not read at all
Readings that match no stream in the key are counted too (accepted ones are silent errors).

Run on the VM from ~/streamwatch:  .venv/bin/python -u -m eval.run [--fresh] [--paid]
--paid reads through the bot's own paid route (Agent Platform, Google Cloud credit) instead of the free tier,
for when the free tier is too busy.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import aiohttp

from streamwatch.config import load_config
from streamwatch.gemini_reader import (FIRST_READER, SECOND_OPINION, GeminiError, GeminiFreeReader, GeminiReader,
                                       parse_response)
from streamwatch.pipeline import TOO_FAR, _merge_readings
from streamwatch.schedule import CheckedEntry, ReadEntry, cross_check, title_similarity, to_utc

HERE = Path(__file__).parent
DATA = HERE / "data"
RIGHT = timedelta(minutes=5)          # a reading this close to the key counts as the right time
REVIEW_TZ = "America/Los_Angeles"     # where the bot places timezone-less readings for review
PAUSE = 7                             # seconds between reads (free tier: about 10 requests a minute)


@dataclass
class Reading:
    checked: CheckedEntry
    start: Optional[datetime]         # UTC, or provisional in REVIEW_TZ when no timezone was understood
    issues: list[str]

    @property
    def accepted(self) -> bool:
        return not self.issues


async def read_cached(reader, folder: Path, n: int, model: str, meta: dict,
                      fresh: bool) -> tuple[bool, list[ReadEntry]]:
    cache = folder / f"reading_{n}.json"
    if cache.exists() and not fresh:
        return parse_response(json.loads(cache.read_text()), model)
    images = [((folder / i["file"]).read_bytes(), i["media_type"]) for i in meta["images"]]
    post_date = datetime.fromisoformat(meta["post_date"])
    for attempt, wait in enumerate([20, 60, 120, 240, 480, None]):
        try:
            data = await reader.read_raw(model, images, meta["streamer"], post_date, meta["post_text"],
                                         meta["reading_notes"])
            break
        except (GeminiError, aiohttp.ClientError, asyncio.TimeoutError) as e:
            busy = any(code in str(e) for code in ("429", "500", "503", "504")) or not isinstance(e, GeminiError)
            if wait is None or not busy:
                raise
            print(f"   free tier busy ({str(e)[:60]}…), retrying in {wait}s")
            await asyncio.sleep(wait)
    cache.write_text(json.dumps(data, ensure_ascii=False))
    if isinstance(reader, GeminiFreeReader):
        await asyncio.sleep(PAUSE)
    return parse_response(data, model)


def decide(c: CheckedEntry, post_date: datetime) -> Reading:
    """The bot's own decision for one reading (pipeline._place without the database)."""
    issues, start = list(c.issues), c.start_utc
    if start is None:
        start, _, more = to_utc(ReadEntry(c.read.date, c.read.start_time, REVIEW_TZ, c.read.title))
        issues += [i for i in more if i not in issues]
    if start is not None and start > post_date + TOO_FAR:
        issues.append("more than 3 weeks away")
    return Reading(c, start, issues)


def pair(key: list[dict], readings: list[Reading]) -> list[tuple[Optional[dict], Optional[Reading]]]:
    """Match key streams to readings: same stream = similar title within 36 h, else nearest time within 90 min."""
    left, pairs = list(readings), []
    for k in key:
        t = datetime.fromisoformat(k["start_utc"])
        near = [r for r in left if r.start and abs(r.start - t) <= timedelta(hours=36)]
        by_title = [r for r in near if title_similarity(r.checked.read.title, k["title"]) >= 0.3]
        pool = by_title or [r for r in near if abs(r.start - t) <= timedelta(minutes=90)]
        best = min(pool, key=lambda r: (-title_similarity(r.checked.read.title, k["title"]), abs(r.start - t)),
                   default=None)
        if best:
            left.remove(best)
        pairs.append((k, best))
    return pairs + [(None, r) for r in left]


def short(issue: str) -> str:
    for label in ("timezones disagree", "differs from the day", "low confidence", "no timezone",
                  "more than 3 weeks", "unknown timezone"):
        if label in issue:
            return label
    return issue[:40]


async def main(fresh: bool, paid: bool) -> None:
    config = load_config()
    key_all = json.loads((HERE / "answers.json").read_text())
    tally, review_reasons, notes = Counter(), Counter(), []
    async with aiohttp.ClientSession() as session:
        reader = (GeminiReader(session, config.google_key_path) if paid
                  else GeminiFreeReader(session, os.environ["GEMINI_API_KEY"]))
        route = "paid route" if paid else "free tier"
        for i, (post_id, key) in enumerate(key_all.items(), 1):
            folder = DATA / post_id
            meta = json.loads((folder / "meta.json").read_text())
            print(f"[{i}/{len(key_all)}] {meta['streamer'][:28]} · {meta['post_text'][:40]!r}")
            post_date = datetime.fromisoformat(meta["post_date"])
            _, first = await read_cached(reader, folder, 1, FIRST_READER, meta, fresh)
            checked = [cross_check(e) for e in first]
            if any(not c.ok for c in checked):
                tally["second reads"] += 1
                _, second = await read_cached(reader, folder, 2, SECOND_OPINION, meta, fresh)
                checked = _merge_readings(checked, [cross_check(e) for e in second])
            tally["reads"] += 1
            for k, r in pair(key, [decide(c, post_date) for c in checked]):
                if r is None:
                    tally["missed"] += 1
                    notes.append(f"MISSED · {meta['streamer'][:14]} · {k['start_utc'][:16]} {k['title']}")
                    continue
                if not r.accepted:
                    for issue in r.issues:
                        review_reasons[short(issue)] += 1
                if k is None:
                    tally["extra accepted" if r.accepted else "extra reviewed"] += 1
                    if r.accepted:
                        notes.append(f"SILENT (extra) · {meta['streamer'][:14]} · {r.start} {r.checked.read.title}")
                    continue
                right = r.start is not None and abs(r.start - datetime.fromisoformat(k["start_utc"])) <= RIGHT
                outcome = ("accepted" if r.accepted else "review") + (" right" if right else " wrong")
                tally[outcome] += 1
                tally["title ok"] += title_similarity(r.checked.read.title, k["title"]) >= 0.5
                if outcome == "accepted wrong":
                    notes.append(f"SILENT · {meta['streamer'][:14]} · key {k['start_utc'][:16]} read "
                                 f"{r.start:%Y-%m-%dT%H:%M} ({r.checked.read.start_time} "
                                 f"{r.checked.read.timezone_as_written}) {k['title']}")
                elif outcome == "review right":
                    notes.append(f"UNNEEDED REVIEW · {meta['streamer'][:14]} · {k['title']} · "
                                 f"{'; '.join(map(short, r.issues))}")
    report = render(tally, review_reasons, notes, sum(map(len, key_all.values())), route)
    (HERE / "report.md").write_text(report)
    print("\n" + report)


def render(t: Counter, reasons: Counter, notes: list[str], total: int, route: str) -> str:
    silent = t["accepted wrong"] + t["extra accepted"]
    asked = t["review right"] + t["review wrong"] + t["extra reviewed"]
    pct = lambda n: f"{n / total:.0%}" if total else "–"
    lines = [
        f"# Image reader eval ({datetime.now():%Y-%m-%d %H:%M}, {FIRST_READER}, {route})", "",
        f"{t['reads']} schedules, {total} streams in the answer key; {t['second reads']} needed a second read.", "",
        "| Result | Streams | Share |", "|---|---|---|",
        f"| Accepted, right time | {t['accepted right']} | {pct(t['accepted right'])} |",
        f"| **Silent errors** (accepted, wrong time, or a stream that doesn't exist) | {silent} | {pct(silent)} |",
        f"| Sent to review, actually right (unnecessary) | {t['review right']} | {pct(t['review right'])} |",
        f"| Sent to review, actually wrong (caught) | {t['review wrong'] + t['extra reviewed']} "
        f"| {pct(t['review wrong'] + t['extra reviewed'])} |",
        f"| Missed (not read at all) | {t['missed']} | {pct(t['missed'])} |", "",
        f"Right time overall: {t['accepted right'] + t['review right']} of {total} "
        f"({pct(t['accepted right'] + t['review right'])}). Review rate: {asked} readings. "
        f"Titles read correctly: {t['title ok']}.", "",
        "## Why readings went to review", "",
    ] + [f"- {reason}: {n}" for reason, n in reasons.most_common()] + ["", "## Details", ""] + \
        [f"- {n}" for n in notes]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    asyncio.run(main("--fresh" in sys.argv, "--paid" in sys.argv))
