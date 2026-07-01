from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from apify import Actor
from apify_shared.utils import create_hmac_signature

from .billing import charge_transcription_minutes, ensure_budget
from .config import TranscriptConfig
from .media import download_source, ffprobe_duration, parse_media_sources, require_ffmpeg
from .transcript import artifact_payloads, transcribe_media
from .utils import ceil_minutes, slugify


ACTOR_ID = "kTgaX3cfI6dlJHa6J"
APIFY_API_BASE_URL = "https://api.apify.com/v2"


def artifact_key(source_id: str, source_name: str, suffix: str) -> str:
    stem = slugify(Path(source_name).stem, "media")
    if suffix == "quality.json":
        return f"{source_id}_{stem}.quality.json"
    return f"{source_id}_{stem}.{suffix}"


def artifact_url(key: str, store_id: str | None = None, signing_secret: str | None = None) -> str | None:
    store_id = store_id or os.environ.get("APIFY_DEFAULT_KEY_VALUE_STORE_ID")
    if not store_id:
        return None
    url = f"{APIFY_API_BASE_URL}/key-value-stores/{store_id}/records/{quote(key, safe='')}"
    if signing_secret:
        signature = create_hmac_signature(signing_secret, key)
        return f"{url}?signature={quote(signature, safe='')}"
    return url


def artifact_urls(keys: dict[str, str], signing_secret: str | None = None) -> dict[str, str]:
    urls = {}
    for suffix, key in keys.items():
        url = artifact_url(key, signing_secret=signing_secret)
        if url:
            urls[suffix] = url
    return urls


async def default_store_signing_secret() -> str | None:
    store_id = os.environ.get("APIFY_DEFAULT_KEY_VALUE_STORE_ID")
    token = os.environ.get("APIFY_TOKEN")
    if not store_id or not token:
        return None
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"{APIFY_API_BASE_URL}/key-value-stores/{store_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
    except Exception:
        return None
    data = response.json().get("data") or {}
    secret = data.get("urlSigningSecretKey")
    return str(secret) if secret else None


async def store_artifacts(actor: object, source_id: str, source_name: str, payloads: dict[str, tuple[bytes, str]]) -> dict[str, str]:
    keys = {}
    for suffix, (content, content_type) in payloads.items():
        key = artifact_key(source_id, source_name, suffix)
        await actor.set_value(key, content, content_type=content_type)
        keys[suffix] = key
    return keys


async def process_one(actor: object, source: Any, config: TranscriptConfig, work_dir: Path) -> dict[str, Any]:
    await actor.set_status_message(f"Preparing {source.name}")
    local = download_source(source, work_dir / "sources", os.environ.get("APIFY_TOKEN"))
    duration = ffprobe_duration(local.path)
    await ensure_budget(actor, duration)

    await actor.set_status_message(f"Transcribing {source.name}")
    bundle = transcribe_media(
        local.path,
        config,
        work_dir / "tmp" / source.source_id,
        log=lambda message: actor.log.info("%s: %s", source.name, message),
    )
    await actor.set_status_message(f"Rendering artifacts for {source.name}")
    payloads = artifact_payloads(bundle, include_zip=config.include_zip)
    await actor.set_status_message(f"Checking billing for {source.name}")
    billing = await charge_transcription_minutes(
        actor,
        duration or bundle.source_duration,
        required=config.require_successful_charge,
    )
    await actor.set_status_message(f"Writing artifacts for {source.name}")
    keys = await store_artifacts(actor, source.source_id, source.name, payloads)
    await actor.set_status_message(f"Signing artifact links for {source.name}")
    signing_secret = await default_store_signing_secret()
    urls = artifact_urls(keys, signing_secret)
    row = {
        "status": "completed",
        "sourceId": source.source_id,
        "sourceName": source.name,
        "source": source.original,
        "provider": bundle.provider,
        "model": bundle.model,
        "qualityStatus": bundle.quality_status,
        "wordCount": bundle.word_count,
        "speakerCount": bundle.speaker_count,
        "sourceDuration": bundle.source_duration,
        "transcriptEndTime": bundle.transcript_end_time,
        "billingEvent": billing.event_name,
        "billableMinutes": ceil_minutes(duration or bundle.source_duration),
        "charged": billing.charged,
        "chargeMessage": billing.message,
        "artifactKeys": keys,
        "txtKey": keys.get("txt"),
        "jsonKey": keys.get("json"),
        "srtKey": keys.get("srt"),
        "vttKey": keys.get("vtt"),
        "qualityKey": keys.get("quality.json"),
        "zipKey": keys.get("zip"),
        "artifactUrls": urls,
        "txtUrl": urls.get("txt"),
        "jsonUrl": urls.get("json"),
        "srtUrl": urls.get("srt"),
        "vttUrl": urls.get("vtt"),
        "qualityUrl": urls.get("quality.json"),
        "zipUrl": urls.get("zip"),
        "warnings": bundle.quality.get("warnings", []),
        "failures": bundle.quality.get("failures", []),
        "error": None,
    }
    await actor.push_data(row)
    return row


async def run(actor: object = Actor) -> dict[str, Any]:
    actor_input = await actor.get_input() or {}
    config = TranscriptConfig.from_input(actor_input, os.environ)
    sources = parse_media_sources(actor_input)
    require_ffmpeg()

    results = []
    with tempfile.TemporaryDirectory(prefix="apify-transcript-") as temp_dir:
        work_dir = Path(temp_dir)
        for source in sources:
            try:
                results.append(await process_one(actor, source, config, work_dir))
            except Exception as exc:
                row = {
                    "status": "failed",
                    "sourceId": source.source_id,
                    "sourceName": source.name,
                    "source": source.original,
                    "provider": config.provider,
                    "model": config.openai_model_for_mode() if config.provider != "elevenlabs" else config.elevenlabs_model,
                    "qualityStatus": None,
                    "wordCount": None,
                    "speakerCount": None,
                    "sourceDuration": None,
                    "transcriptEndTime": None,
                    "billingEvent": None,
                    "billableMinutes": None,
                    "charged": False,
                    "chargeMessage": None,
                    "artifactKeys": {},
                    "artifactUrls": {},
                    "error": str(exc),
                }
                await actor.push_data(row)
                results.append(row)

    summary = {
        "status": "completed" if all(item["status"] == "completed" for item in results) else "partial" if any(item["status"] == "completed" for item in results) else "failed",
        "itemCount": len(results),
        "successfulCount": sum(1 for item in results if item["status"] == "completed"),
        "failedCount": sum(1 for item in results if item["status"] != "completed"),
        "results": results,
    }
    await actor.set_value("OUTPUT", summary, content_type="application/json; charset=utf-8")
    await actor.set_status_message(f"{summary['status']}: {summary['successfulCount']} succeeded, {summary['failedCount']} failed", is_terminal=True)
    if summary["failedCount"]:
        raise RuntimeError(f"{summary['failedCount']} media file(s) failed")
    return summary


async def main() -> None:
    async with Actor:
        await run(Actor)
