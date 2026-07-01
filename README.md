# Large Video to Transcript

Upload a large video or audio file and get an MP3 plus timestamped transcript exports.

Upload MP4, MOV, WebM, MP3, M4A, or WAV files. The Actor prepares the media with ffmpeg, creates a normalized MP3, transcribes it with OpenAI or ElevenLabs, and returns clean signed download links for each file.

This is built for long recordings that are painful to process manually: client calls, Zoom recordings, sales calls, coaching sessions, podcasts, webinars, internal meetings, course videos, and research interviews.

## What You Get

Each successful source produces:

- `MP3`: normalized speech audio
- `TXT`: readable transcript with timestamps and speaker labels
- `JSON`: normalized transcript data with segments and word data when available
- `SRT`: subtitle export
- `VTT`: web subtitle export
- `quality.json`: coverage, word count, speaker count, warnings, and failures
- `ZIP`: one downloadable bundle containing all transcript artifacts when `includeZip` is enabled

The Actor also writes one dataset row per source with status, quality, duration, word count, speaker count, signed MP3/transcript/ZIP links, artifact keys, billing metadata, and any errors.

## How To Run

1. Open the Actor input tab.
2. Add one or more files in `Upload media files`.
3. Leave `Transcript provider` as OpenAI unless you want ElevenLabs.
4. Leave `Quality mode` as Authoritative for client-ready transcripts.
5. Click Run and download outputs from the Output tab.

The MP3, TXT, JSON, SRT, VTT, quality report, and ZIP links are signed browser-download links. You do not need to manually add an API token to open them. These files are stored in the run's default Apify key-value store, so retention follows the run storage settings on your Apify account; download or preserve important outputs before that storage expires.

## Why Use This Actor

- Handles large video and audio files without building your own upload/transcription pipeline.
- Produces transcript exports that are useful outside Apify: text, subtitles, JSON, and ZIP.
- Uses speaker-aware authoritative transcription by default.
- Includes a quality report so you can see whether the transcript looks complete.
- Continues processing later files even if one source fails.

## API Example

```json
{
  "media": ["https://example.com/recording.mp4"],
  "provider": "openai",
  "qualityMode": "authoritative",
  "language": "en",
  "keyterms": "AgentX\nREPC\nMLS",
  "includeZip": true,
  "transcriptConcurrency": 3
}
```

## Provider Keys

Provider keys are resolved in this order:

1. Secret input field, such as `openaiApiKey`
2. Actor environment variable, such as `OPENAI_API_KEY`

If you leave provider keys empty, the Actor uses the encrypted key configured by the Actor owner when available. You can also bring your own OpenAI or ElevenLabs key through the secret input fields.

## Provider Modes

- `openai` authoritative mode uses `gpt-4o-transcribe-diarize`, diarized JSON, automatic chunking, and chunked fallback for long files.
- `openai` fast mode uses `gpt-4o-mini-transcribe` for quicker, lower-cost drafts.
- `elevenlabs` mode uses Scribe v2 with diarization, word timestamps, and keyterms.
- `auto` tries available providers and can fall back when quality validation fails.

## Pricing Behavior

This Actor is designed for Apify Pay Per Event pricing.

The production billing event is:

```text
transcription-minute
```

It represents one started minute of successfully generated transcript output. The Actor checks the run charge limit before expensive transcription work and delivers artifacts only after the configured Apify charge succeeds.

Recommended launch price:

```text
$0.10 per transcription-minute
```

The recommended price is meant to cover provider costs, platform compute, storage, large-file handling, retries, transcript formatting, subtitle generation, ZIP packaging, and quality reporting.

Launch margin policy:

- Keep platform usage pass-through enabled until real paid runs prove the runtime cost.
- Keep managed-key pricing at `$0.10/min` or higher.
- Raise to `$0.12-$0.15/min` if platform usage is absorbed and median runtime exceeds `4x` source audio duration.

## Limitations

- Transcription quality depends on source audio quality, overlapping speakers, background noise, accents, and provider behavior.
- Speaker labels are generic, such as `Speaker 0`, unless a future workflow maps names.
- Direct media URLs are supported for API users by passing strings in the `media` array; URLs must be downloadable by the Actor without an interactive login.
- Upload or transcribe only media you own, are licensed to process, or otherwise have permission to process.

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
