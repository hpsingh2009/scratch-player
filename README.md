# MP3 Player (100% Python)

A music player written entirely in Python with [Flet](https://flet.dev) — no HTML, CSS or JavaScript.

## Features
- Folder sync on every startup, plus a ghost (skeleton) home screen while the library loads/is empty
- Home with recently played, library with search (`/` or `Ctrl+F`), playlists
- Embedded tags & cover art (mutagen), optional online cover lookup (iTunes)
- Lyrics from a sibling `.lrc` file or embedded in the tags (read-only, nothing is downloaded)
- Vinyl disc player — **drag the record to scratch/scrub**, seek bar, volume, shuffle, repeat
- Dark/light theme and accent colours
- Keyboard: `Space` play/pause, `←/→` seek ±5 s

## Project layout
```
main.py              Flet UI + playback
mp3player/
  storage.py         settings, library, playlists, folder sync
  tags.py            ID3 / MP4 / FLAC / Ogg tag + cover + lyrics reader
  naming.py          filename -> artist/title heuristics
  lrc.py             LRC parser
  online.py          optional cover lookup
pyproject.toml       dependencies + `flet build` config
```

## Run
```bash
pip install -r requirements.txt
python main.py
```

## Build
```bash
flet build windows   # Windows desktop app  (build/windows)
flet build apk       # Android APK          (needs Flutter + Android SDK, flet docs: https://flet.dev/docs/publish)
```

## Notes
- Scratching is implemented as drag-to-seek (the audio engine cannot reverse/pitch-bend in real time).
- On Android the default music folder is `/storage/emulated/0/Music`; grant audio permission when asked.

MIT License.
