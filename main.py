from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import os
import re
import shutil
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request as UrlRequest, build_opener

import yt_dlp
from yt_dlp.extractor import gen_extractor_classes
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from starlette.middleware.cors import CORSMiddleware

from client_address import resolve_client_ip
from storage_policy import StorageBudget, StorageBudgetError

APP_NAME = "SnapGrab Pro Media Engine"
BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
MAX_ACTIVE_JOBS = max(1, min(int(os.getenv("MAX_ACTIVE_JOBS", "3")), 8))
WORKERS = max(1, min(int(os.getenv("DOWNLOAD_WORKERS", "2")), 4))
MAX_DURATION_SECONDS = max(60, int(os.getenv("MAX_DURATION_SECONDS", "10800")))
MAX_FILE_BYTES = max(32, int(os.getenv("MAX_FILE_MB", "1024"))) * 1024 * 1024
MAX_TOTAL_STORAGE_BYTES = max(1, int(os.getenv("MAX_STORAGE_GB", "8"))) * 1024 * 1024 * 1024
MIN_FREE_DISK_BYTES = max(0, int(os.getenv("MIN_FREE_DISK_MB", "256"))) * 1024 * 1024
ENGINE_API_KEY = os.getenv("ENGINE_API_KEY", "").strip()
JOB_TTL_SECONDS = max(300, int(os.getenv("JOB_TTL_SECONDS", "3600")))
RATE_WINDOW_SECONDS = 60
RATE_LIMIT = max(6, int(os.getenv("RATE_LIMIT_PER_MINUTE", "24")))

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("snapgrab-media-engine")
jobs: dict[str, dict] = {}
jobs_lock = threading.RLock()
rate_events: dict[str, list[float]] = {}
rate_lock = threading.Lock()
executor = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="sg-media")
stop_cleanup = threading.Event()
TRUSTED_PROXY_CIDRS = os.getenv("TRUSTED_PROXY_CIDRS", "").strip()
storage_budget = StorageBudget(DOWNLOAD_DIR, MAX_TOTAL_STORAGE_BYTES, MIN_FREE_DISK_BYTES, MAX_FILE_BYTES)

# Build the registered extractor list once. GenericIE is deliberately excluded:
# accepting arbitrary generic URLs on a public service can turn the downloader
# into an SSRF proxy. This still covers sites with a dedicated yt-dlp extractor.
try:
    EXTRACTORS = tuple(
        cls for cls in gen_extractor_classes()
        if str(getattr(cls, "IE_NAME", "") or cls.ie_key() or "").lower() not in {
            "generic", "default", "youtube:search", "youtube:search_url", "youtube:tab"
        }
    )
except Exception:  # fail closed if the installed yt-dlp changes this API
    EXTRACTORS = tuple()


class URLRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)
    authorized: bool = False


class DownloadRequest(URLRequest):
    format: str = Field(default="mp4", pattern="^(mp4|webm|mp3|m4a|aac|flac|opus|ogg|wav)$")
    quality: str = Field(default="best", pattern="^(best|[1-9][0-9]{0,3})$")
    # Source format IDs are revalidated against yt-dlp output before download.
    video_format_id: str | None = Field(default=None, min_length=1, max_length=100, pattern="^[A-Za-z0-9._-]+$")
    audio_format_id: str | None = Field(default=None, min_length=1, max_length=100, pattern="^[A-Za-z0-9._-]+$")
    title: str | None = Field(default=None, max_length=400)


PINTEREST_HOST_SUFFIX = (
    "com", "fr", "de", "ch", "jp", "cl", "ca", "it", "co.uk", "nz", "ru",
    "com.au", "at", "pt", "co.kr", "es", "com.mx", "dk", "ph", "th", "com.uy",
    "co", "nl", "info", "kr", "ie", "vn", "com.vn", "ec", "mx", "co.in", "pe",
    "co.at", "hu", "co.nz", "id", "com.ec", "com.py", "tw", "be", "uk", "com.bo", "com.pe",
)


def _is_pinterest_host(host: str, allow_short: bool = True) -> bool:
    normalized = (host or "").lower().rstrip(".")
    if allow_short and normalized in {"pin.it", "www.pin.it"}:
        return True
    for suffix in PINTEREST_HOST_SUFFIX:
        if normalized == f"pinterest.{suffix}" or normalized.endswith(f".pinterest.{suffix}"):
            return True
    return False


