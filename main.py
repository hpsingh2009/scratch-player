#!/usr/bin/env python3
"""MP3 Player - 100% Python (Flet) music player for Windows / macOS / Linux / Android.

Features: library sync on startup, home with recent songs + ghost (skeleton) screen,
playlists, search (/ or Ctrl+F), synced lyrics (.lrc file or embedded tags - no downloading),
embedded cover art, vinyl disc with drag-to-scratch seeking, dark/light theme + accent colour.

Run:  python main.py        Build Android:  flet build apk
"""
from __future__ import annotations

import asyncio
import math
import os
import random
import time

import flet as ft
import flet_audio as fta

from mp3player import APP_NAME, AUDIO_EXTS
from mp3player.lrc import active_index
from mp3player.storage import Store

ACCENTS = ["#d4163c", "#7c3aed", "#0ea5e9", "#16a34a", "#f59e0b", "#ec4899"]
LYRIC_ROW_H = 44
SCRATCH_SEC_PER_REV = 1.8  # 33 1/3 rpm


def fmt_time(sec: float | None) -> str:
    if not sec or sec <= 0 or sec != sec:
        return "--:--"
    sec = int(sec)
    return f"{sec // 60}:{sec % 60:02d}"


def dur_seconds(d) -> float:
    if d is None:
        return 0.0
    if isinstance(d, (int, float)):
        return float(d) / 1000.0
    return (
        getattr(d, "days", 0) * 86400 + getattr(d, "hours", 0) * 3600 + getattr(d, "minutes", 0) * 60
        + getattr(d, "seconds", 0) + getattr(d, "milliseconds", 0) / 1000 + getattr(d, "microseconds", 0) / 1e6
    )


def attached(ctrl) -> bool:
    """True if the control is currently mounted on the page (Control.page raises otherwise)."""
    try:
        return ctrl is not None and ctrl.page is not None
    except RuntimeError:
        return False


def tint(opacity: float = 0.08):
    return ft.Colors.with_opacity(opacity, ft.Colors.ON_SURFACE)


