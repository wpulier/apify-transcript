from __future__ import annotations

import io
import json
import mimetypes
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from zipfile import ZIP_DEFLATED, ZipFile

import httpx
from openai import OpenAI

from .config import (
    DIRECT_UPLOAD_EXTENSIONS,
    ELEVENLABS_STT_URL,
    OPENAI_AUTHORITATIVE_CHUNK_SECONDS,
    OPENAI_FAST_CHUNK_SECONDS,
    OPENAI_UPLOAD_TARGET_BYTES,
    TRANSCRIPT_LOW_AUDIO_BITRATE,
    TRANSCRIPT_MASTER_AUDIO_BITRATE,
    TRANSCRIPT_REQUEST_TIMEOUT_SECONDS,
    TRANSCRIPT_SAMPLE_RATE,
    TRANSCRIPT_VERY_LOW_AUDIO_BITRATE,
    TranscriptConfig,
)
from .media import detect_speech_end_seconds, ffprobe_duration, run_command
from .utils import clean_text, json_safe, normalized_word_count, slugify, subtitle_timestamp, timestamp


LogFn = Callable[[str], None]


@dataclass
class TranscriptBundle:
    provider: str
    model: str
    canonical: dict[str, Any]
    quality: dict[str, Any]
    retry_history: list[dict[str, Any]]

    @property
    def word_count(self) -> int:
        return int(self.quality.get("word_count") or 0)

    @property
    def speaker_count(self) -> int:
        return int(self.quality.get("speaker_count") or 0)

    @property
    def source_duration(self) -> float | None:
        return self.quality.get("source_duration")

    @property
    def transcript_end_time(self) -> float | None:
        return self.quality.get("transcript_end_time")

    @property
    def quality_status(self) -> str:
        return str(self.quality.get("quality_status") or "unknown")


def to_plain_dict(value: Any) -> dict[str, Any]:
    value = json_safe(value)
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return {"text": value}
    return {"raw": value, "text": str(value)}


def segment_speaker(segment: dict[str, Any]) -> str | None:
    for key in ("speaker", "speaker_label", "speaker_id"):
        value = segment.get(key)
        if value is not None:
            label = str(value)
            return label if label.lower().startswith("speaker") else f"Speaker {label}"
    return None


def normalize_openai_response(response: Any, provider: str, model: str) -> dict[str, Any]:
    raw = to_plain_dict(response)
    raw_segments = raw.get("segments") or raw.get("chunks") or []
    segments = []
    for index, item in enumerate(raw_segments):
        if not isinstance(item, dict):
            continue
        text = clean_text(item.get("text"))
        if not text:
            continue
        segments.append(
            {
                "id": item.get("id", index),
                "start": float(item.get("start", item.get("timestamp", [0, 0])[0] or 0)),
                "end": float(item.get("end", item.get("timestamp", [0, 0])[-1] or 0)),
                "speaker": segment_speaker(item) or "Speaker 0",
                "text": text,
            }
        )
    words = []
    for item in raw.get("words") or []:
        if not isinstance(item, dict):
            continue
        word_text = clean_text(item.get("word") or item.get("text"))
        if not word_text:
            continue
        words.append(
            {
                "word": word_text,
                "start": item.get("start"),
                "end": item.get("end"),
                "speaker": segment_speaker(item),
                "confidence": item.get("confidence") or item.get("logprob"),
            }
        )
    text = clean_text(raw.get("text")) or " ".join(segment["text"] for segment in segments)
    return {
        "provider": provider,
        "model": model,
        "language": raw.get("language"),
        "duration": raw.get("duration"),
        "text": text,
        "segments": segments,
        "words": words,
        "raw": raw,
    }


