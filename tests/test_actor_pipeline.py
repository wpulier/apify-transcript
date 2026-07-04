import asyncio
import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from apify_transcript.actor import artifact_url, run
from apify_transcript.config import TranscriptConfig
from apify_transcript.jobs import (
    JobStore,
    create_upload_job,
    chunk_key,
    job_key,
    materialize_ingested_media,
    record_uploaded_chunk,
    upload_is_complete,
    worker_input_for_job,
)
from apify_transcript.media import MediaSource, download_url_source, parse_media_sources
from apify_transcript.standby import TranscriptStandbyHandler, TranscriptStandbyServer, is_standby_origin, render_home_page
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


class OrderTrackingActor(FakeActor):
    def __init__(self, actor_input, events):
        super().__init__(actor_input)
        self.events = events

    async def charge(self, event_name, count=1):
        self.events.append("charge")
        return await super().charge(event_name, count)


class FakeKvsClient:
    def __init__(self):
        self.records = {}

    def get_record(self, key):
        if key not in self.records:
            return None
        return {"key": key, "value": self.records[key][0], "content_type": self.records[key][1]}

    def get_record_as_bytes(self, key):
        if key not in self.records:
            return None
        value, content_type = self.records[key]
        if isinstance(value, bytes):
            body = value
        elif isinstance(value, str):
            body = value.encode("utf-8")
        else:
            body = json.dumps(value).encode("utf-8")
        return {"key": key, "value": body, "content_type": content_type}

    def set_record(self, key, value, content_type=None):
        self.records[key] = (value, content_type)


