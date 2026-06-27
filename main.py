"""
Telegram video / audio downloader bot — webhook architecture for Render Web Service.

The user sends a public video URL (YouTube / TikTok / Instagram) and the bot
replies with two inline buttons:
    1. تحميل فيديو      -> choose a video quality, then download
    2. تحميل صوت MP3     -> extract the audio and convert it to MP3

Every video is re-encoded with ffmpeg to a Telegram/mobile-compatible MP4
(H.264 + AAC, yuv420p, +faststart) before sending, so it plays correctly in the
Telegram player and saves correctly to the phone gallery.

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
import subprocess
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
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
# Target output height for the normalize step (None = keep original resolution).
QUALITY_TARGET_HEIGHTS: Dict[str, int] = {"1080": 1080, "720": 720, "480": 480, "360": 360}

# Prefix for every per-job temp folder; matched by the startup stale-folder sweep.
TEMP_PREFIX = "tg_downloader_"

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


def _mb(num_bytes: int) -> str:
    """Human-friendly MB string for logs."""
    return f"{num_bytes / (1024 * 1024):.1f} MB"


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
MSG_VIDEO_DONE = "تم تحميل الفيديو."
MSG_VIDEO_PROCESS_FAILED = (
    "تعذّرت معالجة الفيديو لجعله متوافقًا مع تيليجرام. جرّب دقة أخرى أو رابطًا آخر."
)
MSG_TOO_LARGE = (
    "⚠️ حجم الملف أكبر من الحد المسموح به في تيليجرام (حوالي 50 ميجابايت)، "
    "لذلك لا يمكن إرساله.\nجرّب فيديو أقصر أو جودة أقل."
)
MSG_TOO_LARGE_TRY_LOWER = (
    "الملف أكبر من حد تلغرام للبوت. جرّب دقة أقل مثل 720p أو 480p."
)
MSG_AUDIO_NO_MP3 = "تم تحميل الصوت، لكن لم أستطع تحويله إلى MP3."
MSG_AUDIO_FAILED = "فشل تحميل الصوت. جرّب رابط آخر أو تأكد أن الرابط عام."
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


def send_video(chat_id: int, path: str, caption: Optional[str] = None) -> bool:
    """Send an MP4 via sendVideo (streamable, video/mp4). Returns success."""
    filename = os.path.basename(path)
    with open(path, "rb") as fh:
        data: Dict[str, Any] = {"chat_id": str(chat_id), "supports_streaming": "true"}
        if caption:
            data["caption"] = caption
        resp = _api(
            "sendVideo",
            data=data,
            files={"video": (filename, fh, "video/mp4")},
            timeout=600,
        )
    return bool(resp and resp.get("ok"))


def send_document(chat_id: int, path: str, caption: Optional[str] = None) -> bool:
    filename = os.path.basename(path)
    with open(path, "rb") as fh:
        data: Dict[str, Any] = {"chat_id": str(chat_id)}
        if caption:
            data["caption"] = caption
        resp = _api(
            "sendDocument",
            data=data,
            files={"document": (filename, fh)},
            timeout=600,
        )
    return bool(resp and resp.get("ok"))


def send_audio(chat_id: int, path: str, caption: Optional[str] = None) -> bool:
    """Send an MP3 via sendAudio (audio/mpeg). Returns success."""
    filename = os.path.basename(path)
    with open(path, "rb") as fh:
        data: Dict[str, Any] = {"chat_id": str(chat_id)}
        if caption:
            data["caption"] = caption
        resp = _api(
            "sendAudio",
            data=data,
            files={"audio": (filename, fh, "audio/mpeg")},
            timeout=600,
        )
    return bool(resp and resp.get("ok"))


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
    """Map a quality key to a yt-dlp format selector string.

    Prefers mp4 / H.264 (avc1) formats before falling back, so the raw download
    is already mobile-friendly when possible (normalization still runs after).
    """
    selectors = {
        "full": (
            "bestvideo[ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/"
            "best[ext=mp4][vcodec^=avc1]/bestvideo+bestaudio/best"
        ),
        "1080": (
            "bestvideo[height<=1080][ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/"
            "best[height<=1080][ext=mp4][vcodec^=avc1]/best[height<=1080]"
        ),
        "720": (
            "bestvideo[height<=720][ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/"
            "best[height<=720][ext=mp4][vcodec^=avc1]/best[height<=720]"
        ),
        "480": (
            "bestvideo[height<=480][ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/"
            "best[height<=480][ext=mp4][vcodec^=avc1]/best[height<=480]"
        ),
        "360": (
            "bestvideo[height<=360][ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/"
            "best[height<=360][ext=mp4][vcodec^=avc1]/best[height<=360]"
        ),
        "small": "worst[ext=mp4]/worst",
    }
    return selectors.get(quality, selectors["720"])


def estimate_video_size(url: str, quality: str) -> Optional[int]:
    """Best-effort estimated raw size (bytes) for the chosen quality, or None.

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
# Temporary storage — strict per-job cleanup
# --------------------------------------------------------------------------- #