class PlayerApp:
    def __init__(self, page: ft.Page) -> None:
        self.page = page
        self.store = Store()
        # navigation
        self.tab = "home"
        self.open_pl: str | None = None
        self.player_open = False
        self.query = ""
        self.syncing = False
        # playback
        self.audio: fta.Audio | None = None
        self.queue: list[str] = []
        self.qi = -1
        self.playing = False
        self.shuffle = False
        self.repeat = 0  # 0 off, 1 all, 2 one
        self.pos_base = 0.0
        self.pos_t0 = time.monotonic()
        self.duration = 0.0
        self.lyrics: list[dict] = []
        self.lyric_idx = -2
        self.disc_angle = 0.0
        self.scratching = False
        self._scratch_last_angle = 0.0
        self._scratch_last_seek = 0.0
        self._scratch_resume = False
        # widgets
        self.search_ref = ft.Ref[ft.TextField]()
        self.body = ft.Container(expand=True)
        self.mini = ft.Container()
        self.nav = ft.NavigationBar(
            destinations=[
                ft.NavigationBarDestination(icon=ft.Icons.HOME_OUTLINED, selected_icon=ft.Icons.HOME, label="Home"),
                ft.NavigationBarDestination(icon=ft.Icons.LIBRARY_MUSIC_OUTLINED, selected_icon=ft.Icons.LIBRARY_MUSIC, label="Library"),
                ft.NavigationBarDestination(icon=ft.Icons.QUEUE_MUSIC_OUTLINED, selected_icon=ft.Icons.QUEUE_MUSIC, label="Playlists"),
                ft.NavigationBarDestination(icon=ft.Icons.SETTINGS_OUTLINED, selected_icon=ft.Icons.SETTINGS, label="Settings"),
            ],
            on_change=self.on_nav,
        )
        self.main_view = ft.Column([self.body, self.mini, self.nav], spacing=0, expand=True)
        self.player_view = ft.Container(expand=True)
        self.root = ft.Container(content=self.main_view, expand=True)
        self.picker = ft.FilePicker()

    # ------------------------------------------------------------------ boot
    @property
    def accent(self) -> str:
        return self.store.settings.get("accent", ACCENTS[0])

    @property
    def current(self) -> dict | None:
        if 0 <= self.qi < len(self.queue):
            return self.store.songs.get(self.queue[self.qi])
        return None

    def apply_theme(self) -> None:
        dark = self.store.settings.get("theme") == "dark"
        self.page.theme_mode = ft.ThemeMode.DARK if dark else ft.ThemeMode.LIGHT
        self.page.theme = ft.Theme(color_scheme_seed=self.accent, use_material3=True)
        self.page.dark_theme = ft.Theme(color_scheme_seed=self.accent, use_material3=True)

    async def start(self) -> None:
        p = self.page
        p.title = APP_NAME
        p.padding = 0
        p.window.width, p.window.height = 1100, 760
        p.window.min_width, p.window.min_height = 380, 560
        self.apply_theme()
        p.services.append(self.picker)
        p.on_keyboard_event = self.on_key
        p.add(ft.SafeArea(self.root, expand=True))
        self.render()
        p.run_task(self.ticker)
        await self.startup_sync()

    async def startup_sync(self) -> None:
        """Sync the music folder on every launch (ghost home is shown while the library is empty)."""
        self.syncing = True
        self.render()
        try:
            added, removed = await asyncio.to_thread(self.store.sync_folder)
        except Exception as exc:  # unreadable folder, etc.
            added = removed = 0
            self.toast(f"Sync failed: {exc}")
        self.syncing = False
        self.render()
        if added or removed:
            self.toast(f"Library synced: +{added} / -{removed}")
        asyncio.create_task(self.cover_job())

    async def cover_job(self) -> None:
        await asyncio.to_thread(self.store.fetch_missing_covers)
        self.render()

    def toast(self, msg: str) -> None:
        self.page.show_dialog(ft.SnackBar(ft.Text(msg), duration=2500))

    # ------------------------------------------------------------- navigation
    def on_nav(self, e) -> None:
        self.tab = ["home", "library", "playlists", "settings"][int(e.control.selected_index)]
        self.open_pl = None
        self.render()

    def go_tab(self, tab: str) -> None:
        self.tab = tab
        self.nav.selected_index = ["home", "library", "playlists", "settings"].index(tab)
        self.render()

    async def on_key(self, e: ft.KeyboardEvent) -> None:
        key = (e.key or "").lower()
        if (key == "/" or (key == "f" and e.ctrl)) and not self.player_open:
            if self.tab not in ("home", "library"):
                self.go_tab("library")
            if self.search_ref.current:
                await self.search_ref.current.focus()
        elif key == " " and self.search_ref.current is not None and not getattr(self.search_ref.current, "focused", False) and self.current:
            await self.toggle_play()
        elif key == "arrow right" and self.current:
            await self.seek(self.cur_pos() + 5)
        elif key == "arrow left" and self.current:
            await self.seek(self.cur_pos() - 5)

    # --------------------------------------------------------------- playback
    def cur_pos(self) -> float:
        pos = self.pos_base + ((time.monotonic() - self.pos_t0) if self.playing and not self.scratching else 0.0)
        return max(0.0, min(pos, self.duration or pos))

    def _set_pos(self, sec: float) -> None:
        self.pos_base, self.pos_t0 = sec, time.monotonic()

    async def play_queue(self, ids: list[str], index: int) -> None:
        self.queue, self.qi = list(ids), index
        await self.load_current(True)

    async def load_current(self, autoplay: bool) -> None:
        song = self.current
        if not song:
            return
        if not os.path.exists(song["path"]):
            self.toast(f"File missing: {song['title']}")
            return
        old = self.audio
        if old is not None:
            try:
                await old.release()
            except Exception:
                pass
            if old in self.page.services:
                self.page.services.remove(old)
        self.duration = float(song.get("duration") or 0.0)
        self._set_pos(0.0)
        self.playing = autoplay
        self.lyrics = self.store.lyrics_for(song)
        self.lyric_idx = -2
        self.audio = fta.Audio(
            src=song["path"],
            autoplay=autoplay,
            volume=float(self.store.settings.get("volume", 0.8)),
            on_position_change=self.on_audio_pos,
            on_duration_change=self.on_audio_dur,
            on_state_change=self.on_audio_state,
        )
        self.page.services.append(self.audio)
        self.store.mark_played(song["id"])
        self.page.title = f"{song['title']} - {song['artist']}"
        self.render()

    def on_audio_pos(self, e) -> None:
        if not self.scratching:
            self._set_pos(float(e.position) / 1000.0)

    def on_audio_dur(self, e) -> None:
        d = dur_seconds(e.duration)
        if d > 0:
            self.duration = d
            song = self.current
            if song and not song.get("duration"):
                song["duration"] = d

    async def on_audio_state(self, e) -> None:
        st = e.state
        if st == fta.AudioState.PLAYING:
            self.playing = True
            self._set_pos(self.pos_base)
        elif st == fta.AudioState.PAUSED:
            self.playing = False
        elif st == fta.AudioState.COMPLETED:
            self.playing = False
            if self.repeat == 2:
                await self.audio.play(0)
            else:
                await self.next(auto=True)
        self.refresh_transport()

    async def toggle_play(self) -> None:
        if not self.audio:
            ids = [s["id"] for s in self.store.ordered_songs()]
            if ids:
                await self.play_queue(ids, 0)
            return
        if self.playing:
            self._set_pos(self.cur_pos())
            self.playing = False
            await self.audio.pause()
        else:
            self._set_pos(self.pos_base)
            self.playing = True
            if self.duration and self.pos_base >= self.duration - 0.5:
                await self.audio.play(0)
                self._set_pos(0.0)
            else:
                await self.audio.resume()
        self.refresh_transport()

    async def seek(self, sec: float) -> None:
        if not self.audio:
            return
        sec = max(0.0, min(sec, max(self.duration - 0.2, 0.0) if self.duration else sec))
        self._set_pos(sec)
        await self.audio.seek(ft.Duration(milliseconds=int(sec * 1000)))
        self.lyric_idx = -2

    async def next(self, auto: bool = False) -> None:
        if not self.queue:
            return
        if self.shuffle and len(self.queue) > 1:
            self.qi = random.choice([i for i in range(len(self.queue)) if i != self.qi])
        elif self.qi + 1 < len(self.queue):
            self.qi += 1
        elif self.repeat == 1 or not auto:
            self.qi = 0
        else:
            self.playing = False
            self.refresh_transport()
            return
        await self.load_current(True)

    async def prev(self) -> None:
        if not self.queue:
            return
        if self.cur_pos() > 3 or self.qi == 0:
            await self.seek(0)
            return
        self.qi -= 1
        await self.load_current(True)

    def set_volume(self, v: float) -> None:
        self.store.settings["volume"] = v
        if self.audio:
            self.audio.volume = v
            self.audio.update()

    # -------------------------------------------------------------- ticker/UI
    async def ticker(self) -> None:
        while True:
            await asyncio.sleep(0.12)
            try:
                if self.current:
                    self.tick_ui()
            except Exception:
                pass

    def tick_ui(self) -> None:
        pos = self.cur_pos()
        frac = (pos / self.duration) if self.duration else 0.0
        if attached(getattr(self, "mini_bar", None)) and not self.player_open:
            self.mini_bar.value = min(frac, 1.0)
            self.mini_bar.update()
        if not self.player_open or not attached(getattr(self, "seek_slider", None)):
            return
        if not self.scratching and self.playing:
            self.disc_angle += 0.12 * 1.3
            self.disc.rotate = ft.Rotate(angle=self.disc_angle, alignment=ft.Alignment.CENTER)
            self.disc.update()
        if not self.seek_dragging:
            self.seek_slider.value = min(pos, self.duration or pos)
            self.seek_slider.update()
        self.time_now.value = fmt_time(pos)
        self.time_now.update()
        if self.lyrics and self.store.settings.get("lyrics", True) and self.lyrics_open:
            idx = active_index(self.lyrics, pos + 0.15)
            if idx != self.lyric_idx:
                self.lyric_idx = idx
                self.paint_lyrics()
                self.page.run_task(self.scroll_lyrics, idx)

    def refresh_transport(self) -> None:
        icon = ft.Icons.PAUSE_ROUNDED if self.playing else ft.Icons.PLAY_ARROW_ROUNDED
        for btn in (getattr(self, "mini_play", None), getattr(self, "big_play", None)):
            if attached(btn):
                btn.icon = icon
                btn.update()

    def render(self) -> None:
        self.apply_theme()
        self.render_mini()
        if self.player_open and self.current:
            self.root.content = self.build_player()
        else:
            self.player_open = False
            self.root.content = self.main_view
            builder = {"home": self.build_home, "library": self.build_library,
                       "playlists": self.build_playlists, "settings": self.build_settings}[self.tab]
            self.body.content = builder()
            self.nav.selected_index = ["home", "library", "playlists", "settings"].index(self.tab)
        self.page.update()

    # ------------------------------------------------------------- components
    def cover(self, song: dict | None, size: int, radius: int | None = None, circle: bool = False) -> ft.Control:
        r = size / 2 if circle else (radius if radius is not None else size * 0.12)
        path = song.get("cover") if song else None
        if path and os.path.exists(path):
            return ft.Image(src=path, width=size, height=size, fit=ft.BoxFit.COVER, border_radius=r)
        seed = sum(ord(c) for c in (song["title"] if song else "x"))
        hue = seed % 360
        c1, c2 = _hsl(hue, 0.65, 0.45), _hsl((hue + 40) % 360, 0.65, 0.3)
        return ft.Container(
            width=size, height=size, border_radius=r, alignment=ft.Alignment.CENTER,
            gradient=ft.LinearGradient(begin=ft.Alignment.TOP_LEFT, end=ft.Alignment.BOTTOM_RIGHT, colors=[c1, c2]),
            content=ft.Icon(ft.Icons.MUSIC_NOTE, color=ft.Colors.WHITE70, size=size * 0.45),
        )

    def song_tile(self, song: dict, ids: list[str], index: int, in_playlist: str | None = None) -> ft.Control:
        playing = bool(self.current and self.current["id"] == song["id"])
        items = [ft.PopupMenuItem(content="Add to playlist", icon=ft.Icons.PLAYLIST_ADD,
                                  on_click=lambda e, s=song: self.add_to_playlist_dialog(s))]
        if in_playlist:
            items.append(ft.PopupMenuItem(content="Remove from playlist", icon=ft.Icons.REMOVE_CIRCLE_OUTLINE,
                                          on_click=lambda e, s=song: self.remove_from_pl(in_playlist, s)))
        return ft.Container(
            ink=True, border_radius=12, padding=ft.Padding.symmetric(horizontal=8, vertical=6),
            bgcolor=tint(0.10) if playing else None,
            on_click=lambda e, i=index: self.page.run_task(self.play_queue, ids, i),
            content=ft.Row(
                [
                    self.cover(song, 48),
                    ft.Column(
                        [
                            ft.Text(song["title"], weight=ft.FontWeight.W_600, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS,
                                    color=self.accent if playing else None),
                            ft.Text(song["artist"], size=12, opacity=0.65, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                        ],
                        spacing=2, expand=True, alignment=ft.MainAxisAlignment.CENTER,
                    ),
                    ft.Text(fmt_time(song.get("duration")), size=12, opacity=0.6),
                    ft.PopupMenuButton(items=items, icon=ft.Icons.MORE_VERT),
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=12,
            ),
        )

    def song_card(self, song: dict, ids: list[str], index: int) -> ft.Control:
        return ft.Container(
            width=150, ink=True, border_radius=14, padding=8,
            on_click=lambda e, i=index: self.page.run_task(self.play_queue, ids, i),
            content=ft.Column(
                [
                    self.cover(song, 134, 12),
                    ft.Text(song["title"], weight=ft.FontWeight.W_600, size=13, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                    ft.Text(song["artist"], size=11, opacity=0.65, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                ],
                spacing=4,
            ),
        )

    def ghost_card(self) -> ft.Control:
        return ft.Container(
            width=150, padding=8,
            content=ft.Column(
                [
                    ft.Container(width=134, height=134, border_radius=12, bgcolor=tint(0.07)),
                    ft.Container(width=100, height=12, border_radius=6, bgcolor=tint(0.07)),
                    ft.Container(width=64, height=10, border_radius=6, bgcolor=tint(0.05)),
                ],
                spacing=8,
            ),
        )

    def ghost_row(self) -> ft.Control:
        return ft.Row(
            [
                ft.Container(width=48, height=48, border_radius=8, bgcolor=tint(0.07)),
                ft.Column([ft.Container(width=180, height=12, border_radius=6, bgcolor=tint(0.07)),
                           ft.Container(width=110, height=10, border_radius=6, bgcolor=tint(0.05))], spacing=6),
            ],
            spacing=12,
        )

    def search_field(self) -> ft.TextField:
        return ft.TextField(
            ref=self.search_ref, value=self.query, hint_text="Search songs or artists  ( / )",
            prefix_icon=ft.Icons.SEARCH, filled=True, dense=True,
            border=ft.OutlineInputBorder(border_radius=24, side=ft.BorderSide(0, ft.Colors.TRANSPARENT)),
            on_change=self.on_search,
        )

    def on_search(self, e) -> None:
        self.query = e.control.value or ""
        self.render_results_only()

    def filtered(self) -> list[dict]:
        q = self.query.strip().lower()
        songs = self.store.ordered_songs()
        return [s for s in songs if q in s["title"].lower() or q in s["artist"].lower()] if q else songs

    def render_results_only(self) -> None:
        if attached(getattr(self, "results", None)):
            self.results.controls = self.result_tiles()
            self.results.update()

    def result_tiles(self) -> list[ft.Control]:
        songs = self.filtered()
        ids = [s["id"] for s in songs]
        if not songs:
            return [ft.Container(padding=40, alignment=ft.Alignment.CENTER,
                                 content=ft.Text("No songs found", opacity=0.6))]
        return [self.song_tile(s, ids, i) for i, s in enumerate(songs)]

    # ------------------------------------------------------------------ pages
    def build_home(self) -> ft.Control:
        hour = time.localtime().tm_hour
        greeting = "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening"
        songs = self.store.ordered_songs()
        header = ft.Row([ft.Text(greeting, size=28, weight=ft.FontWeight.BOLD, expand=True),
                         ft.ProgressRing(width=20, height=20, stroke_width=2) if self.syncing else ft.Container()])
        if not songs:  # ghost home screen
            hint = "Syncing your music…" if self.syncing else "No music yet"
            return ft.Container(
                padding=20, expand=True,
                content=ft.Column(
                    [
                        header,
                        ft.Text(hint, size=16, opacity=0.7),
                        ft.Row([self.ghost_card() for _ in range(5)], scroll=ft.ScrollMode.HIDDEN),
                        ft.Column([self.ghost_row() for _ in range(6)], spacing=14),
                        ft.FilledButton("Choose music folder", icon=ft.Icons.FOLDER_OPEN,
                                        on_click=lambda e: self.page.run_task(self.choose_folder)),
                    ],
                    scroll=ft.ScrollMode.AUTO, spacing=18,
                ),
            )
        recent = self.store.recent_songs()[:12]
        rids = [s["id"] for s in recent]
        self.results = ft.Column(self.result_tiles(), spacing=2)
        sections: list[ft.Control] = [header, self.search_field()]
        if recent and not self.query:
            sections += [
                ft.Text("Recently played", size=18, weight=ft.FontWeight.W_600),
                ft.Row([self.song_card(s, rids, i) for i, s in enumerate(recent)], scroll=ft.ScrollMode.AUTO),
            ]
        sections += [ft.Text("All songs" if not self.query else "Results", size=18, weight=ft.FontWeight.W_600), self.results]
        return ft.Container(padding=20, expand=True, content=ft.Column(sections, scroll=ft.ScrollMode.AUTO, spacing=14))

    def build_library(self) -> ft.Control:
        n = len(self.store.songs)
        self.results = ft.Column(self.result_tiles(), spacing=2)
        return ft.Container(
            padding=20, expand=True,
            content=ft.Column(
                [
                    ft.Row([ft.Text("Library", size=28, weight=ft.FontWeight.BOLD, expand=True),
                            ft.Text(f"{n} songs", opacity=0.6)]),
                    self.search_field(),
                    ft.Column([self.results], scroll=ft.ScrollMode.AUTO, expand=True),
                ],
                spacing=14,
            ),
        )

    def build_playlists(self) -> ft.Control:
        if self.open_pl:
            pl = next((p for p in self.store.playlists if p["id"] == self.open_pl), None)
            if pl:
                songs = self.store.playlist_songs(pl)
                ids = [s["id"] for s in songs]
                return ft.Container(
                    padding=20, expand=True,
                    content=ft.Column(
                        [
                            ft.Row([
                                ft.IconButton(ft.Icons.ARROW_BACK, on_click=lambda e: self.close_pl()),
                                ft.Text(pl["name"], size=26, weight=ft.FontWeight.BOLD, expand=True),
                                ft.FilledButton("Play", icon=ft.Icons.PLAY_ARROW, disabled=not songs,
                                                on_click=lambda e: self.page.run_task(self.play_queue, ids, 0)),
                            ]),
                            ft.Column(
                                [self.song_tile(s, ids, i, in_playlist=pl["id"]) for i, s in enumerate(songs)]
                                or [ft.Text("Empty playlist - add songs from the Library (⋮ menu).", opacity=0.6)],
                                scroll=ft.ScrollMode.AUTO, expand=True, spacing=2,
                            ),
                        ],
                        spacing=12,
                    ),
                )
            self.open_pl = None
        tiles = []
        for pl in self.store.playlists:
            tiles.append(ft.Container(
                ink=True, border_radius=12, padding=12, bgcolor=tint(0.06),
                on_click=lambda e, i=pl["id"]: self.open_playlist(i),
                content=ft.Row([
                    ft.Icon(ft.Icons.QUEUE_MUSIC, color=self.accent),
                    ft.Column([ft.Text(pl["name"], weight=ft.FontWeight.W_600),
                               ft.Text(f"{len(pl['songs'])} songs", size=12, opacity=0.6)], spacing=2, expand=True),
                    ft.IconButton(ft.Icons.DELETE_OUTLINE, tooltip="Delete playlist",
                                  on_click=lambda e, i=pl["id"]: self.delete_pl(i)),
                ]),
            ))
        return ft.Container(
            padding=20, expand=True,
            content=ft.Column(
                [
                    ft.Row([ft.Text("Playlists", size=28, weight=ft.FontWeight.BOLD, expand=True),
                            ft.FilledButton("New", icon=ft.Icons.ADD, on_click=lambda e: self.new_playlist_dialog())]),
                    ft.Column(tiles or [ft.Text("No playlists yet. Tap New to create one.", opacity=0.6)],
                              scroll=ft.ScrollMode.AUTO, expand=True, spacing=8),
                ],
                spacing=14,
            ),
        )

    def build_settings(self) -> ft.Control:
        s = self.store.settings
        swatches = [
            ft.Container(
                width=36, height=36, border_radius=18, bgcolor=c, ink=True,
                border=ft.Border.all(3, ft.Colors.ON_SURFACE) if c == self.accent else None,
                on_click=lambda e, c=c: self.set_setting("accent", c),
            ) for c in ACCENTS
        ]
        return ft.Container(
            padding=20, expand=True,
            content=ft.Column(
                [
                    ft.Text("Settings", size=28, weight=ft.FontWeight.BOLD),
                    ft.Text("Music folder", weight=ft.FontWeight.W_600),
                    ft.Text(s.get("folder") or "Not set", opacity=0.7, selectable=True),
                    ft.Row([
                        ft.FilledButton("Choose folder", icon=ft.Icons.FOLDER_OPEN,
                                        on_click=lambda e: self.page.run_task(self.choose_folder)),
                        ft.OutlinedButton("Add files", icon=ft.Icons.AUDIO_FILE,
                                          on_click=lambda e: self.page.run_task(self.add_files)),
                        ft.OutlinedButton("Sync now", icon=ft.Icons.SYNC,
                                          on_click=lambda e: self.page.run_task(self.startup_sync)),
                    ], wrap=True),
                    ft.Divider(),
                    ft.Switch(label="Dark theme", value=s.get("theme") == "dark",
                              on_change=lambda e: self.set_setting("theme", "dark" if e.control.value else "light")),
                    ft.Switch(label="Show lyrics (from .lrc file or embedded tags)", value=bool(s.get("lyrics", True)),
                              on_change=lambda e: self.set_setting("lyrics", bool(e.control.value))),
                    ft.Text("Accent colour", weight=ft.FontWeight.W_600),
                    ft.Row(swatches),
                    ft.Divider(),
                    ft.Text(f"{APP_NAME} · {len(self.store.songs)} songs · formats: {', '.join(sorted(AUDIO_EXTS))}",
                            size=12, opacity=0.55),
                ],
                scroll=ft.ScrollMode.AUTO, spacing=12,
            ),
        )

    # --------------------------------------------------------------- actions
    def set_setting(self, key: str, value) -> None:
        self.store.settings[key] = value
        self.store.save()
        self.render()

    async def choose_folder(self) -> None:
        path = await self.picker.get_directory_path(dialog_title="Choose your music folder")
        if path:
            self.store.settings["folder"] = path
            self.store.save()
            await self.startup_sync()

    async def add_files(self) -> None:
        files = await self.picker.pick_files(
            dialog_title="Add songs", allow_multiple=True, file_type=ft.FilePickerFileType.CUSTOM,
            allowed_extensions=[e.lstrip(".") for e in AUDIO_EXTS],
        )
        paths = [f.path for f in files if f.path]
        if paths:
            n = await asyncio.to_thread(self.store.add_files, paths)
            self.toast(f"Added {n} songs")
            self.render()

    def open_playlist(self, pid: str) -> None:
        self.open_pl = pid
        self.render()

    def close_pl(self) -> None:
        self.open_pl = None
        self.render()

    def delete_pl(self, pid: str) -> None:
        self.store.delete_playlist(pid)
        self.render()

    def remove_from_pl(self, pid: str, song: dict) -> None:
        self.store.remove_from_playlist(pid, song["id"])
        self.render()

    def new_playlist_dialog(self, then_add: dict | None = None) -> None:
        field = ft.TextField(label="Playlist name", autofocus=True)

        def create(e=None) -> None:
            name = (field.value or "").strip()
            if not name:
                return
            pl = self.store.create_playlist(name)
            if then_add:
                self.store.add_to_playlist(pl["id"], then_add["id"])
            self.page.pop_dialog()
            self.toast(f"Created “{name}”")
            self.render()

        field.on_submit = create
        self.page.show_dialog(ft.AlertDialog(
            title=ft.Text("New playlist"), content=field,
            actions=[ft.TextButton("Cancel", on_click=lambda e: self.page.pop_dialog()),
                     ft.FilledButton("Create", on_click=create)],
        ))

    def add_to_playlist_dialog(self, song: dict) -> None:
        def add(pid: str, name: str) -> None:
            ok = self.store.add_to_playlist(pid, song["id"])
            self.page.pop_dialog()
            self.toast(f"Added to “{name}”" if ok else "Already in that playlist")
            self.render()

        rows = [ft.ListTile(title=ft.Text(p["name"]), leading=ft.Icon(ft.Icons.QUEUE_MUSIC),
                            on_click=lambda e, p=p: add(p["id"], p["name"])) for p in self.store.playlists]
        rows.append(ft.ListTile(title=ft.Text("New playlist…"), leading=ft.Icon(ft.Icons.ADD),
                                on_click=lambda e: (self.page.pop_dialog(), self.new_playlist_dialog(song))))
        self.page.show_dialog(ft.AlertDialog(
            title=ft.Text("Add to playlist"),
            content=ft.Container(width=320, content=ft.Column(rows, tight=True, scroll=ft.ScrollMode.AUTO)),
            actions=[ft.TextButton("Close", on_click=lambda e: self.page.pop_dialog())],
        ))

    # ------------------------------------------------------------ mini player
    def render_mini(self) -> None:
        song = self.current
        if not song:
            self.mini.content = None
            self.mini.visible = False
            return
        self.mini.visible = True
        self.mini_bar = ft.ProgressBar(value=0, height=2, color=self.accent, bgcolor=tint(0.1))
        self.mini_play = ft.IconButton(ft.Icons.PAUSE_ROUNDED if self.playing else ft.Icons.PLAY_ARROW_ROUNDED,
                                       icon_size=30, on_click=lambda e: self.page.run_task(self.toggle_play))
        self.mini.content = ft.Container(
            bgcolor=tint(0.07), ink=True, on_click=lambda e: self.open_player(),
            content=ft.Column([
                self.mini_bar,
                ft.Container(padding=ft.Padding.symmetric(horizontal=12, vertical=6), content=ft.Row([
                    self.cover(song, 44),
                    ft.Column([ft.Text(song["title"], weight=ft.FontWeight.W_600, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                               ft.Text(song["artist"], size=12, opacity=0.65, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)],
                              spacing=2, expand=True),
                    ft.IconButton(ft.Icons.SKIP_PREVIOUS_ROUNDED, on_click=lambda e: self.page.run_task(self.prev)),
                    self.mini_play,
                    ft.IconButton(ft.Icons.SKIP_NEXT_ROUNDED, on_click=lambda e: self.page.run_task(self.next)),
                ], vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=8)),
            ], spacing=0),
        )

    def open_player(self) -> None:
        self.player_open = True
        self.lyrics_open = False
        self.lyric_idx = -2
        self.render()

    def close_player(self) -> None:
        self.player_open = False
        self.render()

    # ---------------------------------------------------------- full player
    seek_dragging = False
    lyrics_open = False

    def build_player(self) -> ft.Control:
        song = self.current
        size = 300
        # vinyl: black disc, grooves, cover label, spindle hole
        vinyl = ft.Stack(
            [
                ft.Container(width=size, height=size, border_radius=size / 2,
                             gradient=ft.RadialGradient(colors=["#2a2a2a", "#0b0b0b", "#1c1c1c", "#0b0b0b"], stops=[0.0, 0.55, 0.8, 1.0]),
                             shadow=ft.BoxShadow(blur_radius=30, color=ft.Colors.with_opacity(0.45, ft.Colors.BLACK))),
                ft.Container(width=size, height=size, alignment=ft.Alignment.CENTER,
                             content=self.cover(song, size * 0.55, circle=True)),
                ft.Container(width=size, height=size, alignment=ft.Alignment.CENTER,
                             content=ft.Container(width=14, height=14, border_radius=7, bgcolor="#111111",
                                                  border=ft.Border.all(2, ft.Colors.with_opacity(0.5, ft.Colors.WHITE)))),
            ],
            width=size, height=size,
        )
        self.disc = ft.Container(
            content=vinyl, width=size, height=size,
            rotate=ft.Rotate(angle=self.disc_angle, alignment=ft.Alignment.CENTER),
            animate_rotation=ft.Animation(120, ft.AnimationCurve.LINEAR),
        )
        scratch = self.scratch_detector()
        self.seek_slider = ft.Slider(min=0, max=max(self.duration, 1.0), value=min(self.cur_pos(), max(self.duration, 1.0)),
                                     active_color=self.accent, expand=True,
                                     on_change_start=self.seek_start, on_change=self.seek_drag, on_change_end=self.seek_end)
        self.time_now = ft.Text(fmt_time(self.cur_pos()), size=12, opacity=0.7)
        self.big_play = ft.IconButton(ft.Icons.PAUSE_ROUNDED if self.playing else ft.Icons.PLAY_ARROW_ROUNDED,
                                      icon_size=44, icon_color=ft.Colors.WHITE, bgcolor=self.accent,
                                      on_click=lambda e: self.page.run_task(self.toggle_play))
        controls = ft.Row(
            [
                ft.IconButton(ft.Icons.SHUFFLE, icon_color=self.accent if self.shuffle else None, on_click=self.toggle_shuffle),
                ft.IconButton(ft.Icons.SKIP_PREVIOUS_ROUNDED, icon_size=36, on_click=lambda e: self.page.run_task(self.prev)),
                self.big_play,
                ft.IconButton(ft.Icons.SKIP_NEXT_ROUNDED, icon_size=36, on_click=lambda e: self.page.run_task(self.next)),
                ft.IconButton([ft.Icons.REPEAT, ft.Icons.REPEAT, ft.Icons.REPEAT_ONE][self.repeat],
                              icon_color=self.accent if self.repeat else None, on_click=self.toggle_repeat),
            ],
            alignment=ft.MainAxisAlignment.CENTER, spacing=8,
        )
        volume = ft.Row([
            ft.Icon(ft.Icons.VOLUME_DOWN, size=20, opacity=0.7),
            ft.Slider(min=0, max=1, value=float(self.store.settings.get("volume", 0.8)), active_color=self.accent, expand=True,
                      on_change=lambda e: self.set_volume(float(e.control.value))),
            ft.Icon(ft.Icons.VOLUME_UP, size=20, opacity=0.7),
        ])
        has_lyrics = bool(self.lyrics) and self.store.settings.get("lyrics", True)
        self.lyrics_view = ft.ListView(spacing=0, expand=True, padding=ft.Padding.symmetric(vertical=60))
        self.lyrics_open = False
        self.lyrics_btn = ft.TextButton("Lyrics", icon=ft.Icons.LYRICS, disabled=not has_lyrics,
                                        on_click=self.toggle_lyrics)
        header = ft.Row([
            ft.IconButton(ft.Icons.KEYBOARD_ARROW_DOWN, icon_size=32, tooltip="Minimize", on_click=lambda e: self.close_player()),
            ft.Text("Now playing", expand=True, text_align=ft.TextAlign.CENTER, opacity=0.7),
            self.lyrics_btn,
        ])
        self.player_stage = ft.Container(content=scratch, alignment=ft.Alignment.CENTER, expand=True)
        return ft.Container(
            padding=ft.Padding.symmetric(horizontal=20, vertical=8), expand=True,
            gradient=ft.LinearGradient(begin=ft.Alignment.TOP_CENTER, end=ft.Alignment.BOTTOM_CENTER,
                                       colors=[ft.Colors.with_opacity(0.28, self.accent), ft.Colors.TRANSPARENT]),
            content=ft.Column([
                header,
                self.player_stage,
                ft.Text("Drag the record to scratch", size=11, opacity=0.5, text_align=ft.TextAlign.CENTER),
                ft.Column([
                    ft.Text(song["title"], size=22, weight=ft.FontWeight.BOLD, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS,
                            text_align=ft.TextAlign.CENTER),
                    ft.Text(song["artist"], size=14, opacity=0.7, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS,
                            text_align=ft.TextAlign.CENTER),
                ], spacing=2, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
                ft.Row([self.time_now, self.seek_slider, ft.Text(fmt_time(self.duration), size=12, opacity=0.7)]),
                controls,
                volume,
            ], spacing=8, horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
        )

    def scratch_detector(self) -> ft.GestureDetector:
        return ft.GestureDetector(
            content=self.disc, drag_interval=40, mouse_cursor=ft.MouseCursor.GRAB,
            on_pan_start=self.scratch_start, on_pan_update=self.scratch_update, on_pan_end=self.scratch_end,
        )

    def toggle_shuffle(self, e) -> None:
        self.shuffle = not self.shuffle
        self.render()

    def toggle_repeat(self, e) -> None:
        self.repeat = (self.repeat + 1) % 3
        self.render()

    # -- lyrics
    def toggle_lyrics(self, e) -> None:
        self.lyrics_open = not self.lyrics_open
        self.lyric_idx = -2
        if self.lyrics_open:
            self.lyrics_view.controls = [
                ft.Container(height=LYRIC_ROW_H, alignment=ft.Alignment.CENTER,
                             content=ft.Text(l["text"], text_align=ft.TextAlign.CENTER, max_lines=2,
                                             overflow=ft.TextOverflow.ELLIPSIS, size=18)) for l in self.lyrics
            ]
            self.player_stage.content = self.lyrics_view
            self.lyrics_btn.icon = ft.Icons.ALBUM
            self.lyrics_btn.text = "Disc"
        else:
            self.player_stage.content = self.scratch_detector()
            self.lyrics_btn.icon = ft.Icons.LYRICS
            self.lyrics_btn.text = "Lyrics"
        self.page.update()

    def paint_lyrics(self) -> None:
        for i, row in enumerate(self.lyrics_view.controls):
            t: ft.Text = row.content
            active = i == self.lyric_idx
            t.color = self.accent if active else None
            t.opacity = 1.0 if active else 0.45
            t.weight = ft.FontWeight.BOLD if active else ft.FontWeight.NORMAL
        self.lyrics_view.update()

    async def scroll_lyrics(self, idx: int) -> None:
        if idx >= 0 and attached(self.lyrics_view):
            await self.lyrics_view.scroll_to(offset=max(0, idx * LYRIC_ROW_H - 100), duration=300)

    # -- seek slider
    def seek_start(self, e) -> None:
        self.seek_dragging = True

    def seek_drag(self, e) -> None:
        self.time_now.value = fmt_time(float(e.control.value))
        self.time_now.update()

    async def seek_end(self, e) -> None:
        self.seek_dragging = False
        await self.seek(float(e.control.value))

    # -- scratching (drag the record: angle change -> seek)
    def _angle(self, pos) -> float:
        c = 150.0
        return math.atan2(pos.y - c, pos.x - c)

    async def scratch_start(self, e) -> None:
        self.scratching = True
        self._scratch_resume = self.playing
        self._set_pos(self.cur_pos())
        self._scratch_last_angle = self._angle(e.local_position)
        if self.playing and self.audio:
            await self.audio.pause()

    async def scratch_update(self, e) -> None:
        if not self.scratching:
            return
        a = self._angle(e.local_position)
        d = a - self._scratch_last_angle
        if d > math.pi:
            d -= 2 * math.pi
        elif d < -math.pi:
            d += 2 * math.pi
        self._scratch_last_angle = a
        self.disc_angle += d
        self.disc.rotate = ft.Rotate(angle=self.disc_angle, alignment=ft.Alignment.CENTER)
        self.disc.update()
        self.pos_base = max(0.0, min(self.pos_base + d / (2 * math.pi) * SCRATCH_SEC_PER_REV, self.duration or 1e9))
        now = time.monotonic()
        if self.audio and now - self._scratch_last_seek > 0.09:
            self._scratch_last_seek = now
            await self.audio.seek(ft.Duration(milliseconds=int(self.pos_base * 1000)))
            if self._scratch_resume:
                await self.audio.resume()  # let a short slice play so the scratch is audible

    async def scratch_end(self, e) -> None:
        self.scratching = False
        self.pos_t0 = time.monotonic()
        if self.audio:
            await self.audio.seek(ft.Duration(milliseconds=int(self.pos_base * 1000)))
            if self._scratch_resume:
                await self.audio.resume()
                self.playing = True
            else:
                await self.audio.pause()
        self.lyric_idx = -2


def _hsl(h: float, s: float, l: float) -> str:
    import colorsys
    r, g, b = colorsys.hls_to_rgb(h / 360.0, l, s)
    return f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"


async def main(page: ft.Page) -> None:
    await PlayerApp(page).start()


if __name__ == "__main__":
    ft.run(main)