def normalize_elevenlabs_response(response: dict[str, Any], provider: str, model: str) -> dict[str, Any]:
    words = []
    for item in response.get("words") or []:
        if not isinstance(item, dict):
            continue
        text = clean_text(item.get("text") or item.get("word"))
        if not text:
            continue
        speaker = item.get("speaker_id") or item.get("speaker") or item.get("speaker_label")
        words.append(
            {
                "word": text,
                "start": item.get("start"),
                "end": item.get("end"),
                "speaker": str(speaker) if speaker is not None else None,
                "confidence": item.get("confidence"),
            }
        )

    segments = []
    current: dict[str, Any] | None = None
    current_words: list[str] = []
    for word in words:
        speaker = word.get("speaker") or "Speaker 0"
        start = word.get("start")
        end = word.get("end")
        starts_new = (
            current is None
            or current.get("speaker") != speaker
            or (isinstance(start, (int, float)) and isinstance(current.get("end"), (int, float)) and start - current["end"] > 4)
        )
        if starts_new:
            if current is not None:
                current["text"] = clean_text(" ".join(current_words))
                segments.append(current)
            current = {
                "id": len(segments),
                "start": float(start or 0),
                "end": float(end or start or 0),
                "speaker": speaker,
                "text": "",
            }
            current_words = []
        current_words.append(str(word["word"]))
        if isinstance(end, (int, float)):
            current["end"] = float(end)
    if current is not None:
        current["text"] = clean_text(" ".join(current_words))
        segments.append(current)

    text = clean_text(response.get("text")) or " ".join(segment["text"] for segment in segments)
    return {
        "provider": provider,
        "model": model,
        "language": response.get("language_code") or response.get("language"),
        "duration": None,
        "text": text,
        "segments": segments,
        "words": words,
        "raw": response,
    }


def transcript_end_time(canonical: dict[str, Any]) -> float | None:
    best = None
    for collection_name in ("segments", "words"):
        for item in canonical.get(collection_name) or []:
            end = item.get("end")
            if isinstance(end, (int, float)):
                best = max(best or 0, float(end))
    duration = canonical.get("duration")
    if isinstance(duration, (int, float)):
        best = max(best or 0, float(duration))
    return best


def speaker_count(canonical: dict[str, Any]) -> int:
    speakers = {
        str(item.get("speaker"))
        for collection in ("segments", "words")
        for item in (canonical.get(collection) or [])
        if item.get("speaker") is not None
    }
    return len(speakers)


def render_txt(canonical: dict[str, Any]) -> str:
    segments = canonical.get("segments") or []
    if not segments:
        return clean_text(canonical.get("text")) + "\n"
    lines = []
    for segment in segments:
        lines.append(
            f"[{timestamp(segment.get('start'))} {segment.get('speaker') or 'Speaker 0'}] "
            f"{clean_text(segment.get('text'))}"
        )
    return "\n\n".join(lines).strip() + "\n"


def render_srt(canonical: dict[str, Any]) -> str:
    parts = []
    for index, segment in enumerate(canonical.get("segments") or [], 1):
        start = subtitle_timestamp(segment.get("start"), ",")
        end = subtitle_timestamp(segment.get("end") or segment.get("start"), ",")
        parts.append(f"{index}\n{start} --> {end}\n{segment.get('speaker') or 'Speaker 0'}: {clean_text(segment.get('text'))}\n")
    return "\n".join(parts)


def render_vtt(canonical: dict[str, Any]) -> str:
    parts = ["WEBVTT\n"]
    for segment in canonical.get("segments") or []:
        start = subtitle_timestamp(segment.get("start"), ".")
        end = subtitle_timestamp(segment.get("end") or segment.get("start"), ".")
        parts.append(f"{start} --> {end}\n{segment.get('speaker') or 'Speaker 0'}: {clean_text(segment.get('text'))}\n")
    return "\n".join(parts)


