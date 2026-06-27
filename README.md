# Telegram Video / Audio Downloader Bot

A Telegram bot that takes a **public** video link from **YouTube, TikTok, or
Instagram** and lets the user download it as **video (MP4)** or **audio (MP3)**.

Built for **Render Web Service** deployment using a **webhook** architecture
(FastAPI + Uvicorn + yt-dlp + ffmpeg). It does **not** use long polling.

```
User sends a link  ->  Bot shows two buttons  ->  [تحميل فيديو] / [تحميل صوت MP3]
                                                        |
                                          yt-dlp + ffmpeg download
                                                        |
                                     sendVideo / sendDocument / sendAudio
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
| `PUBLIC_URL`          | Optional | Public HTTPS base URL. Fallback for when `RENDER_EXTERNAL_URL` is missing.   |
| `DEBUG`               | Optional | `true` enables the debug-only `/webhook-info` endpoint. Default off.         |
| `RENDER_EXTERNAL_URL` | Auto     | Injected by Render automatically — **do not set manually**.                 |
| `PORT`                | Auto     | Injected by Render automatically — the app binds to it.                     |

On startup the app **validates** that `BOT_TOKEN`, `WEBHOOK_SECRET` (safe
characters only), and a public URL are all present. If any are missing or
invalid it logs the problem and **exits** instead of starting in a broken state.

The webhook is registered automatically on startup at:

```
{PUBLIC_URL or RENDER_EXTERNAL_URL}/webhook/{WEBHOOK_SECRET}
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

On startup the app reads `RENDER_EXTERNAL_URL`, builds the webhook URL, and calls
Telegram's `setWebhook` for you. No manual webhook step is needed.

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
3. Tap a button. The bot sends `⏳ جاري التحميل...` and then the file.

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
anything over **~49 MB** and replies with an Arabic "file too large" message.
Use a shorter clip or lower quality. (Raising this limit would require a local
Bot API server, which is out of scope here.)

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
