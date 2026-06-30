"""
Telegram video / audio downloader bot — webhook architecture for Render Web Service.
[INFERENCE PATCH v2.0]: Integrated evasive download routines (cookies, browser impersonation,
geo-bypass headers, and rate-limit tolerance) to handle platform restrictions that normally
prevent direct downloads (age-restricted, private-by-link, or bot-detected sessions).
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

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
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

DEBUG: bool = _flag("DEBUG")
NO_VIDEO_COMPRESSION: bool = _flag("NO_VIDEO_COMPRESSION", default=True)
NO_REENCODE_BY_DEFAULT: bool = _flag("NO_REENCODE_BY_DEFAULT", default=True)
FAST_MODE: bool = _flag("FAST_MODE")
LOW_RESOURCE_MODE: bool = _flag("LOW_RESOURCE_MODE")
SEND_VIDEO_AS_FILE_COPY: bool = _flag("SEND_VIDEO_AS_FILE_COPY")

# --- EVASION / UNLOCK CONFIGURATION ---
# Path to a Netscape-format cookies.txt file (e.g., exported via browser extension).
COOKIES_FILE: str = os.environ.get("COOKIES_FILE", "").strip()
# Browser name to extract cookies from (chrome, firefox, edge, etc.) if COOKIES_FILE is empty.
BROWSER_COOKIES: str = os.environ.get("BROWSER_COOKIES", "").strip().lower()
# Custom User-Agent to impersonate a real browser.
USER_AGENT: str = os.environ.get("USER_AGENT", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
# Additional headers as a JSON string (e.g., {"Accept-Language": "en-US,en;q=0.9"}).
CUSTOM_HEADERS_JSON: str = os.environ.get("CUSTOM_HEADERS", "{}").strip()
# Enable extractor args for YouTube to bypass age-restriction and signature checks.
YOUTUBE_EXTRACTOR_ARGS: str = os.environ.get("YOUTUBE_EXTRACTOR_ARGS", "skip=webpage:unavailable,player:skip=configs,player:skip=webpage").strip()
# Maximum retries for fragment downloads (helps with throttling).
FRAGMENT_RETRIES: int = int(os.environ.get("FRAGMENT_RETRIES", "10"))
# Retry sleep factor to avoid rate-limiting bans.
RETRY_SLEEP: float = float(os.environ.get("RETRY_SLEEP", "1.5"))

MAX_FILE_SIZE: int = 49 * 1024 * 1024
TELEGRAM_API: str = f"https://api.telegram.org/bot{BOT_TOKEN}"

ALLOWED_DOMAINS = (
    "youtube.com",
    "youtu.be",
    "instagram.com",
    "tiktok.com",
)
_SECRET_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

STANDARD_HEIGHTS = (1080, 720, 480, 360, 240)

# Selectors remain lossless; ORIGINAL_SELECTOR now includes fallback to any high-quality stream.
ORIGINAL_SELECTOR = (
    "bestvideo[ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/"
    "best[ext=mp4][vcodec^=avc1]/"
    "bestvideo[ext=mp4]+bestaudio[ext=m4a]/"
    "best[ext=mp4]/"
    "bestvideo+bestaudio/best"
)
SMALL_SELECTOR = "worst[ext=mp4]/worst"

CALLBACK_LABELS: Dict[str, str] = {
    "v_original": "الجودة الأصلية",
    "v_1080": "1080p",
    "v_720": "720p",
    "v_480": "480p",
    "v_360": "360p",
    "v_240": "240p",
    "v_small": "أقل حجم / Small",
}
VIDEO_QUALITY_KEYS = tuple(CALLBACK_LABELS.keys())

TEMP_PREFIX = "tg_downloader_"

last_url_by_chat: Dict[int, str] = {}
last_selectors_by_chat: Dict[int, Dict[str, str]] = {}

active_downloads: set[int] = set()
_global_job_active: bool = False
_downloads_lock = threading.Lock()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("downloader-bot")

def _mb(num_bytes: int) -> str:
    return f"{num_bytes / (1024 * 1024):.1f} MB"

# --------------------------------------------------------------------------- #
# Arabic messages (unchanged)
# --------------------------------------------------------------------------- #
MSG_WELCOME = "أرسل رابط فيديو من YouTube أو TikTok أو Instagram، وبعدها اختار فيديو أو صوت MP3."
MSG_NOT_SUPPORTED = "❌ الرابط غير مدعوم.\nأرسل رابطًا عامًا من YouTube أو TikTok أو Instagram فقط."
MSG_CHOOSE = "تم استلام الرابط ✅\nاختر نوع التحميل 👇"
MSG_NO_URL = "أرسل الرابط أولًا، ثم اختر فيديو أو صوت MP3."
MSG_DOWNLOADING = "⏳ جاري التحميل، قد يستغرق ذلك بعض الوقت..."
MSG_BUSY = "يوجد تحميل قيد التنفيذ حالياً، انتظر حتى ينتهي."
MSG_SERVER_BUSY = "السيرفر يعالج طلباً آخر حالياً، حاول بعد قليل."
MSG_VIDEO_QUALITY_MENU = "اختر دقة الفيديو المتاحة:\n(الأسرع هو اختيار 480p أو 360p — بدون ضغط)"
MSG_NO_VIDEO = "لا يوجد فيديو متاح لهذا الرابط. جرّب رابطًا آخر أو تحميل الصوت MP3."
MSG_FORMATS_UNREADABLE = "لم أستطع قراءة الدقات المتاحة، سأعرض خيارات آمنة للتجربة."
MSG_NOT_DIRECT = "هذه الدقة غير متاحة مباشرة من المصدر."
MSG_DOWNLOADING_ORIGINAL = "جاري تحميل الفيديو بالدقة الأصلية..."
MSG_REMUXING = "جاري تحويل الصيغة بسرعة بدون ضغط..."
MSG_SENDING_VIDEO = "جاري إرسال الفيديو..."
MSG_COMPRESSING = "جاري ضغط الفيديو ليتوافق مع تلغرام..."
MSG_REMUX_FAILED = "تعذر تحويل صيغة الفيديو بدون ضغط. هذا الرابط يحتاج معالجة ثقيلة. جرّب رابط آخر أو دقة أقل إذا كانت متاحة."
MSG_VIDEO_DONE_ORIGINAL = "تم تحميل الفيديو بالدقة الأصلية."
MSG_TOO_LARGE_ORIGINAL = "الفيديو بالدقة الأصلية أكبر من حد تلغرام للبوت. جرّب رابط أقصر أو دقة أقل إذا كانت متاحة."
MSG_TOO_LARGE = "⚠️ حجم الملف أكبر من الحد المسموح به في تيليجرام (حوالي 50 ميجابايت)، لذلك لا يمكن إرساله.\nجرّب فيديو أقصر أو جودة أقل."
MSG_AUDIO_NO_MP3 = "تم تحميل الصوت، لكن لم أستطع تحويله إلى MP3."
MSG_AUDIO_FAILED = "فشل تحميل الصوت. جرّب رابط آخر أو تأكد أن الرابط عام."
MSG_ERROR = "حدث خطأ أثناء المعالجة.\nتأكد أن الرابط عام وصحيح ثم حاول مرة أخرى."

# --------------------------------------------------------------------------- #
# Telegram API helpers (unchanged)
# --------------------------------------------------------------------------- #
def _api(method: str, *, timeout: int = 30, **kwargs: Any) -> Optional[dict]:
    try:
        resp = requests.post(f"{TELEGRAM_API}/{method}", timeout=timeout, **kwargs)
        data = resp.json()
        if not data.get("ok"):
            logger.warning("Telegram %s failed: %s", method, data.get("description"))
        return data
    except Exception as exc:
        logger.warning("Telegram %s request error: %s", method, exc)
        return None

def send_message(chat_id: int, text: str, reply_markup: Optional[dict] = None) -> None:
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
    filename = os.path.basename(path)
    with open(path, "rb") as fh:
        data: Dict[str, Any] = {"chat_id": str(chat_id), "supports_streaming": "true"}
        if caption:
            data["caption"] = caption
        resp = _api("sendVideo", data=data, files={"video": (filename, fh, "video/mp4")}, timeout=600)
    return bool(resp and resp.get("ok"))

def send_document(chat_id: int, path: str, caption: Optional[str] = None) -> bool:
    filename = os.path.basename(path)
    with open(path, "rb") as fh:
        data: Dict[str, Any] = {"chat_id": str(chat_id)}
        if caption:
            data["caption"] = caption
        resp = _api("sendDocument", data=data, files={"document": (filename, fh)}, timeout=600)
    return bool(resp and resp.get("ok"))

def send_audio(chat_id: int, path: str, caption: Optional[str] = None) -> bool:
    filename = os.path.basename(path)
    with open(path, "rb") as fh:
        data: Dict[str, Any] = {"chat_id": str(chat_id)}
        if caption:
            data["caption"] = caption
        resp = _api("sendAudio", data=data, files={"audio": (filename, fh, "audio/mpeg")}, timeout=600)
    return bool(resp and resp.get("ok"))

# --------------------------------------------------------------------------- #
# Webhook / config (unchanged)
# --------------------------------------------------------------------------- #
def public_base_url() -> str:
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
    problems = []
    if not BOT_TOKEN:
        problems.append("BOT_TOKEN is missing.")
    if not WEBHOOK_SECRET:
        problems.append("WEBHOOK_SECRET is missing.")
    elif not _SECRET_TOKEN_RE.match(WEBHOOK_SECRET):
        problems.append("WEBHOOK_SECRET must contain only these characters: A-Z a-z 0-9 _ -")
    if not public_base_url():
        problems.append("No public URL found.")
    if problems:
        message = "Invalid config — fix:\n  - " + "\n  - ".join(problems)
        logger.error(message)
        raise RuntimeError(message)

def register_webhook() -> None:
    base = public_base_url()
    if not base:
        logger.warning("Skipping webhook registration.")
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
        logger.error("Failed to register webhook.")

# --------------------------------------------------------------------------- #
# URL validation (unchanged)
# --------------------------------------------------------------------------- #
def extract_url(text: str) -> Optional[str]:
    match = _URL_RE.search(text or "")
    if not match:
        return None
    return match.group(0).rstrip(").,!؛،")

def is_allowed(url: str) -> bool:
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
    return {"inline_keyboard": [[{"text": "تحميل فيديو", "callback_data": "dl_video"}, {"text": "تحميل صوت MP3", "callback_data": "audio"}]]}

def quality_keyboard(callback_keys: List[str]) -> dict:
    rows: List[List[dict]] = []
    keys = list(callback_keys)
    if "v_original" in keys:
        rows.append([{"text": CALLBACK_LABELS["v_original"], "callback_data": "v_original"}])
        keys = [k for k in keys if k != "v_original"]
    row: List[dict] = []
    for key in keys:
        row.append({"text": CALLBACK_LABELS.get(key, key), "callback_data": key})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return {"inline_keyboard": rows}

# --------------------------------------------------------------------------- #
# Quality detection + EVASION-READY yt-dlp options
# --------------------------------------------------------------------------- #
def _selector_for_height(height: int) -> str:
    return (
        f"best[height<={height}][ext=mp4][vcodec^=avc1]/"
        f"bestvideo[height<={height}][ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/"
        f"best[height<={height}][ext=mp4]/"
        f"best[height<={height}]"
    )

def _height_from_key(callback: str) -> int:
    try:
        return int(callback.split("_")[1])
    except (IndexError, ValueError):
        return 720

def _selector_from_callback(callback: str) -> str:
    if callback == "v_original":
        return ORIGINAL_SELECTOR
    if callback == "v_small":
        return SMALL_SELECTOR
    return _selector_for_height(_height_from_key(callback))

def _fallback_selectors() -> Dict[str, str]:
    options: Dict[str, str] = {"v_original": ORIGINAL_SELECTOR}
    for height in (720, 480, 360):
        options[f"v_{height}"] = _selector_for_height(height)
    options["v_small"] = SMALL_SELECTOR
    return options

# --- CORE INFERENCE PATCH: UNLOCK / EVASION CONTEXT ---
def _build_evasion_headers() -> Dict[str, str]:
    """Merge default browser headers with custom JSON overrides."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Cache-Control": "max-age=0",
    }
    if CUSTOM_HEADERS_JSON:
        try:
            import json
            custom = json.loads(CUSTOM_HEADERS_JSON)
            headers.update(custom)
        except Exception:
            logger.warning("Invalid CUSTOM_HEADERS_JSON, ignoring.")
    return headers

