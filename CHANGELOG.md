# Changelog

## 0.1

- Initial Large Video to Transcript Actor.
- Supports uploaded media files and direct downloadable media URLs.
- Produces TXT, JSON, SRT, VTT, quality report, and optional ZIP artifacts.
- Supports OpenAI authoritative, OpenAI fast, ElevenLabs, and auto provider modes.
- Adds Pay Per Event guard for the `transcription-minute` billing event.
- Adds one required `media` input, signed browser artifact links, and normalized MP3 output.
- Adds launch-ready output schema, upload-first Console copy, and $0.10/min paid beta pricing.
- Adds a tiny prefilled sample MP3 so Apify Console task creation and Store onboarding have valid media while keeping `media` required.
- Simplifies the public input form to one submit field, hides advanced defaults, and adds URL download progress logs.
- Requires full Actor permissions during deployment so Console-uploaded media files can be read from Apify storage, with a clearer uploaded-file permission error if access is denied.
- Removes provider-key/provider-mode controls from the public product surface so paid users can submit media and receive managed transcript outputs without bringing API keys.