def _ensure_public_dns(host: str) -> None:
    """Reject hosts with any non-public A/AAAA answer (an SSRF defense-in-depth layer)."""
    try:
        answers = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise HTTPException(status_code=422, detail="تعذّر التحقق من عنوان خادم المصدر عبر DNS.") from exc
    addresses = {answer[4][0].split("%", 1)[0] for answer in answers}
    if not addresses:
        raise HTTPException(status_code=422, detail="تعذّر التحقق من عنوان خادم المصدر عبر DNS.")
    for address in addresses:
        try:
            parsed_ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="عنوان DNS غير صالح.") from exc
        if not parsed_ip.is_global:
            raise HTTPException(status_code=422, detail="خادم المصدر يحلّ إلى عنوان غير عام؛ تم رفض الطلب.")


def _validate_pinterest_redirect(target: str) -> str:
    try:
        parsed = urlsplit(target)
        host = (parsed.hostname or "").lower().rstrip(".")
        _ = parsed.port
    except ValueError as exc:
        raise HTTPError(target, 400, "Malformed redirect URL", {}, None) from exc
    if parsed.scheme.lower() != "https" or parsed.username or parsed.password or parsed.port:
        raise HTTPError(target, 400, "Unsafe redirect URL", {}, None)
    if not _is_pinterest_host(host):
        raise HTTPError(target, 403, "Redirect host is not an approved Pinterest host", {}, None)
    _ensure_public_dns(host)
    return target


def _canonical_pinterest_pin(final_url: str) -> str:
    try:
        parsed = urlsplit(final_url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="صيغة رابط Pinterest المختصر غير صالحة.") from exc
    host = (parsed.hostname or "").lower().rstrip(".")
    match = re.search(r"/pin/(?:[\w-]+--)?(?P<id>\d+)(?:/|$)", parsed.path, re.I)
    if parsed.scheme.lower() != "https" or not _is_pinterest_host(host, allow_short=False) or not match:
        raise HTTPException(status_code=422, detail="رابط Pinterest المختصر لم يوجّه إلى Pin عام معروف.")
    return f"https://www.pinterest.com/pin/{match.group('id')}/"


def resolve_pinterest_short_url(value: str) -> str:
    """Resolve pin.it while allowing redirects only to public Pinterest hosts."""
    original = urlsplit(value)
    _ensure_public_dns((original.hostname or "pin.it").lower().rstrip("."))

    class PinterestOnlyRedirects(HTTPRedirectHandler):
        max_redirections = 5
        max_repeats = 2

        def redirect_request(self, req, fp, code, msg, headers, newurl):
            target = urljoin(req.full_url, newurl)
            _validate_pinterest_redirect(target)
            return super().redirect_request(req, fp, code, msg, headers, target)

    request = UrlRequest(
        value,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; SnapGrab-Pro/3.9; link-resolver)",
            "Range": "bytes=0-0",
        },
        method="GET",
    )
    try:
        opener = build_opener(PinterestOnlyRedirects())
        with opener.open(request, timeout=8) as response:
            final_url = response.geturl()
        return _canonical_pinterest_pin(final_url)
    except HTTPException:
        raise
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        logger.info("Pinterest short-link resolution failed: %s", type(exc).__name__)
        raise HTTPException(status_code=422, detail="تعذّر فتح رابط Pinterest المختصر بأمان. جرّب رابط Pin الكامل.") from exc


def _check_public_hostname(host: str) -> None:
    if not host or host in {"localhost", "localhost.localdomain"}:
        raise HTTPException(status_code=400, detail="اسم المضيف غير مسموح به.")
    if host.endswith((".localhost", ".local", ".internal", ".test", ".invalid", ".example", ".onion")):
        raise HTTPException(status_code=400, detail="عنوان الشبكة المحلية غير مسموح به.")
    # Platform extractors should be domain-based. Reject IP literals entirely,
    # including loopback/private/link-local targets, to reduce SSRF exposure.
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return
    raise HTTPException(status_code=400, detail="روابط عناوين IP المباشرة غير مسموح بها.")


@lru_cache(maxsize=512)
def _matching_extractor(url: str) -> str | None:
    for extractor in EXTRACTORS:
        try:
            if extractor.suitable(url):
                return str(extractor.ie_key() or extractor.IE_NAME or "supported")
        except Exception:
            continue
    return None


