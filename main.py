"""
Telegram video / audio downloader bot — webhook architecture for Render Web Service.

The user sends a public video URL (YouTube / TikTok / Instagram) and the bot
replies with two inline buttons:
    1. تحميل فيديو      -> choose a video quality, then download (mp4 preferred)
    2. تحميل صوت MP3     -> extract the audio and convert it to MP3

This app is built for WEBHOOK deployment (NOT long polling), so it runs cleanly
as a Render Web Service behind FastAPI + Uvicorn. The webhook handler returns to
Telegram immediately and performs the slow download work in a background task so
Telegram never times out.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import threading
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests
import yt_dlp
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from yt_dlp.utils import DownloadError

# Load a local .env file when running outside Render (no-op if python-dotenv or
# the file is missing). On Render the variables come from the dashboard instead.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - optional dependency
    pass


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

BOT_TOKEN: str = os.environ.get("BOT_TOKEN", "").strip()
WEBHOOK_SECRET: str = os.environ.get("WEBHOOK_SECRET", "").strip()

# Turns on the debug-only /webhook-info endpoint. Off unless explicitly enabled.
DEBUG: bool = os.environ.get("DEBUG", "").strip().lower() in ("1", "true", "yes", "on")

# Telegram Bot API practical upload limit for bots is 50 MB; stay just under it.
MAX_FILE_SIZE: int = 49 * 1024 * 1024  # 49 MB in bytes

# Base URL for every Telegram Bot API call. The token lives here, so this value
# (and any URL derived from it) must never be written to the logs.
TELEGRAM_API: str = f"https://api.telegram.org/bot{BOT_TOKEN}"

# The URL host must equal one of these or be a subdomain of it. "vm.tiktok.com"
# is covered by the ".tiktok.com" suffix check below.
ALLOWED_DOMAINS = (
    "youtube.com",
    "youtu.be",
    "instagram.com",
    "tiktok.com",
)

# Telegram's secret_token header (and our URL secret) may only contain these.
_SECRET_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")

# First http(s) link inside a text message.
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

# Video quality keys -> label shown on the inline button. callback_data is the
# key prefixed with "v_" (kept short for Telegram's callback length limit).
QUALITY_LABELS: Dict[str, str] = {
    "full": "الجودة الأصلية / Full",
    "1080": "1080p",
    "720": "720p",
    "480": "480p",
    "360": "360p",
    "small": "أقل حجم / Small",
}
# Order the buttons are shown in.
QUALITY_ORDER: List[str] = ["full", "1080", "720", "480", "360", "small"]
# Safe options when resolution detection is limited (some TikTok/Instagram links).
FALLBACK_QUALITIES: List[str] = ["full", "720", "480", "small"]
# callback_data ("v_full" ...) -> quality key ("full" ...).
VIDEO_QUALITY_ACTIONS: Dict[str, str] = {f"v_{key}": key for key in QUALITY_LABELS}

# Per-chat memory of the last URL the user sent. In-memory only: cleared on
# every restart / redeploy, which is fine for this simple flow.
last_url_by_chat: Dict[int, str] = {}

# Chats that currently have a download in progress (one active download per
# chat). Guarded by a lock because downloads run in a threadpool.
active_downloads: set[int] = set()
_downloads_lock = threading.Lock()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("downloader-bot")


# --------------------------------------------------------------------------- #
# Arabic user-facing messages (kept together for easy editing)
# --------------------------------------------------------------------------- #

MSG_WELCOME = (
    "أرسل رابط فيديو من YouTube أو TikTok أو Instagram، "
    "وبعدها اختار فيديو أو صوت MP3."
)
MSG_NOT_SUPPORTED = (
    "❌ الرابط غير مدعوم.\n"
    "أرسل رابطًا عامًا من YouTube أو TikTok أو Instagram فقط."
)
MSG_CHOOSE = "تم استلام الرابط ✅\nاختر نوع التحميل 👇"
MSG_NO_URL = "أرسل الرابط أولًا، ثم اختر فيديو أو صوت MP3."
MSG_DOWNLOADING = "⏳ جاري التحميل، قد يستغرق ذلك بعض الوقت..."
MSG_BUSY = "يوجد تحميل قيد التنفيذ حالياً، انتظر حتى ينتهي."
MSG_VIDEO_QUALITY_MENU = "اختر دقة الفيديو:"
MSG_DOWNLOADING_QUALITY = "جاري تحميل الفيديو بالدقة المختارة..."
MSG_QUALITY_UNAVAILABLE = "هذه الدقة غير متاحة لهذا الرابط. جرّب دقة أخرى."
MSG_FORMATS_UNREADABLE = "لم أستطع قراءة الدقات المتاحة، سأعرض خيارات آمنة للتجربة."
MSG_MAYBE_TOO_LARGE = (
    "⚠️ الحجم المتوقع كبير وقد يتجاوز حد تلغرام (حوالي 50 ميجابايت)، "
    "سأحاول التحميل على أي حال."
)
MSG_TOO_LARGE = (
    "⚠️ حجم الملف أكبر من الحد المسموح به في تيليجرام (حوالي 50 ميجابايت)، "
    "لذلك لا يمكن إرساله.\nجرّب فيديو أقصر أو جودة أقل."
)
MSG_TOO_LARGE_TRY_LOWER = (
    "الملف أكبر من حد تلغرام للبوت. جرّب دقة أقل مثل 720p أو 480p."
)
MSG_ERROR = (
    "حدث خطأ أثناء المعالجة.\n"
    "تأكد أن الرابط عام وصحيح ثم حاول مرة أخرى."
)


# --------------------------------------------------------------------------- #
# Telegram Bot API helpers (direct HTTP via requests)
# --------------------------------------------------------------------------- #

def _api(method: str, *, timeout: int = 30, **kwargs: Any) -> Optional[dict]:
    """Call a Telegram Bot API method.

    Returns the parsed JSON response, or None on a transport error. The bot
    token is part of TELEGRAM_API and is deliberately never logged.
    """
    try:
        resp = requests.post(f"{TELEGRAM_API}/{method}", timeout=timeout, **kwargs)
        data = resp.json()
        if not data.get("ok"):
            logger.warning("Telegram %s failed: %s", method, data.get("description"))
        return data
    except Exception as exc:  # noqa: BLE001 - log and keep the bot alive
        logger.warning("Telegram %s request error: %s", method, exc)
        return None


def send_message(
    chat_id: int, text: str, reply_markup: Optional[dict] = None
) -> None:
    payload: Dict[str, Any] = {"chat_id": chat_id, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    _api("sendMessage", json=payload)


def answer_callback(callback_query_id: str, text: Optional[str] = None) -> None:
    payload: Dict[str, Any] = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    _api("answerCallbackQuery", json=payload)


def send_video(chat_id: int, path: str) -> None:
    with open(path, "rb") as fh:
        _api(
            "sendVideo",
            data={"chat_id": str(chat_id), "supports_streaming": "true"},
            files={"video": fh},
            timeout=600,
        )


def send_document(chat_id: int, path: str) -> None:
    with open(path, "rb") as fh:
        _api(
            "sendDocument",
            data={"chat_id": str(chat_id)},
            files={"document": fh},
            timeout=600,
        )


def send_audio(chat_id: int, path: str) -> None:
    with open(path, "rb") as fh:
        _api(
            "sendAudio",
            data={"chat_id": str(chat_id)},
            files={"audio": fh},
            timeout=600,
        )


# --------------------------------------------------------------------------- #
# Configuration validation + webhook registration
# --------------------------------------------------------------------------- #

def public_base_url() -> str:
    """Public HTTPS base URL of this service, resolved in priority order:

    1. PUBLIC_URL                      -> used as-is (override / non-Render / tunnels)
    2. RENDER_EXTERNAL_URL             -> used as-is (full URL, if Render provides it)
    3. https://{RENDER_EXTERNAL_HOSTNAME}  -> built from Render's default host var

    Returns "" when none are set; validate_config() turns that into a clear error.
    """
    public_url = os.environ.get("PUBLIC_URL", "").strip()
    if public_url:
        return public_url.rstrip("/")

    external_url = os.environ.get("RENDER_EXTERNAL_URL", "").strip()
    if external_url:
        return external_url.rstrip("/")

    hostname = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "").strip()
    if hostname:
        return f"https://{hostname}".rstrip("/")

    return ""


def validate_config() -> None:
    """Fail fast (and clearly) when required configuration is missing/invalid."""
    problems = []
    if not BOT_TOKEN:
        problems.append("BOT_TOKEN is missing.")
    if not WEBHOOK_SECRET:
        problems.append("WEBHOOK_SECRET is missing.")
    elif not _SECRET_TOKEN_RE.match(WEBHOOK_SECRET):
        problems.append(
            "WEBHOOK_SECRET must contain only these characters: A-Z a-z 0-9 _ -"
        )
    if not public_base_url():
        problems.append(
            "No public URL found. Set PUBLIC_URL, or run on Render where "
            "RENDER_EXTERNAL_URL / RENDER_EXTERNAL_HOSTNAME is provided."
        )

    if problems:
        message = (
            "Invalid configuration — fix these environment variables:\n  - "
            + "\n  - ".join(problems)
        )
        logger.error(message)
        raise RuntimeError(message)


def register_webhook() -> None:
    """Register the Telegram webhook at {base}/webhook/{WEBHOOK_SECRET}."""
    base = public_base_url()
    if not base:
        logger.warning(
            "No RENDER_EXTERNAL_URL or PUBLIC_URL set; skipping webhook registration."
        )
        return

    url = f"{base}/webhook/{WEBHOOK_SECRET}"
    payload: Dict[str, Any] = {
        "url": url,
        "allowed_updates": ["message", "callback_query"],
        "drop_pending_updates": True,
    }
    # Extra defense in depth: Telegram echoes this back in a request header.
    if _SECRET_TOKEN_RE.match(WEBHOOK_SECRET):
        payload["secret_token"] = WEBHOOK_SECRET

    data = _api("setWebhook", json=payload)
    if data and data.get("ok"):
        logger.info("Webhook registered at %s/webhook/***", base)
    else:
        logger.error("Failed to register webhook (see message above).")


# --------------------------------------------------------------------------- #
# URL validation
# --------------------------------------------------------------------------- #

def extract_url(text: str) -> Optional[str]:
    """Return the first http(s) URL found in a text message, if any."""
    match = _URL_RE.search(text or "")
    if not match:
        return None
    # Trim trailing punctuation that often gets glued to a pasted link.
    return match.group(0).rstrip(").,!؛،")


def is_allowed(url: str) -> bool:
    """True only for public http(s) links on the allowed platforms."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return any(host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS)


