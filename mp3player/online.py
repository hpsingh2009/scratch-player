"""Optional cover-art lookup via the public iTunes Search API (no downloading of audio)."""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request

_UA = "Mozilla/5.0 MP3PlayerPy/3.0"


def _get(url: str, timeout: float = 5.0) -> bytes | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read() if r.status == 200 else None
    except Exception:
        return None


def fetch_cover(artist: str, title: str) -> bytes | None:
    query = f"{'' if artist.lower().startswith('unknown') else artist} {title}".strip()
    if not query:
        return None
    raw = _get(f"https://itunes.apple.com/search?term={urllib.parse.quote(query)}&entity=song&limit=5")
    if not raw:
        return None
    try:
        results = json.loads(raw.decode("utf-8", "replace")).get("results") or []
    except ValueError:
        return None
    if not results:
        return None
    best = results[0]
    words = [w.lower() for w in re.split(r"\W+", artist) if len(w) >= 3]
    for cand in results:
        if any(w in (cand.get("artistName") or "").lower() for w in words):
            best = cand
            break
    url = (best.get("artworkUrl100") or "").replace("100x100bb", "600x600bb")
    data = _get(url) if url else None
    return data if data and len(data) > 64 else None
