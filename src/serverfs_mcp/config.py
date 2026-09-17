"""Runtime settings parsed from environment variables.

Only agent-facing knobs live here. Host paths never enter this container's
environment — Compose maps them to /workdirs/XX bind mounts instead.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """Tunable limits for reads, listings and searches."""

    log_level: str = "INFO"
    max_read_bytes: int = 524_288
    max_read_lines: int = 500
    default_list_limit: int = 100
    max_list_entries: int = 500
    default_search_results: int = 50
    max_search_results: int = 100
    max_walk_entries: int = 200_000
    search_timeout_seconds: float = 15.0
    search_max_file_bytes: int = 52_428_800
    allow_hidden: bool = False


def _get_int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _get_float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _get_bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = env.get(key, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _get_log_level(env: Mapping[str, str]) -> str:
    raw = env.get("SERVERFS_LOG_LEVEL", "INFO").strip().upper()
    return raw if raw in {"DEBUG", "INFO", "WARNING", "ERROR"} else "INFO"


def settings_from_env(env: Mapping[str, str] | None = None) -> Settings:
    """Build Settings from an environment mapping (defaults: os.environ)."""
    if env is None:
        env = os.environ
    return Settings(
        log_level=_get_log_level(env),
        max_read_bytes=_get_int(env, "SERVERFS_MAX_READ_BYTES", 524_288),
        max_read_lines=_get_int(env, "SERVERFS_MAX_READ_LINES", 500),
        default_list_limit=_get_int(env, "SERVERFS_DEFAULT_LIST_LIMIT", 100),
        max_list_entries=_get_int(env, "SERVERFS_MAX_LIST_ENTRIES", 500),
        default_search_results=_get_int(env, "SERVERFS_DEFAULT_SEARCH_RESULTS", 50),
        max_search_results=_get_int(env, "SERVERFS_MAX_SEARCH_RESULTS", 100),
        max_walk_entries=_get_int(env, "SERVERFS_MAX_WALK_ENTRIES", 200_000),
        search_timeout_seconds=_get_float(env, "SERVERFS_SEARCH_TIMEOUT_SECONDS", 15.0),
        search_max_file_bytes=_get_int(env, "SERVERFS_SEARCH_MAX_FILE_BYTES", 52_428_800),
        allow_hidden=_get_bool(env, "SERVERFS_ALLOW_HIDDEN", False),
    )
