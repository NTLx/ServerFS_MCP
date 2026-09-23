"""Small shared helpers."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta


def utc_now() -> str:
    return _format_utc(datetime.now(UTC))


def utc_after(seconds: int) -> str:
    return _format_utc(datetime.now(UTC) + timedelta(seconds=seconds))


def utc_before(seconds: int) -> str:
    return _format_utc(datetime.now(UTC) - timedelta(seconds=seconds))


def seconds_until(value: str) -> float:
    return (_parse_utc(value) - datetime.now(UTC)).total_seconds()


def is_expired(value: str | None) -> bool:
    return value is not None and seconds_until(value) <= 0


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _format_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(12)}"
