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
3. Tap **تحميل فيديو** → menu `اختر دقة الفيديو المتاحة:` (see section 5).
4. Pick a quality → `جاري تجهيز الفيديو...`, then `جاري إرسال الفيديو...` (fast
   remux) or `جاري ضغط الفيديو ليتوافق مع تلغرام...` (re-encode), then the file.
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

## 5. Test video quality options (only available ones)

1. Send a public link, then tap **تحميل فيديو**.
2. The bot replies `اختر دقة الفيديو المتاحة:` (with the
   `الأسرع هو اختيار 480p أو 360p بدون ضغط` note) and shows **only the
   resolutions that exist** for that link — possible: 1080p · 720p · 480p ·
   360p · 240p · أقل حجم (Small). There is **no Full/original button**.

What to verify:

| Action | Expected |
| ------ | -------- |
| Send a **YouTube** link | Menu shows the resolutions YouTube actually offers (e.g. 1080p/720p/480p/360p/240p/Small). No fake buttons. |
| Send a **TikTok / Instagram** link | Only the resolution(s) that exist appear; often just **Small** (single-format sources). |
| `LOW_RESOURCE_MODE=true` | **1080p never appears**, even if the link has it. |
| Tap **480p** / **360p** | Downloads that resolution; logs show `method=direct` or `method=remux` (no re-encode). |
| Tap **أقل حجم / Small** | Smallest file (`worst[ext=mp4]/worst`). |
| Link whose formats can't be read | `لم أستطع قراءة الدقات المتاحة...` then a safe fallback menu (720p/480p/360p/Small). |
| Link with no video | `لا يوجد فيديو متاح لهذا الرابط...` |
| Final file > 49 MB | `الملف أكبر من حد تلغرام للبوت. جرّب دقة أقل مثل 480p أو 360p.` (not sent). |

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
Video request: chat=... quality=720 fast=True low_res=True
yt-dlp download: 6.4s -> <title>-<id>.mp4 (8.1 MB)
ffprobe inspect: 0.05s (compatible=True)
remux: 0.3s
Final file: normalized_output.mp4 (8.1 MB)
Telegram upload: 2.1s (8.1 MB, method=remux)
Cleaned temp folder for chat_id=...
```

If processing fails, you get `تعذّرت معالجة الفيديو لجعله متوافقًا مع
تيليجرام...` instead of a broken video. Confirm ffmpeg/ffprobe are installed —
they are provided by the Docker image, so this should only happen on a broken
source.

> Local note: re-encoding needs **ffmpeg on your PATH** when running without
> Docker. On Render it is already in the image.

---

## 7. Verify temporary file cleanup

Downloaded media must never be left on disk after sending. Each job uses its own
`tg_downloader_{chat_id}_…` folder in the system temp dir and deletes it in a
`finally` block.

**Watch the temp dir during a download (local).** The folder appears mid-download
and is gone right after the bot sends the file:

```bash
# Linux/macOS — system temp is usually /tmp
watch -n 1 'ls -d /tmp/tg_downloader_* 2>/dev/null'
```

```powershell
# Windows PowerShell — temp is $env:TEMP
while ($true) { Get-ChildItem $env:TEMP -Directory -Filter 'tg_downloader_*'; Start-Sleep 1; Clear-Host }
```

Run a download, then confirm:
- A `tg_downloader_<chat_id>_*` folder exists **while** downloading/sending.
- It is **gone** after the bot sends the file (or fails) — and the log shows
  `Cleaned temp folder for chat_id=...`.
- This holds for every outcome: success, bad link, ffmpeg failure, a file over
  49 MB, and Telegram send failure.

**No media in the project directory.** After several downloads, the repo folder
should still contain only source files — no `.mp4`, `.mp3`, `.webm`, `.part`,
`.m4a`, etc. (`.gitignore` also blocks these from ever being committed):

```bash
ls *.mp4 *.mp3 *.webm *.part 2>/dev/null   # expect: nothing
git status --short                          # expect: clean
```

**Startup sweep.** On boot the app runs `cleanup_stale_temp_dirs()` and removes
any `tg_downloader_*` folder older than 1 hour. To see it, create a fake stale
folder, then restart the app:

```bash
mkdir -p "$(python -c 'import tempfile;print(tempfile.gettempdir())')/tg_downloader_test_old"
touch -d '2 hours ago' "$(python -c 'import tempfile;print(tempfile.gettempdir())')/tg_downloader_test_old"
# restart the app -> log: "Startup cleanup removed 1 stale temp folder(s)."
```

---

## 8. Verify low-resource mode (Render Free 512 MB)

Set `LOW_RESOURCE_MODE=true` (env var) and restart. The startup log shows the
flags line, e.g. `Flags: FAST_MODE=True LOW_RESOURCE_MODE=True ...`.

What to verify:
- Tap **تحميل فيديو** on a link that has 1080p → **1080p does not appear** in the
  menu (other available resolutions still do).
- Pick **720p** → first shows `قد يستغرق تحميل 720p وقتاً أطول على الخطة المجانية.`
- If a `v_1080` callback is somehow sent (e.g. an old keyboard), the bot replies
  `دقة 1080p غير متاحة على الخطة المجانية...`.
- **Global lock:** start a download in one chat, then immediately request one
  from a **different** chat → the second gets
  `السيرفر يعالج طلباً آخر حالياً، حاول بعد قليل.`

With `LOW_RESOURCE_MODE` unset/`false` (bigger instance), 1080p appears whenever
the link actually has it.

---

## 9. Verify speed (direct/remux, no re-encode by default)

Startup logs the flags, e.g.
`Flags: FAST_MODE=True LOW_RESOURCE_MODE=True NO_REENCODE_BY_DEFAULT=True SEND_VIDEO_AS_FILE_COPY=False`.

- **Choose 480p on a YouTube link → no heavy re-encode.** The logs should show
  `compatible=True` and `method=direct` (FAST_MODE) or `method=remux` — **never**
  `ffmpeg re-encode ok`. Processing time is ~0s (direct) or a fraction of a
  second (remux):
  ```
  yt-dlp download: 5.1s -> <id>.mp4 (6.0 MB)
  ffprobe inspect: 0.05s (compatible=True, mp4=True, v=h264, a=aac)
  direct done: 0.0s
  Telegram upload: 1.8s (6.0 MB, method=direct)
  ```
- **sendVideo only — never sendDocument.** The video arrives as an inline,
  playable video, **not** a file attachment. With `SEND_VIDEO_AS_FILE_COPY=false`
  (default) there is exactly one upload; the logs show a single
  `Telegram upload` line for the video and no `sendDocument`.
- **Incompatible source with `NO_REENCODE_BY_DEFAULT=true`.** A VP9/AV1-only link
  shows `compatible=False` and the bot replies
  `هذا الفيديو يحتاج تحويل وقد يستغرق وقتاً طويلاً على الخطة المجانية. جرّب دقة أقل.`
  — it does **not** spend minutes re-encoding.
- **Optional re-encode fallback.** Only with `NO_REENCODE_BY_DEFAULT=false` does
  an incompatible source get `جاري ضغط الفيديو...` and
  `ffmpeg re-encode ok (height=... crf=30)`.

`FAST_MODE=false` swaps direct-send for a fast remux (still no re-encode) — handy
to compare `method=direct` vs `method=remux` timings.
