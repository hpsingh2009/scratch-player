package com.musics.mp3player;

import android.Manifest;
import android.app.Activity;
import android.content.ClipData;
import android.content.ContentResolver;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.database.Cursor;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.provider.DocumentsContract;
import android.util.Base64;
import android.view.KeyEvent;
import android.view.View;
import android.view.Window;
import android.view.WindowManager;
import android.webkit.JavascriptInterface;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayInputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.lang.ref.WeakReference;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Map;

public class MainActivity extends Activity {
    private static final int FILE_CHOOSER_RESULT_CODE = 1001;
    private static final int FOLDER_PICKER_RESULT_CODE = 1002;
    private static final int NOTIF_PERMISSION_CODE = 1003;
    private static final String PREFS = "mp3_player_prefs";
    private static final String LOCAL_HOST = "mp3player.local";

    private static WeakReference<MainActivity> currentInstance = new WeakReference<>(null);

    private WebView webView;
    private ValueCallback<Uri[]> filePathCallback;

    /** WebView that never reports itself hidden, so Chromium keeps <audio> playing on lock screen / background. */
    public static class BackgroundAudioWebView extends WebView {
        public BackgroundAudioWebView(Context context) {
            super(context);
        }

        @Override
        protected void onWindowVisibilityChanged(int visibility) {
            if (visibility != View.VISIBLE) {
                super.onWindowVisibilityChanged(View.VISIBLE);
            } else {
                super.onWindowVisibilityChanged(visibility);
            }
        }
    }

    /** Called by PlaybackService (notification buttons, lock screen, headset, Bluetooth) */
    public static void dispatchMediaCommand(final String action, final long seekMs) {
        final MainActivity act = currentInstance.get();
        if (act == null || act.webView == null) return;
        act.runOnUiThread(new Runnable() {
            @Override
            public void run() {
                if (act.webView == null) return;
                String js = "window.__mediaControl && window.__mediaControl("
                        + JSONObject.quote(action) + "," + seekMs + ");";
                act.webView.evaluateJavascript(js, null);
            }
        });
    }

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        currentInstance = new WeakReference<>(this);

        Window window = getWindow();
        if (window != null) {
            window.addFlags(WindowManager.LayoutParams.FLAG_DRAWS_SYSTEM_BAR_BACKGROUNDS);
            window.setStatusBarColor(Color.parseColor("#121318"));
            window.setNavigationBarColor(Color.parseColor("#121318"));
        }

        webView = new BackgroundAudioWebView(this);
        webView.setBackgroundColor(Color.parseColor("#121318"));
        webView.setOverScrollMode(View.OVER_SCROLL_NEVER);
        webView.setVerticalScrollBarEnabled(false);
        webView.setHorizontalScrollBarEnabled(false);
        setContentView(webView);

        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setDatabaseEnabled(true);
        settings.setAllowFileAccess(true);
        settings.setAllowContentAccess(true);
        settings.setAllowFileAccessFromFileURLs(true);
        settings.setAllowUniversalAccessFromFileURLs(true);
        settings.setMediaPlaybackRequiresUserGesture(false);
        settings.setLoadWithOverviewMode(true);
        settings.setUseWideViewPort(true);
        settings.setSupportZoom(false);
        settings.setBuiltInZoomControls(false);
        settings.setDisplayZoomControls(false);

        webView.addJavascriptInterface(new AndroidBridge(), "__androidBridge");

