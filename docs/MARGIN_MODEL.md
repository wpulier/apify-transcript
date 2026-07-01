# Margin Model

This Actor should launch at a price that keeps margin positive under normal and stressed conditions.

## Launch Price

Recommended pay-per-event price:

```text
$0.08 per transcription-minute
```

Primary event:

```text
transcription-minute
```

Launch rule:

- Keep Apify platform usage pass-through enabled until at least 10 real paid runs have been measured.
- Do not reduce price below `$0.08/min` while using managed provider keys.
- If platform usage pass-through is disabled and median runtime is more than `4x` source audio duration, raise price to `$0.10-$0.12/min` or optimize runtime first.

## Margin Formula

Apify Pay Per Event profit is:

```text
profit = (0.8 * event_revenue) - provider_cost - platform_cost
```

If platform usage is passed through to the user:

```text
profit = (0.8 * event_revenue) - provider_cost
```

## Cost Assumptions

Current assumptions as of 2026-07-01:

- Apify marketplace share: `20%`
- OpenAI high-quality transcription estimate: `$0.006/min`
- ElevenLabs Scribe batch transcription: about `$0.22/hour`, or `$0.0037/min`
- Apify compute unit: `1 GB RAM * 1 hour`
- Apify Free/Starter compute: `$0.20/CU`
- Actor default memory: `2048 MB`

Official references:

- Apify PPE profit and platform costs: https://docs.apify.com/platform/actors/publishing/monetize/pay-per-event
- Apify cost table: https://docs.apify.com/platform/actors/publishing/monetize/pricing-and-costs
- Apify compute unit definition: https://help.apify.com/en/articles/3490384-what-is-a-compute-unit
- OpenAI transcription pricing: https://developers.openai.com/api/docs/pricing
- ElevenLabs API pricing: https://elevenlabs.io/pricing/api

## Per-Minute Economics

At `$0.08/min`, Apify keeps 20%, so net before provider/platform is:

```text
$0.08 * 0.8 = $0.064/min
```

With OpenAI at `$0.006/min`:

```text
$0.064 - $0.006 = $0.058/min
```

That is the expected per-minute contribution when platform usage is passed through.

## Absorbed Platform Usage Scenarios

If platform usage is not passed through, compute cost depends on memory and wall-clock runtime.

Formula:

```text
platform_cost_per_audio_minute =
  memory_gb * runtime_ratio * cu_price / 60
```

Where `runtime_ratio` means wall-clock runtime divided by source audio duration.

At 2 GB and `$0.20/CU`:

| Runtime ratio | Platform cost/min | Profit/min after Apify + OpenAI + platform | Margin on customer price |
| --- | ---: | ---: | ---: |
| `1x` | `$0.0067` | `$0.0513` | `64%` |
| `2x` | `$0.0133` | `$0.0447` | `56%` |
| `4x` | `$0.0267` | `$0.0313` | `39%` |
| `6x` | `$0.0400` | `$0.0180` | `23%` |
| `8x` | `$0.0533` | `$0.0047` | `6%` |

Break-even at 2 GB with OpenAI:

```text
runtime_ratio ~= 8.7x
```

This means a 60-minute file would need to take about 8.7 hours of Actor runtime before `$0.08/min` breaks even when platform usage is not passed through.

## Pricing Decision

`$0.08/min` has healthy margin for launch if:

- platform usage pass-through is enabled, or
- default memory stays near 2 GB, and
- typical runtime stays under `4x` audio duration.

Keep the launch price at `$0.08/min` for now.

Raise to `$0.10/min` if:

- customers mainly use managed keys,
- platform usage pass-through is disabled,
- median runtime is above `4x`,
- retries become common, or
- support/manual QA becomes part of the offer.

Raise to `$0.12-$0.15/min` if:

- we market this as an ElevenLabs-class authoritative transcript product,
- we add summaries, action items, named speaker mapping, or CRM/Drive exports,
- we absorb all Apify usage costs permanently.
