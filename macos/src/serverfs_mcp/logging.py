"""Structured JSON logging.

Emits one JSON object per log line to stderr with UTC timestamps.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}

_level = "INFO"


def set_level(level: str) -> None:
    """Set the minimum log level (DEBUG/INFO/WARNING/ERROR)."""
    _set_level_ref[0] = level.upper() if level.upper() in _LEVELS else "INFO"


# simple mutable holder so set_level works without globals juggling in hot paths
_set_level_ref: list[str] = [_level]


def _enabled(level: str) -> bool:
    return _LEVELS[level] >= _LEVELS[_set_level_ref[0]]


def _emit(level: str, event: str, fields: dict[str, Any]) -> None:
    if not _enabled(level):
        return
    record = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        + f".{int(time.time() * 1000) % 1000:03d}Z",
        "level": level,
        "event": event,
    }
    record.update(fields)
    sys.stderr.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    sys.stderr.flush()


def debug(event: str, **fields: Any) -> None:
    _emit("DEBUG", event, fields)


def info(event: str, **fields: Any) -> None:
    _emit("INFO", event, fields)


def warning(event: str, **fields: Any) -> None:
    _emit("WARNING", event, fields)


def error(event: str, **fields: Any) -> None:
    _emit("ERROR", event, fields)
