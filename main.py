"""
Telegram video / audio downloader bot — webhook architecture for Render Web Service.

The user sends a public video URL (YouTube / TikTok / Instagram) and the bot
replies with two inline buttons:
    1. تحميل فيديو      -> show ONLY the qualities that actually exist for the link
    2. تحميل صوت MP3     -> extract the audio and convert it to MP3

Speed first:
- Available qualities are read from the link's real yt-dlp formats; only those
  buttons are shown. Per chat we store just a small {callback -> selector} map.
- After download the file is inspected with ffprobe. A compatible MP4 is sent
  directly (or fast-remuxed, never re-encoded). Heavy re-encoding is avoided by
  default (NO_REENCODE_BY_DEFAULT); incompatible videos ask the user for a
  lower quality instead.
- The video is sent ONCE with sendVideo (never also as a document).

This app is built for WEBHOOK deployment (NOT long polling), so it runs cleanly
as a Render Web Service behind FastAPI + Uvicorn. The webhook handler returns to
Telegram immediately and does the slow work in a background task.
"""

from __future__ import annotations

import json
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

def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


BOT_TOKEN: str = os.environ.get("BOT_TOKEN", "").strip()
WEBHOOK_SECRET: str = os.environ.get("WEBHOOK_SECRET", "").strip()

# Debug-only /webhook-info endpoint.
DEBUG: bool = _flag("DEBUG")

# Low-memory mode for small instances (e.g. Render Free, 512 MB). Hides 1080p,
# caps re-encode height at 720p, serializes media jobs.
LOW_RESOURCE_MODE: bool = _flag("LOW_RESOURCE_MODE")

# Fast mode: send a compatible MP4 directly (no ffmpeg) instead of remuxing.
FAST_MODE: bool = _flag("FAST_MODE")

# Never re-encode by default (the slow path). When True, incompatible videos are
# refused with a "try a lower quality" message instead of being re-encoded.
NO_REENCODE_BY_DEFAULT: bool = _flag("NO_REENCODE_BY_DEFAULT", default=True)

# Optionally ALSO send the video as a document copy. Off by default — the bot
# sends the video only once via sendVideo.
SEND_VIDEO_AS_FILE_COPY: bool = _flag("SEND_VIDEO_AS_FILE_COPY")

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

# Standard heights we offer as buttons (high -> low), plus Small.
STANDARD_HEIGHTS = (1080, 720, 480, 360, 240)

# callback_data -> button label. callback_data is kept short on purpose.
CALLBACK_LABELS: Dict[str, str] = {
    "v_1080": "1080p",
    "v_720": "720p",
    "v_480": "480p",
    "v_360": "360p",
    "v_240": "240p",
    "v_small": "أقل حجم / Small",
}
VIDEO_QUALITY_KEYS = tuple(CALLBACK_LABELS.keys())
SMALL_SELECTOR = "worst[ext=mp4]/worst"

# Prefix for every per-job temp folder; matched by the startup stale-folder sweep.
TEMP_PREFIX = "tg_downloader_"

# Per-chat memory (in-memory only; cleared on restart):
#   last_url_by_chat       -> the last URL the user sent
#   last_selectors_by_chat -> SMALL map {callback -> yt-dlp format selector}
# We deliberately never store the (large) yt-dlp info object.
last_url_by_chat: Dict[int, str] = {}
last_selectors_by_chat: Dict[int, Dict[str, str]] = {}

