import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from apify_transcript.actor import artifact_url, run
from apify_transcript.config import TranscriptConfig
from apify_transcript.media import MediaSource, parse_media_sources
from apify_transcript.transcript import (
    TranscriptBundle,
    artifact_payloads,
    render_srt,
    render_txt,
    render_vtt,
    transcribe_elevenlabs,
    transcribe_openai_authoritative,
    validate_quality,
)


class FakeActor:
    def __init__(self, actor_input):
        self.actor_input = actor_input
        self.dataset = []
        self.values = {}
        self.statuses = []
        self.log = SimpleNamespace(info=lambda *args, **kwargs: None)

    async def get_input(self):
        return self.actor_input

    async def push_data(self, row):
        self.dataset.append(row)

    async def set_value(self, key, value, content_type=None):
        self.values[key] = (value, content_type)

    async def set_status_message(self, message, is_terminal=False):
        self.statuses.append((message, is_terminal))

    def get_charging_manager(self):
        raise RuntimeError("not configured")

    async def charge(self, event_name, count=1):
        return SimpleNamespace(event_charge_limit_reached=False, charged_count=count)


class ChargeFailingActor(FakeActor):
    async def charge(self, event_name, count=1):
        raise RuntimeError("pricing event is not configured")


class ChargeIgnoredActor(FakeActor):
    async def charge(self, event_name, count=1):
        return SimpleNamespace(event_charge_limit_reached=False, charged_count=0)


def sample_canonical():
    return {
        "provider": "openai",
        "model": "gpt-4o-transcribe-diarize",
        "text": "Hello there. General Kenobi.",
        "segments": [
            {"id": 0, "start": 0.0, "end": 1.5, "speaker": "Speaker 0", "text": "Hello there."},
            {"id": 1, "start": 2.0, "end": 4.0, "speaker": "Speaker 1", "text": "General Kenobi."},
        ],
        "words": [],
        "raw": {},
    }


def sample_bundle():
    quality = {
        "provider": "openai",
        "model": "gpt-4o-transcribe-diarize",
        "quality_status": "excellent",
        "source_duration": 4.0,
        "transcript_end_time": 4.0,
        "word_count": 4,
        "speaker_count": 2,
        "warnings": [],
        "failures": [],
        "retry_history": [],
    }
    return TranscriptBundle("openai", "gpt-4o-transcribe-diarize", sample_canonical(), quality, [])


class InputTests(unittest.TestCase):
    def test_rejects_empty_source(self):
        with self.assertRaisesRegex(ValueError, "at least one"):
            parse_media_sources({})

    def test_accepts_uploads_and_urls(self):
        sources = parse_media_sources(
            {
                "mediaFiles": ["https://api.apify.com/v2/key-value-stores/store/records/call.mp4"],
                "mediaUrls": ["https://example.com/demo.mov"],
            }
        )
        self.assertEqual([source.source_id for source in sources], ["001", "002"])
        self.assertEqual(sources[0].name, "call.mp4")

    def test_config_uses_input_secret_before_environment(self):
        config = TranscriptConfig.from_input(
            {"provider": "openai", "openaiApiKey": "input-key"},
            {"OPENAI_API_KEY": "env-key"},
        )
        self.assertEqual(config.openai_api_key, "input-key")

    def test_config_rejects_unknown_provider(self):
        with self.assertRaisesRegex(ValueError, "provider"):
            TranscriptConfig.from_input({"provider": "bad"}, {})

    def test_config_allows_private_charge_override(self):
        config = TranscriptConfig.from_input({"requireSuccessfulCharge": False}, {"OPENAI_API_KEY": "env-key"})
        self.assertFalse(config.require_successful_charge)