        webView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                return false;
            }

            @Override
            public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
                Uri url = request.getUrl();
                if (url != null && LOCAL_HOST.equalsIgnoreCase(url.getHost())) {
                    return handleLocalRequest(url);
                }
                return super.shouldInterceptRequest(view, request);
            }
        });

        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(
                    WebView webView,
                    ValueCallback<Uri[]> filePathCallback,
                    FileChooserParams fileChooserParams) {
                if (MainActivity.this.filePathCallback != null) {
                    MainActivity.this.filePathCallback.onReceiveValue(null);
                }
                MainActivity.this.filePathCallback = filePathCallback;

                Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT);
                intent.addCategory(Intent.CATEGORY_OPENABLE);
                intent.setType("*/*");
                intent.putExtra(Intent.EXTRA_MIME_TYPES, new String[]{
                        "audio/*",
                        "application/ogg",
                        "application/x-flac",
                        "text/plain",
                        "application/octet-stream",
                        "image/*"
                });
                intent.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);
                intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION
                        | Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION);

                try {
                    startActivityForResult(
                            Intent.createChooser(intent, "Select Audio Files"),
                            FILE_CHOOSER_RESULT_CODE);
                } catch (Exception e) {
                    MainActivity.this.filePathCallback = null;
                    return false;
                }
                return true;
            }
        });

        requestNotificationPermissionIfNeeded();
        webView.loadUrl("file:///android_asset/index.html");
    }

    private void requestNotificationPermissionIfNeeded() {
        if (Build.VERSION.SDK_INT >= 33) {
            try {
                if (checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
                    requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, NOTIF_PERMISSION_CODE);
                }
            } catch (Exception ignored) {
            }
        }
    }

    /* ---------------- Local virtual host: folder listing & file streaming ---------------- */
    private WebResourceResponse handleLocalRequest(Uri url) {
        String path = url.getPath() == null ? "" : url.getPath();
        try {
            if (path.endsWith("/__android/list")) {
                String tree = url.getQueryParameter("tree");
                JSONArray arr = listTreeChildren(Uri.parse(tree));
                return jsonResponse(arr.toString(), 200);
            } else if (path.endsWith("/__android/file")) {
                String uriStr = url.getQueryParameter("uri");
                if (uriStr == null || !uriStr.startsWith("content://")) {
                    return jsonResponse("{\"error\":\"bad uri\"}", 400);
                }
                Uri docUri = Uri.parse(uriStr);
                ContentResolver cr = getContentResolver();
                String mime = cr.getType(docUri);
                if (mime == null) mime = "application/octet-stream";
                InputStream is = cr.openInputStream(docUri);
                if (is == null) return jsonResponse("{\"error\":\"not found\"}", 404);
                WebResourceResponse resp = new WebResourceResponse(mime, null, is);
                resp.setResponseHeaders(corsHeaders());
                return resp;
            }
        } catch (Exception e) {
            return jsonResponse("{\"error\":" + JSONObject.quote(String.valueOf(e.getMessage())) + "}", 500);
        }
        return jsonResponse("{\"error\":\"unknown endpoint\"}", 404);
    }

    private Map<String, String> corsHeaders() {
        Map<String, String> h = new HashMap<>();
        h.put("Access-Control-Allow-Origin", "*");
        h.put("Cache-Control", "no-cache");
        return h;
    }

    private WebResourceResponse jsonResponse(String body, int status) {
        WebResourceResponse resp = new WebResourceResponse(
                "application/json", "utf-8",
                new ByteArrayInputStream(body.getBytes(StandardCharsets.UTF_8)));
        String reason = status == 200 ? "OK" : (status == 404 ? "Not Found" : (status == 400 ? "Bad Request" : "Error"));
        resp.setStatusCodeAndReasonPhrase(status, reason);
        resp.setResponseHeaders(corsHeaders());
        return resp;
    }

    private JSONArray listTreeChildren(Uri treeUri) throws Exception {
        JSONArray arr = new JSONArray();
        if (treeUri == null) return arr;
        String treeDocId = DocumentsContract.getTreeDocumentId(treeUri);
        Uri childrenUri = DocumentsContract.buildChildDocumentsUriUsingTree(treeUri, treeDocId);
        String[] cols = new String[]{
                DocumentsContract.Document.COLUMN_DOCUMENT_ID,
                DocumentsContract.Document.COLUMN_DISPLAY_NAME,
                DocumentsContract.Document.COLUMN_MIME_TYPE,
                DocumentsContract.Document.COLUMN_LAST_MODIFIED,
                DocumentsContract.Document.COLUMN_SIZE
        };
        Cursor c = getContentResolver().query(childrenUri, cols, null, null, null);
        if (c == null) return arr;
        try {
            while (c.moveToNext()) {
                String docId = c.getString(0);
                String name = c.getString(1);
                String mime = c.getString(2);
                long modified = c.isNull(3) ? 0L : c.getLong(3);
                long size = c.isNull(4) ? 0L : c.getLong(4);
                if (DocumentsContract.Document.MIME_TYPE_DIR.equals(mime)) continue;
                Uri docUri = DocumentsContract.buildDocumentUriUsingTree(treeUri, docId);
                JSONObject o = new JSONObject();
                o.put("name", name);
                o.put("uri", docUri.toString());
                o.put("mime", mime == null ? "" : mime);
                o.put("lastModified", modified);
                o.put("size", size);
                arr.put(o);
            }
        } finally {
            c.close();
        }
        return arr;
    }

    private Uri findChildByName(Uri treeUri, String fileName) {
        try {
            JSONArray arr = listTreeChildren(treeUri);
            for (int i = 0; i < arr.length(); i++) {
                JSONObject o = arr.getJSONObject(i);
                if (fileName.equals(o.optString("name"))) {
                    return Uri.parse(o.getString("uri"));
                }
            }
        } catch (Exception ignored) {
        }
        return null;
    }

    private String queryTreeDisplayName(Uri treeUri) {
        try {
            String docId = DocumentsContract.getTreeDocumentId(treeUri);
            Uri docUri = DocumentsContract.buildDocumentUriUsingTree(treeUri, docId);
            Cursor c = getContentResolver().query(docUri,
                    new String[]{DocumentsContract.Document.COLUMN_DISPLAY_NAME}, null, null, null);
            if (c != null) {
                try {
                    if (c.moveToFirst()) {
                        String n = c.getString(0);
                        if (n != null && !n.isEmpty()) return n;
                    }
                } finally {
                    c.close();
                }
            }
            int colon = docId.lastIndexOf(':');
            String tail = colon >= 0 ? docId.substring(colon + 1) : docId;
            int slash = tail.lastIndexOf('/');
            tail = slash >= 0 ? tail.substring(slash + 1) : tail;
            if (!tail.isEmpty()) return tail;
        } catch (Exception ignored) {
        }
        return "Music";
    }

    /* ---------------- JavaScript Bridge (window.__androidBridge) ---------------- */
    public class AndroidBridge {
        @JavascriptInterface
        public void pickFolder() {
            runOnUiThread(new Runnable() {
                @Override
                public void run() {
                    try {
                        Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT_TREE);
                        intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION
                                | Intent.FLAG_GRANT_WRITE_URI_PERMISSION
                                | Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION
                                | Intent.FLAG_GRANT_PREFIX_URI_PERMISSION);
                        startActivityForResult(intent, FOLDER_PICKER_RESULT_CODE);
                    } catch (Exception e) {
                        notifyFolderPicked(null, null);
                    }
                }
            });
        }

        @JavascriptInterface
        public String getSavedFolder() {
            SharedPreferences sp = getSharedPreferences(PREFS, MODE_PRIVATE);
            String tree = sp.getString("treeUri", null);
            if (tree == null) return "";
            try {
                JSONObject o = new JSONObject();
                o.put("treeUri", tree);
                o.put("name", sp.getString("treeName", "Music"));
                return o.toString();
            } catch (Exception e) {
                return "";
            }
        }

        @JavascriptInterface
        public String writeTreeFileBase64(String treeUriStr, String fileName, String mime, String base64) {
            try {
                Uri treeUri = Uri.parse(treeUriStr);
                ContentResolver cr = getContentResolver();
                Uri target = findChildByName(treeUri, fileName);
                if (target == null) {
                    String parentId = DocumentsContract.getTreeDocumentId(treeUri);
                    Uri parentDoc = DocumentsContract.buildDocumentUriUsingTree(treeUri, parentId);
                    target = DocumentsContract.createDocument(cr, parentDoc,
                            (mime == null || mime.isEmpty()) ? "application/octet-stream" : mime, fileName);
                }
                if (target == null) return "";
                byte[] data = Base64.decode(base64, Base64.DEFAULT);
                OutputStream os = cr.openOutputStream(target, "wt");
                if (os == null) return "";
                try {
                    os.write(data);
                    os.flush();
                } finally {
                    os.close();
                }
                return target.toString();
            } catch (Exception e) {
                return "";
            }
        }

        @JavascriptInterface
        public boolean deleteTreeFile(String treeUriStr, String fileName) {
            try {
                Uri target = findChildByName(Uri.parse(treeUriStr), fileName);
                if (target == null) return false;
                return DocumentsContract.deleteDocument(getContentResolver(), target);
            } catch (Exception e) {
                return false;
            }
        }

        @JavascriptInterface
        public void updateMediaState(String json) {
            try {
                JSONObject o = new JSONObject(json);
                boolean playing = o.optBoolean("isPlaying", false);
                Intent i = new Intent(MainActivity.this, PlaybackService.class);
                i.setAction(PlaybackService.ACTION_UPDATE);
                i.putExtra(PlaybackService.EXTRA_TITLE, o.optString("title", "Unknown Track"));
                i.putExtra(PlaybackService.EXTRA_ARTIST, o.optString("artist", "Unknown Artist"));
                i.putExtra(PlaybackService.EXTRA_IS_PLAYING, playing);
                i.putExtra(PlaybackService.EXTRA_POSITION_MS, o.optLong("positionMs", 0L));
                i.putExtra(PlaybackService.EXTRA_DURATION_MS, o.optLong("durationMs", 0L));
                i.putExtra(PlaybackService.EXTRA_COVER, o.optString("cover", ""));
                try {
                    if (playing && Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                        startForegroundService(i);
                    } else {
                        startService(i);
                    }
                } catch (Exception first) {
                    try {
                        startService(i);
                    } catch (Exception ignored) {
                    }
                }
            } catch (Exception ignored) {
            }
        }
    }

    private void notifyFolderPicked(final String treeUri, final String name) {
        if (webView == null) return;
        runOnUiThread(new Runnable() {
            @Override
            public void run() {
                String js = "window.__onAndroidFolderPicked && window.__onAndroidFolderPicked("
                        + (treeUri == null ? "null" : JSONObject.quote(treeUri)) + ","
                        + (name == null ? "null" : JSONObject.quote(name)) + ");";
                webView.evaluateJavascript(js, null);
            }
        });
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        if (requestCode == FILE_CHOOSER_RESULT_CODE) {
            if (filePathCallback == null) return;
            Uri[] results = null;
            if (resultCode == Activity.RESULT_OK && data != null) {
                ClipData clipData = data.getClipData();
                if (clipData != null && clipData.getItemCount() > 0) {
                    results = new Uri[clipData.getItemCount()];
                    for (int i = 0; i < clipData.getItemCount(); i++) {
                        Uri u = clipData.getItemAt(i).getUri();
                        takePersistablePermissionSafe(u, Intent.FLAG_GRANT_READ_URI_PERMISSION);
                        results[i] = u;
                    }
                } else if (data.getData() != null) {
                    Uri u = data.getData();
                    takePersistablePermissionSafe(u, Intent.FLAG_GRANT_READ_URI_PERMISSION);
                    results = new Uri[]{u};
                }
            }
            filePathCallback.onReceiveValue(results);
            filePathCallback = null;
        } else if (requestCode == FOLDER_PICKER_RESULT_CODE) {
            if (resultCode == Activity.RESULT_OK && data != null && data.getData() != null) {
                Uri treeUri = data.getData();
                takePersistablePermissionSafe(treeUri,
                        Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_WRITE_URI_PERMISSION);
                String name = queryTreeDisplayName(treeUri);
                getSharedPreferences(PREFS, MODE_PRIVATE).edit()
                        .putString("treeUri", treeUri.toString())
                        .putString("treeName", name)
                        .apply();
                notifyFolderPicked(treeUri.toString(), name);
            } else {
                notifyFolderPicked(null, null);
            }
        } else {
            super.onActivityResult(requestCode, resultCode, data);
        }
    }

    private void takePersistablePermissionSafe(Uri uri, int flags) {
        if (uri == null) return;
        try {
            getContentResolver().takePersistableUriPermission(uri, flags);
        } catch (Exception e) {
            try {
                getContentResolver().takePersistableUriPermission(uri, Intent.FLAG_GRANT_READ_URI_PERMISSION);
            } catch (Exception ignored) {
            }
        }
    }

    /* ---------------- Back navigation: close modal -> player -> subview -> home, else background app ---------------- */
    private void handleBackFromJs() {
        if (webView == null) {
            moveTaskToBack(true);
            return;
        }
        webView.evaluateJavascript(
                "(function(){try{return (window.__handleBackNavigation && window.__handleBackNavigation(false)) ? 'handled' : 'exit';}catch(e){return 'exit';}})()",
                new ValueCallback<String>() {
                    @Override
                    public void onReceiveValue(String value) {
                        if (value == null || !value.contains("handled")) {
                            moveTaskToBack(true);
                        }
                    }
                });
    }

    @Override
    public void onBackPressed() {
        handleBackFromJs();
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_BACK) {
            handleBackFromJs();
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }

    @Override
    protected void onPause() {
        // Intentionally do NOT call webView.onPause()/pauseTimers() so audio keeps playing in background
        super.onPause();
    }

    @Override
    protected void onDestroy() {
        if (currentInstance.get() == this) {
            currentInstance = new WeakReference<>(null);
        }
        try {
            stopService(new Intent(this, PlaybackService.class));
        } catch (Exception ignored) {
        }
        if (webView != null) {
            webView.destroy();
            webView = null;
        }
        super.onDestroy();
    }
}