def download_keyboard() -> dict:
    """First-level menu: pick video (opens quality menu) or audio."""
    return {
        "inline_keyboard": [
            [
                {"text": "تحميل فيديو", "callback_data": "dl_video"},
                {"text": "تحميل صوت MP3", "callback_data": "audio"},
            ]
        ]
    }


def quality_keyboard(options: List[str]) -> dict:
    """Build the quality inline keyboard (two buttons per row)."""
    rows: List[List[dict]] = []
    row: List[dict] = []
    for key in options:
        row.append({"text": QUALITY_LABELS[key], "callback_data": f"v_{key}"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return {"inline_keyboard": rows}


# --------------------------------------------------------------------------- #
# Quality detection + format selection
# --------------------------------------------------------------------------- #

def get_available_quality_options(url: str) -> Optional[List[str]]:
    """Quality keys to offer for a URL.

    Returns:
        None  -> yt-dlp could not read the formats at all (caller shows a notice).
        list  -> quality keys to show. May be the safe FALLBACK_QUALITIES when
                 resolution info is limited (e.g. some TikTok/Instagram links).

    Only resolutions that the source can actually provide (that height or a
    close lower format) are included. We never store the (large) info object.
    """
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # noqa: BLE001 - any failure -> safe fallback path
        logger.warning("Quality analysis failed: %s", exc)
        return None

    if not info:
        return None

    heights = set()
    for fmt in info.get("formats") or []:
        height = fmt.get("height")
        if height and fmt.get("vcodec") not in (None, "none"):
            heights.add(int(height))
    if not heights and info.get("height"):
        heights.add(int(info["height"]))

    if not heights:
        # Formats were readable but carried no resolution info -> safe fallback.
        return list(FALLBACK_QUALITIES)

    max_height = max(heights)
    options = ["full"]
    for res in (1080, 720, 480, 360):
        # Show the cap if the source is at least that tall, or has a close
        # lower format (within ~10%) that the selector would fall back to.
        if max_height >= res or any(h >= res * 0.9 for h in heights):
            options.append(str(res))
    options.append("small")
    return options


def build_video_format_selector(quality: str) -> str:
    """Map a quality key to a yt-dlp format selector string."""
    selectors = {
        "full": "bestvideo+bestaudio/best",
        "1080": (
            "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/"
            "best[height<=1080][ext=mp4]/best[height<=1080]"
        ),
        "720": (
            "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/"
            "best[height<=720][ext=mp4]/best[height<=720]"
        ),
        "480": (
            "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/"
            "best[height<=480][ext=mp4]/best[height<=480]"
        ),
        "360": (
            "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/"
            "best[height<=360][ext=mp4]/best[height<=360]"
        ),
        "small": "worst[ext=mp4]/worst",
    }
    return selectors.get(quality, selectors["720"])


def estimate_video_size(url: str, quality: str) -> Optional[int]:
    """Best-effort estimated size (bytes) for the chosen quality, or None.

    Uses filesize / filesize_approx from yt-dlp without downloading. The info
    object is discarded immediately and never stored.
    """
    opts = {
        "format": build_video_format_selector(quality),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Size estimate failed: %s", exc)
        return None
    if not info:
        return None

    requested = info.get("requested_formats")
    if requested:  # merged video + audio
        total = 0
        for fmt in requested:
            size = fmt.get("filesize") or fmt.get("filesize_approx")
            if size:
                total += size
        return total or None
    return info.get("filesize") or info.get("filesize_approx")


# --------------------------------------------------------------------------- #
# Concurrency control — one active download per chat
# --------------------------------------------------------------------------- #

def try_begin_download(chat_id: int) -> bool:
    """Reserve the single download slot for a chat. False if one is running."""
    with _downloads_lock:
        if chat_id in active_downloads:
            return False
        active_downloads.add(chat_id)
        return True


def end_download(chat_id: int) -> None:
    with _downloads_lock:
        active_downloads.discard(chat_id)


# --------------------------------------------------------------------------- #
# Downloading (yt-dlp + ffmpeg)
# --------------------------------------------------------------------------- #

def _largest_file(folder: str) -> Optional[str]:
    """Return the largest regular file in a folder (the media output)."""
    candidates = [
        os.path.join(folder, name)
        for name in os.listdir(folder)
        if os.path.isfile(os.path.join(folder, name))
    ]
    if not candidates:
        return None
    return max(candidates, key=os.path.getsize)


def download_video(url: str, folder: str, quality: str = "720") -> Optional[str]:
    """Download the video into `folder` using the selector for `quality`."""
    opts: Dict[str, Any] = {
        "format": build_video_format_selector(quality),
        "outtmpl": os.path.join(folder, "%(id)s.%(ext)s"),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    return _largest_file(folder)


def download_audio(url: str, folder: str) -> Optional[str]:
    """Download the best audio and convert it to MP3 via ffmpeg."""
    opts: Dict[str, Any] = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(folder, "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    # The post-processor leaves a single .mp3 behind.
    for name in os.listdir(folder):
        if name.lower().endswith(".mp3"):
            return os.path.join(folder, name)
    return _largest_file(folder)


# --------------------------------------------------------------------------- #
# Request handlers (run inside a background task / threadpool)
# --------------------------------------------------------------------------- #

def show_quality_menu(chat_id: int, url: str) -> None:
    """Analyze the URL and present the available quality options."""
    options = get_available_quality_options(url)
    if options is None:
        # Could not read formats at all: tell the user and offer safe options.
        send_message(chat_id, MSG_FORMATS_UNREADABLE)
        options = list(FALLBACK_QUALITIES)
    send_message(
        chat_id, MSG_VIDEO_QUALITY_MENU, reply_markup=quality_keyboard(options)
    )


def handle_video_request(chat_id: int, url: str, quality: str) -> None:
    folder = tempfile.mkdtemp(prefix="dlbot_")
    try:
        # Best-effort pre-check: warn (but still proceed) on a large estimate,
        # since estimates are often wrong — especially for "Full".
        estimate = estimate_video_size(url, quality)
        if estimate and estimate > MAX_FILE_SIZE:
            send_message(chat_id, MSG_MAYBE_TOO_LARGE)

        try:
            path = download_video(url, folder, quality)
        except DownloadError as exc:
            text = str(exc).lower()
            if "requested format" in text or "not available" in text or "no video" in text:
                send_message(chat_id, MSG_QUALITY_UNAVAILABLE)
            else:
                logger.warning("Video download error: %s", exc)
                send_message(chat_id, MSG_ERROR)
            return

        if not path or not os.path.exists(path):
            send_message(chat_id, MSG_QUALITY_UNAVAILABLE)
            return
        if os.path.getsize(path) > MAX_FILE_SIZE:
            send_message(chat_id, MSG_TOO_LARGE_TRY_LOWER)
            return
        if path.lower().endswith(".mp4"):
            send_video(chat_id, path)
        else:
            send_document(chat_id, path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Video download failed: %s", exc)
        send_message(chat_id, MSG_ERROR)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def handle_audio_request(chat_id: int, url: str) -> None:
    folder = tempfile.mkdtemp(prefix="dlbot_")
    try:
        path = download_audio(url, folder)
        if not path or not os.path.exists(path):
            send_message(chat_id, MSG_ERROR)
            return
        if os.path.getsize(path) > MAX_FILE_SIZE:
            send_message(chat_id, MSG_TOO_LARGE)
            return
        send_audio(chat_id, path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Audio download failed: %s", exc)
        send_message(chat_id, MSG_ERROR)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def handle_message(message: dict) -> None:
    chat_id = message["chat"]["id"]
    text = (message.get("text") or "").strip()
    if not text:
        return

    if text.startswith("/start"):
        send_message(chat_id, MSG_WELCOME)
        return

    url = extract_url(text)
    if not url or not is_allowed(url):
        send_message(chat_id, MSG_NOT_SUPPORTED)
        return

    last_url_by_chat[chat_id] = url
    send_message(chat_id, MSG_CHOOSE, reply_markup=download_keyboard())


def _run_guarded_download(chat_id: int, status_msg: str, work) -> None:
    """Enforce one active download per chat around a download callable."""
    if not try_begin_download(chat_id):
        send_message(chat_id, MSG_BUSY)
        return
    try:
        send_message(chat_id, status_msg)
        work()
    finally:
        end_download(chat_id)


def handle_callback(callback: dict) -> None:
    answer_callback(callback["id"])  # stop the button's loading spinner

    action = callback.get("data")
    message = callback.get("message") or {}
    chat_id = message.get("chat", {}).get("id")
    if chat_id is None:
        return

    url = last_url_by_chat.get(chat_id)
    if not url:
        send_message(chat_id, MSG_NO_URL)
        return

    # Step 1: "تحميل فيديو" -> analyze and show the quality menu (no download yet).
    if action == "dl_video":
        show_quality_menu(chat_id, url)
        return

    # Step 2: a concrete video quality -> guarded download.
    if action in VIDEO_QUALITY_ACTIONS:
        quality = VIDEO_QUALITY_ACTIONS[action]
        _run_guarded_download(
            chat_id,
            MSG_DOWNLOADING_QUALITY,
            lambda: handle_video_request(chat_id, url, quality),
        )
        return

    # Audio path is unchanged.
    if action == "audio":
        _run_guarded_download(
            chat_id,
            MSG_DOWNLOADING,
            lambda: handle_audio_request(chat_id, url),
        )
        return
    # Unknown callback -> ignore.


def process_update(update: dict) -> None:
    """Entry point for a single Telegram update (runs in the background)."""
    try:
        if "message" in update:
            handle_message(update["message"])
        elif "callback_query" in update:
            handle_callback(update["callback_query"])
    except Exception as exc:  # noqa: BLE001 - never let one update kill the worker
        logger.warning("process_update error: %s", exc)


# --------------------------------------------------------------------------- #
# FastAPI application
# --------------------------------------------------------------------------- #

@asynccontextmanager
async def lifespan(_: FastAPI):
    # Fail fast on bad configuration, then point Telegram at this service.
    validate_config()
    register_webhook()
    yield


app = FastAPI(title="Telegram Downloader Bot", lifespan=lifespan)


@app.get("/")
def root() -> dict:
    """Root / wake-up endpoint."""
    return {"status": "ok"}


@app.get("/health")
def health() -> dict:
    """Simple health check endpoint."""
    return {"ok": True, "status": "healthy"}


@app.get("/webhook-info")
def webhook_info() -> dict:
    """Debug-only: return Telegram getWebhookInfo (enabled only when DEBUG=true).

    The bot token is never part of this response; the webhook secret embedded in
    the returned URL is masked before sending it back.
    """
    if not DEBUG:
        raise HTTPException(status_code=404, detail="not found")
    data = _api("getWebhookInfo")
    if not data:
        raise HTTPException(status_code=502, detail="telegram api unreachable")
    result = data.get("result", {})
    if WEBHOOK_SECRET and isinstance(result.get("url"), str):
        result["url"] = result["url"].replace(WEBHOOK_SECRET, "***")
    return {"ok": bool(data.get("ok")), "result": result}


@app.post("/webhook/{secret}")
async def telegram_webhook(
    secret: str, request: Request, background_tasks: BackgroundTasks
) -> dict:
    # Gate 1: the secret embedded in the path must match.
    if not WEBHOOK_SECRET or secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")

    # Gate 2: if Telegram sent the secret_token header, it must match too.
    header_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if header_secret is not None and header_secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")

    update = await request.json()
    # Return to Telegram immediately; do the slow download work afterwards.
    background_tasks.add_task(process_update, update)
    return {"ok": True}