class FakeApifyClient:
    def actor(self, actor_id):
        return SimpleNamespace(start=lambda **kwargs: {"id": "run123"})


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
        with self.assertRaisesRegex(ValueError, "at least one"):
            parse_media_sources({"media": []})

    def test_input_schema_requires_a_media_source(self):
        schema = json.loads((Path(__file__).resolve().parents[1] / ".actor" / "INPUT_SCHEMA.json").read_text())
        self.assertEqual(schema["properties"]["media"]["editor"], "fileupload")
        self.assertEqual(schema["properties"]["media"]["title"], "Upload video or audio")
        self.assertIn("Upload new files", schema["properties"]["media"]["description"])
        self.assertIn("direct downloadable media URL", schema["properties"]["media"]["description"])
        self.assertEqual(schema["properties"]["media"]["minItems"], 1)
        self.assertEqual(schema["properties"]["media"]["maxItems"], 10)
        self.assertNotIn("prefill", schema["properties"]["media"])
        self.assertTrue((Path(__file__).resolve().parents[1] / "samples" / "large-video-to-transcript-sample.mp3").exists())
        self.assertEqual(schema["required"], ["media"])

    def test_input_schema_only_exposes_media(self):
        schema = json.loads((Path(__file__).resolve().parents[1] / ".actor" / "INPUT_SCHEMA.json").read_text())
        self.assertEqual(set(schema["properties"]), {"media"})
        self.assertNotIn("mediaFiles", schema["properties"])
        self.assertNotIn("mediaUrls", schema["properties"])
        self.assertNotIn("provider", schema["properties"])
        self.assertNotIn("qualityMode", schema["properties"])
        self.assertNotIn("openaiApiKey", schema["properties"])
        self.assertNotIn("elevenlabsApiKey", schema["properties"])

    def test_actor_wires_dataset_and_output_schemas(self):
        root = Path(__file__).resolve().parents[1]
        actor = json.loads((root / ".actor" / "actor.json").read_text())
        output_schema = json.loads((root / ".actor" / "output_schema.json").read_text())
        self.assertNotIn("usesStandbyMode", actor)
        self.assertNotIn("webServerSchema", actor)
        self.assertEqual(actor["output"], "./output_schema.json")
        self.assertEqual(actor["storages"]["dataset"], "./dataset_schema.json")
        self.assertIn("results", output_schema["properties"])
        self.assertIn("summary", output_schema["properties"])
        self.assertIn("artifacts", output_schema["properties"])

    def test_deploy_workflow_sets_full_permissions_for_uploads(self):
        workflow = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "apify-push.yml").read_text()
        self.assertIn("Configure managed provider keys", workflow)
        self.assertIn("OPENAI_API_KEY", workflow)
        self.assertIn("is_secret=True", workflow)
        self.assertIn("Configure primary Console upload UX", workflow)
        self.assertIn('actor_permission_level="FULL_PERMISSIONS"', workflow)
        self.assertIn('permission_level != "FULL_PERMISSIONS"', workflow)
        self.assertIn('"media": [', workflow)
        self.assertIn("example_run_input_body=sample_input", workflow)
        self.assertIn("actor_standby_is_enabled=False", workflow)
        self.assertIn("public UX must use one Console upload flow", workflow)

    def test_accepts_uploads_and_urls(self):
        sources = parse_media_sources(
            {
                "mediaFiles": ["https://api.apify.com/v2/key-value-stores/store/records/call.mp4"],
                "mediaUrls": ["https://example.com/demo.mov"],
            }
        )
        self.assertEqual([source.source_id for source in sources], ["001", "002"])
        self.assertEqual(sources[0].name, "call.mp4")

    def test_direct_url_download_logs_progress(self):
        class FakeResponse:
            headers = {"content-length": "10", "content-type": "audio/mpeg"}

            def raise_for_status(self):
                return None

            def iter_bytes(self):
                yield b"12345"
                yield b"67890"

        class FakeStream:
            def __enter__(self):
                return FakeResponse()

            def __exit__(self, exc_type, exc, traceback):
                return False

        messages = []
        source = MediaSource("001", "https://example.com/demo.mp3", "demo.mp3")
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("apify_transcript.media.httpx.stream", return_value=FakeStream()):
                local = download_url_source(source, source.original, Path(temp_dir), progress_log=messages.append)

            self.assertEqual(local.path.read_bytes(), b"1234567890")

        self.assertTrue(any("Download started" in message for message in messages))
        self.assertTrue(any("(100%)" in message for message in messages))

    def test_apify_upload_403_has_clear_message(self):
        class FakeResponse:
            status_code = 403
            headers = {}

            def raise_for_status(self):
                request = httpx.Request("GET", "https://api.apify.com/v2/key-value-stores/store/records/demo.mp4")
                response = httpx.Response(403, request=request)
                raise httpx.HTTPStatusError("forbidden", request=request, response=response)

        class FakeStream:
            def __enter__(self):
                return FakeResponse()

            def __exit__(self, exc_type, exc, traceback):
                return False

        source = MediaSource("001", "https://api.apify.com/v2/key-value-stores/store/records/demo.mp4", "demo.mp4")
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("apify_transcript.media.httpx.stream", return_value=FakeStream()):
                with self.assertRaisesRegex(PermissionError, "Approve the Actor's file/storage permissions"):
                    download_url_source(source, source.original, Path(temp_dir), progress_log=lambda _: None)

    def test_primary_media_parses_before_legacy_fields(self):
        sources = parse_media_sources(
            {
                "media": ["https://example.com/primary.mp4"],
                "mediaFiles": ["https://example.com/legacy.mp3"],
                "mediaUrls": ["https://example.com/url.mov"],
            }
        )
        self.assertEqual([source.name for source in sources], ["primary.mp4", "legacy.mp3", "url.mov"])

    def test_rejects_too_many_sources(self):
        with self.assertRaisesRegex(ValueError, "at most 10"):
            parse_media_sources({"media": [f"https://example.com/{index}.mp4" for index in range(11)]})

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


