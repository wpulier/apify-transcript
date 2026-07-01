# Monetization Setup

This Actor is prepared for Apify Pay Per Event monetization. The code charges only after transcript generation succeeds and before transcript artifacts are delivered.

## Actor

- Actor ID: `kTgaX3cfI6dlJHa6J`
- Actor name: `large-video-to-transcript`
- Store title: `Large Video to Transcript`
- Primary value: convert large MP4/MOV/MP3/M4A/WAV files into transcript bundles with TXT, JSON, SRT, VTT, ZIP, and quality report.

## Required Provider Secrets

Configure these in Apify Console as secret environment variables on Actor version `0.1`:

- `OPENAI_API_KEY`: required for managed OpenAI transcription
- `ELEVENLABS_API_KEY`: optional for ElevenLabs/Scribe fallback

Current v1 can run on OpenAI only. ElevenLabs improves fallback coverage when available.

## Pay Per Event Pricing

In Apify Console:

1. Open the Actor.
2. Go to `Publication`.
3. Complete billing and payout details if prompted.
4. Select `Set up monetization`.
5. Choose `Pay per event`.
6. Add this custom event:

```text
Event name: transcription-minute
Description: One started minute of successfully generated video or audio transcript output.
Price: $0.08
Unit: event
```

7. Set `transcription-minute` as the primary event.
8. Initial launch setting: enable platform usage pass-through until real run costs are measured.
9. Set a minimum max cost per run of at least `$1.00`.
10. Keep the Actor private until the first paid test run succeeds.

## Pricing Rationale

Recommended launch price:

```text
$0.08 per started transcription minute
```

Expected customer prices:

- 30 minute recording: about `$2.40`
- 60 minute recording: about `$4.80`
- 90 minute recording: about `$7.20`
- 120 minute recording: about `$9.60`

This prices the workflow outcome, not just the raw speech-to-text call. The delivered value includes large-file media prep, provider orchestration, diarization, subtitles, JSON export, ZIP packaging, quality audit, dataset rows, and Apify-hosted artifacts.

## Cost Controls

- `requireSuccessfulCharge` defaults to `true`.
- The Actor checks the charge limit before calling the provider.
- Transcript artifacts are delivered only after the pay-per-event charge succeeds.
- Failed provider calls do not charge.
- Failed media files do not stop later media files.

## Store Listing Positioning

Short description:

```text
Upload large MP4, MOV, MP3, M4A, or WAV files and get a complete transcript bundle with speaker labels, timestamps, subtitles, JSON, ZIP, and quality report.
```

Best customer segments:

- agencies processing client calls
- sales teams archiving discovery calls
- coaches and course creators processing long recordings
- podcast and webinar teams generating subtitles
- operations teams turning meetings into searchable records
- real estate teams transcribing REPC, MLS, and transaction calls

## Before Public Launch

- Rotate any Apify token that was pasted into chat or logs.
- Confirm `OPENAI_API_KEY` is set as an Apify secret environment variable.
- Add `ELEVENLABS_API_KEY` if ElevenLabs fallback will be marketed.
- Run one private paid test with a short MP4.
- Confirm dataset row has `charged: true`.
- Confirm TXT, JSON, SRT, VTT, quality, and ZIP artifacts are present.
- Confirm the run cost matches expected billable minutes.
- Review Apify Store copy, support email, and issue reporting link.
- Only then make the Actor public.
