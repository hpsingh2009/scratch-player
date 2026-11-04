# Modern MP3 Player 🎵

A feature-rich, responsive music player desktop application written in **Python**. It features an interactive DJ vinyl record turntable with real-time audio scratching, synchronized lyrics parsing, automated folder synchronization, and embedded metadata extraction.

---

## 🎧 Key Features

- **Interactive DJ Vinyl Turntable**:
  - Real-time vinyl scratching powered by the Web Audio API with rotational angle tracking.
  - Full touch and drag support on mobile devices with downward swipe-to-minimize disambiguation.
- **Synchronized Lyrics**:
  - Displays timed LRC lyrics with smooth auto-scroll.
  - Automatically loads `.lrc` files from the music folder or embedded ID3v2 / M4A metadata.
- **Embedded Metadata & Cover Art**:
  - Tag extractors for ID3v2 (`TIT2`, `TPE1`, `APIC`) and MP4/M4A (`©nam`, `©ART`, `covr`).
  - Automatic bidirectional Title/Artist disambiguation.
- **Configurable Music Folder & Sync**:
  - Seamlessly links to your local music directory (`C:\Users\<User>\Music` or Android Storage Access Framework).
  - Automatically syncs newly added or removed songs with a single click.
- **Desktop Architecture**:
  - Clean, modular Python package (`mp3player`) with native window management via `pywebview`.
  - Native file/folder pickers, atomic file saving, and background metadata caching.

---

## 📂 Project Structure

```text
mp3-player/
├── main.py                 # Primary Python application entry point
├── requirements.txt        # Python dependencies (pywebview, mutagen)
├── mp3player/              # Core Python package
│   ├── __init__.py         # Package metadata & constants
│   ├── app.py              # Window lifecycle & desktop API
│   ├── server.py           # Local bridge HTTP server & RPC routes
│   ├── storage.py          # Settings & library JSON persistence
│   ├── metadata.py         # Tag extractor & artist/title resolver
│   ├── lyrics.py           # LRC lyrics parser & disk cache
│   └── ui/                 # Embedded original GUI assets
│       ├── index.html      # Original HTML5 / CSS3 / Web Audio Turntable UI
│       └── desktop-bridge.js
└── android/                # Native Android companion build
    ├── src/                # Android Java background service & notifications
    ├── build_apk.ps1       # Automated APK compilation script
    └── README.md
```

---

## 🚀 Running the Project

Ensure Python 3.10+ is installed:
```bash
pip install -r requirements.txt
python main.py
```

---

## 📄 License
MIT License. Free to use and modify.
