"""
Synchronized lyrics (LRC) parsing and local disk (.lrc) integration.
"""
from __future__ import annotations

import re
from pathlib import Path

from .storage import CONFIG

_TIME_TAG_RE = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")


def parse_lrc(text: str, fallback_dur: float = 180.0) -> list[dict]:
    """Parse synced ([mm:ss.xx]) or plain lyrics into [{t, text}, ...]."""
    if not text or not isinstance(text, str):
        return []
    synced: list[dict] = []
    plain_lines: list[str] = []
    offset_sec = 0.0

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        offset_match = re.match(r"^\[offset:\s*([+-]?\d+)\s*\]$", line, re.IGNORECASE)
        if offset_match:
            offset_sec = int(offset_match.group(1)) / 1000.0
            continue
        if re.match(r"^\[(id|ar|ti|al|by|re|ve|length|tool|au):", line, re.IGNORECASE):
            continue

        matches = list(_TIME_TAG_RE.finditer(line))
        if matches:
            lyric_text = _TIME_TAG_RE.sub("", line).strip()
            if not lyric_text:
                continue
            for m in matches:
                mins = int(m.group(1))
                secs = int(m.group(2))
                frac_str = m.group(3) or "0"
                frac = int(frac_str) / (1000.0 if len(frac_str) == 3 else (100.0 if len(frac_str) == 2 else 10.0))
                t = max(0.0, round(mins * 60.0 + secs + frac - offset_sec, 3))
                synced.append({"t": t, "text": lyric_text})
        else:
            cleaned = re.sub(r"^\[[^\]]*\]", "", line).strip()
            if cleaned:
                plain_lines.append(cleaned)

    if synced:
        synced.sort(key=lambda item: item["t"])
        return synced

    if not plain_lines:
        return []
    dur = max(float(fallback_dur or 180.0), len(plain_lines) * 2.5)
    step = dur / (len(plain_lines) + 1)
    return [{"t": round(step * (i + 1) * 0.85, 2), "text": ln} for i, ln in enumerate(plain_lines)]


def clean_track_query(text: str) -> str:
    if not text:
        return ""
    cleaned = re.sub(r"^\s*\d{1,3}[\s._-]+", "", str(text))
    cleaned = re.sub(
        r"[\(\[](?:official|lyric|lyrics|audio|video|music video|visualizer|hd|hq|4k|remastered|feat\.?[^)\]]*)[^)\]]*[\)\]]",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\s*-\s*Topic$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"_+", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def fetch_lyrics_lrclib(
    artist: str,
    title: str,
    duration: float = 180.0,
    save_dir: Path | None = None,
    audio_filename: str = "",
) -> dict:
    """Check for local disk (.lrc) lyrics. Automatic online lyric fetching is disabled per user preference."""
    clean_title = clean_track_query(title)
    clean_artist = "" if re.match(r"^unknown", artist or "", re.IGNORECASE) else clean_track_query(artist)

    # Check if a valid local .lrc file exists in save_dir
    if save_dir and CONFIG.is_allowed(save_dir) and save_dir.is_dir():
        candidates = []
        if audio_filename:
            candidates.append(save_dir / (Path(audio_filename).stem + ".lrc"))
        if clean_artist and clean_title:
            safe_pair = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", f"{clean_artist} - {clean_title}").strip(" .") + ".lrc"
            candidates.append(save_dir / safe_pair)
        if clean_title:
            safe_tit = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", clean_title).strip(" .") + ".lrc"
            candidates.append(save_dir / safe_tit)
        for cand_path in candidates:
            if cand_path.is_file():
                try:
                    existing_txt = cand_path.read_text(encoding="utf-8", errors="replace").strip()
                    parsed_existing = parse_lrc(existing_txt, duration or 180.0)
                    if parsed_existing:
                        return {
                            "found": True,
                            "rawLrc": existing_txt,
                            "lyrics": parsed_existing,
                            "savedLrcName": cand_path.name,
                            "artist": clean_artist or artist,
                            "title": clean_title or title,
                        }
                except OSError:
                    pass

    return {
        "found": False,
        "rawLrc": None,
        "lyrics": [],
        "artist": clean_artist or artist,
        "title": clean_title or title,
    }
