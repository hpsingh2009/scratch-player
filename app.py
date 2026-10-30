#!/usr/bin/env python3
"""
MP3 Player - Cross-Platform Python Desktop Application (Windows / macOS / Linux).

Runs the full player UI inside a native OS window via pywebview while handling all
system, storage, metadata, and network operations in Python:
  * Native OS folder & file dialogs (pick_folder, pick_save_file, pick_audio_files)
  * Cross-platform file system access (list, stat, read, atomic write, delete)
  * Persistent library & settings storage in Python (library.json & config.json)
  * Synced lyrics fetching from LRCLIB + LRC parsing + automatic .lrc disk saving
  * Pure-Python ID3v2 & MP4/M4A metadata, embedded lyrics, and cover art extraction

Usage:
  python app.py [--debug] [--port 47831]
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import logging
import mimetypes
import os
import re
import secrets
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import webview

APP_NAME = "MP3 Player"
APP_ID = "mp3-player"
DEFAULT_PORT = 47831          # fixed so the web origin stays stable across launches
MAX_WRITE_BYTES = 1024 * 1024 * 1024   # 1 GB per file write

# PyInstaller one-file builds unpack data files into sys._MEIPASS
APP_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))

log = logging.getLogger(APP_ID)

mimetypes.add_type("audio/mp4", ".m4a")
mimetypes.add_type("audio/flac", ".flac")
mimetypes.add_type("audio/aac", ".aac")
mimetypes.add_type("text/plain", ".lrc")
mimetypes.add_type("text/javascript", ".js")


# --------------------------------------------------------------------------------------
# Per-OS data directory & persisted config / library storage
# --------------------------------------------------------------------------------------
def user_data_dir() -> Path:
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    path = base / APP_ID
    path.mkdir(parents=True, exist_ok=True)
    return path


DATA_DIR = user_data_dir()
COVERS_DIR = DATA_DIR / "covers"
COVERS_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = DATA_DIR / "config.json"
LIBRARY_FILE = DATA_DIR / "library.json"


class Config:
    """Thread-safe JSON config holding granted folders, user settings, and library state."""

    def __init__(self, path: Path, library_path: Path):
        self._path = path
        self._library_path = library_path
        self._lock = threading.Lock()
        self._data: dict = {
            "allowed_dirs": [],
            "settings": {
                "theme": "light",
                "accent": "#d4163c",
                "folder_name": "Music",
                "linked_dir": None,
                "lyrics_enabled": True,
            },
        }
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self._data.update(loaded)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass

    def _save(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)

    def allow_dir(self, directory: Path) -> str:
        resolved = str(directory.resolve())
        with self._lock:
            dirs = self._data.setdefault("allowed_dirs", [])
            if resolved not in dirs:
                dirs.append(resolved)
            self._data.setdefault("settings", {})["linked_dir"] = resolved
            self._data["settings"]["folder_name"] = directory.name or resolved
            self._save()
        return resolved

    def is_allowed(self, directory: Path) -> bool:
        resolved = str(directory.resolve())
        with self._lock:
            return resolved in self._data.get("allowed_dirs", [])

    def get_settings(self) -> dict:
        with self._lock:
            return dict(self._data.get("settings", {}))

    def update_settings(self, updates: dict) -> dict:
        with self._lock:
            settings = self._data.setdefault("settings", {})
            for k in ("theme", "accent", "folder_name", "linked_dir", "lyrics_enabled"):
                if k in updates:
                    settings[k] = updates[k]
            self._save()
            return dict(settings)

    def load_library(self) -> dict:
        with self._lock:
            try:
                data = json.loads(self._library_path.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                return {}

    def save_library(self, snapshot: dict) -> None:
        with self._lock:
            tmp = self._library_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self._library_path)


CONFIG = Config(CONFIG_FILE, LIBRARY_FILE)
TOKEN = secrets.token_urlsafe(32)


# --------------------------------------------------------------------------------------
# Pure-Python LRC parser, LRCLIB lyrics fetcher, and ID3 / MP4 metadata parser
# --------------------------------------------------------------------------------------
_TIME_TAG_RE = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")
_CLEAN_PAREN_RE = re.compile(
    r"[\(\[](?:official|lyric|lyrics|audio|video|music video|visualizer|hd|hq|4k|remastered|feat\.?[^)\]]*)[^)\]]*[\)\]]",
    re.IGNORECASE,
)


def clean_track_query(text: str) -> str:
    if not text:
        return ""
    cleaned = re.sub(r"^\s*\d{1,3}[\s._-]+", "", str(text))
    cleaned = _CLEAN_PAREN_RE.sub("", cleaned)
    cleaned = re.sub(r"\s*-\s*Topic$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"_+", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def clean_youtube_stem(stem: str) -> str:
    if not stem:
        return ""
    s = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", str(stem))
    s = re.sub(
        r"\b(official|music|video|audio|lyric|lyrics|song|full|hd|hq|4k|latest|punjabi|hindi|songs|new|20\d\d|hustle\s*\d*|apna\s*homeground|bhoothnath\s*returns|desi\s*crew|babbar|amar\s*hundal)\b",
        " ",
        s,
        flags=re.IGNORECASE,
    )
    s = re.sub(r"\b[lI]\b", " ", s)
    s = re.sub(r"[|_]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" -–—,")
    return s


def parse_Lrc_metadata_or_lines(text: str, fallback_dur: float = 180.0) -> list[dict]:
    return parse_lrc(text, fallback_dur)


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


def _http_get_json(url: str, timeout: float = 6.0, retries: int = 3):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 MP3PlayerDesktop/2.0",
                    "Accept": "application/json, text/plain, */*",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status != 200:
                    return None
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504) and attempt + 1 < retries:
                time.sleep(0.75 * (attempt + 1))
                continue
            return None
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            if attempt + 1 < retries:
                time.sleep(0.5 * (attempt + 1))
                continue
            return None
    return None


def _download_image_as_data_url(img_url: str, cache_key: str) -> str | None:
    if not img_url:
        return None
    safe_key = hashlib.sha1(cache_key.lower().encode("utf-8", errors="ignore")).hexdigest()
    cache_file = COVERS_DIR / f"{safe_key}.jpg"
    if cache_file.is_file() and cache_file.stat().st_size > 64:
        try:
            raw = cache_file.read_bytes()
            mime = "image/png" if raw[:4] == b"\x89PNG" else "image/jpeg"
            return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
        except OSError:
            pass
    try:
        req = urllib.request.Request(
            img_url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        )
        with urllib.request.urlopen(req, timeout=6.0) as resp:
            if resp.status == 200:
                raw = resp.read()
                if len(raw) > 64:
                    try:
                        cache_file.write_bytes(raw)
                    except OSError:
                        pass
                    mime = "image/png" if raw[:4] == b"\x89PNG" else "image/jpeg"
                    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
    except (urllib.error.URLError, TimeoutError, OSError):
        pass
    return None


def _artist_score(text: str) -> int:
    score = 0
    if re.search(r"\b(feat\.?|ft\.?|featuring|with|prod\.?|x|vs\.?)\b", text, re.I):
        score += 35
    if "," in text:
        score += 30
    if re.search(r"\s+&\s+", text):
        score += 25
    if re.search(r"\b(records|music|entertainment|band|t-series|speed records)\b", text, re.I):
        score += 20
    if re.search(r"\b(remaster(ed)?|remix|version|mix|live|instrumental|deluxe|acoustic|edit|slowed|reverb|radio|official|video|audio|lyric|lyrics|ost|theme)\b", text, re.I):
        score -= 40
    if re.search(r"[!?]$", text.strip()):
        score -= 15
    return score


def _title_score(text: str) -> int:
    score = 0
    if re.search(r"\b(remaster(ed)?|remix|version|mix|live|instrumental|deluxe|acoustic|edit|slowed|reverb|radio|official|video|audio|lyric|lyrics|ost|theme)\b", text, re.I):
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


def _parse_artist_title_from_stem(stem: str) -> tuple[str, str]:
    """Intelligently determine whether a filename is 'Artist - Title' or 'Title - Artist'.
    Returns (artist, title).
    """
    s = re.sub(r"\.[a-zA-Z0-9]{2,5}$", "", (stem or "").strip())
    parts = [p.strip() for p in re.split(r"\s+[-–—]\s+", s) if p.strip()]
    if len(parts) <= 1:
        if "_" in s:
            parts = [p.strip() for p in s.split("_") if p.strip()]
    if len(parts) <= 1:
        return "", s

    if len(parts) == 2:
        left, right = parts[0], parts[1]
        if left.lower() == right.lower():
            return "", left

        # Check for title modifiers like "2012 Remaster", "Live", "Remix"
        if _title_score(right) > 20 and _artist_score(right) <= 0:
            return "", f"{left} ({right})"
        if _title_score(left) > 20 and _artist_score(left) <= 0:
            return "", f"{right} ({left})"

        a_left = _artist_score(left) - _title_score(left)
        a_right = _artist_score(right) - _title_score(right)
        if a_right > a_left + 10:
            return right, left
        elif a_left > a_right + 10:
            return left, right

        # Heuristic: person name (2 title-cased words) vs single word track title
        r_words = right.split()
        l_words = left.split()
        if len(r_words) == 2 and all(w.istitle() for w in r_words) and len(l_words) == 1:
            return right, left
        if len(l_words) == 2 and all(w.istitle() for w in l_words) and len(r_words) == 1:
            return left, right

        # Default standard convention: Artist - Title
        return left, right

    # 3 or more parts (e.g. "Smooth Criminal - 2012 Remaster - Michael Jackson")
    last, first = parts[-1], parts[0]
    if _artist_score(last) >= _artist_score(first):
        rem = parts[:-1]
        tit = f"{rem[0]} ({' - '.join(rem[1:])})" if len(rem) > 1 else rem[0]
        return last, tit
    else:
        rem = parts[1:]
        tit = f"{rem[0]} ({' - '.join(rem[1:])})" if len(rem) > 1 else rem[0]
        return first, tit


def resolve_song_info_and_cover(
    artist: str = "",
    title: str = "",
    filename: str = "",
    need_cover: bool = True,
) -> dict:
    """Resolve canonical artist, clean track title, and coverDataUrl via iTunes & JioSaavn."""
    stem = Path(filename).stem if filename else (title or "")
    raw_artist = "" if re.match(r"^unknown", artist or "", re.IGNORECASE) else clean_track_query(artist)
    raw_title = title or stem

    parsed_artist, parsed_title = _parse_artist_title_from_stem(stem)

    best_artist = raw_artist or parsed_artist
    best_title = raw_title if (raw_title and raw_title != stem) else parsed_title

    # Disambiguation check if best_title looks like artist and best_artist looks like title
    if best_artist and best_title:
        a_title = _artist_score(best_title) - _title_score(best_title)
        a_artist = _artist_score(best_artist) - _title_score(best_artist)
        if a_title > a_artist + 10:
            best_artist, best_title = best_title, best_artist

    cleaned_title = clean_youtube_stem(best_title) or clean_track_query(best_title) or best_title

    # Build search query for iTunes / JioSaavn
    query_str = f"{best_artist} {cleaned_title}".strip() if best_artist else clean_youtube_stem(stem)
    words = query_str.split()
    if len(words) > 6:
        query_str = " ".join(words[:5])

    art_url: str | None = None
    if query_str:
        it_url = f"https://itunes.apple.com/search?term={urllib.parse.quote(query_str)}&entity=song&limit=5"
        it_data = _http_get_json(it_url, timeout=5.0, retries=2)
        results = (it_data or {}).get("results") if isinstance(it_data, dict) else None
        if isinstance(results, list) and results:
            best_it = results[0]
            if best_artist:
                art_words = [w.lower() for w in re.split(r"\W+", best_artist) if len(w) >= 3]
                for cand in results:
                    ca = (cand.get("artistName") or "").lower()
                    if any(w in ca for w in art_words):
                        best_it = cand
                        break
            art_url = (best_it.get("artworkUrl100") or "").replace("100x100bb", "600x600bb") or None
            if not best_artist and best_it.get("artistName"):
                best_artist = best_it["artistName"]
            if (len(best_title.split()) > 4 or "_" in best_title or "  " in best_title) and best_it.get("trackName"):
                cleaned_title = best_it["trackName"]
            elif best_it.get("trackName") and best_title.lower() == stem.lower():
                cleaned_title = best_it["trackName"]

        # JioSaavn fallback if iTunes had no cover or artist
        if not art_url or not best_artist:
            saavn_url = (
                "https://www.jiosaavn.com/api.php?__call=search.getResults"
                f"&_format=json&_marker=0&api_version=4&ctx=web6dot0&n=5&p=1&q={urllib.parse.quote(query_str)}"
            )
            s_data = _http_get_json(saavn_url, timeout=5.0, retries=2)
            s_results = (s_data or {}).get("results") if isinstance(s_data, dict) else None
            if isinstance(s_results, list) and s_results:
                top = s_results[0]
                if not art_url and top.get("image"):
                    art_url = re.sub(r"150x150|50x50", "500x500", str(top["image"]))
                if not best_artist:
                    more = top.get("more_info") or {}
                    amap = more.get("artistMap") or {}
                    prim = amap.get("primary_artists") or []
                    if prim and isinstance(prim[0], dict) and prim[0].get("name"):
                        best_artist = html.unescape(prim[0]["name"])
                if top.get("title") and (len(best_title.split()) > 4 or "_" in best_title):
                    cleaned_title = html.unescape(top["title"])

    final_artist = re.sub(r"\s*-\s*Topic$", "", (best_artist or "").strip(), flags=re.IGNORECASE).strip()
    final_title = cleaned_title or best_title

    cover_data_url = _download_image_as_data_url(art_url, f"{final_artist}-{final_title}") if (need_cover and art_url) else None
    return {
        "artist": final_artist or "Unknown artist",
        "title": final_title,
        "coverDataUrl": cover_data_url,
    }


def fetch_lyrics_lrclib(
    artist: str,
    title: str,
    duration: float = 180.0,
    save_dir: Path | None = None,
    audio_filename: str = "",
) -> dict:
    """Check for local disk (.lrc) lyrics. Automatic online lyric fetching disabled per user request."""
    clean_title = clean_track_query(title)
    clean_artist = "" if re.match(r"^unknown", artist or "", re.IGNORECASE) else clean_track_query(artist)

    # 0. Check if a valid local .lrc file already exists in save_dir
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

    # Automatic online lyrics lookup and auto-saving disabled per user request
    return {"found": False, "rawLrc": None, "lyrics": [], "artist": clean_artist or artist, "title": clean_title or title}


def _remove_unsync(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        b = data[i]
        out.append(b)
        if b == 0xFF and i + 1 < n and data[i + 1] == 0x00:
            i += 2
        else:
            i += 1
    return bytes(out)


def _decode_id3_text(enc: int, raw: bytes) -> str:
    if enc == 0:
        return raw.decode("latin-1", errors="replace").rstrip("\x00")
    if enc == 1:
        return raw.decode("utf-16", errors="replace").rstrip("\x00")
    if enc == 2:
        return raw.decode("utf-16-be", errors="replace").rstrip("\x00")
    return raw.decode("utf-8", errors="replace").rstrip("\x00")


def _skip_term(data: bytes, start: int, enc: int) -> int:
    if enc in (1, 2):
        i = start
        while i + 1 < len(data):
            if data[i] == 0 and data[i + 1] == 0:
                return i + 2
            i += 2
        return len(data)
    idx = data.find(b"\x00", start)
    return (idx + 1) if idx != -1 else len(data)


def extract_audio_metadata(data: bytes) -> dict:
    """Extract title, artist, embedded lyrics, and cover art (as data URL) from ID3v2 or MP4/M4A bytes."""
    result: dict = {"title": None, "artist": None, "lyrics": None, "coverDataUrl": None}
    if len(data) < 12:
        return result

    # ---- ID3v2 (MP3) ----
    if data[:3] == b"ID3":
        ver = data[3]
        flags = data[5]
        tag_size = (
            ((data[6] & 0x7F) << 21)
            | ((data[7] & 0x7F) << 14)
            | ((data[8] & 0x7F) << 7)
            | (data[9] & 0x7F)
        )
        end = min(len(data), 10 + tag_size)
        pos = 10
        if flags & 0x40 and pos + 4 <= end:
            ext_size = (
                (((data[pos] & 0x7F) << 21) | ((data[pos + 1] & 0x7F) << 14) | ((data[pos + 2] & 0x7F) << 7) | (data[pos + 3] & 0x7F))
                if ver == 4
                else struct.unpack(">I", data[pos:pos + 4])[0]
            )
            pos += ext_size if ver == 4 else (ext_size + 4)

        header_len = 6 if ver == 2 else 10
        while pos + header_len <= end:
            if data[pos] == 0:
                break
            if ver == 2:
                fid = data[pos:pos + 3].decode("latin-1", errors="ignore")
                fsize = (data[pos + 3] << 16) | (data[pos + 4] << 8) | data[pos + 5]
                fflags2 = 0
            else:
                fid = data[pos:pos + 4].decode("latin-1", errors="ignore")
                if ver == 4:
                    fsize = (
                        ((data[pos + 4] & 0x7F) << 21)
                        | ((data[pos + 5] & 0x7F) << 14)
                        | ((data[pos + 6] & 0x7F) << 7)
                        | (data[pos + 7] & 0x7F)
                    )
                else:
                    fsize = struct.unpack(">I", data[pos + 4:pos + 8])[0]
                fflags2 = data[pos + 9]

            pos += header_len
            if fsize <= 0 or pos + fsize > end:
                break
            payload = data[pos:pos + fsize]
            pos += fsize

            if (flags & 0x80) or (fflags2 & 0x02):
                payload = _remove_unsync(payload)
            if fflags2 & 0x01 and len(payload) > 4:
                payload = payload[4:]
            if not payload:
                continue

            if fid in ("TIT2", "TT2") and not result["title"]:
                result["title"] = _decode_id3_text(payload[0], payload[1:]).strip() or None
            elif fid in ("TPE1", "TP1") and not result["artist"]:
                art = _decode_id3_text(payload[0], payload[1:]).strip()
                art = re.sub(r"\s*-\s*Topic$", "", art, flags=re.IGNORECASE).strip()
                result["artist"] = art or None
            elif fid in ("USLT", "ULT") and not result["lyrics"] and len(payload) > 4:
                enc = payload[0]
                body_pos = _skip_term(payload, 4, enc)
                txt = _decode_id3_text(enc, payload[body_pos:]).strip()
                if txt:
                    result["lyrics"] = txt
            elif fid in ("APIC", "PIC") and not result["coverDataUrl"] and len(payload) > 8:
                enc = payload[0]
                if fid == "PIC":
                    img_Pos = _skip_term(payload, 5, enc)
                else:
                    mime_end = _skip_term(payload, 1, 0)
                    img_Pos = _skip_term(payload, mime_end + 1, enc)
                img_bytes = payload[img_Pos:]
                for k in range(min(len(img_bytes) - 3, 64)):
                    if (img_bytes[k] == 0xFF and img_bytes[k + 1] == 0xD8) or img_bytes[k:k + 4] == b"\x89PNG":
                        img_bytes = img_bytes[k:]
                        break
                if len(img_bytes) > 16:
                    mime = "image/png" if img_bytes[:4] == b"\x89PNG" else "image/jpeg"
                    b64 = base64.b64encode(img_bytes).decode("ascii")
                    result["coverDataUrl"] = f"data:{mime};base64,{b64}"

    # ---- MP4 / M4A (ftyp / ilst atoms) ----
    if b"ftyp" in data[:16]:
        for atom_id, field in ((b"\xa9nam", "title"), (b"\xa9ART", "artist"), (b"aART", "artist"), (b"\xa9lyr", "lyrics")):
            if result[field]:
                continue
            idx = data.find(atom_id)
            if idx != -1 and idx + 20 < len(data):
                d_idx = data.find(b"data", idx + 4, min(idx + 32, len(data)))
                if d_idx != -1 and d_idx >= 4 and d_idx + 12 <= len(data):
                    asize = struct.unpack(">I", data[d_idx - 4:d_idx])[0]
                    start = d_idx + 12
                    end_atom = min(d_idx - 4 + asize, len(data))
                    if end_atom > start:
                        val = data[start:end_atom].decode("utf-8", errors="replace").strip("\x00 ")
                        if field == "artist":
                            val = re.sub(r"\s*-\s*Topic$", "", val, flags=re.IGNORECASE).strip()
                        if val:
                            result[field] = val
        if not result["coverDataUrl"]:
            covr_idx = data.find(b"covr")
            if covr_idx != -1:
                d_idx = data.find(b"data", covr_idx + 4, min(covr_idx + 64, len(data)))
                if d_idx != -1 and d_idx >= 4 and d_idx + 12 <= len(data):
                    asize = struct.unpack(">I", data[d_idx - 4:d_idx])[0]
                    start = d_idx + 12
                    end_atom = min(d_idx - 4 + asize, len(data))
                    img_bytes = data[start:end_atom]
                    if len(img_bytes) > 16:
                        mime = "image/png" if img_bytes[:4] == b"\x89PNG" else "image/jpeg"
                        b64 = base64.b64encode(img_bytes).decode("ascii")
                        result["coverDataUrl"] = f"data:{mime};base64,{b64}"

    return result


# --------------------------------------------------------------------------------------
# HTTP server: static app files + token-protected Python backend bridge
# --------------------------------------------------------------------------------------
class BridgeError(Exception):
    def __init__(self, status: HTTPStatus, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _safe_name(name: str) -> str:
    """Only plain file names inside the granted folder are allowed (no paths)."""
    if not name or name in (".", "..") or "/" in name or "\\" in name or "\x00" in name:
        raise BridgeError(HTTPStatus.BAD_REQUEST, "Invalid file name")
    return name


class Handler(SimpleHTTPRequestHandler):
    server_version = "MP3PlayerDesktop/2.0"
    STATIC_FILES = {"/", "/index.html", "/desktop-bridge.js"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(APP_DIR), **kwargs)

    def log_message(self, fmt, *args):
        log.debug("%s - %s", self.address_string(), fmt % args)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def _static_allowed(self) -> bool:
        return urlparse(self.path).path in self.STATIC_FILES

    # ---- routing ------------------------------------------------------------------
    def do_GET(self):
        if self.path.startswith("/__bridge/"):
            return self._bridge("GET")
        if not self._static_allowed():
            return self.send_error(HTTPStatus.NOT_FOUND)
        return super().do_GET()

    def do_HEAD(self):
        if self.path.startswith("/__bridge/") or not self._static_allowed():
            return self.send_error(HTTPStatus.NOT_FOUND)
        return super().do_HEAD()

    def do_POST(self):
        return self._bridge("POST")

    def do_PUT(self):
        return self._bridge("PUT")

    def do_DELETE(self):
        return self._bridge("DELETE")

    # ---- helpers ------------------------------------------------------------------
    def _send_json(self, status: HTTPStatus, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 50 * 1024 * 1024:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except (ValueError, UnicodeDecodeError):
            raise BridgeError(HTTPStatus.BAD_REQUEST, "Invalid JSON payload")

    def _granted_dir(self, query: dict) -> Path:
        raw = (query.get("dir") or [""])[0]
        if not raw:
            raise BridgeError(HTTPStatus.BAD_REQUEST, "Missing dir")
        directory = Path(raw)
        if not directory.is_absolute() or not CONFIG.is_allowed(directory):
            raise BridgeError(HTTPStatus.FORBIDDEN, "Folder access was not granted")
        if not directory.is_dir():
            raise BridgeError(HTTPStatus.NOT_FOUND, "Folder no longer exists")
        return directory

    def _bridge(self, method: str) -> None:
        try:
            if not secrets.compare_digest(self.headers.get("X-Bridge-Token", ""), TOKEN):
                raise BridgeError(HTTPStatus.FORBIDDEN, "Bad token")
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            route = parsed.path[len("/__bridge/"):]

            # 1. Settings & Library Persistence in Python
            if route == "settings":
                if method == "GET":
                    return self._send_json(HTTPStatus.OK, CONFIG.get_settings())
                if method in ("PUT", "POST"):
                    return self._send_json(HTTPStatus.OK, CONFIG.update_settings(self._read_json_body()))

            if route == "library":
                if method == "GET":
                    return self._send_json(HTTPStatus.OK, CONFIG.load_library())
                if method in ("PUT", "POST"):
                    CONFIG.save_library(self._read_json_body())
                    return self._send_json(HTTPStatus.OK, {"saved": True})

            # 2. Lyrics Fetching & LRC Parsing in Python
            if route == "lyrics" and method == "GET":
                artist = (query.get("artist") or [""])[0]
                title = (query.get("title") or [""])[0]
                audio_name = (query.get("name") or [""])[0]
                try:
                    dur = float((query.get("duration") or ["180"])[0])
                except ValueError:
                    dur = 180.0
                dir_raw = (query.get("dir") or [""])[0]
                save_dir = Path(dir_raw) if dir_raw else None
                if not save_dir:
                    linked = CONFIG.get_settings().get("linked_dir")
                    if linked:
                        save_dir = Path(linked)
                return self._send_json(HTTPStatus.OK, fetch_lyrics_lrclib(artist, title, dur, save_dir, audio_name))

            if route == "parse-lrc" and method == "POST":
                body = self._read_json_body()
                return self._send_json(HTTPStatus.OK, {
                    "lyrics": parse_lrc(body.get("text", ""), float(body.get("duration") or 180.0))
                })

            # 3. Audio Metadata & Online Cover/Artist Enrichment in Python
            if route == "metadata" and method == "GET":
                dir_raw = (query.get("dir") or [""])[0]
                if not dir_raw:
                    dir_raw = CONFIG.get_settings().get("linked_dir") or ""
                file_name = (query.get("name") or [""])[0]
                q_artist = (query.get("artist") or [""])[0]
                q_title = (query.get("title") or [""])[0]

                meta: dict = {"title": None, "artist": None, "lyrics": None, "coverDataUrl": None}
                if dir_raw and file_name:
                    try:
                        directory = Path(dir_raw)
                        if directory.is_absolute() and CONFIG.is_allowed(directory) and directory.is_dir():
                            target = directory / _safe_name(file_name)
                            if target.is_file():
                                sz = target.stat().st_size
                                with open(target, "rb") as fh:
                                    if sz <= 16 * 1024 * 1024:
                                        file_bytes = fh.read()
                                    else:
                                        head_b = fh.read(256 * 1024)
                                        fh.seek(max(0, sz - 1024 * 1024))
                                        tail_b = fh.read()
                                        file_bytes = head_b + tail_b
                                meta = extract_audio_metadata(file_bytes)
                                # Check for local .lrc alongside audio file
                                if not meta.get("lyrics"):
                                    lrc_file = target.with_suffix(".lrc")
                                    if lrc_file.is_file():
                                        try:
                                            meta["lyrics"] = lrc_file.read_text(encoding="utf-8", errors="replace").strip() or None
                                        except OSError:
                                            pass
                    except BridgeError:
                        pass

                cur_artist = meta.get("artist") or ("" if re.match(r"^unknown", q_artist, re.I) else q_artist)
                cur_title = meta.get("title") or q_title or (Path(file_name).stem if file_name else "")
                need_cover = not bool(meta.get("coverDataUrl"))
                has_authoritative_tags = bool(meta.get("title") and meta.get("artist"))
                need_info = not has_authoritative_tags and (not cur_artist or len(cur_title.split()) > 4 or "_" in cur_title or "-" in cur_title)
                if need_cover or need_info:
                    enriched = resolve_song_info_and_cover(cur_artist, cur_title, file_name, need_cover=need_cover)
                    if not meta.get("artist") and enriched.get("artist"):
                        meta["artist"] = enriched["artist"]
                    if not meta.get("title") and enriched.get("title"):
                        meta["title"] = enriched["title"]
                    if not meta.get("coverDataUrl") and enriched.get("coverDataUrl"):
                        meta["coverDataUrl"] = enriched["coverDataUrl"]
                return self._send_json(HTTPStatus.OK, meta)

            # 4. Folder & File Operations in Python
            directory = self._granted_dir(query)
            if route == "list" and method == "GET":
                return self._list(directory)
            if route in ("file", "stat"):
                target = directory / _safe_name((query.get("name") or [""])[0])
                if route == "stat" and method == "GET":
                    return self._stat(target)
                if route == "file" and method == "GET":
                    return self._read(target)
                if route == "file" and method == "PUT":
                    return self._write(target)
                if route == "file" and method == "DELETE":
                    return self._delete(target)

            raise BridgeError(HTTPStatus.NOT_FOUND, "Unknown bridge route")
        except BridgeError as exc:
            self._send_json(exc.status, {"error": exc.message})
        except PermissionError:
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "Permission denied by the OS"})
        except OSError as exc:
            log.warning("Bridge I/O error: %s", exc)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    # ---- file operations ----------------------------------------------------------
    def _list(self, directory: Path) -> None:
        entries = []
        with os.scandir(directory) as it:
            for entry in it:
                if entry.name.startswith(".") or entry.name.endswith(".mp3player-tmp"):
                    continue
                try:
                    st = entry.stat()
                    entries.append({
                        "name": entry.name,
                        "kind": "directory" if entry.is_dir() else "file",
                        "size": st.st_size,
                        "lastModified": int(st.st_mtime * 1000),
                    })
                except OSError:
                    continue
        self._send_json(HTTPStatus.OK, entries)

    def _stat(self, target: Path) -> None:
        if not target.exists():
            raise BridgeError(HTTPStatus.NOT_FOUND, "Not found")
        st = target.stat()
        self._send_json(HTTPStatus.OK, {
            "name": target.name,
            "kind": "directory" if target.is_dir() else "file",
            "size": st.st_size,
            "lastModified": int(st.st_mtime * 1000),
        })

    def _read(self, target: Path) -> None:
        if not target.is_file():
            raise BridgeError(HTTPStatus.NOT_FOUND, "Not found")
        st = target.stat()
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(st.st_size))
        self.send_header("X-Last-Modified", str(int(st.st_mtime * 1000)))
        self.end_headers()
        with open(target, "rb") as fh:
            while chunk := fh.read(256 * 1024):
                self.wfile.write(chunk)

    def _write(self, target: Path) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0 or length > MAX_WRITE_BYTES:
            raise BridgeError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "File too large")
        tmp = target.with_name(target.name + ".mp3player-tmp")
        try:
            remaining = length
            with open(tmp, "wb") as fh:
                while remaining > 0:
                    chunk = self.rfile.read(min(256 * 1024, remaining))
                    if not chunk:
                        raise BridgeError(HTTPStatus.BAD_REQUEST, "Upload interrupted")
                    fh.write(chunk)
                    remaining -= len(chunk)
            os.replace(tmp, target)   # atomic write
        finally:
            tmp.unlink(missing_ok=True)
        self._send_json(HTTPStatus.OK, {"name": target.name, "size": length})

    def _delete(self, target: Path) -> None:
        if not target.is_file():
            raise BridgeError(HTTPStatus.NOT_FOUND, "Not found")
        target.unlink()
        self._send_json(HTTPStatus.OK, {"deleted": target.name})


def start_server(preferred_port: int) -> ThreadingHTTPServer:
    try:
        server = ThreadingHTTPServer(("127.0.0.1", preferred_port), Handler)
    except OSError:
        log.warning("Port %d is busy; using an available port.", preferred_port)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="bridge-http", daemon=True).start()
    return server


# --------------------------------------------------------------------------------------
# Native dialogs & Python API exposed to JavaScript (window.pywebview.api.*)
# --------------------------------------------------------------------------------------
def _dialog_type(new_name: str, legacy_name: str):
    file_dialog = getattr(webview, "FileDialog", None)   # pywebview >= 6
    if file_dialog is not None:
        return getattr(file_dialog, new_name)
    return getattr(webview, legacy_name)                 # pywebview 5.x


def _first_path(result) -> str | None:
    if not result:
        return None
    if isinstance(result, (list, tuple)):
        return str(result[0]) if result else None
    return str(result)


class DesktopApi:
    def __init__(self):
        self._window: webview.Window | None = None

    def attach(self, window: webview.Window) -> None:
        self._window = window

    def pick_folder(self):
        """Native folder chooser. Returns {dir, name} or None if cancelled."""
        result = self._window.create_file_dialog(_dialog_type("FOLDER", "FOLDER_DIALOG"))
        chosen = _first_path(result)
        if not chosen:
            return None
        directory = Path(chosen)
        return {"dir": CONFIG.allow_dir(directory), "name": directory.name or str(directory)}

    def pick_save_file(self, suggested_name: str = "song.mp3"):
        """Native save dialog. Returns {dir, name} or None if cancelled."""
        result = self._window.create_file_dialog(
            _dialog_type("SAVE", "SAVE_DIALOG"),
            save_filename=os.path.basename(suggested_name or "song.mp3"),
        )
        chosen = _first_path(result)
        if not chosen:
            return None
        target = Path(chosen)
        return {"dir": CONFIG.allow_dir(target.parent), "name": target.name}

    def pick_audio_files(self):
        """Native multi-file chooser for adding songs directly via Python."""
        result = self._window.create_file_dialog(
            _dialog_type("OPEN", "OPEN_DIALOG"),
            allow_multiple=True,
            file_types=("Audio & Lyrics (*.mp3;*.wav;*.ogg;*.m4a;*.flac;*.aac;*.lrc)", "All files (*.*)"),
        )
        if not result:
            return []
        paths = [Path(p) for p in (result if isinstance(result, (list, tuple)) else [result])]
        out = []
        for p in paths:
            if p.is_file():
                out.append({"dir": CONFIG.allow_dir(p.parent), "name": p.name})
        return out

    def fetch_lyrics(self, artist: str, title: str, duration: float = 180.0, save_dir: str = ""):
        """Direct Python RPC for fetching synced lyrics & saving .lrc to disk."""
        folder = Path(save_dir) if save_dir else None
        return fetch_lyrics_lrclib(artist, title, float(duration or 180.0), folder)

    def platform_info(self):
        return {
            "platform": sys.platform,
            "dataDir": str(DATA_DIR),
            "settings": CONFIG.get_settings(),
        }


# --------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--debug", action="store_true", help="enable dev tools and verbose logs")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="local port for the UI server")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if not (APP_DIR / "index.html").is_file():
        sys.exit(f"index.html not found next to app.py ({APP_DIR})")

    server = start_server(args.port)
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/index.html?desktop=1#token={TOKEN}"
    log.info("Serving %s on http://127.0.0.1:%d", APP_DIR, port)

    api = DesktopApi()
    window = webview.create_window(
        APP_NAME,
        url,
        js_api=api,
        width=1280,
        height=820,
        min_size=(360, 600),
        background_color="#0f1115",
        text_select=False,
    )
    api.attach(window)

    try:
        webview.start(
            debug=args.debug,
            private_mode=False,                         # keep localStorage / IndexedDB
            storage_path=str(DATA_DIR / "webview"),
        )
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
