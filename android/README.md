# MP3 Player for Android 📱

A self-contained native Android project hosting the interactive DJ turntable music player with background audio playback, system notifications, and lock-screen media controls.

---

## 🎧 Key Features on Android

- **Background Audio Service**:
  - `PlaybackService.java` runs as a high-priority Android Foreground Service so music plays continuously when the screen is turned off or when switching apps.
- **System Media Notifications & Lock Screen**:
  - `MediaSessionCompat` integration displays rich lock-screen controls, live track progress, artist/title metadata, and high-resolution album artwork.
- **Audio Focus**:
  - Automatically handles ducking and pausing during incoming phone calls, alarms, or navigation prompts.
- **Storage Access Framework (SAF)**:
  - Allows granting access to any music folder on device storage or SD card.
- **Self-Contained UI**:
  - Bundled in `assets/index.html`, running inside a hardware-accelerated Android WebView with Web Audio API support.

---

## 📂 Folder Structure

```text
android/
├── AndroidManifest.xml       # App permissions, activities, and background service
├── build_apk.ps1             # Automated compilation and signing script
├── release.keystore          # Keystore for release APK signing
├── README.md                 # This documentation
├── assets/                   # Self-contained web player assets
│   ├── index.html            # Core player UI (HTML5 + CSS3 + Web Audio)
│   └── desktop-bridge.js
├── res/                      # Android resources
│   ├── drawable/             # App icons (ic_launcher.xml)
│   └── values/               # App name and string resources
└── src/                      # Native Java source code
    └── com/musics/mp3player/
        ├── MainActivity.java     # WebView container and JavaScript interface
        └── PlaybackService.java  # Foreground service & MediaSession handlers
```

---

## 🛠️ Building the APK

Run the automated compilation script from this directory:

```powershell
.\build_apk.ps1
```

The script will:
1. Compile resources using Android SDK `aapt2`.
2. Compile Java source code using `javac`.
3. Convert bytecode to Dalvik executable `classes.dex` using `d8`.
4. Package and zipalign the APK using `zipalign`.
5. Cryptographically sign the APK with v1 + v2 + v3 schemes using `apksigner`.

**Output:** `android/MP3-Player.apk`

---

## 📲 Installing on Your Phone

### Option 1: Via ADB (USB Debugging)
```bash
adb install -r MP3-Player.apk
```

### Option 2: Direct Transfer
1. Send `MP3-Player.apk` to your phone (via Discord, USB cable, Google Drive, or Bluetooth).
2. Tap the file in your phone's file manager and select **Install** (enable "Install unknown apps" if prompted).

---

## 📄 License
MIT License. Free to use and modify.