def validate_quality(audio_path: Path, canonical: dict[str, Any], provider: str, model: str, mode: str, retry_history: list[dict[str, Any]]) -> dict[str, Any]:
    source_duration = ffprobe_duration(audio_path)
    speech_end = detect_speech_end_seconds(audio_path, source_duration)
    end_time = transcript_end_time(canonical)
    text = clean_text(canonical.get("text")) or " ".join(clean_text(segment.get("text")) for segment in canonical.get("segments") or [])
    word_count = normalized_word_count(text)
    segment_count = len(canonical.get("segments") or [])
    speakers = speaker_count(canonical)
    speech_minutes = max(float(speech_end or source_duration or 0) / 60.0, 0)
    words_per_spoken_minute = word_count / speech_minutes if speech_minutes else None
    warnings: list[str] = []
    failures: list[str] = []

    if mode == "authoritative":
        if segment_count == 0:
            failures.append("missing speaker/timestamp segments")
        if speakers == 0:
            failures.append("missing speaker labels")
    if speech_end is not None and speech_end >= 120:
        if end_time is None:
            failures.append(f"transcript ends are missing before speech ends at {timestamp(speech_end)}")
        elif end_time < speech_end - 60:
            failures.append(f"transcript ends at {timestamp(end_time)} before speech ends at {timestamp(speech_end)}")
    if words_per_spoken_minute is not None and speech_minutes >= 5 and words_per_spoken_minute < 90:
        warnings.append(f"low word density: {words_per_spoken_minute:.1f} words/min")

    previous_end = None
    for segment in canonical.get("segments") or []:
        start = segment.get("start")
        end = segment.get("end")
        if isinstance(start, (int, float)) and previous_end is not None and start - previous_end > 180:
            warnings.append(f"large transcript gap near {timestamp(previous_end)}")
            break
        if isinstance(end, (int, float)):
            previous_end = float(end)

    quality_status = "failed_qa" if failures else ("warning" if warnings else "excellent")
    return {
        "provider": provider,
        "model": model,
        "quality_status": quality_status,
        "quality_label": {"excellent": "Excellent", "warning": "Warning", "failed_qa": "Failed QA"}[quality_status],
        "source_duration": source_duration,
        "detected_speech_duration": speech_end,
        "transcript_end_time": end_time,
        "word_count": word_count,
        "words_per_spoken_minute": words_per_spoken_minute,
        "segment_count": segment_count,
        "speaker_count": speakers,
        "warnings": warnings,
        "failures": failures,
        "retry_history": retry_history,
    }


def audio_bitrate_for_duration(duration: float | None) -> str:
    if not duration:
        return TRANSCRIPT_MASTER_AUDIO_BITRATE
    if duration * 4000 <= OPENAI_UPLOAD_TARGET_BYTES:
        return TRANSCRIPT_MASTER_AUDIO_BITRATE
    if duration * 3000 <= OPENAI_UPLOAD_TARGET_BYTES:
        return TRANSCRIPT_LOW_AUDIO_BITRATE
    return TRANSCRIPT_VERY_LOW_AUDIO_BITRATE


def prepare_openai_audio(media_path: Path, temp_dir: Path) -> tuple[Path, Path | None]:
    try:
        if media_path.stat().st_size <= OPENAI_UPLOAD_TARGET_BYTES and media_path.suffix.lower() in DIRECT_UPLOAD_EXTENSIONS:
            return media_path, None
    except OSError as exc:
        raise RuntimeError(f"could not read media file: {exc}") from exc

    temp_dir.mkdir(parents=True, exist_ok=True)
    duration = ffprobe_duration(media_path)
    bitrate = audio_bitrate_for_duration(duration)
    master_path = temp_dir / f"transcript_master_{slugify(media_path.stem)}.mp3"
    run_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(media_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            TRANSCRIPT_SAMPLE_RATE,
            "-codec:a",
            "libmp3lame",
            "-b:a",
            bitrate,
            str(master_path),
        ],
        "ffmpeg could not prepare transcript audio",
    )
    return master_path, master_path


