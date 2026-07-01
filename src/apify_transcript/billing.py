from __future__ import annotations

from dataclasses import dataclass

from .utils import ceil_minutes


TRANSCRIPTION_MINUTE_EVENT = "transcription-minute"
TRANSCRIPTION_MINUTE_DESCRIPTION = "One started minute of successfully generated video or audio transcript output."
RECOMMENDED_TRANSCRIPTION_MINUTE_PRICE_USD = 0.08


@dataclass(frozen=True)
class BillingResult:
    charged: bool
    minutes: int
    event_name: str = TRANSCRIPTION_MINUTE_EVENT
    message: str | None = None


async def ensure_budget(actor: object, duration_seconds: float | None) -> None:
    minutes = ceil_minutes(duration_seconds)
    try:
        manager = actor.get_charging_manager()
        limit = manager.calculate_max_event_charge_count_within_limit(TRANSCRIPTION_MINUTE_EVENT)
    except Exception:
        return
    if limit is not None and limit < minutes:
        raise RuntimeError(
            f"Run charge limit allows {limit} transcription minute(s), "
            f"but this file needs {minutes} minute(s)."
        )


async def charge_transcription_minutes(
    actor: object,
    duration_seconds: float | None,
    *,
    required: bool = True,
) -> BillingResult:
    minutes = ceil_minutes(duration_seconds)
    try:
        result = await actor.charge(event_name=TRANSCRIPTION_MINUTE_EVENT, count=minutes)
    except Exception as exc:
        if required:
            raise RuntimeError(f"Could not charge {minutes} {TRANSCRIPTION_MINUTE_EVENT} event(s): {exc}") from exc
        return BillingResult(charged=False, minutes=minutes, message=str(exc))
    limit_reached = getattr(result, "event_charge_limit_reached", False)
    charged_count = getattr(result, "charged_count", minutes if not limit_reached else 0)
    if charged_count < minutes:
        message = (
            f"charged {charged_count}/{minutes} {TRANSCRIPTION_MINUTE_EVENT} event(s)"
            if not limit_reached
            else f"charge limit reached after charging {charged_count}/{minutes} {TRANSCRIPTION_MINUTE_EVENT} event(s)"
        )
        if required:
            raise RuntimeError(message)
        return BillingResult(charged=False, minutes=minutes, message=message)
    return BillingResult(
        charged=True,
        minutes=minutes,
        message=None,
    )
