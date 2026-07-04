# Large Video to Transcript

Upload a large video or audio file, click Run, and get an MP3 plus timestamped transcript exports when the job is done.

Open the Actor input form, upload MP4, MOV, WebM, MP3, M4A, or WAV files, or paste a direct downloadable media URL. The Actor handles the rest: media transfer, media prep, MP3 creation, transcription, subtitles, quality reporting, ZIP packaging, and clean signed download links.

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

1. Open the Actor's **Input** tab.
2. In **Upload video or audio**, click **Upload new files** and choose your video/audio file.
3. You can also paste a direct downloadable media URL in that same field.
4. Click **Run**.
5. When the run finishes, open **Output**.
6. Download the MP3, transcript, subtitles, JSON, quality report, or ZIP bundle from the Output table.

This is the only public product path. There is no separate Standby tab or second upload screen to choose from.

Local files still need to transfer to Apify before the run starts. For very large files, especially over 500 MB, a direct downloadable media URL is usually faster and easier to retry. The finished MP3, TXT, JSON, SRT, VTT, quality report, and ZIP links are signed browser-download links, so you do not need to manually add an API token to open the results after a run completes. These files are stored in the run's default Apify key-value store, so retention follows the run storage settings on your Apify account; download or preserve important outputs before that storage expires.

## Why Use This Actor

- Handles large video and audio files without building your own upload/transcription pipeline.
- Produces transcript exports that are useful outside Apify: text, subtitles, JSON, and ZIP.
- Uses speaker-aware authoritative transcription by default.
- Includes a quality report so you can see whether the transcript looks complete.
- Continues processing later files even if one source fails.

## API Example

Run the Actor with a direct media URL:

```bash
curl -X POST "https://api.apify.com/v2/acts/kTgaX3cfI6dlJHa6J/runs?token=$APIFY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"media":["https://example.com/recording.mp4"]}'
```

Request body:

```json
{
  "media": ["https://example.com/recording.mp4"]
}
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
