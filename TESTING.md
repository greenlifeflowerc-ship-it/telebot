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
- A short clip → video arrives via `sendVideo` (mp4) or `sendDocument`.
- Audio button → an `.mp3` arrives via `sendAudio`.
- A **large** file (> ~49 MB) → `⚠️ حجم الملف أكبر من الحد المسموح...` (not sent).
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