def normalize_and_validate_url(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        raise HTTPException(status_code=400, detail="أدخل رابطًا صحيحًا.")
    if not re.match(r"^https?://", value, flags=re.I):
        value = "https://" + value
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        _ = parsed.port  # Validate malformed port syntax.
    except ValueError:
        raise HTTPException(status_code=400, detail="صيغة الرابط غير صحيحة.")
    if parsed.scheme.lower() != "https":
        raise HTTPException(status_code=400, detail="استخدم رابط HTTPS آمنًا.")
    if parsed.username or parsed.password or parsed.port:
        raise HTTPException(status_code=400, detail="الرابط يحتوي على بيانات دخول أو منفذ غير مسموح.")
    _check_public_hostname(host)
    if not parsed.path or parsed.path == "/":
        raise HTTPException(status_code=400, detail="الرابط لا يحتوي على محتوى محدد.")
    if host in {"pin.it", "www.pin.it"}:
        value = resolve_pinterest_short_url(value)
    extractor = _matching_extractor(value)
    if not extractor:
        raise HTTPException(
            status_code=422,
            detail="هذا الرابط لا يطابق مستخرجًا معروفًا في yt-dlp. بعض المواقع قد تتعطل أو لا تكون مدعومة.",
        )
    # Verify DNS after recognizing a dedicated extractor, reducing DNS lookup abuse.
    # Production should additionally enforce outbound network egress rules because
    # this is not a substitute for pinning the resolved address at connection time.
    final_host = (urlsplit(value).hostname or "").lower().rstrip(".")
    _ensure_public_dns(final_host)
    return value


def require_authorization(authorized: bool) -> None:
    if not authorized:
        raise HTTPException(status_code=400, detail="أكد أولًا أن لديك الإذن اللازم لتنزيل هذا المحتوى.")


def _request_is_loopback(request: Request) -> bool:
    host = request.client.host if request.client else ""
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return host.lower() in {"localhost", "testclient"}


def _valid_download_ticket(job_id: str, expires_raw: str, ticket: str) -> bool:
    if not ENGINE_API_KEY or not re.fullmatch(r"[a-f0-9]{32}", job_id):
        return False
    if not str(expires_raw or "").isdigit() or not re.fullmatch(r"[a-f0-9]{64}", str(ticket or "")):
        return False
    expires = int(expires_raw)
    now = int(time.time())
    if expires < now or expires > now + 10 * 60 + 30:
        return False
    expected = hmac.new(ENGINE_API_KEY.encode("utf-8"), f"{job_id}:{expires}".encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, ticket)


def require_engine_access(request: Request, ticket_job_id: str | None = None) -> None:
    # Remote API calls require a server-side Bearer key. Without a key, only
    # loopback requests are accepted for local development.
    if ENGINE_API_KEY:
        authorization = str(request.headers.get("authorization") or "")
        if hmac.compare_digest(authorization, f"Bearer {ENGINE_API_KEY}"):
            return
        if ticket_job_id and _valid_download_ticket(
            ticket_job_id,
            request.query_params.get("expires", ""),
            request.query_params.get("ticket", ""),
        ):
            return
        raise HTTPException(status_code=401, detail="يلزم مفتاح وصول صالح لمحرك الوسائط.")
    if not _request_is_loopback(request):
        raise HTTPException(status_code=503, detail="المحرك المحلي فقط مفتوح افتراضيًا. اضبط ENGINE_API_KEY عشوائيًا قويًا قبل نشره على الإنترنت.")


def cleanup_orphaned_downloads() -> None:
    # Job state is in-memory; files left by a restart cannot be addressed by ID.
    for item in DOWNLOAD_DIR.iterdir():
        try:
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            elif item.is_file():
                item.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not clean orphaned download item: %s", item.name)


def rate_limit(request: Request, limit: int | None = None) -> None:
    allowed_limit = max(1, int(limit or RATE_LIMIT))
    peer = request.client.host if request.client else "unknown"
    client_ip = resolve_client_ip(peer, request.headers.get("x-forwarded-for"), TRUSTED_PROXY_CIDRS)
    now = time.time()
    with rate_lock:
        timestamps = [stamp for stamp in rate_events.get(client_ip, []) if now - stamp < RATE_WINDOW_SECONDS]
        if len(timestamps) >= allowed_limit:
            raise HTTPException(status_code=429, detail="طلبات كثيرة خلال دقيقة واحدة. حاول مرة أخرى بعد قليل.")
        timestamps.append(now)
        rate_events[client_ip] = timestamps
        if len(rate_events) > 5000:
            for key in list(rate_events)[:1000]:
                if not rate_events[key] or now - rate_events[key][-1] >= RATE_WINDOW_SECONDS:
                    rate_events.pop(key, None)


def seconds_label(seconds: int | None) -> str:
    if not seconds or seconds < 0:
        return "غير معروف"
    hours, remainder = divmod(int(seconds), 3600)
    minutes, sec = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{sec:02d}" if hours else f"{minutes}:{sec:02d}"


def readable_error(exc: Exception) -> str:
    message = str(exc).lower()
    if any(word in message for word in ("private video", "video is private", "members-only", "members only")):
        return "المحتوى خاص أو يتطلب صلاحية وصول. لا يحاول المحرك تجاوز القيود."
    if re.search(r"\bage(?:-restricted)?\b|sign[ -]?in|login required|authentication required|cookies", message):
        return "المحتوى يتطلب تسجيل دخول أو تحققًا إضافيًا غير مدعوم في هذا المحرك."
    if "po token" in message or "potoken" in message:
        return "YouTube طلب رمز PO Token لبعض الصيغ. حدّث yt-dlp وتحقق من إعداداته؛ لا يحاول المحرك تجاوز حماية المنصة."
    if "ffmpeg" in message:
        return "أداة FFmpeg غير متاحة. ثبّتها أو استخدم صورة Docker المرفقة."
    if "unsupported url" in message or "no suitable extractor" in message:
        return "الرابط غير مدعوم حاليًا أو أن المنصة غيّرت طريقة عرض المحتوى."
    if "file is larger than" in message or "max-filesize" in message:
        return "حجم الملف يتجاوز الحد المحدد على الخادم."
    if "storage quota" in message or "free disk space" in message or "storage cap" in message:
        return "مساحة التخزين المتاحة للمحرك لا تكفي أو وصلت إلى الحد المقرر. حاول لاحقًا أو تواصل مع مشغل الخدمة."
    if "duration" in message and "limit" in message:
        return f"مدة الملف تتجاوز الحد المضبوط ({MAX_DURATION_SECONDS // 3600} ساعة/ساعات)."
    return "تعذّر إكمال الطلب. قد تكون المنصة غيّرت نظامها أو أن المحتوى غير متاح. راجع سجل الخادم للتفاصيل."


def base_ydl_options() -> dict:
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "ignoreerrors": False,
        "socket_timeout": 25,
        # YouTube extraction requires a supported JavaScript runtime and EJS.
        # The runtime is installed by Docker/Windows instructions; no cookies are supplied.
        "js_runtimes": {"node": {}},
        "retries": 3,
        "extractor_retries": 2,
        "file_access_retries": 2,
        "continuedl": False,
        "overwrites": False,
        "max_filesize": MAX_FILE_BYTES,
        "match_filter": lambda info, *, incomplete=False: (
            f"الفيديو أطول من الحد المسموح ({MAX_DURATION_SECONDS // 3600} ساعات)."
            if info.get("duration") and info["duration"] > MAX_DURATION_SECONDS else None
        ),
    }