# Concurrency state (guarded by _downloads_lock; downloads run in a threadpool):
#   active_downloads   -> one active download per chat_id
#   _global_job_active -> at most ONE media job server-wide (low-memory safety)
active_downloads: set[int] = set()
_global_job_active: bool = False
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
MSG_SERVER_BUSY = "السيرفر يعالج طلباً آخر حالياً، حاول بعد قليل."
MSG_VIDEO_QUALITY_MENU = (
    "اختر دقة الفيديو المتاحة:\n"
    "(الأسرع هو اختيار 480p أو 360p بدون ضغط)"
)
MSG_720_SLOW = "قد يستغرق تحميل 720p وقتاً أطول على الخطة المجانية."
MSG_NO_VIDEO = "لا يوجد فيديو متاح لهذا الرابط. جرّب رابطًا آخر أو تحميل الصوت MP3."
MSG_FORMATS_UNREADABLE = "لم أستطع قراءة الدقات المتاحة، سأعرض خيارات آمنة للتجربة."
MSG_PREPARING = "جاري تجهيز الفيديو..."
MSG_SENDING_VIDEO = "جاري إرسال الفيديو..."
MSG_COMPRESSING = "جاري ضغط الفيديو ليتوافق مع تلغرام..."
MSG_NEEDS_CONVERSION = (
    "هذا الفيديو يحتاج تحويل وقد يستغرق وقتاً طويلاً على الخطة المجانية. جرّب دقة أقل."
)
MSG_QUALITY_UNAVAILABLE = "هذه الدقة غير متاحة لهذا الرابط. جرّب دقة أخرى."
MSG_1080_BLOCKED_LOWRES = (
    "دقة 1080p غير متاحة على الخطة المجانية بسبب محدودية الرام. "
    "جرّب 720p أو 480p."
)
MSG_VIDEO_DONE = "تم تحميل الفيديو."
MSG_VIDEO_PROCESS_FAILED = (
    "تعذّرت معالجة الفيديو لجعله متوافقًا مع تيليجرام. جرّب دقة أخرى أو رابطًا آخر."
)
MSG_TOO_LARGE = (
    "⚠️ حجم الملف أكبر من الحد المسموح به في تيليجرام (حوالي 50 ميجابايت)، "
    "لذلك لا يمكن إرساله.\nجرّب فيديو أقصر أو جودة أقل."
)
MSG_TOO_LARGE_VIDEO = (
    "الملف أكبر من حد تلغرام للبوت. جرّب دقة أقل مثل 480p أو 360p."
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


def quality_keyboard(callback_keys: List[str]) -> dict:
    """Build the quality inline keyboard (two buttons per row) from callback keys."""
    rows: List[List[dict]] = []
    row: List[dict] = []
    for key in callback_keys:
        row.append({"text": CALLBACK_LABELS.get(key, key), "callback_data": key})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return {"inline_keyboard": rows}


# --------------------------------------------------------------------------- #
# Quality detection (real formats) + format selectors
# --------------------------------------------------------------------------- #

def _selector_for_height(height: int) -> str:
    """yt-dlp selector for a height: prefer progressive mp4/H.264, then mp4
    video + m4a audio, then any mp4, then any format <= height."""
    return (
        f"best[height<={height}][ext=mp4][vcodec^=avc1]/"
        f"bestvideo[height<={height}][ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/"
        f"best[height<={height}][ext=mp4]/"
        f"best[height<={height}]"
    )


def _selector_from_callback(callback: str) -> str:
    """Fallback selector when no stored map exists (e.g. after a restart)."""
    if callback == "v_small":
        return SMALL_SELECTOR
    return _selector_for_height(_height_from_key(callback))


def _height_from_key(callback: str) -> int:
    if callback == "v_small":
        return 360
    try:
        return int(callback.split("_")[1])
    except (IndexError, ValueError):
        return 720


def _fallback_selectors() -> Dict[str, str]:
    """Safe options when the link's formats can't be read."""
    options: Dict[str, str] = {}
    for height in (720, 480, 360):
        options[f"v_{height}"] = _selector_for_height(height)
    options["v_small"] = SMALL_SELECTOR
    return options


def analyze_video_qualities(url: str) -> Optional[Dict[str, str]]:
    """Inspect the link (download=False) and return ONLY the qualities that
    actually exist as a small {callback -> selector} map.

    Returns None if formats can't be read at all, or {} if there is no video.
    The large yt-dlp info object is never stored.
    """
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "cachedir": False,
        "retries": 2,
        "socket_timeout": 30,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Quality analysis failed: %s", exc)
        return None
    if not info:
        return None

    video_formats = [
        f for f in (info.get("formats") or []) if f.get("vcodec") not in (None, "none")
    ]
    heights = sorted({int(f["height"]) for f in video_formats if f.get("height")})
    if not heights and info.get("height"):
        heights = [int(info["height"])]

    has_video = bool(video_formats) or bool(heights) or bool(info.get("height"))
    if not has_video:
        return {}

    options: Dict[str, str] = {}
    if heights:
        min_h, max_h = min(heights), max(heights)
        for bucket in STANDARD_HEIGHTS:
            if LOW_RESOURCE_MODE and bucket == 1080:
                continue  # 1080p hidden on low-memory instances
            # A bucket is "available" only when the source genuinely has it:
            # at least one format <= bucket AND a format that reaches it.
            if min_h <= bucket <= max_h:
                options[f"v_{bucket}"] = _selector_for_height(bucket)
    # Small is always offered when a video exists.
    options["v_small"] = SMALL_SELECTOR
    return options


# --------------------------------------------------------------------------- #
# Concurrency control — one per chat AND one global media job
# --------------------------------------------------------------------------- #

def try_begin_download(chat_id: int) -> tuple[bool, Optional[str]]:
    """Reserve the per-chat slot AND the single global job slot."""
    global _global_job_active
    with _downloads_lock:
        if chat_id in active_downloads:
            return False, MSG_BUSY
        if _global_job_active:
            return False, MSG_SERVER_BUSY
        active_downloads.add(chat_id)
        _global_job_active = True
        return True, None


def end_download(chat_id: int) -> None:
    global _global_job_active
    with _downloads_lock:
        active_downloads.discard(chat_id)
        _global_job_active = False


# --------------------------------------------------------------------------- #
# Temporary storage — strict per-job cleanup
# --------------------------------------------------------------------------- #

def cleanup_workdir(workdir: str | Path, chat_id: int | None = None) -> None:
    """Delete a job's temp folder and everything inside it. Never raises."""
    shutil.rmtree(workdir, ignore_errors=True)
    if chat_id is not None:
        logger.info("Cleaned temp folder for chat_id=%s", chat_id)
    else:
        logger.info("Cleaned temp folder %s", os.path.basename(str(workdir)))


def cleanup_stale_temp_dirs(max_age_seconds: int = 3600) -> None:
    """Remove leftover ``tg_downloader_*`` folders older than ``max_age_seconds``."""
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
# Downloading (yt-dlp) + ffprobe inspection + remux/re-encode (ffmpeg)
# --------------------------------------------------------------------------- #

def _base_ydl_opts(workdir: str) -> Dict[str, Any]:
    """Shared yt-dlp options: everything stays in workdir, low-resource friendly."""
    return {
        "outtmpl": str(Path(workdir) / "%(title).80s-%(id)s.%(ext)s"),
        "noplaylist": True,
        "restrictfilenames": True,
        "quiet": True,
        "no_warnings": True,
        "cachedir": False,
        "retries": 2,
        "socket_timeout": 30,
        "concurrent_fragment_downloads": 1,
    }


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


def download_video(url: str, workdir: str, selector: str) -> Optional[str]:
    """Download the raw video into `workdir` using the given format selector."""
    opts = _base_ydl_opts(workdir)
    opts["format"] = selector
    opts["merge_output_format"] = "mp4"
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    return _largest_file(workdir)


def probe_media(path: str) -> dict:
    """Return ffprobe JSON (streams + format), or {} on failure (small output)."""
    cmd = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_streams", "-show_format", path,
    ]
    try:
        result = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.warning("ffprobe unavailable/timeout: %s", exc)
        return {}
    if result.returncode != 0:
        logger.warning("ffprobe rc=%s: %s", result.returncode, (result.stderr or "")[-2000:])
        return {}
    try:
        return json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return {}


