"""
Configuration, local library persistence, and data directory management.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import sys
import threading
from pathlib import Path

from . import APP_ID

log = logging.getLogger(APP_ID)


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
