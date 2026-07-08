# Large Video to Transcript

Upload a large video or audio file, click **Start transcript**, and get an MP3 plus timestamped transcript exports when the job is done.

Use the Actor's **Standby** tab for the customer flow. Do not start from **Runs** or the raw **Input** form unless you intentionally want the fallback Apify batch path.

Open the Standby uploader, choose an MP4, MOV, WebM, MP3, M4A, or WAV file, or paste a direct downloadable media URL. The Actor handles the rest: upload, media prep, MP3 creation, transcription, subtitles, quality reporting, ZIP packaging, and clean signed download links.

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

1. Open the Actor's **Standby** tab. If you are on **Runs** or **Input**, you are in the fallback Apify form, not the simple upload flow.
2. Open the upload page.
3. Choose one video/audio file or paste one direct downloadable media URL.
4. Click **Start transcript**.
5. Keep the job page open, or bookmark it and come back later.
6. Download the MP3, transcript, subtitles, JSON, quality report, or ZIP bundle when the job is complete. The **Transcript** link downloads the `.txt` file directly.

The normal Apify **Input** form still works as a fallback/API-compatible batch path, but the Standby uploader is the primary product experience. It avoids Apify's technical upload modal and keeps the flow to: upload, start transcript, download results.

Local files still need to transfer before transcription starts, but the Standby uploader shows progress and starts the worker run automatically. For very large files, especially over 500 MB, a direct downloadable media URL is usually faster and easier to retry. The finished MP3, TXT, JSON, SRT, VTT, quality report, and ZIP links are signed browser-download links, so you do not need to manually add an API token to open the results after a run completes. These files are stored in the run's default Apify key-value store, so retention follows the run storage settings on your Apify account; download or preserve important outputs before that storage expires.

## Why Use This Actor

- Handles large video and audio files without building your own upload/transcription pipeline.
- Produces transcript exports that are useful outside Apify: text, subtitles, JSON, and ZIP.
- Uses speaker-aware authoritative transcription by default, with a prompt-guided recovery pass when coverage fails QA.
- Includes a quality report so you can see whether the transcript looks complete.
- Continues processing later files even if one source fails.

## API Example

Run the Actor in normal mode with a direct media URL:

```bash
curl -X POST "https://api.apify.com/v2/acts/kTgaX3cfI6dlJHa6J/runs?token=$APIFY_TOKEN&maxTotalChargeUsd=10" \
  -H "Content-Type: application/json" \
  -d '{"media":["https://example.com/recording.mp4"]}'
```

Request body:

```json
{
  "media": ["https://example.com/recording.mp4"]
}
```

The Standby API also supports job-based submission:

```bash
curl -X POST "$ACTOR_STANDBY_URL/jobs" \
  -H "Authorization: Bearer $APIFY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"mediaUrl":"https://example.com/recording.mp4"}'
```

## Managed Transcription

No provider account or API key is required. The managed transcription provider is included in the Actor price, so users can run the Actor like a normal paid Apify tool: submit media and receive MP3, transcript, subtitle, JSON, quality, and ZIP outputs.

Provider selection, audio preparation, chunking, retries, and quality checks are handled inside the Actor. Advanced provider overrides may be used for private testing, but they are not part of the public product surface.

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
- The Actor is optimized for spoken-word recordings. Music, singing, and lyric-style audio get a prompt-guided recovery pass, but can still fail QA if the model cannot capture enough intelligible words.
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
