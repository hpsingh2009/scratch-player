"""
Audio metadata extraction (ID3v2, MP4/M4A, FLAC) and intelligent artist/title heuristics.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
import re
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .lyrics import clean_track_query
from .storage import COVERS_DIR

log = logging.getLogger("mp3player.metadata")


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
    if re.search(
        r"\b(remaster(ed)?|remix|version|mix|live|instrumental|deluxe|acoustic|edit|slowed|reverb|radio|official|video|audio|lyric|lyrics|ost|theme)\b",
        text,
        re.I,
    ):
        score -= 40
    if re.search(r"[!?]$", text.strip()):
        score -= 15
    return score


def _title_score(text: str) -> int:
    score = 0
    if re.search(
        r"\b(remaster(ed)?|remix|version|mix|live|instrumental|deluxe|acoustic|edit|slowed|reverb|radio|official|video|audio|lyric|lyrics|ost|theme)\b",
        text,
        re.I,
    ):
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

        r_words = right.split()
        l_words = left.split()
        if len(r_words) == 2 and all(w.istitle() for w in r_words) and len(l_words) == 1:
            return right, left
        if len(l_words) == 2 and all(w.istitle() for w in l_words) and len(r_words) == 1:
            return left, right

        return left, right

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

    if best_artist and best_title:
        a_title = _artist_score(best_title) - _title_score(best_title)
        a_artist = _artist_score(best_artist) - _title_score(best_artist)
        if a_title > a_artist + 10:
            best_artist, best_title = best_title, best_artist

    cleaned_title = clean_youtube_stem(best_title) or clean_track_query(best_title) or best_title

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

    cover_data_url = (
        _download_image_as_data_url(art_url, f"{final_artist}-{final_title}")
        if (need_cover and art_url)
        else None
    )
    return {
        "artist": final_artist or "Unknown artist",
        "title": final_title,
        "coverDataUrl": cover_data_url,
    }


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


def _parse_sylt_frame(payload: bytes) -> str:
    if len(payload) < 10:
        return ""
    enc = payload[0]
    time_format = payload[4]
    p = _skip_term(payload, 6, enc)
    lines: list[str] = []
    while p < len(payload):
        text_start = p
        p = _skip_term(payload, p, enc)
        if p + 4 > len(payload):
            break
        text_bytes = payload[text_start : p - (2 if enc in (1, 2) else 1)]
        time_ms = struct.unpack(">I", payload[p : p + 4])[0]
        p += 4
        line_text = _decode_id3_text(enc, text_bytes).strip()
        if line_text:
            sec = time_ms * 0.026 if time_format == 1 else time_ms / 1000.0
            mm = int(sec // 60)
            ss = int(sec % 60)
            cs = int((sec % 1) * 100)
            lines.append(f"[{mm:02d}:{ss:02d}.{cs:02d}] {line_text}")
    return "\n".join(lines)


def _parse_txxx_lyrics(payload: bytes) -> str:
    if len(payload) < 5:
        return ""
    enc = payload[0]
    desc_end = _skip_term(payload, 1, enc)
    desc_bytes = payload[1 : desc_end - (2 if enc in (1, 2) else 1)]
    desc = _decode_id3_text(enc, desc_bytes).strip().upper()
    if desc in ("LYRICS", "UNSYNCEDLYRICS", "SYNCEDLYRICS"):
        return _decode_id3_text(enc, payload[desc_end:]).strip()
    return ""


def extract_audio_metadata(data: bytes) -> dict:
    """Extract title, artist, embedded lyrics, and cover art (as data URL) from ID3v2 or MP4/M4A bytes."""
    result: dict = {"title": None, "artist": None, "lyrics": None, "coverDataUrl": None}
    if len(data) < 12:
        return result

    # ID3v2 (MP3)
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
                else struct.unpack(">I", data[pos : pos + 4])[0]
            )
            pos += ext_size if ver == 4 else (ext_size + 4)

        header_len = 6 if ver == 2 else 10
        while pos + header_len <= end:
            if data[pos] == 0:
                break
            if ver == 2:
                fid = data[pos : pos + 3].decode("latin-1", errors="ignore")
                fsize = (data[pos + 3] << 16) | (data[pos + 4] << 8) | data[pos + 5]
                fflags2 = 0
            else:
                fid = data[pos : pos + 4].decode("latin-1", errors="ignore")
                if ver == 4:
                    fsize = (
                        ((data[pos + 4] & 0x7F) << 21)
                        | ((data[pos + 5] & 0x7F) << 14)
                        | ((data[pos + 6] & 0x7F) << 7)
                        | (data[pos + 7] & 0x7F)
                    )
                else:
                    fsize = struct.unpack(">I", data[pos + 4 : pos + 8])[0]
                fflags2 = data[pos + 9]

            pos += header_len
            if fsize <= 0 or pos + fsize > end:
                break
            payload = data[pos : pos + fsize]
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
            elif fid in ("SYLT", "SLT") and not result["lyrics"] and len(payload) > 8:
                txt = _parse_sylt_frame(payload)
                if txt:
                    result["lyrics"] = txt
            elif fid == "TXXX" and not result["lyrics"] and len(payload) > 6:
                txt = _parse_txxx_lyrics(payload)
                if txt:
                    result["lyrics"] = txt
            elif fid in ("APIC", "PIC") and not result["coverDataUrl"] and len(payload) > 8:
                enc = payload[0]
                if fid == "PIC":
                    img_pos = _skip_term(payload, 5, enc)
                else:
                    mime_end = _skip_term(payload, 1, 0)
                    img_pos = _skip_term(payload, mime_end + 1, enc)
                img_bytes = payload[img_pos:]
                for k in range(min(len(img_bytes) - 3, 64)):
                    if (img_bytes[k] == 0xFF and img_bytes[k + 1] == 0xD8) or img_bytes[k : k + 4] == b"\x89PNG":
                        img_bytes = img_bytes[k:]
                        break
                if len(img_bytes) > 16:
                    mime = "image/png" if img_bytes[:4] == b"\x89PNG" else "image/jpeg"
                    b64 = base64.b64encode(img_bytes).decode("ascii")
                    result["coverDataUrl"] = f"data:{mime};base64,{b64}"

    # MP4 / M4A (ftyp / ilst atoms)
    if b"ftyp" in data[:16]:
        for atom_id, field in ((b"\xa9nam", "title"), (b"\xa9ART", "artist"), (b"aART", "artist"), (b"\xa9lyr", "lyrics")):
            if result[field]:
                continue
            idx = data.find(atom_id)
            if idx != -1 and idx + 20 < len(data):
                d_idx = data.find(b"data", idx + 4, min(idx + 32, len(data)))
                if d_idx != -1 and d_idx >= 4 and d_idx + 12 <= len(data):
                    asize = struct.unpack(">I", data[d_idx - 4 : d_idx])[0]
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
                    asize = struct.unpack(">I", data[d_idx - 4 : d_idx])[0]
                    start = d_idx + 12
                    end_atom = min(d_idx - 4 + asize, len(data))
                    img_bytes = data[start:end_atom]
                    if len(img_bytes) > 16:
                        mime = "image/png" if img_bytes[:4] == b"\x89PNG" else "image/jpeg"
                        b64 = base64.b64encode(img_bytes).decode("ascii")
                        result["coverDataUrl"] = f"data:{mime};base64,{b64}"

    # FLAC / OGG Vorbis Comments
    if not result["lyrics"]:
        for marker in (b"LYRICS=", b"UNSYNCEDLYRICS=", b"SYNCEDLYRICS="):
            v_idx = data.find(marker)
            if v_idx != -1:
                start = v_idx + len(marker)
                end_v = data.find(b"\x00", start, min(start + 65536, len(data)))
                if end_v == -1:
                    end_v = min(start + 65536, len(data))
                txt = data[start:end_v].decode("utf-8", errors="replace").strip()
                if len(txt) > 10:
                    result["lyrics"] = txt
                    break

    return result
