"""Heuristics to turn messy file names into clean (artist, title) pairs."""
from __future__ import annotations

import re

_NOISE_PAREN_RE = re.compile(
    r"[\(\[](?:official|lyric|lyrics|audio|video|music video|visualizer|hd|hq|4k|remastered|feat\.?[^)\]]*)[^)\]]*[\)\]]",
    re.IGNORECASE,
)
_TITLE_WORDS = r"\b(remaster(ed)?|remix|version|mix|live|instrumental|deluxe|acoustic|edit|slowed|reverb|radio|official|video|audio|lyric|lyrics|ost|theme)\b"


def clean_track_query(text: str) -> str:
    if not text:
        return ""
    s = re.sub(r"^\s*\d{1,3}[\s._-]+", "", str(text))
    s = _NOISE_PAREN_RE.sub("", s)
    s = re.sub(r"\s*-\s*Topic$", "", s, flags=re.IGNORECASE)
    s = re.sub(r"_+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def clean_youtube_stem(stem: str) -> str:
    if not stem:
        return ""
    s = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", str(stem))
    s = re.sub(
        r"\b(official|music|video|audio|lyric|lyrics|song|full|hd|hq|4k|latest|songs|new|20\d\d)\b",
        " ",
        s,
        flags=re.IGNORECASE,
    )
    s = re.sub(r"\b[lI]\b", " ", s)
    s = re.sub(r"[|_]+", " ", s)
    return re.sub(r"\s+", " ", s).strip(" -–—,")


def artist_score(text: str) -> int:
    score = 0
    if re.search(r"\b(feat\.?|ft\.?|featuring|with|prod\.?|x|vs\.?)\b", text, re.I):
        score += 35
    if "," in text:
        score += 30
    if re.search(r"\s+&\s+", text):
        score += 25
    if re.search(r"\b(records|music|entertainment|band|t-series)\b", text, re.I):
        score += 20
    if re.search(_TITLE_WORDS, text, re.I):
        score -= 40
    if re.search(r"[!?]$", text.strip()):
        score -= 15
    return score


def title_score(text: str) -> int:
    score = 0
    if re.search(_TITLE_WORDS, text, re.I):
        score += 40
    if re.search(r"[!?]$", text.strip()):
        score += 15
    if re.search(r"\b\d{4}\s+remaster\b", text, re.I):
        score += 50
    if "," in text:
        score -= 25
    if re.search(r"\s+&\s+", text):
        score -= 20
    return score


def parse_artist_title(stem: str) -> tuple[str, str]:
    """Decide whether a file name is 'Artist - Title' or 'Title - Artist'. Returns (artist, title)."""
    s = re.sub(r"\.[a-zA-Z0-9]{2,5}$", "", (stem or "").strip())
    parts = [p.strip() for p in re.split(r"\s+[-–—]\s+", s) if p.strip()]
    if len(parts) <= 1 and "_" in s:
        parts = [p.strip() for p in s.split("_") if p.strip()]
    if len(parts) <= 1:
        return "", s

    if len(parts) == 2:
        left, right = parts
        if left.lower() == right.lower():
            return "", left
        if title_score(right) > 20 and artist_score(right) <= 0:
            return "", f"{left} ({right})"
        if title_score(left) > 20 and artist_score(left) <= 0:
            return "", f"{right} ({left})"
        a_left = artist_score(left) - title_score(left)
        a_right = artist_score(right) - title_score(right)
        if a_right > a_left + 10:
            return right, left
        if a_left > a_right + 10:
            return left, right
        rw, lw = right.split(), left.split()
        if len(rw) == 2 and all(w.istitle() for w in rw) and len(lw) == 1:
            return right, left
        if len(lw) == 2 and all(w.istitle() for w in lw) and len(rw) == 1:
            return left, right
        return left, right

    first, last = parts[0], parts[-1]
    artist_last = artist_score(last) >= artist_score(first)
    rest = parts[:-1] if artist_last else parts[1:]
    title = f"{rest[0]} ({' - '.join(rest[1:])})" if len(rest) > 1 else rest[0]
    return (last if artist_last else first), title


def resolve_names(artist: str, title: str, stem: str) -> tuple[str, str]:
    """Combine embedded tags with filename heuristics into the best (artist, title)."""
    raw_artist = "" if re.match(r"^unknown", artist or "", re.I) else clean_track_query(artist)
    p_artist, p_title = parse_artist_title(stem)
    best_artist = raw_artist or p_artist
    best_title = title if (title and title != stem) else p_title
    if best_artist and best_title:
        if (artist_score(best_title) - title_score(best_title)) > (artist_score(best_artist) - title_score(best_artist)) + 10:
            best_artist, best_title = best_title, best_artist
    best_title = clean_youtube_stem(best_title) or clean_track_query(best_title) or best_title or stem
    best_artist = re.sub(r"\s*-\s*Topic$", "", best_artist or "", flags=re.I).strip()
    return best_artist or "Unknown artist", best_title