def cleanup_workdir(workdir: str | Path, chat_id: int | None = None) -> None:
    """Delete a job's temp folder and everything inside it.

    Removes raw yt-dlp downloads, the normalized MP4, the MP3 conversion,
    thumbnails, fragments, and .part files — the whole folder. Safe to call
    more than once and never raises.
    """
    shutil.rmtree(workdir, ignore_errors=True)
    if chat_id is not None:
        logger.info("Cleaned temp folder for chat_id=%s", chat_id)
    else:
        logger.info("Cleaned temp folder %s", os.path.basename(str(workdir)))


def cleanup_stale_temp_dirs(max_age_seconds: int = 3600) -> None:
    """Remove leftover ``tg_downloader_*`` folders older than ``max_age_seconds``.

    Runs once on startup to clear anything a previous crash or restart may have
    left in the system temp directory. Never raises.
    """
    temp_root = tempfile.gettempdir()
    try:
        entries = os.listdir(temp_root)
    except OSError as exc:  # noqa: BLE001
        logger.warning("Stale temp cleanup skipped (cannot list temp dir): %s", exc)
        return

    now = time.time()
    removed = 0
    for name in entries:
        if not name.startswith(TEMP_PREFIX):
            continue
        path = os.path.join(temp_root, name)
        if not os.path.isdir(path):
            continue
        try:
            age = now - os.path.getmtime(path)
        except OSError:
            continue
        if age >= max_age_seconds:
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
    if removed:
        logger.info("Startup cleanup removed %d stale temp folder(s).", removed)


# --------------------------------------------------------------------------- #
# Downloading (yt-dlp) + normalization (ffmpeg)
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