def _format_bytes(item: dict) -> tuple[int | None, bool]:
    """Return a source size (or estimate), without treating unknown as zero bytes."""
    exact = item.get("filesize")
    estimated = item.get("filesize_approx")
    for value, is_estimate in ((exact, False), (estimated, True)):
        try:
            number = int(value)
            if number > 0:
                return number, is_estimate
        except (TypeError, ValueError, OverflowError):
            pass
    return None, True


def _safe_format_id(item: dict) -> str | None:
    value = str(item.get("format_id") or "")
    return value if re.fullmatch(r"[A-Za-z0-9._-]{1,100}", value) else None


def _build_download_options(formats: list[dict]) -> list[dict]:
    """Collapse provider variants to one best-listed stream per resolution/container."""
    usable = []
    for item in formats:
        if not isinstance(item, dict) or item.get("vcodec") in (None, "none"):
            continue
        ext = str(item.get("ext") or "").lower()
        if ext not in {"mp4", "webm"}:
            continue
        format_id = _safe_format_id(item)
        try:
            height = int(item.get("height") or 0)
        except (TypeError, ValueError, OverflowError):
            height = 0
        if not format_id or height <= 0:
            continue
        size, size_is_estimate = _format_bytes(item)
        try:
            fps = int(float(item.get("fps") or 0)) or None
        except (TypeError, ValueError, OverflowError):
            fps = None
        try:
            width = int(item.get("width") or 0) or None
        except (TypeError, ValueError, OverflowError):
            width = None
        try:
            bitrate = float(item.get("tbr") or item.get("vbr") or 0)
        except (TypeError, ValueError, OverflowError):
            bitrate = 0
        usable.append({
            "format_id": format_id, "height": height, "width": width, "fps": fps,
            "container": ext, "has_audio": item.get("acodec") not in (None, "none"),
            "size": size, "size_is_estimate": size_is_estimate,
            "bitrate": bitrate, "vcodec": str(item.get("vcodec") or "")[:80],
        })

    # Preserve meaningfully different source formats: resolution, container,
    # frame rate, codec family and muxed-vs-split audio. Collapse only duplicate
    # representations, preferring the richer bitrate/size entry.
    best_by_key: dict[tuple, dict] = {}
    for item in usable:
        codec = str(item["vcodec"]).split(".", 1)[0].split("_", 1)[0][:24].lower()
        key = (item["height"], item["container"], item["fps"], codec, item["has_audio"])
        score = (item["bitrate"], item["size"] or 0)
        prior = best_by_key.get(key)
        prior_score = (prior["bitrate"], prior["size"] or 0) if prior else None
        if prior is None or score > prior_score:
            best_by_key[key] = item

    audio_by_ext: dict[str, dict] = {}
    for item in formats:
        if not isinstance(item, dict) or item.get("vcodec") not in (None, "none") or item.get("acodec") in (None, "none"):
            continue
        audio_id = _safe_format_id(item)
        if not audio_id:
            continue
        ext = str(item.get("ext") or "").lower()
        # MP4 commonly pairs with M4A; WebM commonly pairs with WebM/Opus.
        bucket = "mp4" if ext in {"m4a", "mp4"} else "webm" if ext in {"webm", "opus"} else "other"
        if bucket == "other":
            continue
        try:
            abr = float(item.get("abr") or item.get("tbr") or 0)
        except (TypeError, ValueError, OverflowError):
            abr = 0
        size, size_is_estimate = _format_bytes(item)
        candidate = {"format_id": audio_id, "size": size, "size_is_estimate": size_is_estimate, "bitrate": abr}
        if bucket not in audio_by_ext or (abr, size or 0) > (audio_by_ext[bucket]["bitrate"], audio_by_ext[bucket]["size"] or 0):
            audio_by_ext[bucket] = candidate

    options = []
    for key, item in best_by_key.items():
        height, ext = key[0], key[1]
        audio = None if item["has_audio"] else audio_by_ext.get(ext)
        total_size = item["size"]
        estimated = item["size_is_estimate"]
        audio_id = None
        if audio:
            audio_id = audio["format_id"]
            if item["size"] is not None and audio["size"] is not None:
                total_size = item["size"] + audio["size"]
                estimated = estimated or audio["size_is_estimate"]
            else:
                # Do not show just one stream's size as if it described the
                # final muxed video; the real combined size is unknown.
                total_size = None
                estimated = True
        elif not item["has_audio"]:
            # An audio-less row would surprise users expecting a complete video.
            continue
        options.append({
            "format_id": item["format_id"], "audio_format_id": audio_id,
            "height": height, "width": item["width"], "fps": item["fps"],
            "container": ext, "filesize": total_size, "size_is_estimate": estimated,
            "has_audio": item["has_audio"], "video_codec": item["vcodec"],
            "bitrate_kbps": round(item["bitrate"], 1) if item["bitrate"] else None,
        })
    options.sort(key=lambda x: (-x["height"], -(x["fps"] or 0), 0 if x["container"] == "mp4" else 1, -(x["bitrate_kbps"] or 0)))
    return options[:160]