def _probe_streams(path: str) -> Optional[dict]:
    """Summarise a media file's container + codecs for the send decision."""
    info = probe_media(path)
    if not info:
        return None
    fmt = (info.get("format", {}).get("format_name") or "").lower()
    is_mp4 = path.lower().endswith(".mp4") and ("mp4" in fmt or "mov" in fmt)
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    acodecs = [
        (s.get("codec_name") or "").lower()
        for s in streams if s.get("codec_type") == "audio"
    ]
    return {
        "is_mp4": is_mp4,
        "vcodec": (video.get("codec_name") or "").lower() if video else "",
        "pix": (video.get("pix_fmt") or "").lower() if video else "",
        "acodecs": acodecs,
    }


def _is_compatible(streams: Optional[dict]) -> bool:
    """True when streams are Telegram/mobile-friendly (H.264 + AAC/no-audio)."""
    if not streams:
        return False
    if streams["vcodec"] not in ("h264", "avc1"):
        return False
    if streams["pix"] and streams["pix"] != "yuv420p":
        return False
    return all(a == "aac" for a in streams["acodecs"])  # empty -> True (no audio)


def _run_ffmpeg(cmd: List[str], output_path: str) -> bool:
    """Run ffmpeg with stderr -> a small log file (tail logged on error)."""
    log_path = output_path + ".log"
    try:
        with open(log_path, "wb") as errlog:
            result = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=errlog,
                timeout=600,
            )
    except FileNotFoundError:
        logger.error("ffmpeg not found on PATH.")
        return False
    except subprocess.TimeoutExpired:
        logger.warning("ffmpeg timed out.")
        return False

    if result.returncode != 0:
        err_tail = ""
        try:
            with open(log_path, "r", errors="replace") as fh:
                err_tail = fh.read()[-2000:]
        except OSError:
            pass
        logger.warning("ffmpeg failed (rc=%s): %s", result.returncode, err_tail)
        return False
    return True


