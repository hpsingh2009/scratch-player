"""
Desktop application runner, pywebview window lifecycle, and native system dialogs.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import webview

from . import APP_ID, APP_NAME, DEFAULT_PORT
from .lyrics import fetch_lyrics_lrclib
from .server import UI_DIR, start_server
from .storage import CONFIG, DATA_DIR, TOKEN

log = logging.getLogger("mp3player.app")


def _dialog_type(new_name: str, legacy_name: str):
    file_dialog = getattr(webview, "FileDialog", None)  # pywebview >= 6
    if file_dialog is not None:
        return getattr(file_dialog, new_name)
    return getattr(webview, legacy_name)  # pywebview 5.x


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


def main() -> None:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--debug", action="store_true", help="enable dev tools and verbose logs")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="local port for the UI server")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not (UI_DIR / "index.html").is_file():
        sys.exit(f"index.html not found in {UI_DIR}")

    server = start_server(args.port)
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/index.html?desktop=1#token={TOKEN}"
    log.info("Serving UI from %s on http://127.0.0.1:%d", UI_DIR, port)

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
            private_mode=False,
            storage_path=str(DATA_DIR / "webview"),
        )
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
