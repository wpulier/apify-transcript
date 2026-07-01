from __future__ import annotations

import mimetypes
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

from .config import SUPPORTED_MEDIA_EXTENSIONS
from .utils import slugify


@dataclass(frozen=True)
class MediaSource:
    source_id: str
    original: str
    name: str


@dataclass(frozen=True)
class LocalMedia:
    source: MediaSource
    path: Path
    content_type: str


def parse_media_sources(actor_input: dict) -> list[MediaSource]:
    values: list[str] = []
    for key in ("mediaFiles", "mediaUrls"):
        raw = actor_input.get(key) or []
        if isinstance(raw, str):
            values.append(raw)
        elif isinstance(raw, list):
            values.extend(str(item) for item in raw if str(item).strip())
        else:
            raise ValueError(f"{key} must be a string or list of strings")
    sources = []
    for index, value in enumerate(values, 1):
        cleaned = value.strip()
        if not cleaned:
            continue
        sources.append(MediaSource(f"{index:03d}", cleaned, guess_source_name(cleaned, index)))
    if not sources:
        raise ValueError("Provide at least one uploaded media file or direct media URL")
    return sources


def guess_source_name(value: str, index: int) -> str:
    parsed = urlparse(value)
    if parsed.path:
        name = Path(unquote(parsed.path)).name
        if name:
            return name
    if Path(value).name and not re.match(r"^[a-z]+://", value, re.I):
        return Path(value).name
    return f"media-{index:03d}"


def ensure_supported_media(path: Path) -> None:
    if path.suffix.lower() not in SUPPORTED_MEDIA_EXTENSIONS:
        raise ValueError(
            f"unsupported media extension '{path.suffix}'. Supported extensions: "
            + ", ".join(sorted(SUPPORTED_MEDIA_EXTENSIONS))
        )


def download_source(source: MediaSource, target_dir: Path, apify_token: str | None = None) -> LocalMedia:
    target_dir.mkdir(parents=True, exist_ok=True)
    parsed = urlparse(source.original)
    if parsed.scheme in {"http", "https"}:
        return download_url_source(source, source.original, target_dir, apify_token)
    if parsed.scheme == "apify":
        return download_url_source(source, apify_to_api_url(source.original), target_dir, apify_token)
    local_path = Path(source.original).expanduser()
    if local_path.exists():
        ensure_supported_media(local_path)
        content_type = mimetypes.guess_type(local_path.name)[0] or "application/octet-stream"
        return LocalMedia(source=source, path=local_path.resolve(), content_type=content_type)
    raise ValueError(f"unsupported source or missing local file: {source.original}")


def apify_to_api_url(value: str) -> str:
    parsed = urlparse(value)
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.netloc == "key-value-stores" and len(parts) >= 3 and parts[1] == "records":
        store_id = parts[0]
        record_key = "/".join(parts[2:])
        return f"https://api.apify.com/v2/key-value-stores/{store_id}/records/{record_key}"
    raise ValueError(f"unsupported Apify file URL: {value}")


def download_url_source(
    source: MediaSource,
    url: str,
    target_dir: Path,
    apify_token: str | None = None,
) -> LocalMedia:
    headers = {}
    if apify_token and "api.apify.com/" in url and "token=" not in url:
        headers["Authorization"] = f"Bearer {apify_token}"
    suffix = Path(urlparse(url).path).suffix.lower() or Path(source.name).suffix.lower()
    if suffix and suffix not in SUPPORTED_MEDIA_EXTENSIONS:
        raise ValueError(f"unsupported media extension '{suffix}' in source URL")
    filename = f"{source.source_id}_{slugify(Path(source.name).stem, 'media')}{suffix or '.media'}"
    path = target_dir / filename
    with httpx.stream("GET", url, headers=headers, follow_redirects=True, timeout=300) as response:
        response.raise_for_status()
        content_type = response.headers.get("content-type") or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        with path.open("wb") as handle:
            for chunk in response.iter_bytes():
                if chunk:
                    handle.write(chunk)
    if path.suffix.lower() in SUPPORTED_MEDIA_EXTENSIONS:
        ensure_supported_media(path)
    return LocalMedia(source=source, path=path, content_type=content_type)


def require_ffmpeg() -> None:
    missing = [name for name in ("ffmpeg", "ffprobe") if shutil.which(name) is None]
    if missing:
        raise RuntimeError("Missing required executable(s): " + ", ".join(missing))


def run_command(command: list[str], failure_prefix: str) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        raise RuntimeError(f"{failure_prefix}: {detail}")
    return completed


def ffprobe_duration(media_path: Path) -> float | None:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nokey=1:noprint_wrappers=1",
            str(media_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode:
        return None
    try:
        return float(completed.stdout.strip())
    except ValueError:
        return None


def detect_speech_end_seconds(media_path: Path, fallback_duration: float | None) -> float | None:
    completed = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(media_path),
            "-vn",
            "-af",
            "silencedetect=noise=-50dB:d=2",
            "-f",
            "null",
            "-",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    text = completed.stderr or completed.stdout
    silence_start = None
    final_silence_start = None
    for line in text.splitlines():
        if "silence_start:" in line:
            try:
                silence_start = float(line.rsplit("silence_start:", 1)[1].strip())
                final_silence_start = silence_start
            except ValueError:
                pass
        elif "silence_end:" in line:
            final_silence_start = None
    if final_silence_start is not None and fallback_duration and fallback_duration - final_silence_start >= 30:
        return final_silence_start
    return fallback_duration
