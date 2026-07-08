# Changelog

## 0.4

- Treats `failed_qa` transcripts as failed media files instead of successful outputs.
- Stops before billing and artifact delivery when quality validation fails.
- Preserves failed-QA details such as word count, speaker count, warnings, failures, and error message in the dataset row.

## 0.3

- Makes the Standby upload page the primary product UX: upload media, click Go, and return to a job page for results.
- Re-enables Apify Standby with an OpenAPI web server schema and low-idle web-server settings.
- Preserves normal Actor input as a fallback/API-compatible batch path.
- Passes the Standby run charge limit into the background transcription worker.
- Keeps transcript TXT links as browser-download attachments.

## 0.2

- Collapses the public UX to one path: upload media in the Actor Input tab, click Run, and download results from Output.
- Disables the public Standby tab to avoid two competing upload flows.
- Updates Store copy and input field text to make the upload flow explicit.

## 0.1

- Initial Large Video to Transcript Actor.
- Supports uploaded media files and direct downloadable media URLs.
- Produces TXT, JSON, SRT, VTT, quality report, and optional ZIP artifacts.
- Supports OpenAI authoritative, OpenAI fast, ElevenLabs, and auto provider modes.
- Adds Pay Per Event guard for the `transcription-minute` billing event.
- Adds one required `media` input, signed browser artifact links, and normalized MP3 output.
- Adds launch-ready output schema, upload-first Console copy, and $0.10/min paid beta pricing.
- Simplifies the public input form to one required upload field, hides advanced defaults, and adds URL download progress logs.
- Requires full Actor permissions during deployment so Console-uploaded media files can be read from Apify storage, with a clearer uploaded-file permission error if access is denied.
- Removes provider-key/provider-mode controls from the public product surface so paid users can submit media and receive managed transcript outputs without bringing API keys.
- Keeps the Standby job server code available for future advanced/API use, but it is not the public buyer UX.