def remux_video(input_path: str, output_path: str) -> bool:
    """Fast path: stream-copy into an MP4 with +faststart (no re-encode)."""
    cmd = [
        "ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-i", input_path,
        "-c", "copy",
        "-movflags", "+faststart",
        output_path,
    ]
    return _run_ffmpeg(cmd, output_path)


def normalize_video(
    input_path: str,
    output_path: str,
    target_height: Optional[int] = None,
    crf: int = 30,
    audio_bitrate: str = "96k",
) -> bool:
    """Slow fallback: re-encode to H.264/AAC MP4 (only when explicitly allowed)."""
    cmd = [
        "ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-threads", "1",
        "-i", input_path,
        "-map", "0:v:0", "-map", "0:a?",
    ]
    if target_height:
        cmd += ["-vf", f"scale=-2:min({target_height}\\,ih)"]
    cmd += [
        "-c:v", "libx264", "-preset", "ultrafast", "-profile:v", "main",
        "-pix_fmt", "yuv420p", "-crf", str(crf),
        "-c:a", "aac", "-b:a", audio_bitrate,
        "-movflags", "+faststart",
        output_path,
    ]
    ok = _run_ffmpeg(cmd, output_path)
    if ok:
        logger.info("ffmpeg re-encode ok (height=%s crf=%s)", target_height, crf)
    return ok


def _reencode_params(quality_key: str) -> tuple[Optional[int], int, str]:
    """(target_height, crf, audio_bitrate) for the optional re-encode fallback."""
    if quality_key == "v_small":
        return 360, 32, "96k"
    height = _height_from_key(quality_key)
    if LOW_RESOURCE_MODE:
        height = min(height, 720)
    return height, 30, "96k"


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
    """Download the best audio and convert to MP3 (192k) via ffmpeg."""
    opts = _base_ydl_opts(workdir)
    opts["format"] = "bestaudio[ext=m4a]/bestaudio/best"
    opts["postprocessors"] = [
        {
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }
    ]
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
    """Analyze the link and present ONLY the qualities that actually exist."""
    options = analyze_video_qualities(url)
    if options is None:
        send_message(chat_id, MSG_FORMATS_UNREADABLE)
        options = _fallback_selectors()
    if not options:
        send_message(chat_id, MSG_NO_VIDEO)
        return

    # Store the small {callback -> selector} map for this chat (not the info obj).
    last_selectors_by_chat[chat_id] = options
    send_message(
        chat_id, MSG_VIDEO_QUALITY_MENU, reply_markup=quality_keyboard(list(options.keys()))
    )


