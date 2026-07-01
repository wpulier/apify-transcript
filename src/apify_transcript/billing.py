from __future__ import annotations

from dataclasses import dataclass

from .utils import ceil_minutes


TRANSCRIPTION_MINUTE_EVENT = "transcription-minute"


@dataclass(frozen=True)
class BillingResult:
    charged: bool
    minutes: int
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


async def charge_transcription_minutes(actor: object, duration_seconds: float | None) -> BillingResult:
    minutes = ceil_minutes(duration_seconds)
    try:
        result = await actor.charge(event_name=TRANSCRIPTION_MINUTE_EVENT, count=minutes)
    except Exception as exc:
        return BillingResult(charged=False, minutes=minutes, message=str(exc))
    limit_reached = getattr(result, "event_charge_limit_reached", False)
    return BillingResult(
        charged=not bool(limit_reached),
        minutes=minutes,
        message="charge limit reached" if limit_reached else None,
    )

