from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


DEFAULT_OPENAI_MODEL = "gpt-4o-transcribe-diarize"
DEFAULT_OPENAI_FAST_MODEL = "gpt-4o-mini-transcribe"
DEFAULT_ELEVENLABS_MODEL = "scribe_v2"
ELEVENLABS_STT_URL = "https://api.elevenlabs.io/v1/speech-to-text"
DEFAULT_LANGUAGE = "en"
DEFAULT_PROVIDER = "openai"
DEFAULT_QUALITY_MODE = "authoritative"
DEFAULT_CONCURRENCY = 3
DEFAULT_REQUIRE_SUCCESSFUL_CHARGE = True

TRANSCRIPT_MASTER_AUDIO_BITRATE = "32k"
TRANSCRIPT_LOW_AUDIO_BITRATE = "24k"
TRANSCRIPT_VERY_LOW_AUDIO_BITRATE = "16k"
TRANSCRIPT_SAMPLE_RATE = "16000"
OPENAI_UPLOAD_TARGET_BYTES = 24 * 1024 * 1024
OPENAI_AUTHORITATIVE_CHUNK_SECONDS = 5 * 60
OPENAI_FAST_CHUNK_SECONDS = 15 * 60
TRANSCRIPT_REQUEST_TIMEOUT_SECONDS = 3600

SUPPORTED_MEDIA_EXTENSIONS = {
    ".aac",
    ".aiff",
    ".flac",
    ".m4a",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".mpeg",
    ".mpga",
    ".ogg",
    ".opus",
    ".wav",
    ".webm",
}

DIRECT_UPLOAD_EXTENSIONS = {
    ".flac",
    ".m4a",
    ".mp3",
    ".mp4",
    ".mpeg",
    ".mpga",
    ".ogg",
    ".wav",
    ".webm",
}

DEFAULT_KEYTERMS = (
    "AgentX",
    "REPC",
    "SkySlope",
    "Dotloop",
    "LoanX",
    "MLS",
    "addendum",
    "counteroffer",
    "buyer broker",
)


def input_bool(actor_input: dict, key: str, default: bool) -> bool:
    value = actor_input.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)


@dataclass(frozen=True)
class TranscriptConfig:
    provider: str = DEFAULT_PROVIDER
    quality_mode: str = DEFAULT_QUALITY_MODE
    language: str = DEFAULT_LANGUAGE
    include_zip: bool = True
    concurrency: int = DEFAULT_CONCURRENCY
    require_successful_charge: bool = DEFAULT_REQUIRE_SUCCESSFUL_CHARGE
    openai_model: str = DEFAULT_OPENAI_MODEL
    openai_fast_model: str = DEFAULT_OPENAI_FAST_MODEL
    elevenlabs_model: str = DEFAULT_ELEVENLABS_MODEL
    openai_api_key: str | None = None
    elevenlabs_api_key: str | None = None
    keyterms: tuple[str, ...] = field(default_factory=lambda: DEFAULT_KEYTERMS)

    @classmethod
    def from_input(cls, actor_input: dict, env: dict[str, str]) -> "TranscriptConfig":
        keyterms = list(DEFAULT_KEYTERMS)
        raw_keyterms = actor_input.get("keyterms") or ""
        if isinstance(raw_keyterms, str):
            keyterms.extend(term.strip() for term in raw_keyterms.replace(",", "\n").splitlines())
        elif isinstance(raw_keyterms, Iterable):
            keyterms.extend(str(term).strip() for term in raw_keyterms)
        cleaned_terms = []
        seen = set()
        for term in keyterms:
            if not term:
                continue
            key = term.lower()
            if key in seen:
                continue
            seen.add(key)
            cleaned_terms.append(term)

        provider = str(actor_input.get("provider") or DEFAULT_PROVIDER).lower()
        if provider not in {"openai", "elevenlabs", "auto"}:
            raise ValueError("provider must be one of: openai, elevenlabs, auto")
        quality_mode = str(actor_input.get("qualityMode") or DEFAULT_QUALITY_MODE).lower()
        if quality_mode not in {"authoritative", "fast"}:
            raise ValueError("qualityMode must be one of: authoritative, fast")
        concurrency = int(actor_input.get("transcriptConcurrency") or DEFAULT_CONCURRENCY)
        if concurrency < 1 or concurrency > 8:
            raise ValueError("transcriptConcurrency must be between 1 and 8")

        return cls(
            provider=provider,
            quality_mode=quality_mode,
            language=str(actor_input.get("language") or DEFAULT_LANGUAGE),
            include_zip=input_bool(actor_input, "includeZip", True),
            concurrency=concurrency,
            require_successful_charge=input_bool(actor_input, "requireSuccessfulCharge", DEFAULT_REQUIRE_SUCCESSFUL_CHARGE),
            openai_api_key=actor_input.get("openaiApiKey") or env.get("OPENAI_API_KEY"),
            elevenlabs_api_key=actor_input.get("elevenlabsApiKey") or env.get("ELEVENLABS_API_KEY"),
            keyterms=tuple(cleaned_terms[:1000]),
        )

    def selected_providers(self) -> list[str]:
        if self.provider == "openai":
            return ["openai"]
        if self.provider == "elevenlabs":
            return ["elevenlabs"]
        providers = []
        if self.openai_api_key:
            providers.append("openai")
        if self.elevenlabs_api_key:
            providers.append("elevenlabs")
        return providers

    def openai_model_for_mode(self) -> str:
        return self.openai_fast_model if self.quality_mode == "fast" else self.openai_model
