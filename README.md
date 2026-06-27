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
3. Tap **تحميل فيديو** → a quality menu (`اختر دقة الفيديو:`) appears; pick a
   resolution and the bot downloads and sends the file. Tap **تحميل صوت MP3**
   for an MP3 instead.

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

## 🎚️ Video quality selection

Tapping **تحميل فيديو** no longer downloads immediately. The bot first inspects
the link with `yt-dlp` (metadata only, `download=False`) and shows the
resolutions that link can actually provide:

| Button (Arabic / label)   | Behaviour                                                       |
| ------------------------- | -------------------------------------------------------------- |
| الجودة الأصلية / Full     | Best available — `bestvideo+bestaudio/best`, merged to mp4.     |
| 1080p                     | Best video up to 1080p + best audio.                           |
| 720p                      | Best video up to 720p + best audio.                            |
| 480p                      | Best video up to 480p + best audio.                            |
| 360p                      | Best video up to 360p + best audio.                            |
| أقل حجم / Small           | Smallest file — `worst[ext=mp4]/worst`.                        |

- A resolution button is shown **only** when that height (or a close lower
  format) is likely available.
- If resolution detection is limited (common on some **TikTok / Instagram**
  links), the bot falls back to a safe set: **Full / 720p / 480p / Small**.
- If `yt-dlp` cannot read formats at all, it says so in Arabic and still offers
  the safe set so you can try.
- Callback data is kept short (`v_full`, `v_1080`, `v_720`, `v_480`, `v_360`,
  `v_small`, `audio`) to stay within Telegram's callback-length limit.
- The 49 MB guard still applies: if the estimate looks large it warns first but
  still tries; if the **final** file exceeds 49 MB it is not sent and the bot
  replies `الملف أكبر من حد تلغرام للبوت. جرّب دقة أقل مثل 720p أو 480p.`
- The **audio (MP3)** flow is unchanged. One active download per chat still
  applies, and large `yt-dlp` info objects are never stored in memory.

The two helpers driving this are `get_available_quality_options(url)` and
`build_video_format_selector(quality)` in [main.py](main.py).

---

## 📱 Mobile / Telegram compatibility (normalization)

Some sources hand back codecs/containers that the Telegram player and phone
galleries handle poorly — **VP9 / AV1 / WebM**, 10-bit pixel formats, or badly
muxed MP4. That is what caused frozen-image-but-audio-plays videos and clips
that didn't appear correctly in the gallery after saving.

To fix this, **every video is re-encoded with ffmpeg before sending**, even when
yt-dlp already produced an mp4:

- Container **`.mp4`**, video **H.264 (libx264, profile main)**, audio **AAC 128k**
- Pixel format **`yuv420p`** and **`-movflags +faststart`** (instant playback / streaming)
- Selected height kept for **1080p/720p/480p/360p** (scaled down only, never upscaled);
  **Full** keeps the original resolution; a video with no audio still yields a valid MP4
- Format selection now **prefers mp4 / H.264 (`avc1`)** before falling back, so the
  raw file is already friendly when possible
- The raw yt-dlp file is **never sent directly**; if normalization fails the bot
  replies in Arabic instead of sending a broken video
- The final, normalized file is checked against the **49 MB** limit and sent with
  `sendVideo` (`supports_streaming=true`, `video/mp4`)

If **Full** quality is too large for Telegram, pick **720p** or **480p**. Long or
high-quality videos can still exceed the 49 MB bot limit even after normalization.

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
