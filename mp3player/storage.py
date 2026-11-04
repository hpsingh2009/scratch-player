"""Persistent storage (settings, library, playlists) and music-folder syncing."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
from pathlib import Path
from typing import Callable

from . import APP_ID, AUDIO_EXTS
from .lrc import parse_lrc
from .naming import resolve_names
from .online import fetch_cover
from .tags import read_tags


def data_dir() -> Path:
    env = os.environ.get("FLET_APP_STORAGE_DATA")  # set by Flet on Android / iOS
    if env:
        base = Path(env)
    elif sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / APP_ID
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / APP_ID
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / APP_ID
    base.mkdir(parents=True, exist_ok=True)
    return base


def default_music_dir() -> str:
    if os.environ.get("FLET_PLATFORM", "").lower() == "android":
        return "/storage/emulated/0/Music"
    return str(Path.home() / "Music")


def song_id(path: str) -> str:
    return hashlib.sha1(os.path.normcase(path).encode("utf-8", "ignore")).hexdigest()[:16]


class Store:
    DEFAULTS = {"theme": "dark", "accent": "#d4163c", "folder": None, "lyrics": True, "volume": 0.8}

    def __init__(self) -> None:
        self.dir = data_dir()
        self.covers = self.dir / "covers"
        self.covers.mkdir(exist_ok=True)
        self._file = self.dir / "state.json"
        self._lock = threading.RLock()
        self.settings: dict = dict(self.DEFAULTS)
        self.songs: dict[str, dict] = {}
        self.playlists: list[dict] = []   # {id, name, songs: [song_id]}
        self.recent: list[str] = []
        self._load()
        if not self.settings.get("folder"):
            self.settings["folder"] = default_music_dir()

    # ---------- persistence ----------
    def _load(self) -> None:
        try:
            d = json.loads(self._file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.settings.update(d.get("settings", {}))
        self.songs = d.get("songs", {})
        self.playlists = d.get("playlists", [])
        self.recent = d.get("recent", [])

    def save(self) -> None:
        with self._lock:
            tmp = self._file.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(
                    {"settings": self.settings, "songs": self.songs, "playlists": self.playlists, "recent": self.recent},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            os.replace(tmp, self._file)

    # ---------- library ----------
    def ordered_songs(self) -> list[dict]:
        return sorted(self.songs.values(), key=lambda s: (s["title"].lower(), s["artist"].lower()))

    def _save_cover(self, sid: str, data: bytes) -> str:
        p = self.covers / f"{sid}.jpg"
        p.write_bytes(data)
        return str(p)

    def _build_song(self, path: str) -> dict:
        sid = song_id(path)
        tags = read_tags(path)
        stem = Path(path).stem
        artist, title = resolve_names(tags["artist"] or "", tags["title"] or "", stem)
        cover = self._save_cover(sid, tags["cover"]) if tags["cover"] else None
        return {
            "id": sid, "path": path, "title": title, "artist": artist,
            "duration": tags["duration"], "cover": cover, "lyrics": tags["lyrics"],
            "added": os.path.getmtime(path) if os.path.exists(path) else 0,
        }

    def sync_folder(self, on_progress: Callable[[], None] | None = None) -> tuple[int, int]:
        """Add new files, drop deleted ones. Returns (added, removed). Blocking - run in a thread."""
        folder = self.settings.get("folder")
        found: set[str] = set()
        if folder and os.path.isdir(folder):
            for root, _dirs, files in os.walk(folder):
                for f in files:
                    if Path(f).suffix.lower() in AUDIO_EXTS:
                        found.add(os.path.join(root, f))
        added = removed = 0
        with self._lock:
            known = {s["path"]: sid for sid, s in self.songs.items() if not s.get("manual")}
        for path, sid in known.items():
            if path not in found and not os.path.exists(path):
                with self._lock:
                    self.songs.pop(sid, None)
                    for pl in self.playlists:
                        if sid in pl["songs"]:
                            pl["songs"].remove(sid)
                removed += 1
        for path in sorted(found):
            if song_id(path) in self.songs:
                continue
            song = self._build_song(path)
            with self._lock:
                self.songs[song["id"]] = song
            added += 1
            if on_progress and added % 10 == 0:
                on_progress()
        self.save()
        return added, removed

    def add_files(self, paths: list[str]) -> int:
        n = 0
        for p in paths:
            if Path(p).suffix.lower() in AUDIO_EXTS and song_id(p) not in self.songs:
                song = self._build_song(p)
                song["manual"] = True
                self.songs[song["id"]] = song
                n += 1
        self.save()
        return n

    def fetch_missing_covers(self, on_progress: Callable[[], None] | None = None) -> None:
        """Best-effort online cover lookup for songs with no embedded art (background)."""
        for s in list(self.songs.values()):
            if s.get("cover") or s.get("cover_tried"):
                continue
            data = fetch_cover(s["artist"], s["title"])
            s["cover_tried"] = True
            if data:
                s["cover"] = self._save_cover(s["id"], data)
                if on_progress:
                    on_progress()
        self.save()

    def lyrics_for(self, song: dict) -> list[dict]:
        """Synced lines from a sibling .lrc file, else lyrics embedded in the audio file."""
        lrc = Path(song["path"]).with_suffix(".lrc")
        text = None
        if lrc.is_file():
            try:
                text = lrc.read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass
        return parse_lrc(text or song.get("lyrics") or "", song.get("duration") or 180.0)

    # ---------- recents & playlists ----------
    def mark_played(self, sid: str) -> None:
        if sid in self.recent:
            self.recent.remove(sid)
        self.recent.insert(0, sid)
        del self.recent[30:]
        self.save()

    def recent_songs(self) -> list[dict]:
        return [self.songs[i] for i in self.recent if i in self.songs]

    def create_playlist(self, name: str) -> dict:
        pl = {"id": hashlib.sha1(f"{name}{len(self.playlists)}{os.urandom(4)}".encode()).hexdigest()[:10], "name": name, "songs": []}
        self.playlists.append(pl)
        self.save()
        return pl

    def delete_playlist(self, pid: str) -> None:
        self.playlists = [p for p in self.playlists if p["id"] != pid]
        self.save()

    def playlist_songs(self, pl: dict) -> list[dict]:
        return [self.songs[i] for i in pl["songs"] if i in self.songs]

    def add_to_playlist(self, pid: str, sid: str) -> bool:
        for pl in self.playlists:
            if pl["id"] == pid and sid not in pl["songs"]:
                pl["songs"].append(sid)
                self.save()
                return True
        return False

    def remove_from_playlist(self, pid: str, sid: str) -> None:
        for pl in self.playlists:
            if pl["id"] == pid and sid in pl["songs"]:
                pl["songs"].remove(sid)
        self.save()
