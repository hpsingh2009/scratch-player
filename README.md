# Modern MP3 Player 🎵

A feature-rich, responsive music player designed for Desktop, Web, and Android. It features an interactive DJ vinyl record turntable with real-time audio scratching, synchronized lyrics parsing, automated folder synchronization, and embedded metadata extraction.

---

## 🌟 Key Features

- **Interactive DJ Vinyl Turntable**:
  - Real-time vinyl scratching powered by the Web Audio API with rotational angle tracking.
  - Full touch and drag support on mobile devices with downward swipe-to-minimize disambiguation.
- **Synchronized Lyrics**:
  - Displays timed LRC lyrics with smooth auto-scroll.
  - Automatically loads `.lrc` files from the music folder or embedded ID3v2 / M4A metadata.
- **Embedded Metadata & Cover Art**:
  - Pure JavaScript and Python tag extractors for ID3v2 (`TIT2`, `TPE1`, `APIC`) and MP4/M4A (`©nam`, `©ART`, `covr`).
  - Automatic bidirectional Title/Artist disambiguation.
- **Configurable Music Folder & Sync**:
  - Seamlessly links to your local music directory (`C:\Users\<User>\Music` or Android Storage Access Framework).
  - Automatically syncs newly added or removed songs with a single click.
- **System Media Controls**:
  - MediaSession API integration for Windows volume flyouts, Android lock screen, and system notification controls.
- **Cross-Platform**:
  - **Desktop**: Standalone executable using lightweight Python WebView.
  - **Android**: Native APK with background `PlaybackService` and MediaStyle notifications.
  - **Web**: Progressive standalone responsive web app.

---

## 📂 Project Structure

```text
mp3-player/
├── index.html            # Core HTML5, CSS3 & JavaScript application
├── app.py                # Python desktop bridge (pywebview + metadata server)
├── desktop-bridge.js     # Desktop File System Access & RPC polyfill
├── MP3-Player.apk        # Compiled & signed Android package
├── android/              # Native Android project source & build scripts
│   ├── src/              # Android Java source (MainActivity & PlaybackService)
│   ├── assets/           # Bundled web assets
│   └── build_apk.ps1     # Automated APK compilation script
└── README.md
```

---

## 🚀 Running the Project

### 1. Web Version
Open `index.html` directly in any modern browser (Chrome, Edge, Firefox, Safari).

### 2. Desktop App (Python WebView)
Ensure Python 3.10+ is installed with `pywebview`:
```bash
pip install pywebview
python app.py
```

### 3. Android APK
Install `MP3-Player.apk` onto your Android device:
```bash
adb install -r MP3-Player.apk
```
Or transfer `MP3-Player.apk` to your phone and tap to install.

---

## 📜 License
MIT License. Free to use and modify.
