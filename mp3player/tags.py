"""Embedded tag, lyrics and cover-art extraction (via mutagen)."""
from __future__ import annotations

from pathlib import Path

import mutagen
from mutagen.flac import FLAC, Picture
from mutagen.id3 import ID3
from mutagen.mp4 import MP4


def _ms_to_lrc(ms: int) -> str:
    s = ms / 1000.0
    return f"[{int(s // 60):02d}:{int(s % 60):02d}.{int((s % 1) * 100):02d}]"


def _id3_info(tags: ID3, out: dict) -> None:
    def text(key: str) -> str | None:
        f = tags.get(key)
        return str(f.text[0]).strip() if f is not None and getattr(f, "text", None) else None

    out["title"] = text("TIT2")
    out["artist"] = text("TPE1")
    for f in tags.getall("USLT"):
        if f.text and f.text.strip():
            out["lyrics"] = f.text.strip()
            break
    if not out["lyrics"]:
        for f in tags.getall("SYLT"):
            lines = [f"{_ms_to_lrc(ts)} {tx.strip()}" for tx, ts in f.text if tx.strip()]
            if lines:
                out["lyrics"] = "\n".join(lines)
                break
    if not out["lyrics"]:
        for f in tags.getall("TXXX"):
            if (f.desc or "").upper() in ("LYRICS", "UNSYNCEDLYRICS", "SYNCEDLYRICS") and f.text:
                out["lyrics"] = str(f.text[0]).strip()
                break
    apics = tags.getall("APIC")
    if apics:
        out["cover"] = apics[0].data


def read_tags(path: str | Path) -> dict:
    """Return {title, artist, lyrics, duration, cover(bytes|None)}. Never raises."""
    out: dict = {"title": None, "artist": None, "lyrics": None, "duration": 0.0, "cover": None}
    try:
        audio = mutagen.File(str(path))
    except Exception:
        return out
    if audio is None:
        return out
    try:
        out["duration"] = float(getattr(audio.info, "length", 0.0) or 0.0)
    except Exception:
        pass
    try:
        if isinstance(audio, MP4):
            t = audio.tags or {}
            out["title"] = (t.get("\xa9nam") or [None])[0]
            out["artist"] = (t.get("\xa9ART") or t.get("aART") or [None])[0]
            out["lyrics"] = (t.get("\xa9lyr") or [None])[0]
            if t.get("covr"):
                out["cover"] = bytes(t["covr"][0])
        elif isinstance(audio, FLAC):
            t = audio.tags or {}
            out["title"] = (t.get("title") or [None])[0]
            out["artist"] = (t.get("artist") or [None])[0]
            out["lyrics"] = (t.get("lyrics") or t.get("unsyncedlyrics") or [None])[0]
            if audio.pictures:
                out["cover"] = audio.pictures[0].data
        elif isinstance(getattr(audio, "tags", None), ID3):
            _id3_info(audio.tags, out)
        elif getattr(audio, "tags", None):  # Ogg / Opus / generic vorbis comments
            t = audio.tags
            out["title"] = (t.get("title") or [None])[0]
            out["artist"] = (t.get("artist") or [None])[0]
            out["lyrics"] = (t.get("lyrics") or t.get("unsyncedlyrics") or [None])[0]
    except Exception:
        pass
    for k in ("title", "artist", "lyrics"):
        if out[k] is not None:
            out[k] = str(out[k]).strip() or None
    return out