class ArtifactTests(unittest.TestCase):
    def test_artifact_url_can_be_signed_for_browser_access(self):
        with patch.dict(
            "os.environ",
            {"APIFY_DEFAULT_KEY_VALUE_STORE_ID": "store123"},
            clear=True,
        ):
            url = artifact_url("001_demo.txt", signing_secret="secret")
        self.assertEqual(
            url,
            "https://api.apify.com/v2/key-value-stores/store123/records/001_demo.txt?signature=VT6pvNYVcXQtMYUm72LS",
        )

    def test_renders_transcript_artifacts(self):
        canonical = sample_canonical()
        self.assertIn("[00:00:00 Speaker 0] Hello there.", render_txt(canonical))
        self.assertIn("00:00:00,000 --> 00:00:01,500", render_srt(canonical))
        self.assertIn("WEBVTT", render_vtt(canonical))

        payloads = artifact_payloads(sample_bundle(), include_zip=True)
        self.assertIn("txt", payloads)
        self.assertIn("quality.json", payloads)
        self.assertIn("zip", payloads)
        self.assertEqual(payloads["txt"][1], "text/plain; charset=utf-8")

    def test_quality_flags_early_end_and_missing_speakers(self):
        canonical = {"text": "too short", "segments": [], "words": []}
        with tempfile.TemporaryDirectory() as temp_dir:
            audio = Path(temp_dir) / "audio.mp3"
            audio.write_bytes(b"audio")
            with patch("apify_transcript.transcript.ffprobe_duration", return_value=600.0):
                with patch("apify_transcript.transcript.detect_speech_end_seconds", return_value=600.0):
                    quality = validate_quality(audio, canonical, "openai", "model", "authoritative", [])
        self.assertEqual(quality["quality_status"], "failed_qa")
        self.assertTrue(any("missing speaker" in failure for failure in quality["failures"]))
        self.assertTrue(any("transcript ends" in failure for failure in quality["failures"]))


