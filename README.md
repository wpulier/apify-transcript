# Large Video to Transcript

Apify Actor that converts large video and audio files into speaker-labeled transcript bundles.

Upload MP4, MOV, WebM, MP3, M4A, WAV, or provide direct downloadable media URLs. The Actor writes transcript artifacts to the run's default key-value store and one result row per source to the default dataset.

## Outputs

Each successful source produces:

- `TXT`: readable speaker-labeled transcript with timestamps
- `JSON`: normalized transcript data with segments and words when available
- `SRT`: subtitle export
- `VTT`: subtitle export
- `quality.json`: coverage, word count, speakers, warnings, and failures
- `ZIP`: bundle of all transcript artifacts when `includeZip` is enabled

The run also writes an `OUTPUT` record with the final summary.

## Input

```json
{
  "mediaFiles": [],
  "mediaUrls": ["https://example.com/recording.mp4"],
  "provider": "openai",
  "qualityMode": "authoritative",
  "language": "en",
  "keyterms": "AgentX\nREPC\nMLS",
  "includeZip": true,
  "transcriptConcurrency": 3
}
```

Provider keys are resolved in this order:

1. Secret input field, such as `openaiApiKey`
2. Actor environment variable, such as `OPENAI_API_KEY`

## Provider Modes

- OpenAI authoritative mode uses `gpt-4o-transcribe-diarize`, diarized JSON, automatic chunking, and a chunk fallback for long files.
- OpenAI fast mode uses `gpt-4o-mini-transcribe` and returns a quicker text-first transcript.
- ElevenLabs mode uses Scribe v2 with diarization, word timestamps, and keyterms.

## Development

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover -s tests
python -m py_compile main.py src/apify_transcript/*.py
```

Docker build:

```bash
docker build -f .actor/Dockerfile .
```

## Monetization

The code is pay-per-event ready. Once pricing is configured in Apify Console, successful transcript runs charge the `transcription-minute` event after transcript artifacts are written.

Keep the Actor private until provider costs, pricing, and Store copy are finalized.

