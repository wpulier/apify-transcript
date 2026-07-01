from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any


def slugify(value: str, fallback: str = "media", max_length: int = 96) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip().lower()).strip("-._")
    slug = re.sub(r"-{2,}", "-", slug)
    return (slug or fallback)[:max_length].strip("-._") or fallback


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def json_safe(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        if hasattr(value, "model_dump"):
            return json_safe(value.model_dump())
        if hasattr(value, "dict"):
            return json_safe(value.dict())
        if isinstance(value, dict):
            return {str(k): json_safe(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [json_safe(v) for v in value]
        return str(value)


def timestamp(seconds: float | int | None) -> str:
    if seconds is None:
        return "00:00:00"
    seconds_float = max(float(seconds), 0.0)
    total = int(seconds_float)
    hours = total // 3600
    minutes = (total % 3600) // 60
    secs = total % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def subtitle_timestamp(seconds: float | int | None, separator: str = ",") -> str:
    seconds_float = max(float(seconds or 0), 0.0)
    whole = int(seconds_float)
    millis = int(round((seconds_float - whole) * 1000))
    if millis >= 1000:
        whole += 1
        millis -= 1000
    hours = whole // 3600
    minutes = (whole % 3600) // 60
    secs = whole % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{separator}{millis:03d}"


def normalized_word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9]+(?:['-][A-Za-z0-9]+)?", text or ""))


def ceil_minutes(seconds: float | int | None) -> int:
    return max(1, int(math.ceil(float(seconds or 0) / 60.0)))


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))

