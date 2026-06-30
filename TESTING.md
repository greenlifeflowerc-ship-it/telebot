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
4. Pick a quality → `جاري تحميل الفيديو بالدقة الأصلية...`, then (for non-MP4)
   `جاري تحويل الصيغة بسرعة بدون ضغط...`, then `جاري إرسال الفيديو...`, then the file.
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
2. The bot replies `اختر دقة الفيديو المتاحة:` with a full-width
   **الجودة الأصلية** button on top, then **only the resolutions that exist**
   (1080p · 720p · 480p · 360p · 240p · أقل حجم / Small).

What to verify:

| Action | Expected |
| ------ | -------- |
| Send a **YouTube** link | Menu shows **الجودة الأصلية** + the resolutions YouTube actually offers. No fake buttons. |
| Send a **TikTok / Instagram** link | **الجودة الأصلية** + only the resolution(s) that exist (often just Small). |
| Tap **الجودة الأصلية** | Downloads best original quality; logs show `method=direct` or `method=remux` — **never** a re-encode. |
| Tap **480p** / **360p** | Selects the closest real source format (no scaling, no re-encode). |
| A picked quality not directly available | `هذه الدقة غير متاحة مباشرة من المصدر.` |
| Link whose formats can't be read | `لم أستطع قراءة الدقات المتاحة...` then a safe fallback (Original/720p/480p/360p/Small). |
| Link with no video | `لا يوجد فيديو متاح لهذا الرابط...` |
| **Original** file > 49 MB | `الفيديو بالدقة الأصلية أكبر من حد تلغرام للبوت...` — **rejected, not compressed**. |

Tip: a long 4K/1080p YouTube video is an easy way to trigger the > 49 MB path;
a short clip is an easy way to confirm a successful send.

---

## 6. Verify NO compression — original quality + fast remux

The bot must never compress/re-encode video; it downloads the original and only
stream-copies the container to MP4.

What to verify:
- The received video is sent via **`sendVideo`** (inline player), once, never as
  a document.
- **Original quality is preserved.** Download the sent file and compare its
  resolution/bitrate to the source — they match (no downscale, no quality loss).
- **Logs confirm `-c copy`, no `libx264`.** A non-MP4 source logs
  `remux ok (stream copy, -c copy, no re-encode)`; you must **never** see
  `ffmpeg re-encode ok` while `NO_VIDEO_COMPRESSION=true`. Confirm with ffprobe
  that the sent video's codec equals the source's (e.g. h264 stays h264).
- **Remux is fast** — a fraction of a second (vs many seconds/minutes for a
  re-encode).

Diagnostic trail of one video request (no secrets are logged):

```
Video request: chat=... quality=v_original no_compression=True fast=True
yt-dlp download: 6.4s -> <title>-<id>.mp4 (8.1 MB)
direct: 0.0s                       # already MP4 + FAST_MODE -> sent as-is
Final file: <title>-<id>.mp4 (8.1 MB) method=direct
Telegram upload: 2.1s (8.1 MB, method=direct)
Cleaned temp folder for chat_id=...
```

For a non-MP4 (WebM/MKV) source the middle line becomes
`remux ok (stream copy, -c copy, no re-encode)` then `remux: 0.Xs`. If the remux
genuinely fails the bot replies `تعذر تحويل صيغة الفيديو بدون ضغط...` and does
**not** compress.

> Local note: remux needs **ffmpeg on your PATH** when running without Docker.
> On Render it is already in the image.

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

## 8. Verify no-compression speed + large-file rejection

Startup logs the flags, e.g.
`Flags: NO_VIDEO_COMPRESSION=True FAST_MODE=True NO_REENCODE_BY_DEFAULT=True SEND_VIDEO_AS_FILE_COPY=False`.

- **Original downloads without compression.** Tap **الجودة الأصلية** on a
  YouTube link. Logs show `method=direct` (already MP4 + FAST_MODE) or
  `method=remux` — **never** `ffmpeg re-encode ok`. The sent file's resolution
  matches the source.
- **`-c copy` only / no `libx264`.** For a non-MP4 source the log line
  `remux ok (stream copy, -c copy, no re-encode)` appears. Grepping logs for
  `libx264` or `re-encode` returns nothing while `NO_VIDEO_COMPRESSION=true`.
- **Remux is fast.** `direct: 0.0s` or `remux: 0.Xs` — not minutes.
- **sendVideo only — never sendDocument.** Exactly one `Telegram upload` line per
  video; no `sendDocument` (unless `SEND_VIDEO_AS_FILE_COPY=true`).
- **Large originals are rejected, not compressed.** Pick **الجودة الأصلية** on a
  long 1080p/4K video so the file exceeds 49 MB. The bot replies
  `الفيديو بالدقة الأصلية أكبر من حد تلغرام للبوت...` and sends nothing — it does
  **not** re-encode to shrink it. Then pick **480p/360p** and confirm it sends.
- **Temp cleanup still happens** after every outcome (see section 7) — the log
  ends with `Cleaned temp folder for chat_id=...`.

The only opt-in re-encode path requires **both** `NO_VIDEO_COMPRESSION=false`
**and** `NO_REENCODE_BY_DEFAULT=false`; only then can `ffmpeg re-encode ok`
appear (after `جاري ضغط الفيديو...`).

`FAST_MODE=false` swaps direct-send for a stream-copy remux (still no re-encode)
— handy to compare `method=direct` vs `method=remux` timings.

---

## 9. Verify "download-disabled" public videos + locked-content messages

The bot should download PUBLIC videos even when the platform's in-app download
button is OFF, and clearly explain genuinely-locked content (without bypassing).

Setup: keep `IMPERSONATE=true` and make sure `curl_cffi` installed (it's in
`requirements.txt`). On startup, impersonation is best-effort — if `curl_cffi`
is missing the bot still runs, just without it.

What to verify:

| Test | Expected |
| ---- | -------- |
| A **public** TikTok with "Allow download" turned OFF | Downloads normally (the toggle is not an access lock). Logs show `method=direct`/`remux`. |
| A **public** Instagram reel (no native download button) | Downloads; if it intermittently rate-limits, the retries help; persistent failure → `…rate limit…` message. |
| A **public** YouTube video | Downloads. (No per-video toggle blocks yt-dlp for public videos.) |
| Anti-bot was 403'ing it | With `IMPERSONATE=true` it now succeeds; with `IMPERSONATE=false` you may see the temporary-failure message. |
| A **private** account video | `هذا المحتوى خاص أو يتطلب تسجيل دخول…` — **not** bypassed. |
| YouTube "Sign in to confirm you're not a bot" | `المنصّة تطلب تسجيل دخول للتأكد أنك لست روبوتاً…` — **not** bypassed. |
| Age-restricted / members-only / DRM / geo-blocked | The matching Arabic reason; never bypassed. |
| A flaky/temporary failure | `تعذّر التنزيل مؤقتاً… أعد المحاولة` (suggests retry / updating yt-dlp). |

Confirm in logs that **no cookies/login/DRM/geo bypass** is ever used — only
`impersonate` (browser fingerprint) and retries appear. The classifier routing
is covered by mirrored unit checks during development.
