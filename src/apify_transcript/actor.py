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
from .jobs import JOB_STORE_NAME, JobStore, materialize_ingested_media
from .media import MediaSource, download_source, ensure_supported_media, ffprobe_duration, parse_media_sources, require_ffmpeg
from .transcript import artifact_payloads, prepare_mp3_artifact, transcribe_media
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
    await actor.set_status_message(f"Downloading media: {source.name}")
    local = download_source(
        source,
        work_dir / "sources",
        os.environ.get("APIFY_TOKEN"),
        progress_log=lambda message: actor.log.info("%s", message),
    )
    duration = ffprobe_duration(local.path)
    await ensure_budget(actor, duration)

    await actor.set_status_message(f"Preparing audio: {source.name}")
    await actor.set_status_message(f"Transcribing: {source.name}")
    bundle = transcribe_media(
        local.path,
        config,
        work_dir / "tmp" / source.source_id,
        log=lambda message: actor.log.info("%s: %s", source.name, message),
    )
    await actor.set_status_message(f"Packaging results: {source.name}")
    mp3_path = prepare_mp3_artifact(local.path, work_dir / "tmp" / source.source_id)
    payloads = artifact_payloads(bundle, include_zip=config.include_zip, mp3_path=mp3_path)
    await actor.set_status_message(f"Charging: {source.name}")
    billing = await charge_transcription_minutes(
        actor,
        duration or bundle.source_duration,
        required=config.require_successful_charge,
    )
    await actor.set_status_message(f"Writing output: {source.name}")
    keys = await store_artifacts(actor, source.source_id, source.name, payloads)
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
        "durationMinutes": round(float(bundle.source_duration or duration or 0) / 60.0, 2) if (bundle.source_duration or duration) else None,
        "transcriptEndTime": bundle.transcript_end_time,
        "billingEvent": billing.event_name,
        "billableMinutes": ceil_minutes(duration or bundle.source_duration),
        "charged": billing.charged,
        "chargeMessage": billing.message,
        "artifactKeys": keys,
        "mp3Key": keys.get("mp3"),
        "txtKey": keys.get("txt"),
        "jsonKey": keys.get("json"),
        "srtKey": keys.get("srt"),
        "vttKey": keys.get("vtt"),
        "qualityKey": keys.get("quality.json"),
        "zipKey": keys.get("zip"),
        "artifactUrls": urls,
        "mp3Url": urls.get("mp3"),
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
    await actor.set_status_message(f"Done: {source.name}")
    return row


async def fail_before_processing(actor: object, message: str) -> None:
    summary = {
        "status": "failed",
        "itemCount": 0,
        "successfulCount": 0,
        "failedCount": 1,
        "error": message,
        "results": [],
    }
    await actor.set_value("OUTPUT", summary, content_type="application/json; charset=utf-8")
    await actor.set_status_message(f"failed: {message}", is_terminal=True)


def standby_job_store(actor_input: dict[str, Any]) -> tuple[JobStore, str] | None:
    ref = actor_input.get("standbyJob") or {}
    job_id = ref.get("jobId")
    if not job_id:
        return None
    token = os.environ.get("APIFY_TOKEN") or ""
    store_name = ref.get("storeName") or JOB_STORE_NAME
    return JobStore.from_token(token, store_name), str(job_id)


def patch_standby_job(ref: tuple[JobStore, str] | None, **updates: Any) -> None:
    if not ref:
        return
    store, job_id = ref
    try:
        store.patch_job(job_id, **updates)
    except Exception:
        return


def actor_run_url() -> str | None:
    run_id = os.environ.get("APIFY_ACTOR_RUN_ID")
    if not run_id:
        return None
    return f"https://console.apify.com/actors/{os.environ.get('APIFY_ACTOR_ID') or ACTOR_ID}/runs/{run_id}"


def source_from_ingested_media(actor_input: dict[str, Any], work_dir: Path) -> MediaSource | None:
    spec = actor_input.get("ingestedMedia")
    if not spec:
        return None
    path = materialize_ingested_media(spec, work_dir / "ingested", os.environ.get("APIFY_TOKEN") or "")
    ensure_supported_media(path)
    return MediaSource("001", str(path), str(spec.get("fileName") or path.name))


async def run(actor: object = Actor) -> dict[str, Any]:
    actor_input = await actor.get_input() or {}
    standby_ref = standby_job_store(actor_input)
    try:
        config = TranscriptConfig.from_input(actor_input, os.environ)
        require_ffmpeg()
    except Exception as exc:
        message = str(exc)
        await fail_before_processing(actor, message)
        patch_standby_job(standby_ref, status="failed", error=message, message=message)
        raise RuntimeError(message) from exc

    results = []
    with tempfile.TemporaryDirectory(prefix="apify-transcript-") as temp_dir:
        work_dir = Path(temp_dir)
        try:
            ingested_source = source_from_ingested_media(actor_input, work_dir)
            sources = [ingested_source] if ingested_source else parse_media_sources(actor_input)
        except Exception as exc:
            message = str(exc)
            await fail_before_processing(actor, message)
            patch_standby_job(standby_ref, status="failed", error=message, message=message, runUrl=actor_run_url())
            raise RuntimeError(message) from exc
        patch_standby_job(
            standby_ref,
            status="processing",
            message="Transcription is running.",
            runId=os.environ.get("APIFY_ACTOR_RUN_ID"),
            runUrl=actor_run_url(),
        )
        for source in sources:
            try:
                row = await process_one(actor, source, config, work_dir)
                results.append(row)
                patch_standby_job(
                    standby_ref,
                    status="completed",
                    message="Transcript artifacts are ready.",
                    result=row,
                    artifactUrls=row.get("artifactUrls", {}),
                    error=None,
                )
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
                    "durationMinutes": None,
                    "transcriptEndTime": None,
                    "billingEvent": None,
                    "billableMinutes": None,
                    "charged": False,
                    "chargeMessage": None,
                    "artifactKeys": {},
                    "artifactUrls": {},
                    "mp3Key": None,
                    "mp3Url": None,
                    "error": str(exc),
                }
                await actor.push_data(row)
                results.append(row)
                patch_standby_job(
                    standby_ref,
                    status="failed",
                    message="Transcript job failed.",
                    error=str(exc),
                    result=row,
                    artifactUrls={},
                )

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
        from .standby import is_standby_origin, serve_standby

        if is_standby_origin(Actor):
            serve_standby(Actor)
        else:
            await run(Actor)
