# Telegram Video / Audio Downloader Bot

A Telegram bot that takes a **public** video link from **YouTube, TikTok, or
Instagram** and lets the user download it as **video (MP4)** or **audio (MP3)**.

Built for **Render Web Service** deployment using a **webhook** architecture
(FastAPI + Uvicorn + yt-dlp + ffmpeg). It does **not** use long polling.

```
User sends a link  ->  [تحميل فيديو] / [تحميل صوت MP3]
                              |                  |
                   quality menu (Full /     bestaudio -> MP3
                   1080p/720p/480p/360p/         |
                   Small)                    sendAudio
                              |
                       yt-dlp download
                              |
            ffmpeg normalize -> MP4 (H.264 + AAC, yuv420p)
                              |
                         sendVideo
```

---

## ⚖️ Legal & responsible use

**Use this bot only for content you own or have explicit permission to
download.** Respect the Terms of Service of YouTube, TikTok, and Instagram, and
respect copyright law in your country.

This project intentionally does **not** include — and you should **not** add —
any cookie bypassing, DRM circumvention, login scraping, or downloading of
private/restricted content. It only handles public links that are accessible
without authentication.

---

## 📁 Project structure

| File               | Purpose                                            |
| ------------------ | -------------------------------------------------- |
| `main.py`          | FastAPI app, webhook endpoint, bot logic           |
| `requirements.txt` | Python dependencies                                |
| `Dockerfile`       | Container image (Python 3.11 + ffmpeg)             |
| `.env.example`     | Documented environment variables (placeholders)    |
| `render.yaml`      | Optional Render Blueprint for one-click deploy     |
| `TESTING.md`       | How to test locally, on Render, logs, and links    |
| `README.md`        | This file                                          |

---

## 1. Create a Telegram bot token (BotFather)

