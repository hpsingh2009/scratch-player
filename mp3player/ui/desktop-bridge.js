/*
 * desktop-bridge.js
 * -----------------
 * Active only when the player runs inside the Python desktop shell (app.py), which opens
 * index.html?desktop=1#token=... . It connects the player UI to Python for:
 *   1. Native OS folder & file dialogs (window.pywebview.api.pick_folder / pick_save_file)
 *   2. Cross-platform File System Access API polyfill (/ __bridge / list | stat | file)
 *   3. Persistent library & settings storage in Python (/ __bridge / library | settings)
 *   4. Synced lyrics fetching & .lrc disk saving in Python (/ __bridge / lyrics)
 *   5. Audio metadata & cover extraction in Python (/ __bridge / metadata)
 *
 * Works identically on Windows (WebView2), macOS (WKWebView) and Linux (WebKitGTK / Qt).
 * In a normal web browser this file is a no-op.
 */
(function () {
  'use strict';

  const params = new URLSearchParams(location.search);
  if (params.get('desktop') !== '1') return;

  // Token arrives in the URL hash; keep it for reloads, then strip it from the address.
  const hashToken = new URLSearchParams(location.hash.slice(1)).get('token');
  if (hashToken) {
    try { sessionStorage.setItem('mp3_bridge_token', hashToken); } catch (_) {}
    history.replaceState(null, '', location.pathname + location.search);
  }
  let TOKEN = hashToken;
  try { TOKEN = TOKEN || sessionStorage.getItem('mp3_bridge_token'); } catch (_) {}
  if (!TOKEN) {
    console.warn('[desktop-bridge] No bridge token; native folder access disabled.');
    return;
  }

  /* ---------- pywebview API readiness ---------- */
  function waitForApi(timeoutMs = 8000) {
    if (window.pywebview && window.pywebview.api) return Promise.resolve(window.pywebview.api);
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new DOMException('Desktop API not ready', 'InvalidStateError')), timeoutMs);
      window.addEventListener('pywebviewready', () => {
        clearTimeout(timer);
        resolve(window.pywebview.api);
      }, { once: true });
    });
  }

  /* ---------- Bridge HTTP helpers ---------- */
  function bridgeUrl(route, queryObj = {}) {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(queryObj)) {
      if (v != null && v !== '') q.set(k, v);
    }
    const qs = q.toString();
    return `/__bridge/${route}${qs ? '?' + qs : ''}`;
  }

  async function bridgeFetch(route, queryObj = {}, init = {}) {
    const res = await fetch(bridgeUrl(route, queryObj), {
      ...init,
      headers: { ...(init.headers || {}), 'X-Bridge-Token': TOKEN },
      cache: 'no-store'
    });
    if (res.ok) return res;
    let message = `HTTP ${res.status}`;
    try { message = (await res.json()).error || message; } catch (_) {}
    if (res.status === 404) throw new DOMException(message, 'NotFoundError');
    if (res.status === 403) throw new DOMException(message, 'NotAllowedError');
    if (res.status === 400) throw new DOMException(message, 'TypeError');
    throw new DOMException(message, 'InvalidStateError');
  }

  /* ---------- Writable stream (buffers, then writes atomically on close) ---------- */
  class DesktopWritable {
    constructor(fileHandle) {
      this._handle = fileHandle;
      this._chunks = [];
      this._closed = false;
    }
    async write(data) {
      if (this._closed) throw new DOMException('Stream closed', 'InvalidStateError');
      if (data && typeof data === 'object' && 'type' in data && !(data instanceof Blob)) {
        if (data.type === 'truncate') { this._chunks = []; return; }
        if (data.type !== 'write') throw new DOMException('Only sequential writes are supported', 'NotSupportedError');
        data = data.data;
      }
      this._chunks.push(data instanceof Blob ? data : new Blob([data]));
    }
    async truncate(size) {
      if (size === 0) this._chunks = [];
      else throw new DOMException('Partial truncate not supported', 'NotSupportedError');
    }
    async close() {
      if (this._closed) return;
      this._closed = true;
      const body = new Blob(this._chunks);
      this._chunks = [];
      await bridgeFetch('file', { dir: this._handle.__desktopDir, name: this._handle.name }, { method: 'PUT', body });
    }
    async abort() {
      this._closed = true;
      this._chunks = [];
    }
  }

  /* ---------- Handles (own enumerable props only so IndexedDB can store them) ---------- */
  class DesktopFileHandle {
    constructor(dir, name) {
      this.kind = 'file';
      this.name = name;
      this.__desktopDir = dir;
    }
    async getFile() {
      const res = await bridgeFetch('file', { dir: this.__desktopDir, name: this.name });
      const blob = await res.blob();
      const lastModified = Number(res.headers.get('X-Last-Modified')) || Date.now();
      return new File([blob], this.name, { type: blob.type || res.headers.get('Content-Type') || '', lastModified });
    }
    async createWritable() { return new DesktopWritable(this); }
    async queryPermission() { return 'granted'; }
    async requestPermission() { return 'granted'; }
    async isSameEntry(other) {
      return !!other && other.kind === 'file' && other.name === this.name && other.__desktopDir === this.__desktopDir;
    }
  }

  class DesktopDirectoryHandle {
    constructor(dir, name) {
      this.kind = 'directory';
      this.name = name;
      this.__desktopDir = dir;
    }
    async queryPermission() { return 'granted'; }
    async requestPermission() { return 'granted'; }
    async isSameEntry(other) {
      return !!other && other.kind === 'directory' && other.__desktopDir === this.__desktopDir;
    }
    async getFileHandle(name, options = {}) {
      try {
        const res = await bridgeFetch('stat', { dir: this.__desktopDir, name });
        const info = await res.json();
        if (info.kind !== 'file') throw new DOMException(`"${name}" is a folder`, 'TypeMismatchError');
      } catch (err) {
        if (!(err && err.name === 'NotFoundError' && options.create)) throw err;
        // Created lazily: the file appears on disk when its writable is closed.
      }
      return new DesktopFileHandle(this.__desktopDir, name);
    }
    async getDirectoryHandle() {
      throw new DOMException('Sub-folders are not supported in the desktop bridge', 'NotSupportedError');
    }
    async removeEntry(name) {
      await bridgeFetch('file', { dir: this.__desktopDir, name }, { method: 'DELETE' });
    }
    async *entries() {
      const res = await bridgeFetch('list', { dir: this.__desktopDir });
      const list = await res.json();
      for (const item of list) {
        if (item.kind !== 'file') continue;
        yield [item.name, new DesktopFileHandle(this.__desktopDir, item.name)];
      }
    }
    async *keys() { for await (const [name] of this.entries()) yield name; }
    async *values() { for await (const [, handle] of this.entries()) yield handle; }
    [Symbol.asyncIterator]() { return this.entries(); }
  }

  /* ---------- Pickers ---------- */
  async function showDirectoryPicker() {
    const api = await waitForApi();
    const picked = await api.pick_folder();
    if (!picked) throw new DOMException('The user aborted a request.', 'AbortError');
    return new DesktopDirectoryHandle(picked.dir, picked.name);
  }

  async function showSaveFilePicker(options = {}) {
    const api = await waitForApi();
    const picked = await api.pick_save_file(options.suggestedName || 'song.mp3');
    if (!picked) throw new DOMException('The user aborted a request.', 'AbortError');
    return new DesktopFileHandle(picked.dir, picked.name);
  }

  window.showDirectoryPicker = showDirectoryPicker;
  window.showSaveFilePicker = showSaveFilePicker;

  /* ---------- Public Python-backed helpers used by index.html ---------- */
  window.__desktopBridge = {
    isDesktop: true,
    reviveHandle(obj) {
      if (!obj || typeof obj !== 'object' || !obj.__desktopDir) return obj || null;
      return obj.kind === 'file'
        ? new DesktopFileHandle(obj.__desktopDir, obj.name)
        : new DesktopDirectoryHandle(obj.__desktopDir, obj.name);
    },
    async fetchLyrics(artist, title, duration = 180, saveDir = '', fileName = '') {
      try {
        const res = await bridgeFetch('lyrics', {
          artist: artist || '',
          title: title || '',
          duration: String(duration || 180),
          dir: saveDir || '',
          name: fileName || ''
        });
        return await res.json();
      } catch (_) {
        return { found: false, rawLrc: null, lyrics: [] };
      }
    },
    async fetchMetadata(artist, title, fileName = '', saveDir = '') {
      try {
        const res = await bridgeFetch('metadata', {
          artist: artist || '',
          title: title || '',
          name: fileName || '',
          dir: saveDir || ''
        });
        return await res.json();
      } catch (_) {
        return null;
      }
    },
    async saveLibrary(snapshot) {
      try {
        await bridgeFetch('library', {}, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(snapshot)
        });
      } catch (_) {}
    },
    async loadLibrary() {
      try {
        const res = await bridgeFetch('library');
        return await res.json();
      } catch (_) {
        return null;
      }
    },
    async saveSettings(settings) {
      try {
        await bridgeFetch('settings', {}, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(settings)
        });
      } catch (_) {}
    },
    async loadSettings() {
      try {
        const res = await bridgeFetch('settings');
        return await res.json();
      } catch (_) {
        return null;
      }
    }
  };

  document.documentElement.classList.add('desktop-app');
})();
