package com.musics.mp3player;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.ServiceInfo;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.LinearGradient;
import android.graphics.Paint;
import android.graphics.Path;
import android.graphics.RectF;
import android.graphics.Shader;
import android.graphics.drawable.Icon;
import android.media.AudioManager;
import android.media.MediaMetadata;
import android.media.session.MediaSession;
import android.media.session.PlaybackState;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.PowerManager;
import android.os.SystemClock;
import android.util.Base64;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public class PlaybackService extends Service {
    public static final String ACTION_UPDATE = "com.musics.mp3player.ACTION_UPDATE";
    public static final String ACTION_CMD_PLAY = "com.musics.mp3player.ACTION_CMD_PLAY";
    public static final String ACTION_CMD_PAUSE = "com.musics.mp3player.ACTION_CMD_PAUSE";
    public static final String ACTION_CMD_PREV = "com.musics.mp3player.ACTION_CMD_PREV";
    public static final String ACTION_CMD_NEXT = "com.musics.mp3player.ACTION_CMD_NEXT";
    public static final String ACTION_CMD_STOP = "com.musics.mp3player.ACTION_CMD_STOP";

    public static final String EXTRA_TITLE = "title";
    public static final String EXTRA_ARTIST = "artist";
    public static final String EXTRA_IS_PLAYING = "isPlaying";
    public static final String EXTRA_POSITION_MS = "positionMs";
    public static final String EXTRA_DURATION_MS = "durationMs";
    public static final String EXTRA_COVER = "cover";

    private static final String CHANNEL_ID = "mp3_playback_channel";
    private static final int NOTIF_ID = 2001;

    private MediaSession mediaSession;
    private NotificationManager notificationManager;
    private AudioManager audioManager;
    private PowerManager.WakeLock wakeLock;
    private ExecutorService artExecutor;
    private Handler mainHandler;

    private String currentTitle = "Music Player";
    private String currentArtist = "Unknown Artist";
    private boolean isPlaying = false;
    private long positionMs = 0L;
    private long durationMs = 0L;
    private String lastCoverKey = "";
    private Bitmap currentArtBitmap = null;
    private Bitmap fallbackArtBitmap = null;

    private Icon iconPrev;
    private Icon iconPlay;
    private Icon iconPause;
    private Icon iconNext;

    private boolean noisyReceiverRegistered = false;

    private final BroadcastReceiver noisyReceiver = new BroadcastReceiver() {
        @Override
        public void onReceive(Context context, Intent intent) {
            if (AudioManager.ACTION_AUDIO_BECOMING_NOISY.equals(intent.getAction())) {
                if (isPlaying) {
                    MainActivity.dispatchMediaCommand("pause", -1);
                }
            }
        }
    };

    @Override
    public void onCreate() {
        super.onCreate();
        mainHandler = new Handler(Looper.getMainLooper());
        artExecutor = Executors.newSingleThreadExecutor();
        notificationManager = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
        audioManager = (AudioManager) getSystemService(Context.AUDIO_SERVICE);

        PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
        if (pm != null) {
            wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "MP3Player::MediaPlaybackLock");
            wakeLock.setReferenceCounted(false);
        }

        ensureNotificationChannel();
        buildTransportIcons();
        fallbackArtBitmap = createFallbackArtwork();

        mediaSession = new MediaSession(this, "MP3PlayerMediaSession");
        mediaSession.setSessionActivity(buildContentPendingIntent());
        mediaSession.setCallback(new MediaSession.Callback() {
            @Override
            public void onPlay() {
                MainActivity.dispatchMediaCommand("play", -1);
            }

            @Override
            public void onPause() {
                MainActivity.dispatchMediaCommand("pause", -1);
            }

            @Override
            public void onSkipToNext() {
                MainActivity.dispatchMediaCommand("next", -1);
            }

            @Override
            public void onSkipToPrevious() {
                MainActivity.dispatchMediaCommand("prev", -1);
            }

            @Override
            public void onSeekTo(long pos) {
                positionMs = Math.max(0L, pos);
                publishSessionAndNotification(false);
                MainActivity.dispatchMediaCommand("seekTo", pos);
            }

            @Override
            public void onStop() {
                MainActivity.dispatchMediaCommand("pause", -1);
            }
        });
        mediaSession.setActive(true);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent == null) {
            publishSessionAndNotification(true);
            return START_NOT_STICKY;
        }

        String action = intent.getAction();
        if (ACTION_CMD_PLAY.equals(action)) {
            MainActivity.dispatchMediaCommand("play", -1);
            publishSessionAndNotification(true);
            return START_NOT_STICKY;
        } else if (ACTION_CMD_PAUSE.equals(action)) {
            MainActivity.dispatchMediaCommand("pause", -1);
            publishSessionAndNotification(true);
            return START_NOT_STICKY;
        } else if (ACTION_CMD_PREV.equals(action)) {
            MainActivity.dispatchMediaCommand("prev", -1);
            publishSessionAndNotification(true);
            return START_NOT_STICKY;
        } else if (ACTION_CMD_NEXT.equals(action)) {
            MainActivity.dispatchMediaCommand("next", -1);
            publishSessionAndNotification(true);
            return START_NOT_STICKY;
        } else if (ACTION_CMD_STOP.equals(action)) {
            isPlaying = false;
            updateWakeLockAndReceivers();
            MainActivity.dispatchMediaCommand("pause", -1);
            stopForeground(true);
            stopSelf();
            return START_NOT_STICKY;
        }

        // ACTION_UPDATE
        String newTitle = intent.getStringExtra(EXTRA_TITLE);
        String newArtist = intent.getStringExtra(EXTRA_ARTIST);
        if (newTitle != null && !newTitle.isEmpty()) currentTitle = newTitle;
        if (newArtist != null && !newArtist.isEmpty()) currentArtist = newArtist;

        isPlaying = intent.getBooleanExtra(EXTRA_IS_PLAYING, isPlaying);
        positionMs = Math.max(0L, intent.getLongExtra(EXTRA_POSITION_MS, positionMs));
        durationMs = Math.max(0L, intent.getLongExtra(EXTRA_DURATION_MS, durationMs));

        updateWakeLockAndReceivers();

        String cover = intent.getStringExtra(EXTRA_COVER);
        resolveCoverArtwork(cover);

        publishSessionAndNotification(true);
        return START_STICKY;
    }

    private void updateWakeLockAndReceivers() {
        try {
            if (isPlaying) {
                if (wakeLock != null && !wakeLock.isHeld()) {
                    wakeLock.acquire();
                }
                if (!noisyReceiverRegistered) {
                    IntentFilter filter = new IntentFilter(AudioManager.ACTION_AUDIO_BECOMING_NOISY);
                    if (Build.VERSION.SDK_INT >= 33) {
                        registerReceiver(noisyReceiver, filter, Context.RECEIVER_EXPORTED);
                    } else {
                        registerReceiver(noisyReceiver, filter);
                    }
                    noisyReceiverRegistered = true;
                }
            } else {
                if (wakeLock != null && wakeLock.isHeld()) {
                    wakeLock.release();
                }
                if (noisyReceiverRegistered) {
                    try {
                        unregisterReceiver(noisyReceiver);
                    } catch (Exception ignored) {
                    }
                    noisyReceiverRegistered = false;
                }
            }
        } catch (Exception ignored) {
        }
    }

    private void resolveCoverArtwork(final String cover) {
        if (cover == null || cover.isEmpty()) {
            if (!"".equals(lastCoverKey)) {
                lastCoverKey = "";
                currentArtBitmap = null;
            }
            return;
        }
        String key = cover.length() > 160
                ? cover.substring(0, 96) + "#" + cover.length() + "#" + cover.hashCode()
                : cover;
        if (key.equals(lastCoverKey) && currentArtBitmap != null) {
            return;
        }
        lastCoverKey = key;

        if (cover.startsWith("data:image/")) {
            int comma = cover.indexOf(',');
            if (comma > 0) {
                try {
                    String b64 = cover.substring(comma + 1);
                    byte[] raw = Base64.decode(b64, Base64.DEFAULT);
                    Bitmap bmp = decodeScaledBitmap(raw, 384);
                    if (bmp != null) {
                        currentArtBitmap = bmp;
                    }
                } catch (Exception ignored) {
                }
            }
            return;
        }

        if (cover.startsWith("http://") || cover.startsWith("https://")) {
            final String expectedKey = key;
            artExecutor.execute(new Runnable() {
                @Override
                public void run() {
                    HttpURLConnection conn = null;
                    try {
                        URL url = new URL(cover);
                        conn = (HttpURLConnection) url.openConnection();
                        conn.setConnectTimeout(5000);
                        conn.setReadTimeout(5000);
                        conn.setRequestProperty("User-Agent", "MP3Player-Android/1.1");
                        conn.connect();
                        if (conn.getResponseCode() == 200) {
                            InputStream is = conn.getInputStream();
                            ByteArrayOutputStream bos = new ByteArrayOutputStream();
                            byte[] buf = new byte[8192];
                            int n;
                            while ((n = is.read(buf)) != -1) {
                                bos.write(buf, 0, n);
                                if (bos.size() > 4 * 1024 * 1024) break;
                            }
                            is.close();
                            final Bitmap bmp = decodeScaledBitmap(bos.toByteArray(), 384);
                            if (bmp != null) {
                                mainHandler.post(new Runnable() {
                                    @Override
                                    public void run() {
                                        if (expectedKey.equals(lastCoverKey)) {
                                            currentArtBitmap = bmp;
                                            publishSessionAndNotification(false);
                                        }
                                    }
                                });
                            }
                        }
                    } catch (Exception ignored) {
                    } finally {
                        if (conn != null) conn.disconnect();
                    }
                }
            });
        }
    }

    private static Bitmap decodeScaledBitmap(byte[] data, int maxDim) {
        if (data == null || data.length == 0) return null;
        try {
            BitmapFactory.Options bounds = new BitmapFactory.Options();
            bounds.inJustDecodeBounds = true;
            BitmapFactory.decodeByteArray(data, 0, data.length, bounds);
            if (bounds.outWidth <= 0 || bounds.outHeight <= 0) return null;

            int sample = 1;
            while (bounds.outWidth / sample > maxDim * 2 || bounds.outHeight / sample > maxDim * 2) {
                sample *= 2;
            }
            BitmapFactory.Options opts = new BitmapFactory.Options();
            opts.inSampleSize = sample;
            opts.inPreferredConfig = Bitmap.Config.ARGB_8888;
            Bitmap decoded = BitmapFactory.decodeByteArray(data, 0, data.length, opts);
            if (decoded == null) return null;

            int w = decoded.getWidth();
            int h = decoded.getHeight();
            if (w > maxDim || h > maxDim) {
                float scale = Math.min((float) maxDim / w, (float) maxDim / h);
                int nw = Math.max(1, Math.round(w * scale));
                int nh = Math.max(1, Math.round(h * scale));
                Bitmap scaled = Bitmap.createScaledBitmap(decoded, nw, nh, true);
                if (scaled != decoded) decoded.recycle();
                return scaled;
            }
            return decoded;
        } catch (Exception e) {
            return null;
        }
    }

    private void publishSessionAndNotification(boolean ensureForegroundCall) {
        if (mediaSession == null) return;

        Bitmap art = currentArtBitmap != null ? currentArtBitmap : fallbackArtBitmap;

        MediaMetadata.Builder metaBuilder = new MediaMetadata.Builder()
                .putString(MediaMetadata.METADATA_KEY_TITLE, currentTitle)
                .putString(MediaMetadata.METADATA_KEY_ARTIST, currentArtist)
                .putString(MediaMetadata.METADATA_KEY_DISPLAY_TITLE, currentTitle)
                .putString(MediaMetadata.METADATA_KEY_DISPLAY_SUBTITLE, currentArtist)
                .putLong(MediaMetadata.METADATA_KEY_DURATION, durationMs > 0 ? durationMs : -1L);
        if (art != null) {
            metaBuilder.putBitmap(MediaMetadata.METADATA_KEY_ALBUM_ART, art);
            metaBuilder.putBitmap(MediaMetadata.METADATA_KEY_ART, art);
        }
        mediaSession.setMetadata(metaBuilder.build());

        long actions = PlaybackState.ACTION_PLAY
                | PlaybackState.ACTION_PAUSE
                | PlaybackState.ACTION_PLAY_PAUSE
                | PlaybackState.ACTION_SKIP_TO_PREVIOUS
                | PlaybackState.ACTION_SKIP_TO_NEXT
                | PlaybackState.ACTION_SEEK_TO
                | PlaybackState.ACTION_STOP;

        int stateCode = isPlaying ? PlaybackState.STATE_PLAYING : PlaybackState.STATE_PAUSED;
        float speed = isPlaying ? 1.0f : 0.0f;
        PlaybackState state = new PlaybackState.Builder()
                .setActions(actions)
                .setState(stateCode, positionMs, speed, SystemClock.elapsedRealtime())
                .build();
        mediaSession.setPlaybackState(state);
        mediaSession.setActive(true);

        Notification notification = buildMediaNotification(art);
        try {
            if (ensureForegroundCall || isPlaying) {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                    startForeground(NOTIF_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK);
                } else {
                    startForeground(NOTIF_ID, notification);
                }
                if (!isPlaying) {
                    stopForeground(Service.STOP_FOREGROUND_DETACH);
                    if (notificationManager != null) {
                        notificationManager.notify(NOTIF_ID, notification);
                    }
                }
            } else if (notificationManager != null) {
                notificationManager.notify(NOTIF_ID, notification);
            }
        } catch (Exception e) {
            if (notificationManager != null) {
                try {
                    notificationManager.notify(NOTIF_ID, notification);
                } catch (Exception ignored) {
                }
            }
        }
    }

    private Notification buildMediaNotification(Bitmap art) {
        Notification.Builder builder;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            builder = new Notification.Builder(this, CHANNEL_ID);
        } else {
            //noinspection deprecation
            builder = new Notification.Builder(this);
        }

        Notification.Action prevAction = new Notification.Action.Builder(
                iconPrev, "Previous", buildServicePendingIntent(ACTION_CMD_PREV, 10)).build();
        Notification.Action playPauseAction = isPlaying
                ? new Notification.Action.Builder(iconPause, "Pause", buildServicePendingIntent(ACTION_CMD_PAUSE, 11)).build()
                : new Notification.Action.Builder(iconPlay, "Play", buildServicePendingIntent(ACTION_CMD_PLAY, 12)).build();
        Notification.Action nextAction = new Notification.Action.Builder(
                iconNext, "Next", buildServicePendingIntent(ACTION_CMD_NEXT, 13)).build();

        Notification.MediaStyle mediaStyle = new Notification.MediaStyle()
                .setMediaSession(mediaSession.getSessionToken())
                .setShowActionsInCompactView(0, 1, 2);

        builder.setStyle(mediaStyle)
                .setSmallIcon(R.drawable.ic_launcher)
                .setContentTitle(currentTitle)
                .setContentText(currentArtist)
                .setContentIntent(buildContentPendingIntent())
                .setDeleteIntent(buildServicePendingIntent(ACTION_CMD_STOP, 14))
                .setVisibility(Notification.VISIBILITY_PUBLIC)
                .setCategory(Notification.CATEGORY_TRANSPORT)
                .setOnlyAlertOnce(true)
                .setOngoing(isPlaying)
                .setShowWhen(false)
                .addAction(prevAction)
                .addAction(playPauseAction)
                .addAction(nextAction);

        if (art != null) {
            builder.setLargeIcon(art);
        }
        return builder.build();
    }

    private PendingIntent buildContentPendingIntent() {
        Intent openIntent = new Intent(this, MainActivity.class);
        openIntent.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        int flags = PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE;
        return PendingIntent.getActivity(this, 1, openIntent, flags);
    }

    private PendingIntent buildServicePendingIntent(String action, int reqCode) {
        Intent i = new Intent(this, PlaybackService.class);
        i.setAction(action);
        int flags = PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE;
        return PendingIntent.getService(this, reqCode, i, flags);
    }

    private void ensureNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O && notificationManager != null) {
            NotificationChannel channel = new NotificationChannel(
                    CHANNEL_ID,
                    "Music Playback",
                    NotificationManager.IMPORTANCE_LOW);
            channel.setDescription("Background music playback controls and lock screen player");
            channel.setShowBadge(false);
            channel.setLockscreenVisibility(Notification.VISIBILITY_PUBLIC);
            notificationManager.createNotificationChannel(channel);
        }
    }

    private void buildTransportIcons() {
        iconPrev = Icon.createWithBitmap(drawTransportBitmap(0));
        iconPlay = Icon.createWithBitmap(drawTransportBitmap(1));
        iconPause = Icon.createWithBitmap(drawTransportBitmap(2));
        iconNext = Icon.createWithBitmap(drawTransportBitmap(3));
    }

    private static Bitmap drawTransportBitmap(int kind) {
        int size = 72;
        Bitmap bmp = Bitmap.createBitmap(size, size, Bitmap.Config.ARGB_8888);
        Canvas c = new Canvas(bmp);
        Paint p = new Paint(Paint.ANTI_ALIAS_FLAG);
        p.setColor(Color.WHITE);
        p.setStyle(Paint.Style.FILL);

        if (kind == 0) {
            // Previous: vertical bar on left + left-pointing triangle
            c.drawRoundRect(new RectF(14f, 16f, 21f, 56f), 3.5f, 3.5f, p);
            Path tri = new Path();
            tri.moveTo(56f, 16f);
            tri.lineTo(56f, 56f);
            tri.lineTo(24f, 36f);
            tri.close();
            c.drawPath(tri, p);
        } else if (kind == 1) {
            // Play: right-pointing triangle
            Path tri = new Path();
            tri.moveTo(20f, 14f);
            tri.lineTo(58f, 36f);
            tri.lineTo(20f, 58f);
            tri.close();
            c.drawPath(tri, p);
        } else if (kind == 2) {
            // Pause: two rounded vertical bars
            c.drawRoundRect(new RectF(18f, 14f, 31f, 58f), 4f, 4f, p);
            c.drawRoundRect(new RectF(41f, 14f, 54f, 58f), 4f, 4f, p);
        } else {
            // Next: right-pointing triangle + vertical bar on right
            Path tri = new Path();
            tri.moveTo(16f, 16f);
            tri.lineTo(48f, 36f);
            tri.lineTo(16f, 56f);
            tri.close();
            c.drawPath(tri, p);
            c.drawRoundRect(new RectF(51f, 16f, 58f, 56f), 3.5f, 3.5f, p);
        }
        return bmp;
    }

    private static Bitmap createFallbackArtwork() {
        int size = 256;
        Bitmap bmp = Bitmap.createBitmap(size, size, Bitmap.Config.ARGB_8888);
        Canvas c = new Canvas(bmp);
        Paint bg = new Paint(Paint.ANTI_ALIAS_FLAG);
        bg.setShader(new LinearGradient(
                0f, 0f, size, size,
                Color.parseColor("#231423"),
                Color.parseColor("#D4163C"),
                Shader.TileMode.CLAMP));
        c.drawRect(0f, 0f, size, size, bg);

        Paint ring = new Paint(Paint.ANTI_ALIAS_FLAG);
        ring.setStyle(Paint.Style.STROKE);
        ring.setStrokeWidth(4f);
        ring.setColor(Color.argb(65, 255, 255, 255));
        c.drawCircle(size / 2f, size / 2f, 92f, ring);

        Paint note = new Paint(Paint.ANTI_ALIAS_FLAG);
        note.setColor(Color.WHITE);
        note.setStyle(Paint.Style.FILL);
        c.drawCircle(104f, 158f, 18f, note);
        c.drawCircle(156f, 146f, 18f, note);
        c.drawRect(116f, 92f, 124f, 158f, note);
        c.drawRect(168f, 80f, 176f, 146f, note);
        Path beam = new Path();
        beam.moveTo(116f, 92f);
        beam.lineTo(176f, 80f);
        beam.lineTo(176f, 96f);
        beam.lineTo(116f, 108f);
        beam.close();
        c.drawPath(beam, note);
        return bmp;
    }

    @Override
    public void onTaskRemoved(Intent rootIntent) {
        if (!isPlaying) {
            stopForeground(true);
            stopSelf();
        }
        super.onTaskRemoved(rootIntent);
    }

    @Override
    public void onDestroy() {
        isPlaying = false;
        updateWakeLockAndReceivers();
        if (artExecutor != null) {
            artExecutor.shutdownNow();
        }
        if (mediaSession != null) {
            try {
                mediaSession.setActive(false);
                mediaSession.release();
            } catch (Exception ignored) {
            }
            mediaSession = null;
        }
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