1. Open Telegram and chat with [@BotFather](https://t.me/BotFather).
2. Send `/newbot` and follow the prompts (choose a name and a username ending in
   `bot`).
3. BotFather replies with a **token** that looks like
   `123456789:AAH...`. Keep it secret — anyone with it controls your bot.
4. You will set this as the `BOT_TOKEN` environment variable.

Pick a **webhook secret** too (any long random string using only
`A-Z a-z 0-9 _ -`). For example:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

---

## 2. Required environment variables

| Variable              | Required | Description                                                                 |
| --------------------- | -------- | --------------------------------------------------------------------------- |
| `BOT_TOKEN`           | ✅ Yes   | Token from BotFather.                                                        |
| `WEBHOOK_SECRET`      | ✅ Yes   | Secret used in the webhook path and `secret_token` header.                  |
| `PUBLIC_URL`              | Optional | Public HTTPS base URL. Only needed **outside Render**, or if automatic hostname detection fails. Used as-is when set. |
| `DEBUG`                   | Optional | `true` enables the debug-only `/webhook-info` endpoint. Default off.         |
| `LOW_RESOURCE_MODE`       | Optional | `true` for small instances (Render Free 512 MB): hides 1080p, caps re-encode at 720p, serializes jobs. |
| `FAST_MODE`               | Optional | `true` sends a compatible MP4 directly (no ffmpeg); `false` fast-remuxes it. Big speed win on slow CPUs. |
| `NO_REENCODE_BY_DEFAULT`  | Optional | **Default `true`.** Never re-encode; incompatible videos ask for a lower quality instead of the slow re-encode. |
| `SEND_VIDEO_AS_FILE_COPY` | Optional | Default `false`. Keep `false` — the bot sends each video only once via `sendVideo`. |
| `RENDER_EXTERNAL_URL`     | Auto     | Injected by Render (when available) — **do not set manually**.              |
| `RENDER_EXTERNAL_HOSTNAME`| Auto     | Render's standard host var — the app builds `https://{hostname}` from it. **Do not set manually**. |
| `PORT`                    | Auto     | Injected by Render automatically — the app binds to it.                     |

**How the public URL is resolved** (first match wins):

1. `PUBLIC_URL` — used as-is.
2. `RENDER_EXTERNAL_URL` — used as-is.
3. `https://{RENDER_EXTERNAL_HOSTNAME}` — built automatically on Render.
4. Otherwise the app fails on startup with a clear error.

On Render you normally set **nothing** for the URL — `RENDER_EXTERNAL_HOSTNAME`
(or `RENDER_EXTERNAL_URL`) is detected automatically.

On startup the app **validates** that `BOT_TOKEN`, `WEBHOOK_SECRET` (safe
characters only), and a public URL are all present. If any are missing or
invalid it logs the problem and **exits** instead of starting in a broken state.

The webhook is registered automatically on startup at:

```
{resolved public URL}/webhook/{WEBHOOK_SECRET}
```

---

## 3. Deploy to Render (Docker)

### Option A — Blueprint (uses `render.yaml`)

1. Push this project to a GitHub/GitLab repository.
2. In the Render dashboard: **New → Blueprint**, and select your repo.
3. Render reads `render.yaml` and creates a Docker Web Service.
4. Set the secret env vars when prompted: `BOT_TOKEN` and `WEBHOOK_SECRET`.
5. Click **Apply** / **Create** and wait for the first deploy to finish.

### Option B — Manual Web Service

1. Push this project to a Git repository.
2. **New → Web Service**, select your repo.
3. **Runtime: Docker** (Render auto-detects the `Dockerfile`).
4. Add environment variables: `BOT_TOKEN`, `WEBHOOK_SECRET` (and `PUBLIC_URL`
   only if you are not on Render).
5. (Optional) Set **Health Check Path** to `/health`.
6. Create the service and wait for it to go live.

On startup the app detects its public URL automatically on Render (from
`RENDER_EXTERNAL_HOSTNAME`, or `RENDER_EXTERNAL_URL` if present), builds the
webhook URL, and calls Telegram's `setWebhook` for you. No manual webhook step
is needed.

### Run locally with Docker

```bash
docker build -t tg-downloader .
docker run --rm -p 10000:10000 \
  -e BOT_TOKEN=your_token \
  -e WEBHOOK_SECRET=your_secret \
  -e PUBLIC_URL=https://your-public-https-url \
  tg-downloader
```

`PUBLIC_URL` must be a real HTTPS URL reachable by Telegram (e.g. an ngrok
tunnel) for the webhook to work locally.

### Run locally without Docker

```bash
pip install -r requirements.txt
# ffmpeg must be installed and on your PATH
cp .env.example .env   # then fill in the values
uvicorn main:app --host 0.0.0.0 --port ${PORT:-10000}
```

---

## 4. How to test the webhook

After the service is live, verify Telegram is pointed at it. Replace
`<TOKEN>` with your bot token:

```bash
# Check the current webhook status (should show your /webhook/... URL,
# pending_update_count, and no last_error_message)
curl "https://api.telegram.org/bot<TOKEN>/getWebhookInfo"
```

Then test the bot itself:

1. Open your bot in Telegram and send `/start`.
   You should get the Arabic welcome message.
2. Send a public link, e.g. a YouTube URL.
   The bot replies with **تحميل فيديو** / **تحميل صوت MP3** buttons.
3. Tap **تحميل فيديو** → a menu of the **available** resolutions
   (`اختر دقة الفيديو المتاحة:`) appears; pick one and the bot sends the file.
   Tap **تحميل صوت MP3** for an MP3 instead.

### Service endpoints

| Method & path           | Purpose                                                              |
| ----------------------- | ------------------------------------------------------------------- |
| `GET /`                 | Root / wake-up. Returns `{"status":"ok"}`.                          |
| `GET /health`           | Health check. Returns `{"ok":true,"status":"healthy"}`.            |
| `GET /webhook-info`     | **Debug only** (`DEBUG=true`). Returns `getWebhookInfo`; secret masked, token never shown. `404` when DEBUG is off. |
| `POST /webhook/{secret}`| Telegram webhook receiver.                                          |

To re-register the webhook manually (rarely needed — it happens on every
startup):

```bash
curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://your-app.onrender.com/webhook/<WEBHOOK_SECRET>"
```

---

## 5. Common issues

### 1. Render free plan sleeps
On the **free** plan the service **spins down after ~15 minutes of inactivity**.
The first request after it sleeps wakes it up but is slow, and a Telegram update
that arrives while it is waking can be missed. For anything beyond casual testing,
**use a paid Render instance** — it stays always-on, which is strongly
recommended for production. This app intentionally contains **no self-ping or
keep-alive logic**; keeping the service awake is a hosting/plan concern, not the
app's job.

### 2. Telegram bot file-size limit
Bots can only **send files up to 50 MB** via the Bot API. This project refuses
anything over **~49 MB** (checked on the final, normalized file) and replies with
an Arabic message. If **Full** quality is too large, pick **720p** or **480p**;
long or high-quality videos can still exceed the limit even after re-encoding.
(Raising this limit would require a local Bot API server, which is out of scope.)

### 3. Private Instagram / TikTok links may fail
Only **public** content works. Private, age-restricted, region-locked, or
login-required videos will fail by design — this bot does not log in or bypass
restrictions. TikTok short links (`vm.tiktok.com`) are supported.

### 4. yt-dlp may need updates when platforms change
YouTube/TikTok/Instagram change their internals frequently, which can break
downloads until `yt-dlp` is updated. `requirements.txt` keeps `yt-dlp`
unpinned, so a **fresh deploy** (Manual Deploy → *Clear build cache & deploy* on
Render) pulls the latest version. If downloads suddenly start failing, redeploy.

---

## 🎚️ Video quality selection (only what's available)

Tapping **تحميل فيديو** does **not** download immediately. The bot inspects the
link with `yt-dlp` (`download=False`), reads the **real** formats, and shows
**only the resolutions that actually exist** for that link:

```
اختر دقة الفيديو المتاحة:
(الأسرع هو اختيار 480p أو 360p بدون ضغط)
```

- Possible buttons: **1080p · 720p · 480p · 360p · 240p · أقل حجم (Small)**. Each
  resolution appears **only if the source genuinely has it** (a button is shown
  only when there is a format `≤` that height *and* the source reaches it). No
  fake buttons, and there is no "Full/original" button.
- **Small** is always shown when any video exists (`worst[ext=mp4]/worst`).
- **1080p is hidden** when `LOW_RESOURCE_MODE=true`.
- If the formats can't be read (some TikTok/Instagram links), the bot says so in
  Arabic and offers a safe fallback set (720p / 480p / 360p / Small).
- Selectors prefer **progressive MP4 / H.264 (avc1)** with audio, then MP4 video
  + M4A audio, so most downloads come back already compatible.
- Callback data stays short: `v_1080`, `v_720`, `v_480`, `v_360`, `v_240`,
  `v_small`, `audio`.
- **Memory:** only a tiny per-chat map `{callback → selector}` is stored — never
  the large `yt-dlp` info object.
- The 49 MB guard still applies; if the final file is too large the bot replies
  `الملف أكبر من حد تلغرام للبوت. جرّب دقة أقل مثل 480p أو 360p.`

The helpers driving this are `analyze_video_qualities(url)` and
`_selector_for_height(h)` in [main.py](main.py).

---

## 📱 Mobile / Telegram compatibility

Some sources hand back codecs the Telegram player and phone galleries handle
poorly — **VP9 / AV1 / WebM**, 10-bit pixel formats. The bot keeps videos
compatible **without** paying for a full re-encode on every download:

- Selectors prefer **progressive MP4 / H.264 (avc1) + AAC**, so most downloads
  are already compatible.
- A compatible file is **sent directly** (FAST_MODE) or **fast-remuxed**
  (`-c copy -movflags +faststart`) — never re-encoded.
- An **incompatible** file is **not** re-encoded by default (`NO_REENCODE_BY_DEFAULT`):
  the bot asks for a lower quality instead, keeping Render Free fast and stable.
- If you set `NO_REENCODE_BY_DEFAULT=false`, the slow fallback re-encodes to
  H.264/AAC/`yuv420p` MP4 (`-preset ultrafast`, CRF 30; Small → 360p/CRF 32).
- Either way the file is checked against the **49 MB** limit and sent **once**
  with `sendVideo` — never also as a document.

Long or high-quality videos can still exceed 49 MB — pick **480p** or **360p**.

---

## ⚡ Speed: no compression by default

Full ffmpeg re-encoding is CPU-heavy and **slow on Render Free**, so the bot
**avoids it by default**. After download it inspects the file with **ffprobe**
and picks the fastest safe path:

1. **Compatible MP4** (`.mp4`, **H.264/avc1**, **AAC** or no audio, **yuv420p**):
   - `FAST_MODE=true` → **send directly** with no ffmpeg at all (fastest).
   - `FAST_MODE=false` → a fast **stream-copy remux**
     (`ffmpeg -i in -c copy -movflags +faststart out.mp4`, near-instant).
2. **Compatible streams in a non-MP4 container** → fast remux to MP4.
3. **Incompatible** (VP9 / AV1 / etc.) with **`NO_REENCODE_BY_DEFAULT=true`**
   (the default) → the bot does **not** compress; it replies
   `هذا الفيديو يحتاج تحويل وقد يستغرق وقتاً طويلاً على الخطة المجانية. جرّب دقة أقل.`
   Only when `NO_REENCODE_BY_DEFAULT=false` does it re-encode as a slow fallback.

Because the selectors prefer progressive MP4 / H.264, most YouTube links hit the
instant direct/remux path. The chosen path shows in the timing logs:

```
yt-dlp download: 6.4s -> <title>-<id>.mp4 (8.1 MB)
ffprobe inspect: 0.05s (compatible=True, mp4=True, v=h264, a=aac)
direct done: 0.0s
Telegram upload: 2.1s (8.1 MB, method=direct)
```

The video is sent **once** via `sendVideo` (`supports_streaming=true`,
`video/mp4`, caption `تم تحميل الفيديو.`) — never also as a document.

**Render Free is slow for video conversion.** Prefer **480p** or **360p** there;
they download and upload fastest and stay well under the 49 MB limit.

User-facing flow (Arabic): `جاري تجهيز الفيديو...` → `جاري إرسال الفيديو...`
(direct/remux) or `جاري ضغط الفيديو ليتوافق مع تلغرام...` (only if re-encode is
enabled). Choosing 720p in low-resource mode also shows
`قد يستغرق تحميل 720p وقتاً أطول على الخطة المجانية.`

---

## 🪫 Low-resource mode (Render Free 512 MB)

On a 512 MB instance, heavy ffmpeg work can crash the worker. The bot already
avoids re-encoding by default; **`LOW_RESOURCE_MODE=true`** adds extra guards:

- **1080p is hidden** from the quality menu. If a `v_1080` callback is somehow
  received, the bot replies suggesting 720p/480p.
- **One media job at a time, server-wide.** A second user gets
  `السيرفر يعالج طلباً آخر حالياً، حاول بعد قليل.` until the current job finishes
  (the per-chat lock still applies too).
- Choosing **720p** shows `قد يستغرق تحميل 720p وقتاً أطول على الخطة المجانية.`
- If a re-encode ever runs (`NO_REENCODE_BY_DEFAULT=false`), its height is
  **capped at 720p**, single-thread, `-preset ultrafast`.
- **yt-dlp** runs with `cachedir=False`, `retries=2`, `socket_timeout=30`, and
  `concurrent_fragment_downloads=1`. The video is sent once via `sendVideo`.

The Docker image already starts uvicorn with a single worker and a small
concurrency cap (`--workers 1 --limit-concurrency 4`) to bound memory.

### Want 1080p (and incompatible-source support)?

Run on something with more RAM and leave `LOW_RESOURCE_MODE` unset/`false` (and
optionally `NO_REENCODE_BY_DEFAULT=false` to allow the re-encode fallback):

- A small VPS such as **Oracle Cloud Always Free (Ampere A1)** — generous free
  RAM/CPU, good for heavier encoding.
- A **paid Render instance** (more RAM, always-on).

There, 1080p shows whenever the link has it, and incompatible sources can be
re-encoded instead of refused.

---

## 🧹 Temporary storage and cleanup

Nothing downloaded is ever kept on the server. Each request is fully
self-contained and its files are deleted as soon as the Telegram upload returns.

- **One folder per job.** Every download creates its own unique temp folder via
  `tempfile.mkdtemp(prefix="tg_downloader_{chat_id}_")`.
- **Everything stays inside it.** yt-dlp output (`outtmpl`), `.part`/fragments,
  the normalized `normalized_output.mp4`, and the MP3 conversion are all written
  **only** inside that folder — never the project root or any persistent path.
- **Always deleted.** A `finally` block calls
  `cleanup_workdir(workdir, chat_id)` → `shutil.rmtree(workdir, ignore_errors=True)`,
  so the folder is removed whether the download succeeds, the download fails,
  ffmpeg fails, the Telegram send fails, the file is over 49 MB, or the link was
  bad. The file is deleted **only after** the upload request returns (never
  before), and a line like `Cleaned temp folder for chat_id=...` is logged.
- **Startup sweep.** On boot, `cleanup_stale_temp_dirs()` removes any
  `tg_downloader_*` folders older than 1 hour that a previous crash/restart may
  have left in the system temp directory.
- **No secrets in logs** — the bot token, webhook secret, and full private URLs
  are never logged.

This matters on Render because the container's disk is **ephemeral but shared
across requests** while the instance is alive — leftover media would otherwise
accumulate until the next redeploy.

---

## Notes on architecture

- **Webhook, not polling.** `run_polling()` is intentionally not used.
- **Fast webhook response.** The endpoint validates the secret, schedules the
  download via FastAPI `BackgroundTasks`, and returns `200` immediately so
  Telegram never times out.
- **Per-download temp folders.** Each job uses its own `tempfile` directory that
  is always deleted afterwards, on success or failure.
- **Token safety.** The bot token is never written to logs.
- **In-memory URL store.** The last URL per chat is kept in memory and cleared on
  restart; this is fine for the simple "send link → pick format" flow.

For high traffic you would replace `BackgroundTasks` with a real task queue
(e.g. Celery/RQ) and a worker process, but that is beyond this project's scope.
