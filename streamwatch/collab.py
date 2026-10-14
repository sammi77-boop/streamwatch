"""Collab detection from waiting-room titles and descriptions (PRD X-5, applied to waiting rooms too).

Streamers announce partners as a marker ("Collab w/", "w/", "ft.", "コラボ") followed by @handles on the
next lines of the description, or by names in the title ("… w/ Claude, Dubstep, Seible, Shu 【…】").
"with" alone isn't a marker: "Camping with KRISISIS" means with the fans.
"""

from __future__ import annotations

import re
from typing import Optional

MARKER_RE = re.compile(r"collab|コラボ|(?<![\w/])w/(?!o\b)|(?<!\w)ft\.|(?<!\w)feat\.?(?!\w)", re.IGNORECASE)
HANDLE_RE = re.compile(r"@[A-Za-z0-9_.\-]{3,30}")
HANDLE_LINE_RE = re.compile(r"^\s*(@[A-Za-z0-9_.\-]{3,30}[\s,、]*)+$")
TITLE_PARTNERS_RE = re.compile(r"(?:(?<![\w/])w/(?!o\b)|(?<!\w)ft\.|(?<!\w)feat\.?)\s*([^【\[\(（|｜]+)", re.IGNORECASE)
COLLAB_WORD_RE = re.compile(r"collab|コラボ", re.IGNORECASE)
MAX_PARTNERS = 8


def partners_from_description(description: str, own_handle: Optional[str]) -> list[str]:
    """@handles listed right after a collab marker line."""
    lines = description.splitlines()
    for i, line in enumerate(lines):
        if not MARKER_RE.search(line):
            continue
        handles = HANDLE_RE.findall(line)
        for following in lines[i + 1:]:
            if not following.strip():
                if handles:
                    break
                continue
            if not HANDLE_LINE_RE.match(following):
                break
            handles += HANDLE_RE.findall(following)
        handles = _without_self(handles, own_handle)
        if handles:
            return handles[:MAX_PARTNERS]
    return []


def partners_from_title(title: str) -> list[str]:
    match = TITLE_PARTNERS_RE.search(title)
    if not match:
        return []
    names = re.split(r"\s*(?:,|、|&|＆|/|\band\b)\s*", match.group(1).strip(" !！"))
    return [n.strip(" !！") for n in names if n.strip(" !！")][:MAX_PARTNERS]


def detect_collab(title: str, description: str, own_handle: Optional[str]) -> tuple[bool, list[str]]:
    """Returns (is_collab, partners). Partners are @handles (preferred) or plain names from the title."""
    partners = partners_from_description(description, own_handle) or partners_from_title(title)
    return bool(partners) or bool(COLLAB_WORD_RE.search(title)), partners


def _without_self(handles: list[str], own_handle: Optional[str]) -> list[str]:
    own = (own_handle or "").lower()
    seen, result = set(), []
    for h in handles:
        if h.lower() != own and h.lower() not in seen:
            seen.add(h.lower())
            result.append(h)
    return result


def short_channel_name(title: str) -> str:
    """'Dubstep 【NIJISANJI EN】' → 'Dubstep'. Keeps the name if stripping would leave nothing."""
    stripped = re.sub(r"\s*[【\[(（].*?[】\])）]\s*", " ", title).strip(" /|")
    return stripped or title