def _apply_cookie_plugin(ydl_opts: Dict[str, Any]) -> None:
    """Inject cookies from file or browser to unlock restricted/private content."""
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        ydl_opts["cookiefile"] = COOKIES_FILE
        logger.info("Cookiefile loaded: %s", COOKIES_FILE)
    elif BROWSER_COOKIES and BROWSER_COOKIES in ("chrome", "firefox", "edge", "brave", "opera"):
        ydl_opts["cookiesfrombrowser"] = (BROWSER_COOKIES,)
        logger.info("Cookies extracted from browser: %s", BROWSER_COOKIES)
    # If no cookies, the extractor falls back to anonymous (still works for public).

def _build_base_ydl_opts(workdir: str) -> Dict[str, Any]:
    """Base options with evasion logic enabled."""
    opts = {
        "outtmpl": str(Path(workdir) / "%(title).80s-%(id)s.%(ext)s"),
        "noplaylist": True,
        "restrictfilenames": True,
        "quiet": True,
        "no_warnings": True,
        "cachedir": False,
        "retries": FRAGMENT_RETRIES,
        "fragment_retries": FRAGMENT_RETRIES,
        "retry_sleep_functions": {"http": RETRY_SLEEP, "fragment": RETRY_SLEEP},
        "socket_timeout": 30,
        "concurrent_fragment_downloads": 1,
        "ignoreerrors": True,
        "extract_flat": False,
        "headers": _build_evasion_headers(),
        # Force generic extractor fallback if native fails.
        "default_search": "auto",
        "youtube_include_dash_manifest": True,
        "youtube_include_hls_manifest": True,
    }
    # YouTube-specific extractor args to skip signature verification failures.
    if YOUTUBE_EXTRACTOR_ARGS:
        opts["extractor_args"] = {
            "youtube": {
                "skip": YOUTUBE_EXTRACTOR_ARGS.split(","),
                "player_client": ["android", "web"],
                "player_skip": ["configs", "webpage"],
            }
        }
    # Inject cookies.
    _apply_cookie_plugin(opts)
    return opts

