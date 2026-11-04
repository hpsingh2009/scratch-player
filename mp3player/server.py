"""
Local bridge HTTP server: serves the original UI assets and token-protected Python RPC endpoints.
"""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import secrets
import sys
import threading
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .lyrics import fetch_lyrics_lrclib, parse_lrc
from .metadata import extract_audio_metadata, resolve_song_info_and_cover
from .storage import CONFIG, TOKEN

log = logging.getLogger("mp3player.server")
MAX_WRITE_BYTES = 1024 * 1024 * 1024  # 1 GB per file write

mimetypes.add_type("audio/mp4", ".m4a")
mimetypes.add_type("audio/flac", ".flac")
mimetypes.add_type("audio/aac", ".aac")
mimetypes.add_type("text/plain", ".lrc")
mimetypes.add_type("text/javascript", ".js")


def find_ui_dir() -> Path:
    """Find the directory containing index.html."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        p1 = Path(meipass) / "mp3player" / "ui"
        if (p1 / "index.html").is_file():
            return p1
        p2 = Path(meipass)
        if (p2 / "index.html").is_file():
            return p2

    p_pkg = Path(__file__).resolve().parent / "ui"
    if (p_pkg / "index.html").is_file():
        return p_pkg

    p_root = Path(__file__).resolve().parent.parent
    if (p_root / "index.html").is_file():
        return p_root

    return p_pkg


UI_DIR = find_ui_dir()


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
    server_version = "MP3PlayerDesktop/2.1"
    STATIC_FILES = {"/", "/index.html", "/desktop-bridge.js"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(UI_DIR), **kwargs)

    def log_message(self, fmt, *args):
        log.debug("%s - %s", self.address_string(), fmt % args)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def _static_allowed(self) -> bool:
        return urlparse(self.path).path in self.STATIC_FILES

    # routing
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

    # helpers
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
            route = parsed.path[len("/__bridge/") :]

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

            # 2. Lyrics & LRC Parsing in Python
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
                return self._send_json(
                    HTTPStatus.OK,
                    {"lyrics": parse_lrc(body.get("text", ""), float(body.get("duration") or 180.0))},
                )

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
                                            meta["lyrics"] = (
                                                lrc_file.read_text(encoding="utf-8", errors="replace").strip() or None
                                            )
                                        except OSError:
                                            pass
                    except BridgeError:
                        pass

                cur_artist = meta.get("artist") or ("" if re.match(r"^unknown", q_artist, re.I) else q_artist)
                cur_title = meta.get("title") or q_title or (Path(file_name).stem if file_name else "")
                need_cover = not bool(meta.get("coverDataUrl"))
                has_authoritative_tags = bool(meta.get("title") and meta.get("artist"))
                need_info = not has_authoritative_tags and (
                    not cur_artist or len(cur_title.split()) > 4 or "_" in cur_title or "-" in cur_title
                )
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

    def _list(self, directory: Path) -> None:
        entries = []
        with os.scandir(directory) as it:
            for entry in it:
                if entry.name.startswith(".") or entry.name.endswith(".mp3player-tmp"):
                    continue
                try:
                    st = entry.stat()
                    entries.append(
                        {
                            "name": entry.name,
                            "kind": "directory" if entry.is_dir() else "file",
                            "size": st.st_size,
                            "lastModified": int(st.st_mtime * 1000),
                        }
                    )
                except OSError:
                    continue
        self._send_json(HTTPStatus.OK, entries)

    def _stat(self, target: Path) -> None:
        if not target.exists():
            raise BridgeError(HTTPStatus.NOT_FOUND, "Not found")
        st = target.stat()
        self._send_json(
            HTTPStatus.OK,
            {
                "name": target.name,
                "kind": "directory" if target.is_dir() else "file",
                "size": st.st_size,
                "lastModified": int(st.st_mtime * 1000),
            },
        )

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
            os.replace(tmp, target)
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