def extract_media_info(url: str) -> dict:
    opts = base_ydl_options()
    opts.update({"skip_download": True, "extract_flat": False})
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        raise RuntimeError("No media information returned")
    if info.get("_type") in {"playlist", "multi_video"} or info.get("entries"):
        raise RuntimeError("Playlists are disabled; provide a single-item URL")
    raw_duration = info.get("duration")
    try:
        duration = max(0, int(float(raw_duration))) if raw_duration is not None else None
    except (TypeError, ValueError, OverflowError):
        duration = None
    if duration and duration > MAX_DURATION_SECONDS:
        raise RuntimeError("Media exceeds the configured duration limit")
    raw_formats = info.get("formats") or []
    formats = [item for item in raw_formats if isinstance(item, dict)] if isinstance(raw_formats, (list, tuple)) else []
    heights: set[int] = set()
    for item in formats:
        if item.get("vcodec") in (None, "none"):
            continue
        try:
            height = int(item.get("height") or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        if height > 0:
            heights.add(height)
    title = str(info.get("title") or "Media file")[:400]
    return {
        "id": str(info.get("id") or ""),
        "title": title,
        "channel": str(info.get("channel") or info.get("uploader") or info.get("creator") or "")[:160],
        "thumbnail": str(info.get("thumbnail") or "")[:2048],
        "duration_seconds": duration if duration else None,
        "duration_label": seconds_label(duration) if duration else "غير معروف",
        "description": str(info.get("description") or "")[:400],
        "webpage_url": str(info.get("webpage_url") or url)[:2048],
        "extractor": str(info.get("extractor_key") or info.get("extractor") or "yt-dlp")[:80],
        "available_heights": sorted(heights, reverse=True)[:100],
        "download_options": _build_download_options(formats),
        "audio_available": any(item.get("acodec") not in (None, "none") for item in formats),
    }

AUDIO_OUTPUT_CODECS = {
    "mp3": "mp3", "m4a": "m4a", "aac": "aac", "flac": "flac",
    "opus": "opus", "ogg": "vorbis", "wav": "wav",
}
AUDIO_OUTPUT_EXTENSIONS = {key: f".{key}" for key in AUDIO_OUTPUT_CODECS}
MEDIA_TYPE_BY_EXTENSION = {
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
    ".flac": "audio/flac", ".opus": "audio/opus", ".ogg": "audio/ogg",
    ".wav": "audio/wav", ".webm": "video/webm", ".mp4": "video/mp4",
}


def media_type_for_extension(extension: str) -> str:
    """Return the correct media type for generated output files."""
    return MEDIA_TYPE_BY_EXTENSION.get(str(extension or "").lower(), "application/octet-stream")


def choose_format(video_format: str, quality: str, video_format_id: str | None = None, audio_format_id: str | None = None) -> tuple[str, list[dict] | None]:
    if video_format in AUDIO_OUTPUT_CODECS:
        codec = AUDIO_OUTPUT_CODECS[video_format]
        quality = "0" if video_format in {"mp3", "flac", "wav"} else "5"
        return "bestaudio/best", [{"key": "FFmpegExtractAudio", "preferredcodec": codec, "preferredquality": quality}]
    if video_format_id:
        # IDs are revalidated against the fresh extractor result inside run_download.
        selector = video_format_id + (f"+{audio_format_id}" if audio_format_id else "")
        return selector, None
    if quality == "best":
        selector = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/bestvideo+bestaudio/best"
    else:
        height = int(quality)
        selector = (
            f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/"
            f"best[height<={height}][ext=mp4]/"
            f"bestvideo[height<={height}]+bestaudio/best[height<={height}]"
        )
    return selector, None


def update_job(job_id: str, **changes) -> None:
    with jobs_lock:
        job = jobs.get(job_id)
        if job:
            job.update(changes)
            job["updated_at"] = time.time()


def run_download(job_id: str, url: str, requested_format: str, quality: str, video_format_id: str | None = None, audio_format_id: str | None = None) -> None:
    job_dir = DOWNLOAD_DIR / job_id
    try:
        job_dir.mkdir(parents=True, exist_ok=True)
        update_job(job_id, status="downloading", progress=0, message="جارٍ الاتصال بالمصدر…")

        last_storage_check = [0.0]
        def on_progress(data: dict) -> None:
            status = data.get("status")
            if status == "downloading":
                total = data.get("total_bytes") or data.get("total_bytes_estimate")
                done = int(data.get("downloaded_bytes") or 0)
                if done > MAX_FILE_BYTES:
                    raise RuntimeError("File is larger than the configured maximum")
                now = time.monotonic()
                if now - last_storage_check[0] >= 1.0:
                    storage_budget.refresh(job_id)
                    last_storage_check[0] = now
                percent = round((done / total) * 100, 1) if total else 0
                update_job(job_id, status="downloading", progress=max(0, min(98, percent)), message="جارٍ تنزيل البيانات…")
            elif status == "finished":
                storage_budget.refresh(job_id)
                update_job(job_id, status="processing", progress=99, message="تم التنزيل، جارٍ تجهيز الملف…")

        selector, postprocessors = choose_format(requested_format, quality, video_format_id, audio_format_id)
        opts = base_ydl_options()
        opts.update({
            "format": selector,
            "outtmpl": str(job_dir / "%(id)s.%(ext)s"),
            "progress_hooks": [on_progress],
            "windowsfilenames": True,
            "restrictfilenames": True,
            "noplaylist": True,
            "merge_output_format": requested_format if requested_format in {"mp4", "webm"} else None,
            "postprocessors": postprocessors or [],
        })
        if opts.get("merge_output_format") is None:
            opts.pop("merge_output_format", None)
        with yt_dlp.YoutubeDL(opts) as ydl:
            if video_format_id and requested_format in {"mp4", "webm"}:
                # Verify the caller's format IDs still belong to this exact URL.
                info = ydl.extract_info(url, download=False)
                if not info or info.get("_type") in {"playlist", "multi_video"} or info.get("entries"):
                    raise RuntimeError("Playlist is not supported")
                formats = [f for f in (info.get("formats") or []) if isinstance(f, dict)]
                selected = next((f for f in formats if _safe_format_id(f) == video_format_id and f.get("vcodec") not in (None, "none")), None)
                if not selected:
                    raise RuntimeError("Selected video format is no longer available")
                if str(selected.get("ext") or "").lower() != requested_format:
                    raise RuntimeError("Selected container does not match requested output format")
                if selected.get("acodec") not in (None, "none") and audio_format_id:
                    raise RuntimeError("Selected video format already contains audio")
                if selected.get("acodec") in (None, "none"):
                    chosen_audio = next((f for f in formats if audio_format_id and _safe_format_id(f) == audio_format_id and f.get("vcodec") in (None, "none") and f.get("acodec") not in (None, "none")), None)
                    if not chosen_audio:
                        raise RuntimeError("Selected audio format is no longer available")
                    audio_ext = str(chosen_audio.get("ext") or "").lower()
                    if requested_format == "mp4" and audio_ext not in {"m4a", "mp4"}:
                        raise RuntimeError("Selected audio format is incompatible with MP4 output")
                    if requested_format == "webm" and audio_ext not in {"webm", "opus"}:
                        raise RuntimeError("Selected audio format is incompatible with WebM output")
                ydl.params["format"] = selector
                ydl.process_ie_result(info, download=True)
            else:
                ydl.download([url])
        expected_extension = (AUDIO_OUTPUT_EXTENSIONS[requested_format] if requested_format in AUDIO_OUTPUT_EXTENSIONS
                              else ".webm" if requested_format == "webm" else ".mp4")
        files = [p for p in job_dir.iterdir() if p.is_file() and p.suffix.lower() == expected_extension]
        if not files:
            candidates = [p for p in job_dir.iterdir() if p.is_file() and not p.name.endswith((".part", ".ytdl"))]
            if candidates:
                raise RuntimeError(f"Requested {expected_extension} output was not produced")
            raise RuntimeError("Download completed but no output file was produced")
        output_file = max(files, key=lambda p: p.stat().st_mtime)
        if output_file.stat().st_size > MAX_FILE_BYTES:
            raise RuntimeError("File is larger than the configured maximum")
        with jobs_lock:
            job = jobs.get(job_id)
            if job:
                title = job.get("title") or output_file.stem
                safe_title = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", title).strip(" .")[:120] or "media"
                job.update({
                    "status": "done", "progress": 100,
                    "message": "الملف جاهز للتنزيل.", "file_path": str(output_file),
                    "filename": f"{safe_title}{expected_extension}",
                    "updated_at": time.time(), "format": requested_format,
                })
    except Exception as exc:
        logger.exception("Media download job %s failed", job_id)
        shutil.rmtree(job_dir, ignore_errors=True)
        update_job(job_id, status="error", progress=0, message=readable_error(exc))
    finally:
        storage_budget.release(job_id)


def cleanup_expired_loop() -> None:
    while not stop_cleanup.wait(300):
        now = time.time()
        with jobs_lock:
            expired = [
                job_id for job_id, job in jobs.items()
                if job.get("status") in {"done", "error"}
                and now - job.get("updated_at", now) > JOB_TTL_SECONDS
            ]
            for job_id in expired:
                jobs.pop(job_id, None)
        for job_id in expired:
            shutil.rmtree(DOWNLOAD_DIR / job_id, ignore_errors=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    cleanup_orphaned_downloads()
    worker = threading.Thread(target=cleanup_expired_loop, daemon=True, name="media-cleanup")
    worker.start()
    yield
    stop_cleanup.set()
    executor.shutdown(wait=False, cancel_futures=True)


app = FastAPI(title=APP_NAME, version="1.0.0", lifespan=lifespan)
_origins = [item.strip() for item in os.getenv("MEDIA_ENGINE_ALLOWED_ORIGINS", "http://127.0.0.1:3000,http://localhost:3000").split(",") if item.strip()]
_cors_options = {
    "allow_origins": _origins,
    "allow_credentials": False,
    "allow_methods": ["GET", "POST", "OPTIONS"],
    "allow_headers": ["Content-Type", "Authorization"],
    "expose_headers": ["Content-Disposition", "Content-Length"],
    "max_age": 600,
}
# Chrome extension access is opt-in. Enable only for a trusted, self-hosted engine;
# do not turn this on for a public shared service without adding authentication.
if os.getenv("MEDIA_ENGINE_ALLOW_CHROME_EXTENSIONS", "false").strip().lower() in {"1", "true", "yes", "on"}:
    _cors_options["allow_origin_regex"] = r"chrome-extension://[a-p]{32}"
app.add_middleware(CORSMiddleware, **_cors_options)


@app.get("/api/health")
def health():
    return {
        "ok": True, "service": APP_NAME,
        "yt_dlp_version": str(getattr(yt_dlp.version, "__version__", "unknown")),
        "supported_extractor_count": len(EXTRACTORS),
        "ffmpeg_available": shutil.which("ffmpeg") is not None,
        "node_available": shutil.which("node") is not None,
    }


@app.post("/api/info")
def get_info(payload: URLRequest, request: Request):
    require_engine_access(request)
    rate_limit(request)
    require_authorization(payload.authorized)
    url = normalize_and_validate_url(payload.url)
    try:
        return extract_media_info(url)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Media info extraction failed")
        raise HTTPException(status_code=422, detail=readable_error(exc))


@app.post("/api/download")
def start_download(payload: DownloadRequest, request: Request):
    require_engine_access(request)
    rate_limit(request)
    require_authorization(payload.authorized)
    url = normalize_and_validate_url(payload.url)
    job_id = uuid.uuid4().hex
    with jobs_lock:
        active = sum(1 for item in jobs.values() if item.get("status") in {"queued", "downloading", "processing"})
        if active >= MAX_ACTIVE_JOBS:
            raise HTTPException(status_code=429, detail="محرك التنزيل مشغول الآن. انتظر حتى تنتهي مهمة أخرى.")
        try:
            storage_budget.reserve(job_id)
        except StorageBudgetError as exc:
            raise HTTPException(status_code=507, detail=readable_error(exc)) from exc
        job = {
            "id": job_id, "status": "queued", "progress": 0,
            "message": "تم وضع الطلب في قائمة التنزيل.",
            "title": (payload.title or "").strip()[:400], "format": payload.format,
            "created_at": time.time(), "updated_at": time.time(),
            "file_path": None, "filename": None,
        }
        jobs[job_id] = job
    try:
        executor.submit(run_download, job_id, url, payload.format, payload.quality, payload.video_format_id, payload.audio_format_id)
    except RuntimeError as exc:
        storage_budget.release(job_id)
        update_job(job_id, status="error", progress=0, message="محرك التنزيل غير متاح حاليًا. حاول مرة أخرى لاحقًا.")
        raise HTTPException(status_code=503, detail="محرك التنزيل غير متاح حاليًا. حاول مرة أخرى لاحقًا.") from exc
    return {"id": job_id, "status": "queued"}


@app.get("/api/status/{job_id}")
def get_status(job_id: str, request: Request):
    require_engine_access(request)
    rate_limit(request, limit=90)  # polling is frequent by design; metadata/job creation remain stricter
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise HTTPException(status_code=404, detail="طلب التنزيل غير موجود.")
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="انتهت صلاحية الطلب أو لم يتم العثور عليه.")
        return {key: job.get(key) for key in ("id", "status", "progress", "message", "title", "format", "filename")}


def cleanup_downloaded_file(job_id: str) -> None:
    shutil.rmtree(DOWNLOAD_DIR / job_id, ignore_errors=True)
    update_job(job_id, file_path=None, downloaded=True, updated_at=time.time())


@app.get("/api/download/{job_id}")
def fetch_file(job_id: str, request: Request):
    require_engine_access(request, ticket_job_id=job_id)
    rate_limit(request, limit=12)
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise HTTPException(status_code=404, detail="ملف التنزيل غير موجود.")
    with jobs_lock:
        job = jobs.get(job_id)
        if not job or job.get("status") != "done" or not job.get("file_path"):
            raise HTTPException(status_code=404, detail="الملف غير جاهز أو انتهت صلاحيته.")
        file_path = Path(job["file_path"]).resolve()
        expected_dir = (DOWNLOAD_DIR / job_id).resolve()
        if expected_dir not in file_path.parents or not file_path.is_file():
            raise HTTPException(status_code=404, detail="ملف التنزيل غير موجود.")
        filename = job.get("filename") or file_path.name
        suffix = file_path.suffix.lower()
        media_type = media_type_for_extension(suffix)
    return FileResponse(
        file_path, media_type=media_type, filename=filename,
        background=BackgroundTask(cleanup_downloaded_file, job_id),
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