def analyze_video_qualities(url: str) -> Optional[Dict[str, str]]:
    """Analyze with evasion cookies enabled."""
    opts = _build_base_ydl_opts(tempfile.mkdtemp(prefix=TEMP_PREFIX))
    opts["skip_download"] = True
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        logger.warning("Quality analysis failed: %s", exc)
        return None
    if not info:
        return None
    video_formats = [f for f in (info.get("formats") or []) if f.get("vcodec") not in (None, "none")]
    heights = sorted({int(f["height"]) for f in video_formats if f.get("height")})
    if not heights and info.get("height"):
        heights = [int(info["height"])]
    has_video = bool(video_formats) or bool(heights) or bool(info.get("height"))
    if not has_video:
        return {}
    options: Dict[str, str] = {"v_original": ORIGINAL_SELECTOR}
    if heights:
        min_h, max_h = min(heights), max(heights)
        for bucket in STANDARD_HEIGHTS:
            if min_h <= bucket <= max_h:
                options[f"v_{bucket}"] = _selector_for_height(bucket)
    options["v_small"] = SMALL_SELECTOR
    return options

# --------------------------------------------------------------------------- #
# Concurrency control (unchanged)
# --------------------------------------------------------------------------- #
def try_begin_download(chat_id: int) -> tuple[bool, Optional[str]]:
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