def download_video(url: str, workdir: str, quality: str = "720") -> Optional[str]:
    """Download the raw video into `workdir` using the selector for `quality`."""
    opts: Dict[str, Any] = {
        "format": build_video_format_selector(quality),
        # All output (and any fragments/.part files) stays inside workdir.
        "outtmpl": str(Path(workdir) / "%(title).80s-%(id)s.%(ext)s"),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    return _largest_file(workdir)


def normalize_video(
    input_path: str, output_path: str, target_height: Optional[int] = None
) -> bool:
    """Re-encode `input_path` to a Telegram/mobile-compatible MP4.

    H.264 (libx264, profile main, yuv420p) + AAC, +faststart. Audio is optional
    (-map 0:a?) so a video with no audio still produces a valid MP4. When
    target_height is set the video is scaled down to it (never upscaled), keeping
    aspect ratio with even dimensions. Returns True on success.
    """
    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-map", "0:v:0", "-map", "0:a?",
    ]
    if target_height:
        # Downscale only: height = min(target, input height); width auto (even).
        cmd += ["-vf", f"scale=-2:min({target_height}\\,ih)"]
    cmd += [
        "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "main",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        output_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except FileNotFoundError:
        logger.error("ffmpeg not found on PATH; cannot normalize video.")
        return False
    except subprocess.TimeoutExpired:
        logger.warning("ffmpeg normalization timed out.")
        return False

    if result.returncode != 0:
        tail = (result.stderr or "")[-500:]
        logger.warning("ffmpeg failed (rc=%s): %s", result.returncode, tail)
        return False

    logger.info("ffmpeg normalization ok (target_height=%s)", target_height)
    return True


def _find_audio_file(folder: str) -> Optional[str]:
    """Prefer the converted .mp3, then any other audio container produced."""
    names = os.listdir(folder)
    for name in names:
        if name.lower().endswith(".mp3"):
            return os.path.join(folder, name)
    for ext in (".m4a", ".webm", ".opus", ".aac", ".ogg", ".mka"):
        for name in names:
            if name.lower().endswith(ext):
                return os.path.join(folder, name)
    return _largest_file(folder)


def download_audio(url: str, workdir: str) -> Optional[str]:
    """Download the best audio and convert to MP3 (192k) via ffmpeg.

    Returns the resulting file path. If the MP3 conversion failed but a raw
    audio file was produced, that path is returned instead (caller handles it).
    """
    opts: Dict[str, Any] = {
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        # All output (raw audio + the converted mp3) stays inside workdir.
        "outtmpl": str(Path(workdir) / "%(title).80s-%(id)s.%(ext)s"),
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
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as exc:  # noqa: BLE001 - keep any partial audio for fallback
        logger.warning("Audio download/convert raised: %s", exc)
    return _find_audio_file(workdir)


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
    workdir = tempfile.mkdtemp(prefix=f"{TEMP_PREFIX}{chat_id}_")
    try:
        logger.info("Video request: chat=%s quality=%s", chat_id, quality)

        # Best-effort pre-check: warn (but still proceed) on a large estimate,
        # since estimates are often wrong — especially for "Full".
        estimate = estimate_video_size(url, quality)
        if estimate and estimate > MAX_FILE_SIZE:
            logger.info("Pre-download estimate %s exceeds limit", _mb(estimate))
            send_message(chat_id, MSG_MAYBE_TOO_LARGE)

        # 1) Download the raw file (inside workdir).
        try:
            raw_path = download_video(url, workdir, quality)
        except DownloadError as exc:
            text = str(exc).lower()
            if "requested format" in text or "not available" in text or "no video" in text:
                send_message(chat_id, MSG_QUALITY_UNAVAILABLE)
            else:
                logger.warning("Video download error: %s", exc)
                send_message(chat_id, MSG_ERROR)
            return

        if not raw_path or not os.path.exists(raw_path):
            send_message(chat_id, MSG_QUALITY_UNAVAILABLE)
            return
        logger.info(
            "Raw download: %s (%s)", os.path.basename(raw_path),
            _mb(os.path.getsize(raw_path)),
        )

        # 2) Normalize to a Telegram/mobile-compatible MP4 (inside workdir). We
        #    never send the raw file directly — a broken codec is the bug.
        normalized_path = str(Path(workdir) / "normalized_output.mp4")
        target_height = QUALITY_TARGET_HEIGHTS.get(quality)
        if not normalize_video(raw_path, normalized_path, target_height) or not os.path.exists(
            normalized_path
        ):
            send_message(chat_id, MSG_VIDEO_PROCESS_FAILED)
            return

        final_size = os.path.getsize(normalized_path)
        logger.info(
            "Normalized: %s (%s)", os.path.basename(normalized_path), _mb(final_size)
        )

        # 3) Enforce the Telegram size limit on the FINAL file.
        if final_size > MAX_FILE_SIZE:
            send_message(chat_id, MSG_TOO_LARGE_TRY_LOWER)
            return

        # 4) Send as a streamable video (deleted only after this returns/fails).
        logger.info("Sending via sendVideo")
        if not send_video(chat_id, normalized_path, caption=MSG_VIDEO_DONE):
            send_message(chat_id, MSG_ERROR)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Video request failed: %s", exc)
        send_message(chat_id, MSG_ERROR)
    finally:
        cleanup_workdir(workdir, chat_id)


def handle_audio_request(chat_id: int, url: str) -> None:
    workdir = tempfile.mkdtemp(prefix=f"{TEMP_PREFIX}{chat_id}_")
    try:
        logger.info("Audio request: chat=%s", chat_id)
        path = download_audio(url, workdir)
        if not path or not os.path.exists(path):
            send_message(chat_id, MSG_AUDIO_FAILED)
            return

        size = os.path.getsize(path)
        logger.info("Audio file: %s (%s)", os.path.basename(path), _mb(size))
        if size > MAX_FILE_SIZE:
            send_message(chat_id, MSG_TOO_LARGE)
            return

        if path.lower().endswith(".mp3"):
            logger.info("Sending via sendAudio")
            if not send_audio(chat_id, path):
                logger.info("sendAudio failed; falling back to sendDocument")
                send_document(chat_id, path)
        else:
            # MP3 conversion didn't happen; send the raw audio as a document.
            logger.info("MP3 missing; sending raw audio via sendDocument")
            send_document(chat_id, path, caption=MSG_AUDIO_NO_MP3)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Audio request failed: %s", exc)
        send_message(chat_id, MSG_AUDIO_FAILED)
    finally:
        cleanup_workdir(workdir, chat_id)


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

    # Audio path.
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
    # Fail fast on bad config, sweep any old temp folders, then register.
    validate_config()
    cleanup_stale_temp_dirs()
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