def handle_video_request(chat_id: int, url: str, quality_key: str, selector: str) -> None:
    workdir = tempfile.mkdtemp(prefix=f"{TEMP_PREFIX}{chat_id}_")
    try:
        logger.info(
            "Video request: chat=%s quality=%s fast=%s low_res=%s no_reencode=%s",
            chat_id, quality_key, FAST_MODE, LOW_RESOURCE_MODE, NO_REENCODE_BY_DEFAULT,
        )
        if LOW_RESOURCE_MODE and quality_key == "v_720":
            send_message(chat_id, MSG_720_SLOW)

        # 1) Download with the selected format selector.
        dl_start = time.monotonic()
        try:
            raw_path = download_video(url, workdir, selector)
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
            "yt-dlp download: %.1fs -> %s (%s)",
            time.monotonic() - dl_start, os.path.basename(raw_path),
            _mb(os.path.getsize(raw_path)),
        )

        # 2) Inspect codecs/container with ffprobe.
        probe_start = time.monotonic()
        streams = _probe_streams(raw_path)
        compatible = _is_compatible(streams)
        logger.info(
            "ffprobe inspect: %.2fs (compatible=%s, mp4=%s, v=%s, a=%s)",
            time.monotonic() - probe_start, compatible,
            streams["is_mp4"] if streams else "?",
            streams["vcodec"] if streams else "?",
            ",".join(streams["acodecs"]) if streams else "?",
        )

        output_path = str(Path(workdir) / "video_output.mp4")
        proc_start = time.monotonic()

        if compatible:
            send_message(chat_id, MSG_SENDING_VIDEO)
            if streams["is_mp4"] and FAST_MODE:
                method = "direct"          # compatible MP4 -> send as-is (no ffmpeg)
                final_path: Optional[str] = raw_path
            else:
                method = "remux"           # compatible streams -> fast remux to mp4
                final_path = output_path if remux_video(raw_path, output_path) else None
        elif NO_REENCODE_BY_DEFAULT:
            # Heavy conversion would be needed; we don't re-encode by default.
            logger.info("Incompatible and NO_REENCODE_BY_DEFAULT -> ask for lower quality.")
            send_message(chat_id, MSG_NEEDS_CONVERSION)
            return
        else:
            method = "reencode"
            send_message(chat_id, MSG_COMPRESSING)
            th, crf, ab = _reencode_params(quality_key)
            final_path = output_path if normalize_video(raw_path, output_path, th, crf, ab) else None

        if not final_path or not os.path.exists(final_path):
            send_message(chat_id, MSG_VIDEO_PROCESS_FAILED)
            return
        logger.info("%s done: %.1fs", method, time.monotonic() - proc_start)

        # 3) Enforce the Telegram size limit on the FINAL file.
        final_size = os.path.getsize(final_path)
        logger.info("Final file: %s (%s) method=%s", os.path.basename(final_path), _mb(final_size), method)
        if final_size > MAX_FILE_SIZE:
            send_message(chat_id, MSG_TOO_LARGE_VIDEO)
            return

        # 4) Send ONCE via sendVideo (a document copy only if explicitly enabled).
        up_start = time.monotonic()
        sent = send_video(chat_id, final_path, caption=MSG_VIDEO_DONE)
        logger.info(
            "Telegram upload: %.1fs (%s, method=%s)",
            time.monotonic() - up_start, _mb(final_size), method,
        )
        if not sent:
            send_message(chat_id, MSG_ERROR)
            return
        if SEND_VIDEO_AS_FILE_COPY:
            send_document(chat_id, final_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Video request failed: %s", exc)
        send_message(chat_id, MSG_ERROR)
    finally:
        cleanup_workdir(workdir, chat_id)


def handle_audio_request(chat_id: int, url: str) -> None:
    workdir = tempfile.mkdtemp(prefix=f"{TEMP_PREFIX}{chat_id}_")
    try:
        logger.info("Audio request: chat=%s", chat_id)
        dl_start = time.monotonic()
        path = download_audio(url, workdir)
        if not path or not os.path.exists(path):
            send_message(chat_id, MSG_AUDIO_FAILED)
            return

        size = os.path.getsize(path)
        logger.info(
            "Audio download: %.1fs -> %s (%s)",
            time.monotonic() - dl_start, os.path.basename(path), _mb(size),
        )
        if size > MAX_FILE_SIZE:
            send_message(chat_id, MSG_TOO_LARGE)
            return

        up_start = time.monotonic()
        if path.lower().endswith(".mp3"):
            logger.info("Sending via sendAudio")
            if not send_audio(chat_id, path):
                logger.info("sendAudio failed; falling back to sendDocument")
                send_document(chat_id, path)
        else:
            logger.info("MP3 missing; sending raw audio via sendDocument")
            send_document(chat_id, path, caption=MSG_AUDIO_NO_MP3)
        logger.info("Telegram upload (audio): %.1fs (%s)", time.monotonic() - up_start, _mb(size))
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
    last_selectors_by_chat.pop(chat_id, None)  # invalidate any old quality map
    send_message(chat_id, MSG_CHOOSE, reply_markup=download_keyboard())


def _run_guarded_download(chat_id: int, status_msg: str, work) -> None:
    """Enforce one-per-chat AND one-global media job around a download callable."""
    ok, reason = try_begin_download(chat_id)
    if not ok:
        send_message(chat_id, reason)
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

    # Step 1: "تحميل فيديو" -> analyze and show the available-qualities menu.
    if action == "dl_video":
        show_quality_menu(chat_id, url)
        return

    # Step 2: a concrete video quality -> guarded download.
    if action in VIDEO_QUALITY_KEYS:
        if LOW_RESOURCE_MODE and action == "v_1080":
            send_message(chat_id, MSG_1080_BLOCKED_LOWRES)
            return
        selector = last_selectors_by_chat.get(chat_id, {}).get(action) or _selector_from_callback(action)
        _run_guarded_download(
            chat_id,
            MSG_PREPARING,
            lambda: handle_video_request(chat_id, url, action, selector),
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
    validate_config()
    cleanup_stale_temp_dirs()
    logger.info(
        "Flags: FAST_MODE=%s LOW_RESOURCE_MODE=%s NO_REENCODE_BY_DEFAULT=%s SEND_VIDEO_AS_FILE_COPY=%s",
        FAST_MODE, LOW_RESOURCE_MODE, NO_REENCODE_BY_DEFAULT, SEND_VIDEO_AS_FILE_COPY,
    )
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
    """Debug-only: return Telegram getWebhookInfo (enabled only when DEBUG=true)."""
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
    if not WEBHOOK_SECRET or secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")
    header_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if header_secret is not None and header_secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")

    update = await request.json()
    background_tasks.add_task(process_update, update)
    return {"ok": True}