def prepare_chunks(audio_path: Path, temp_dir: Path, segment_seconds: int) -> tuple[list[Path], Path]:
    chunk_dir = temp_dir / f"chunks_{slugify(audio_path.stem)}"
    if chunk_dir.exists():
        shutil.rmtree(chunk_dir)
    chunk_dir.mkdir(parents=True, exist_ok=True)
    output_pattern = chunk_dir / "chunk_%03d.mp3"
    run_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(audio_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            TRANSCRIPT_SAMPLE_RATE,
            "-codec:a",
            "libmp3lame",
            "-b:a",
            TRANSCRIPT_MASTER_AUDIO_BITRATE,
            "-f",
            "segment",
            "-segment_time",
            str(segment_seconds),
            "-reset_timestamps",
            "1",
            str(output_pattern),
        ],
        "ffmpeg could not prepare transcript chunks",
    )
    chunks = sorted(chunk_dir.glob("chunk_*.mp3"))
    if not chunks:
        raise RuntimeError("ffmpeg did not create transcript chunks")
    return chunks, chunk_dir


def should_retry_openai_chunked(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(term in message for term in ("corrupted", "unsupported", "invalid_value", "too large", "maximum"))


def create_openai_diarized(client: OpenAI, audio_path: Path, config: TranscriptConfig, model: str) -> dict[str, Any]:
    with audio_path.open("rb") as audio_file:
        response = client.audio.transcriptions.create(
            model=model,
            file=audio_file,
            response_format="diarized_json",
            chunking_strategy="auto",
            language=config.language or None,
        )
    return normalize_openai_response(response, "openai", model)


def offset_canonical(canonical: dict[str, Any], offset: float, chunk_index: int) -> dict[str, Any]:
    canonical = json_safe(canonical)
    for index, segment in enumerate(canonical.get("segments") or []):
        if isinstance(segment.get("start"), (int, float)):
            segment["start"] = float(segment["start"]) + offset
        if isinstance(segment.get("end"), (int, float)):
            segment["end"] = float(segment["end"]) + offset
        segment["id"] = f"chunk_{chunk_index:03d}_{segment.get('id', index)}"
    for word in canonical.get("words") or []:
        if isinstance(word.get("start"), (int, float)):
            word["start"] = float(word["start"]) + offset
        if isinstance(word.get("end"), (int, float)):
            word["end"] = float(word["end"]) + offset
    return canonical


def merge_canonicals(chunks: list[dict[str, Any]], provider: str, model: str) -> dict[str, Any]:
    segments = []
    words = []
    texts = []
    duration = None
    language = None
    for chunk in chunks:
        language = language or chunk.get("language")
        texts.append(clean_text(chunk.get("text")))
        segments.extend(chunk.get("segments") or [])
        words.extend(chunk.get("words") or [])
        end = transcript_end_time(chunk)
        if end is not None:
            duration = max(duration or 0, end)
    return {
        "provider": provider,
        "model": model,
        "language": language,
        "duration": duration,
        "text": "\n\n".join(text for text in texts if text),
        "segments": segments,
        "words": words,
        "raw": {"chunks": [chunk.get("raw", {}) for chunk in chunks]},
    }


def transcribe_openai_chunk(api_key: str, chunk_path: Path, config: TranscriptConfig, model: str, index: int, offset: float) -> tuple[int, dict[str, Any]]:
    client = OpenAI(api_key=api_key, timeout=TRANSCRIPT_REQUEST_TIMEOUT_SECONDS)
    canonical = create_openai_diarized(client, chunk_path, config, model)
    return index, offset_canonical(canonical, offset, index)


def transcribe_openai_authoritative(media_path: Path, config: TranscriptConfig, temp_dir: Path, log: LogFn) -> tuple[dict[str, Any], str]:
    if not config.openai_api_key:
        raise RuntimeError("OpenAI transcript output requires an OpenAI API key")
    model = config.openai_model_for_mode()
    client = OpenAI(api_key=config.openai_api_key, timeout=TRANSCRIPT_REQUEST_TIMEOUT_SECONDS)
    upload_path, cleanup_path = prepare_openai_audio(media_path, temp_dir)
    try:
        try:
            if upload_path.stat().st_size > OPENAI_UPLOAD_TARGET_BYTES:
                raise RuntimeError("prepared audio is too large for single upload")
            log(f"OpenAI authoritative upload: {upload_path.name} ({upload_path.stat().st_size / (1024 * 1024):.1f} MB)")
            return create_openai_diarized(client, upload_path, config, model), model
        except Exception as exc:
            if not should_retry_openai_chunked(exc):
                raise
            chunks, chunk_dir = prepare_chunks(upload_path, temp_dir, OPENAI_AUTHORITATIVE_CHUNK_SECONDS)
            try:
                log(f"OpenAI full upload rejected; retrying as {len(chunks)} chunks with concurrency {config.concurrency}")
                offsets = []
                offset = 0.0
                for chunk in chunks:
                    offsets.append(offset)
                    offset += ffprobe_duration(chunk) or 0
                results: dict[int, dict[str, Any]] = {}
                with ThreadPoolExecutor(max_workers=config.concurrency) as executor:
                    futures = {
                        executor.submit(transcribe_openai_chunk, config.openai_api_key, chunk, config, model, index, offsets[index]): index
                        for index, chunk in enumerate(chunks)
                    }
                    for future in as_completed(futures):
                        index, canonical = future.result()
                        results[index] = canonical
                        log(f"OpenAI chunk {index + 1}/{len(chunks)} complete")
                return merge_canonicals([results[index] for index in range(len(chunks))], "openai", model), model
            finally:
                shutil.rmtree(chunk_dir, ignore_errors=True)
    finally:
        if cleanup_path is not None:
            cleanup_path.unlink(missing_ok=True)


def transcribe_openai_fast(media_path: Path, config: TranscriptConfig, temp_dir: Path) -> tuple[dict[str, Any], str]:
    if not config.openai_api_key:
        raise RuntimeError("OpenAI transcript output requires an OpenAI API key")
    model = config.openai_model_for_mode()
    client = OpenAI(api_key=config.openai_api_key, timeout=TRANSCRIPT_REQUEST_TIMEOUT_SECONDS)
    upload_path, cleanup_path = prepare_openai_audio(media_path, temp_dir)
    chunk_dir = None
    try:
        audio_files = [upload_path]
        if upload_path.stat().st_size > OPENAI_UPLOAD_TARGET_BYTES:
            audio_files, chunk_dir = prepare_chunks(upload_path, temp_dir, OPENAI_FAST_CHUNK_SECONDS)
        parts = []
        for audio_path in audio_files:
            with audio_path.open("rb") as audio_file:
                response = client.audio.transcriptions.create(model=model, file=audio_file, response_format="text")
            parts.append(str(response).strip())
        return normalize_openai_response({"text": "\n\n".join(part for part in parts if part)}, "openai", model), model
    finally:
        if chunk_dir is not None:
            shutil.rmtree(chunk_dir, ignore_errors=True)
        if cleanup_path is not None:
            cleanup_path.unlink(missing_ok=True)


def transcribe_elevenlabs(media_path: Path, config: TranscriptConfig) -> tuple[dict[str, Any], str]:
    if not config.elevenlabs_api_key:
        raise RuntimeError("ElevenLabs transcript output requires an ElevenLabs API key")
    model = config.elevenlabs_model
    data: list[tuple[str, str]] = [
        ("model_id", model),
        ("diarize", "true"),
        ("timestamps_granularity", "word"),
        ("no_verbatim", "false"),
    ]
    if config.language:
        data.append(("language_code", config.language))
    data.extend(("keyterms", term) for term in config.keyterms)
    mime_type = mimetypes.guess_type(media_path.name)[0] or "application/octet-stream"
    with media_path.open("rb") as audio_file:
        files = {"file": (media_path.name, audio_file, mime_type)}
        response = httpx.post(
            ELEVENLABS_STT_URL,
            headers={"xi-api-key": config.elevenlabs_api_key},
            data=data,
            files=files,
            timeout=TRANSCRIPT_REQUEST_TIMEOUT_SECONDS,
        )
    response.raise_for_status()
    return normalize_elevenlabs_response(response.json(), "elevenlabs", model), model


def transcribe_with_provider(media_path: Path, config: TranscriptConfig, provider: str, temp_dir: Path, log: LogFn, retry_history: list[dict[str, Any]]) -> TranscriptBundle:
    if provider == "openai":
        if config.quality_mode == "fast":
            canonical, model = transcribe_openai_fast(media_path, config, temp_dir)
        else:
            canonical, model = transcribe_openai_authoritative(media_path, config, temp_dir, log)
    elif provider == "elevenlabs":
        canonical, model = transcribe_elevenlabs(media_path, config)
    else:
        raise RuntimeError(f"unknown provider: {provider}")
    log("Validating transcript quality")
    quality = validate_quality(media_path, canonical, provider, model, config.quality_mode, retry_history)
    log(
        "Quality %s: %s words, %s speaker(s)"
        % (quality.get("quality_status"), quality.get("word_count"), quality.get("speaker_count"))
    )
    return TranscriptBundle(provider=provider, model=model, canonical=canonical, quality=quality, retry_history=retry_history)


def transcribe_media(media_path: Path, config: TranscriptConfig, temp_dir: Path, log: LogFn = print) -> TranscriptBundle:
    providers = config.selected_providers()
    if not providers:
        raise RuntimeError("No transcription provider API key is available")
    retry_history: list[dict[str, Any]] = []
    last_bundle: TranscriptBundle | None = None
    last_error: Exception | None = None
    for provider in providers:
        try:
            bundle = transcribe_with_provider(media_path, config, provider, temp_dir, log, retry_history)
            retry_history.append(
                {
                    "provider": bundle.provider,
                    "model": bundle.model,
                    "quality_status": bundle.quality_status,
                    "word_count": bundle.word_count,
                }
            )
            bundle.quality["retry_history"] = retry_history
            last_bundle = bundle
            if bundle.quality_status != "failed_qa":
                return bundle
        except Exception as exc:
            last_error = exc
            retry_history.append({"provider": provider, "error": str(exc)})
    if last_bundle is not None:
        last_bundle.quality["retry_history"] = retry_history
        return last_bundle
    raise RuntimeError(str(last_error) if last_error else "transcription failed")


def artifact_payloads(bundle: TranscriptBundle, include_zip: bool = True) -> dict[str, tuple[bytes, str]]:
    canonical = dict(bundle.canonical)
    canonical["quality_status"] = bundle.quality_status
    payloads = {
        "txt": (render_txt(bundle.canonical).encode("utf-8"), "text/plain; charset=utf-8"),
        "json": (json.dumps(canonical, ensure_ascii=False, indent=2).encode("utf-8"), "application/json; charset=utf-8"),
        "srt": (render_srt(bundle.canonical).encode("utf-8"), "application/x-subrip; charset=utf-8"),
        "vtt": (render_vtt(bundle.canonical).encode("utf-8"), "text/vtt; charset=utf-8"),
        "quality.json": (json.dumps(bundle.quality, ensure_ascii=False, indent=2).encode("utf-8"), "application/json; charset=utf-8"),
    }
    if include_zip:
        buffer = io.BytesIO()
        with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
            for suffix, (content, _) in payloads.items():
                archive.writestr(f"transcript.{suffix}", content)
        payloads["zip"] = (buffer.getvalue(), "application/zip")
    return payloads