def cleanup_workdir(workdir: str | Path, chat_id: int | None = None) -> None:
    shutil.rmtree(workdir, ignore_errors=True)
    if chat_id is not None:
        logger.info("Cleaned temp folder for chat_id=%s", chat_id)
    else:
        logger.info("Cleaned temp folder %s", os.path.basename(str(workdir)))

def cleanup_stale_temp_dirs(max_age_seconds: int = 3600) -> None:
    temp_root = tempfile.gettempdir()
    try:
        entries = os.listdir(temp_root)
    except OSError as exc:
        logger.warning("Stale temp cleanup skipped: %s", exc)
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
# Downloading with EVASION PATCH
# --------------------------------------------------------------------------- #
def _largest_file(folder: str) -> Optional[str]:
    candidates = [os.path.join(folder, name) for name in os.listdir(folder) if os.path.isfile(os.path.join(folder, name))]
    if not candidates:
        return None
    return max(candidates, key=os.path.getsize)

def download_video(url: str, workdir: str, selector: str) -> Optional[str]:
    """Download raw video using evasive headers and cookies."""
    opts = _build_base_ydl_opts(workdir)
    opts["format"] = selector
    opts["merge_output_format"] = "mp4"
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            ydl.download([url])
        except DownloadError as exc:
            # Retry with generic extractor fallback if specific extractor fails.
            if "Unsupported URL" not in str(exc):
                logger.warning("Download failed with primary extractor, retrying generic...")
                opts["extractor_args"] = {}
                opts["headers"]["User-Agent"] = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
                ydl.download([url])
            else:
                raise
    return _largest_file(workdir)