class StandbyTests(unittest.TestCase):
    def test_standby_origin_detection(self):
        actor = SimpleNamespace(configuration=SimpleNamespace(meta_origin="STANDBY"))
        self.assertTrue(is_standby_origin(actor))
        actor = SimpleNamespace(configuration=SimpleNamespace(meta_origin="WEBHOOK"))
        with patch.dict("os.environ", {"APIFY_META_ORIGIN": "STANDBY"}):
            self.assertTrue(is_standby_origin(actor))

    def test_home_page_renders_submit_controls(self):
        html = render_home_page()
        self.assertIn("Submit media", html)
        self.assertIn("media-file", html)
        self.assertIn("media-url", html)
        self.assertIn("/jobs", html)

    def test_upload_job_chunks_materialize_for_worker(self):
        fake_client = FakeKvsClient()
        store = JobStore(fake_client)
        job = create_upload_job("demo.mp4", 6, "video/mp4", chunk_size=3)
        self.assertNotIn("/", job_key(job["jobId"]))
        self.assertNotIn("/", chunk_key(job["jobId"], 0))
        store.save_job(job)
        store.set_chunk(job["jobId"], 0, b"abc")
        store.set_chunk(job["jobId"], 1, b"def")
        record_uploaded_chunk(job, 0, 3)
        record_uploaded_chunk(job, 1, 3)
        store.save_job(job)
        self.assertTrue(upload_is_complete(job))
        self.assertIn("media", worker_input_for_job(job))
        self.assertIn("ingestedMedia", worker_input_for_job(job))

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("apify_transcript.jobs.JobStore.from_token", return_value=store):
                path = materialize_ingested_media(
                    {
                        "jobId": job["jobId"],
                        "storeName": "fake",
                        "fileName": "demo.mp4",
                        "totalChunks": 2,
                    },
                    Path(temp_dir),
                    "token",
                )
            self.assertEqual(path.read_bytes(), b"abcdef")

    def test_standby_direct_url_job_starts_worker(self):
        fake_store = JobStore(FakeKvsClient())
        with patch("apify_transcript.standby.JobStore.from_token", return_value=fake_store):
            server = TranscriptStandbyServer(("127.0.0.1", 0), TranscriptStandbyHandler, token="token", actor_id="actor123", job_store_name="fake")
        server.apify_client = FakeApifyClient()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}/jobs"
            request = urllib.request.Request(
                url,
                data=json.dumps({"mediaUrl": "https://example.com/demo.mp4"}).encode("utf-8"),
                headers={"content-type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()

        self.assertEqual(payload["job"]["status"], "queued")
        self.assertEqual(payload["job"]["runId"], "run123")
        self.assertIn("/jobs/", payload["jobUrl"])


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

    def test_renders_mp3_artifact_without_duplicating_it_in_zip(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            mp3 = Path(temp_dir) / "audio.mp3"
            mp3.write_bytes(b"mp3-bytes")
            payloads = artifact_payloads(sample_bundle(), include_zip=True, mp3_path=mp3)

        self.assertEqual(payloads["mp3"], (b"mp3-bytes", "audio/mpeg"))
        self.assertIn("zip", payloads)
        self.assertLess(len(payloads["zip"][0]), 2000)

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
    def test_missing_input_writes_clean_output_summary(self):
        fake_actor = FakeActor({})

        with patch("apify_transcript.actor.require_ffmpeg"):
            with self.assertRaisesRegex(RuntimeError, "at least one"):
                asyncio.run(run(fake_actor))

        self.assertEqual(fake_actor.values["OUTPUT"][0]["status"], "failed")
        self.assertEqual(fake_actor.values["OUTPUT"][0]["itemCount"], 0)
        self.assertIn("at least one", fake_actor.values["OUTPUT"][0]["error"])

    def test_failed_first_file_does_not_stop_second_file(self):
        fake_actor = FakeActor(
            {
                "mediaUrls": ["https://example.com/a.mp4", "https://example.com/b.mp4"],
                "provider": "openai",
                "openaiApiKey": "key",
            }
        )

        def fake_download(source, target_dir, apify_token=None, progress_log=None):
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
                        with patch("apify_transcript.actor.prepare_mp3_artifact", return_value=Path("audio.mp3")):
                            with patch("pathlib.Path.read_bytes", return_value=b"mp3"):
                                with self.assertRaisesRegex(RuntimeError, "1 media file"):
                                    asyncio.run(run(fake_actor))

        self.assertEqual(len(fake_actor.dataset), 2)
        self.assertEqual(fake_actor.dataset[0]["status"], "failed")
        self.assertEqual(fake_actor.dataset[1]["status"], "completed")
        self.assertEqual(fake_actor.dataset[1]["mp3Key"], "002_b.mp3")
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

        def fake_download(source, target_dir, apify_token=None, progress_log=None):
            path = Path(target_dir) / f"{source.source_id}.mp4"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"media")
            return SimpleNamespace(source=source, path=path, content_type="video/mp4")

        with patch("apify_transcript.actor.require_ffmpeg"):
            with patch("apify_transcript.actor.download_source", side_effect=fake_download):
                with patch("apify_transcript.actor.ffprobe_duration", return_value=64.0):
                    with patch("apify_transcript.actor.transcribe_media", return_value=sample_bundle()):
                        with patch("apify_transcript.actor.prepare_mp3_artifact", return_value=Path("audio.mp3")):
                            with patch("apify_transcript.actor.artifact_payloads", return_value={"txt": (b"text", "text/plain")}):
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

        def fake_download(source, target_dir, apify_token=None, progress_log=None):
            path = Path(target_dir) / f"{source.source_id}.mp4"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"media")
            return SimpleNamespace(source=source, path=path, content_type="video/mp4")

        with patch("apify_transcript.actor.require_ffmpeg"):
            with patch("apify_transcript.actor.download_source", side_effect=fake_download):
                with patch("apify_transcript.actor.ffprobe_duration", return_value=64.0):
                    with patch("apify_transcript.actor.transcribe_media", return_value=sample_bundle()):
                        with patch("apify_transcript.actor.prepare_mp3_artifact", return_value=Path("audio.mp3")):
                            with patch("apify_transcript.actor.artifact_payloads", return_value={"txt": (b"text", "text/plain")}):
                                with self.assertRaisesRegex(RuntimeError, "1 media file"):
                                    asyncio.run(run(fake_actor))

        self.assertEqual(fake_actor.dataset[0]["status"], "failed")
        self.assertIn("charged 0/2", fake_actor.dataset[0]["error"])
        self.assertFalse(fake_actor.dataset[0]["charged"])
        self.assertNotIn("001_a.txt", fake_actor.values)

    def test_charge_happens_after_local_payload_generation_and_before_store(self):
        events = []
        fake_actor = OrderTrackingActor(
            {
                "mediaUrls": ["https://example.com/a.mp4"],
                "provider": "openai",
                "openaiApiKey": "key",
            },
            events,
        )

        def fake_download(source, target_dir, apify_token=None, progress_log=None):
            path = Path(target_dir) / f"{source.source_id}.mp4"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"media")
            return SimpleNamespace(source=source, path=path, content_type="video/mp4")

        def fake_prepare(*args, **kwargs):
            events.append("prepare")
            return Path("audio.mp3")

        def fake_payloads(*args, **kwargs):
            events.append("payloads")
            return {"txt": (b"text", "text/plain")}

        async def fake_store(*args, **kwargs):
            events.append("store")
            return {"txt": "001_a.txt"}

        with patch("apify_transcript.actor.require_ffmpeg"):
            with patch("apify_transcript.actor.download_source", side_effect=fake_download):
                with patch("apify_transcript.actor.ffprobe_duration", return_value=64.0):
                    with patch("apify_transcript.actor.transcribe_media", return_value=sample_bundle()):
                        with patch("apify_transcript.actor.prepare_mp3_artifact", side_effect=fake_prepare):
                            with patch("apify_transcript.actor.artifact_payloads", side_effect=fake_payloads):
                                with patch("apify_transcript.actor.store_artifacts", side_effect=fake_store):
                                    asyncio.run(run(fake_actor))

        self.assertEqual(events, ["prepare", "payloads", "charge", "store"])
        self.assertEqual(fake_actor.dataset[0]["status"], "completed")


if __name__ == "__main__":
    unittest.main()