class ProviderTests(unittest.TestCase):
    def test_openai_authoritative_payload_uses_diarized_json_and_chunking_strategy(self):
        calls = []

        class FakeTranscriptions:
            def create(self, **kwargs):
                calls.append(kwargs)
                return sample_canonical()

        class FakeOpenAI:
            def __init__(self, **kwargs):
                self.audio = SimpleNamespace(transcriptions=FakeTranscriptions())

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            audio = root / "audio.mp3"
            audio.write_bytes(b"audio")
            config = TranscriptConfig(openai_api_key="key")
            with patch("apify_transcript.transcript.OpenAI", FakeOpenAI):
                with patch("apify_transcript.transcript.prepare_openai_audio", return_value=(audio, None)):
                    canonical, model = transcribe_openai_authoritative(audio, config, root / "tmp", lambda _: None)

        self.assertEqual(model, "gpt-4o-transcribe-diarize")
        self.assertEqual(calls[0]["response_format"], "diarized_json")
        self.assertEqual(calls[0]["chunking_strategy"], "auto")
        self.assertEqual(canonical["segments"][0]["speaker"], "Speaker 0")

    def test_elevenlabs_payload_uses_scribe_diarization_and_keyterms(self):
        calls = []

        def fake_post(*args, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {
                    "text": "Hello",
                    "words": [
                        {"text": "Hello", "start": 0.0, "end": 1.0, "speaker_id": "speaker_0"}
                    ],
                },
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            audio = Path(temp_dir) / "audio.mp3"
            audio.write_bytes(b"audio")
            config = TranscriptConfig(provider="elevenlabs", elevenlabs_api_key="key", keyterms=("REPC",))
            with patch("apify_transcript.transcript.httpx.post", side_effect=fake_post):
                canonical, model = transcribe_elevenlabs(audio, config)

        self.assertEqual(model, "scribe_v2")
        self.assertIn(("diarize", "true"), calls[0]["data"])
        self.assertIn(("timestamps_granularity", "word"), calls[0]["data"])
        self.assertIn(("keyterms", "REPC"), calls[0]["data"])
        self.assertEqual(canonical["speaker_count"] if "speaker_count" in canonical else len(canonical["segments"]), 1)


class ActorRunTests(unittest.TestCase):
    def test_failed_first_file_does_not_stop_second_file(self):
        fake_actor = FakeActor(
            {
                "mediaUrls": ["https://example.com/a.mp4", "https://example.com/b.mp4"],
                "provider": "openai",
                "openaiApiKey": "key",
            }
        )

        def fake_download(source, target_dir, apify_token=None):
            path = Path(target_dir) / f"{source.source_id}.mp4"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"media")
            return SimpleNamespace(source=source, path=path, content_type="video/mp4")

        def fake_transcribe(path, config, temp_dir, log=print):
            if path.name.startswith("001"):
                raise RuntimeError("boom")
            return sample_bundle()

        with patch("apify_transcript.actor.require_ffmpeg"):
            with patch("apify_transcript.actor.download_source", side_effect=fake_download):
                with patch("apify_transcript.actor.ffprobe_duration", return_value=4.0):
                    with patch("apify_transcript.actor.transcribe_media", side_effect=fake_transcribe):
                        with self.assertRaisesRegex(RuntimeError, "1 media file"):
                            asyncio.run(run(fake_actor))

        self.assertEqual(len(fake_actor.dataset), 2)
        self.assertEqual(fake_actor.dataset[0]["status"], "failed")
        self.assertEqual(fake_actor.dataset[1]["status"], "completed")
        self.assertIn("OUTPUT", fake_actor.values)
        output = fake_actor.values["OUTPUT"][0]
        self.assertEqual(output["status"], "partial")

    def test_charge_failure_prevents_artifact_delivery(self):
        fake_actor = ChargeFailingActor(
            {
                "mediaUrls": ["https://example.com/a.mp4"],
                "provider": "openai",
                "openaiApiKey": "key",
            }
        )

        def fake_download(source, target_dir, apify_token=None):
            path = Path(target_dir) / f"{source.source_id}.mp4"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"media")
            return SimpleNamespace(source=source, path=path, content_type="video/mp4")

        with patch("apify_transcript.actor.require_ffmpeg"):
            with patch("apify_transcript.actor.download_source", side_effect=fake_download):
                with patch("apify_transcript.actor.ffprobe_duration", return_value=64.0):
                    with patch("apify_transcript.actor.transcribe_media", return_value=sample_bundle()):
                        with self.assertRaisesRegex(RuntimeError, "1 media file"):
                            asyncio.run(run(fake_actor))

        self.assertEqual(fake_actor.dataset[0]["status"], "failed")
        self.assertIn("Could not charge", fake_actor.dataset[0]["error"])
        self.assertFalse(fake_actor.dataset[0]["charged"])
        self.assertNotIn("001_a.txt", fake_actor.values)

    def test_ignored_charge_prevents_artifact_delivery_when_required(self):
        fake_actor = ChargeIgnoredActor(
            {
                "mediaUrls": ["https://example.com/a.mp4"],
                "provider": "openai",
                "openaiApiKey": "key",
            }
        )

        def fake_download(source, target_dir, apify_token=None):
            path = Path(target_dir) / f"{source.source_id}.mp4"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"media")
            return SimpleNamespace(source=source, path=path, content_type="video/mp4")

        with patch("apify_transcript.actor.require_ffmpeg"):
            with patch("apify_transcript.actor.download_source", side_effect=fake_download):
                with patch("apify_transcript.actor.ffprobe_duration", return_value=64.0):
                    with patch("apify_transcript.actor.transcribe_media", return_value=sample_bundle()):
                        with self.assertRaisesRegex(RuntimeError, "1 media file"):
                            asyncio.run(run(fake_actor))

        self.assertEqual(fake_actor.dataset[0]["status"], "failed")
        self.assertIn("charged 0/2", fake_actor.dataset[0]["error"])
        self.assertFalse(fake_actor.dataset[0]["charged"])
        self.assertNotIn("001_a.txt", fake_actor.values)


if __name__ == "__main__":
    unittest.main()