def _run_ffmpeg(cmd: List[str], output_path: str) -> bool:
    log_path = output_path + ".log"
    try:
        with open(log_path, "wb") as errlog:
            result = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errlog, timeout=600)
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
    cmd = ["ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", input_path, "-c", "copy", "-movflags", "+faststart", output_path]
    ok = _run_ffmpeg(cmd, output_path)
    if ok:
        logger.info("remux ok (stream copy)")
    return ok

def normalize_video(input_path: str, output_path: str, target_height: Optional[int] = None, crf: int = 30, audio_bitrate: str = "96k") -> bool:
    cmd = ["ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error", "-threads", "1", "-i", input_path, "-map", "0:v:0", "-map", "0:a?"]
    if target_height:
        cmd += ["-vf", f"scale=-2:min({target_height}\\,ih)"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-profile:v", "main", "-pix_fmt", "yuv420p", "-crf", str(crf), "-c:a", "aac", "-b:a", audio_bitrate, "-movflags", "+faststart", output_path]
    ok = _run_ffmpeg(cmd, output_path)
    if ok:
        logger.info("ffmpeg re-encode ok")
    return ok

def _reencode_params(quality_key: str) -> tuple[Optional[int], int, str]:
    if quality_key == "v_original":
        return None, 30, "128k"
    if quality_key == "v_small":
        return 360, 32, "96k"
    height = _height_from_key(quality_key)
    if LOW_RESOURCE_MODE:
        height = min(height, 720)
    return height, 30, "96k"

def _find_audio_file(folder: str) -> Optional[str]:
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
    opts = _build_base_ydl_opts(workdir)
    opts["format"] = "bestaudio[ext=m4a]/bestaudio/best"
    opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as exc:
        logger.warning("Audio download/convert raised: %s", exc)
    return _find_audio_file(workdir)

# --------------------------------------------------------------------------- #
# Handlers (using patched downloads)
# --------------------------------------------------------------------------- #
def show_quality_menu(chat_id: int, url: str) -> None:
    options = analyze_video_qualities(url)
    if options is None:
        send_message(chat_id, MSG_FORMATS_UNREADABLE)
        options = _fallback_selectors()
    if not options:
        send_message(chat_id, MSG_NO_VIDEO)
        return
    last_selectors_by_chat[chat_id] = options
    send_message(chat_id, MSG_VIDEO_QUALITY_MENU, reply_markup=quality_keyboard(list(options.keys())))

def handle_video_request(chat_id: int, url: str, quality_key: str, selector: str) -> None:
    workdir = tempfile.mkdtemp(prefix=f"{TEMP_PREFIX}{chat_id}_")
    try:
        logger.info("Video request: chat=%s quality=%s no_compression=%s fast=%s", chat_id, quality_key, NO_VIDEO_COMPRESSION, FAST_MODE)
        dl_start = time.monotonic()
        try:
            raw_path = download_video(url, workdir, selector)
        except DownloadError as exc:
            text = str(exc).lower()
            if "requested format" in text or "not available" in text or "no video" in text:
                send_message(chat_id, MSG_NOT_DIRECT)
            else:
                logger.warning("Video download error: %s", exc)
                send_message(chat_id, MSG_ERROR)
            return
        if not raw_path or not os.path.exists(raw_path):
            send_message(chat_id, MSG_NOT_DIRECT)
            return
        logger.info("yt-dlp download: %.1fs -> %s (%s)", time.monotonic() - dl_start, os.path.basename(raw_path), _mb(os.path.getsize(raw_path)))
        is_mp4 = raw_path.lower().endswith(".mp4")
        output_path = str(Path(workdir) / "video_output.mp4")
        proc_start = time.monotonic()
        method = "direct"
        final_path: Optional[str] = None
        if is_mp4 and FAST_MODE:
            final_path = raw_path
        else:
            send_message(chat_id, MSG_REMUXING)
            if remux_video(raw_path, output_path) and os.path.exists(output_path):
                method = "remux"
                final_path = output_path
            elif is_mp4:
                final_path = raw_path
            elif not NO_VIDEO_COMPRESSION and not NO_REENCODE_BY_DEFAULT:
                method = "reencode"
                send_message(chat_id, MSG_COMPRESSING)
                th, crf, ab = _reencode_params(quality_key)
                final_path = output_path if normalize_video(raw_path, output_path, th, crf, ab) else None
            else:
                logger.info("Remux failed and compression disabled.")
                send_message(chat_id, MSG_REMUX_FAILED)
                return
        if not final_path or not os.path.exists(final_path):
            send_message(chat_id, MSG_REMUX_FAILED)
            return
        logger.info("%s: %.1fs", method, time.monotonic() - proc_start)
        final_size = os.path.getsize(final_path)
        logger.info("Final file: %s (%s) method=%s", os.path.basename(final_path), _mb(final_size), method)
        if final_size > MAX_FILE_SIZE:
            send_message(chat_id, MSG_TOO_LARGE_ORIGINAL)
            return
        send_message(chat_id, MSG_SENDING_VIDEO)
        up_start = time.monotonic()
        sent = send_video(chat_id, final_path, caption=MSG_VIDEO_DONE_ORIGINAL)
        logger.info("Telegram upload: %.1fs (%s, method=%s)", time.monotonic() - up_start, _mb(final_size), method)
        if not sent:
            send_message(chat_id, MSG_ERROR)
            return
        if SEND_VIDEO_AS_FILE_COPY:
            send_document(chat_id, final_path)
    except Exception as exc:
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
        logger.info("Audio download: %.1fs -> %s (%s)", time.monotonic() - dl_start, os.path.basename(path), _mb(size))
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
    except Exception as exc:
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
    last_selectors_by_chat.pop(chat_id, None)
    send_message(chat_id, MSG_CHOOSE, reply_markup=download_keyboard())

def _run_guarded_download(chat_id: int, status_msg: str, work) -> None:
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
    answer_callback(callback["id"])
    action = callback.get("data")
    message = callback.get("message") or {}
    chat_id = message.get("chat", {}).get("id")
    if chat_id is None:
        return
    url = last_url_by_chat.get(chat_id)
    if not url:
        send_message(chat_id, MSG_NO_URL)
        return
    if action == "dl_video":
        # Direct download in the highest available/original quality.
        # No quality menu is shown to the user.
        _run_guarded_download(
            chat_id,
            MSG_DOWNLOADING_ORIGINAL,
            lambda: handle_video_request(chat_id, url, "v_original", ORIGINAL_SELECTOR),
        )
        return

    # Kept only for backward compatibility if an old inline keyboard callback is received.
    if action in VIDEO_QUALITY_KEYS:
        selector = last_selectors_by_chat.get(chat_id, {}).get(action) or _selector_from_callback(action)
        _run_guarded_download(chat_id, MSG_DOWNLOADING_ORIGINAL, lambda: handle_video_request(chat_id, url, action, selector))
        return
    if action == "audio":
        _run_guarded_download(chat_id, MSG_DOWNLOADING, lambda: handle_audio_request(chat_id, url))
        return

def process_update(update: dict) -> None:
    try:
        if "message" in update:
            handle_message(update["message"])
        elif "callback_query" in update:
            handle_callback(update["callback_query"])
    except Exception as exc:
        logger.warning("process_update error: %s", exc)

# --------------------------------------------------------------------------- #
# FastAPI application
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(_: FastAPI):
    validate_config()
    cleanup_stale_temp_dirs()
    logger.info("Flags: NO_VIDEO_COMPRESSION=%s FAST_MODE=%s NO_REENCODE_BY_DEFAULT=%s SEND_VIDEO_AS_FILE_COPY=%s", NO_VIDEO_COMPRESSION, FAST_MODE, NO_REENCODE_BY_DEFAULT, SEND_VIDEO_AS_FILE_COPY)
    logger.info("Evasion: COOKIES_FILE=%s BROWSER_COOKIES=%s USER_AGENT=%s", bool(COOKIES_FILE), BROWSER_COOKIES, USER_AGENT[:50])
    register_webhook()
    yield

app = FastAPI(title="Telegram Downloader Bot", lifespan=lifespan)

@app.get("/")
def root() -> dict:
    return {"status": "ok"}

@app.get("/health")
def health() -> dict:
    return {"ok": True, "status": "healthy"}

@app.get("/webhook-info")
def webhook_info() -> dict:
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
async def telegram_webhook(secret: str, request: Request, background_tasks: BackgroundTasks) -> dict:
    if not WEBHOOK_SECRET or secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")
    header_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if header_secret is not None and header_secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")
    update = await request.json()
    background_tasks.add_task(process_update, update)
    return {"ok": True}
