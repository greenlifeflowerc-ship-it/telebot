# Testing Guide

How to test the bot locally, verify the webhook after a Render deploy, read
logs, and try real public links.

> Reminder: only test with content you own or have permission to download.

---

## 1. Test locally with uvicorn

```bash
# 1. Install deps (ffmpeg must also be installed and on your PATH)
pip install -r requirements.txt

# 2. Configure env
cp .env.example .env
#    Edit .env and set BOT_TOKEN, WEBHOOK_SECRET, and PUBLIC_URL.

# 3. Run the server
uvicorn main:app --host 0.0.0.0 --port ${PORT:-10000}
```

Quick checks (in another terminal):

```bash
curl http://localhost:10000/                # {"status":"ok"}
curl http://localhost:10000/health          # {"ok":true,"status":"healthy"}
```

Telegram requires a **public HTTPS** URL to deliver updates, so for a full
local end-to-end test expose your port with a tunnel and set `PUBLIC_URL` to it:

```bash
# Example using ngrok
ngrok http 10000
# Then set PUBLIC_URL=https://<your-id>.ngrok-free.app in .env and restart.
```

**Public URL resolution.** On Render the app finds its URL automatically from
`RENDER_EXTERNAL_HOSTNAME` (building `https://{hostname}`), or from
`RENDER_EXTERNAL_URL` if present. `PUBLIC_URL` is only needed **outside Render**
(like the ngrok tunnel above) or if automatic hostname detection fails.

On startup the app validates config and registers the webhook automatically.
If `BOT_TOKEN`, `WEBHOOK_SECRET`, or a public URL is missing/invalid, the app
exits and prints exactly what to fix.

---

## 2. Test the webhook after Render deploy

After the Render service is **Live**, replace `<TOKEN>` with your bot token:

```bash
curl "https://api.telegram.org/bot<TOKEN>/getWebhookInfo"
```

A healthy response shows:
- `"url"` ending in `/webhook/<your-secret>`
- `"pending_update_count": 0` (or a small, draining number)
- no `"last_error_message"`

You can also use the built-in debug endpoint (only when `DEBUG=true`). It hides
the secret and never shows the token:

```bash
curl "https://your-app.onrender.com/webhook-info"
```

Then test the bot in Telegram:
1. Send `/start` → Arabic welcome message.
2. Send a public link → two buttons: **تحميل فيديو** / **تحميل صوت MP3**.
3. Tap **تحميل فيديو** → quality menu `اختر دقة الفيديو:` (see section 5).
4. Pick a quality → `جاري تحميل الفيديو بالدقة المختارة...` then the file.
5. Tap **تحميل صوت MP3** → `⏳ جاري التحميل...` then an MP3.
6. Tap again while a download is running → `يوجد تحميل قيد التنفيذ حالياً...`.

---

## 3. How to check logs

**On Render:** open your service → **Logs** tab (live stream). Look for:
- `Webhook registered at https://.../webhook/***` on startup (success).
- `Invalid configuration — fix these environment variables:` (bad env vars).
- `Telegram <method> failed: ...` or `... download failed: ...` (runtime issues).

The bot token is never printed to logs.

**Locally:** logs print to the terminal running uvicorn. Set `DEBUG=true` in
`.env` to also enable the `/webhook-info` endpoint while debugging.

---

## 4. Test YouTube / TikTok / Instagram public links

Send any of these **public** link shapes to the bot, then choose video or audio:

| Platform   | Example link shape                                  |
| ---------- | --------------------------------------------------- |
| YouTube    | `https://www.youtube.com/watch?v=XXXXXXXXXXX`       |
| YouTube    | `https://youtu.be/XXXXXXXXXXX`                       |
| TikTok     | `https://www.tiktok.com/@user/video/0000000000`     |
| TikTok     | `https://vm.tiktok.com/XXXXXXX/`                     |
| Instagram  | `https://www.instagram.com/reel/XXXXXXXXXXX/`        |

What to verify:
- A **non-allowed** link (e.g. a plain website) → `❌ الرابط غير مدعوم.`
- A short clip → a normalized mp4 arrives via `sendVideo` with caption
  `تم تحميل الفيديو.` It should **play with image (not frozen)** and **save
  correctly to the phone gallery**.
- Audio button → an `.mp3` arrives via `sendAudio`. If MP3 conversion fails you
  instead get the raw audio as a document with
  `تم تحميل الصوت، لكن لم أستطع تحويله إلى MP3.`; if nothing downloads →
  `فشل تحميل الصوت. جرّب رابط آخر أو تأكد أن الرابط عام.`
- A **large** file (> ~49 MB after normalization) → not sent; try a lower quality.
- **Private / login-required** content → fails by design with the Arabic error
  message. This bot does not bypass logins or restrictions.

If a platform suddenly stops working, update `yt-dlp` by redeploying
(Render → **Manual Deploy → Clear build cache & deploy**).

---

## 5. Test video quality options

1. Send a public link, then tap **تحميل فيديو**.
2. The bot replies `اختر دقة الفيديو:` with a subset of:
   **الجودة الأصلية / Full · 1080p · 720p · 480p · 360p · أقل حجم / Small**.

What to verify:

| Action | Expected |
| ------ | -------- |
| Tap **الجودة الأصلية / Full** | Best quality, merged to mp4 (`bestvideo+bestaudio/best`). |
| Tap **1080p / 720p / 480p / 360p** | Video capped at that height + best audio. |
| Tap **أقل حجم / Small** | Smallest file (`worst[ext=mp4]/worst`). |
| Pick a resolution the link cannot provide | `هذه الدقة غير متاحة لهذا الرابط. جرّب دقة أخرى.` |
| Send a **YouTube** link (rich formats) | Menu typically shows multiple resolutions. |
| Send a **TikTok / Instagram** link (limited formats) | Falls back to **Full / 720p / 480p / Small**. |
| Link whose formats can't be read | `لم أستطع قراءة الدقات المتاحة، سأعرض خيارات آمنة للتجربة.` then the safe menu. |
| Large estimate before download | `⚠️ الحجم المتوقع كبير...` warning, but it still tries. |
| Final file > 49 MB | `الملف أكبر من حد تلغرام للبوت. جرّب دقة أقل مثل 720p أو 480p.` (not sent) — try 720p/480p. |

Tip: a long 4K/1080p YouTube video is an easy way to trigger the > 49 MB path;
a short clip is an easy way to confirm a successful send.

---

## 6. Verify mobile / Telegram compatibility (normalization)

Every video is re-encoded with ffmpeg before sending, so it plays correctly in
the Telegram player and saves correctly to the gallery.

What to verify:
- The received video **plays with both image and sound** (not audio-only with a
  frozen frame) — including links that originally served **VP9 / AV1 / WebM**.
- **Saving** the video to the phone gallery produces a normal, playable MP4.
- It always arrives via **`sendVideo`** (inline player), not as a file/document.

Check the Render logs for the diagnostic trail of one video request (no secrets
are logged):

```
Video request: chat=... quality=720
Raw download: <id>.webm (12.3 MB)
ffmpeg normalization ok (target_height=720)
Normalized: normalized.mp4 (9.8 MB)
Sending via sendVideo
```

If normalization itself fails, you get `تعذّرت معالجة الفيديو لجعله متوافقًا مع
تيليجرام...` instead of a broken video. Confirm ffmpeg is installed — it is
provided by the Docker image, so this should only happen on a broken/odd source.

> Local note: re-encoding needs **ffmpeg on your PATH** when running without
> Docker. On Render it is already in the image.
