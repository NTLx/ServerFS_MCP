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
    """Tunable limits for reads, listings, searches and mutations."""

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
    extra_deny_globs: tuple[str, ...] = ()
    disable_default_deny: bool = False
    max_write_bytes: int = 1_048_576
    binary_transfer_enabled: bool = False
    max_binary_transfer_bytes: int = 8_388_608
    file_ingress_enabled: bool = False
    file_ingress_timeout_seconds: float = 30.0
    agent_mode: str = "disabled"
    agent_runtimes: frozenset[str] = frozenset()
    max_edits_per_call: int = 50
    agent_bridge_enabled: bool = False
    agent_bridge_socket: str = "/run/serverfs-agent-bridge/bridge.sock"
    agent_bridge_timeout_seconds: float = 30.0
    agent_lock_dir: str = "/run/serverfs-agent-locks"


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


_TRUE_VALUES = frozenset({"true", "1", "yes", "on"})
_FALSE_VALUES = frozenset({"false", "0", "no", "off"})


def _get_strict_bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = env.get(key, "").strip().lower()
    if not raw:
        return default
    if raw in _TRUE_VALUES:
        return True
    if raw in _FALSE_VALUES:
        return False
    raise ValueError(f"invalid {key} value {env.get(key, '')!r}")


def _get_positive_int_strict(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"invalid {key} value {env.get(key, '')!r}") from exc
    if value <= 0:
        raise ValueError(f"invalid {key} value {env.get(key, '')!r}")
    return value


def _get_log_level(env: Mapping[str, str]) -> str:
    raw = env.get("SERVERFS_LOG_LEVEL", "INFO").strip().upper()
    return raw if raw in {"DEBUG", "INFO", "WARNING", "ERROR"} else "INFO"


def _get_deny_globs(env: Mapping[str, str]) -> tuple[str, ...]:
    """Parse a comma-separated glob list; empty/whitespace entries dropped."""
    raw = env.get("SERVERFS_EXTRA_DENY_GLOBS", "")
    return tuple(g for g in (s.strip() for s in raw.split(",")) if g)


def _get_agent_mode(env: Mapping[str, str], key: str = "SERVERFS_AGENT_MODE") -> str:
    raw = env.get(key, "").strip().lower()
    if not raw:
        return "disabled"
    if raw not in {"disabled", "review", "workspace-write"}:
        raise ValueError(f"invalid {key} value {env.get(key, '')!r}")
    return raw


def _get_agent_runtimes(
    env: Mapping[str, str], key: str = "SERVERFS_AGENT_RUNTIMES"
) -> frozenset[str]:
    raw = env.get(key, "")
    values = [item.strip().lower() for item in raw.split(",") if item.strip()]
    if len(values) != len(set(values)) or set(values) - {"codex", "claude"}:
        raise ValueError(f"invalid {key} value {raw!r}")
    return frozenset(values)


def settings_from_env(env: Mapping[str, str] | None = None) -> Settings:
    """Build Settings from an environment mapping (defaults: os.environ)."""
    if env is None:
        env = os.environ
    agent_mode = _get_agent_mode(env)
    agent_runtimes = _get_agent_runtimes(env)
    if agent_mode == "disabled" and agent_runtimes:
        raise ValueError("SERVERFS_AGENT_MODE=disabled cannot have Agent runtimes")
    if agent_mode != "disabled" and not agent_runtimes:
        raise ValueError("enabled SERVERFS_AGENT_MODE requires Agent runtimes")
    return Settings(
        log_level=_get_log_level(env),
        max_read_bytes=_get_positive_int_strict(env, "SERVERFS_MAX_READ_BYTES", 524_288),
        max_read_lines=_get_positive_int_strict(env, "SERVERFS_MAX_READ_LINES", 500),
        default_list_limit=_get_int(env, "SERVERFS_DEFAULT_LIST_LIMIT", 100),
        max_list_entries=_get_int(env, "SERVERFS_MAX_LIST_ENTRIES", 500),
        default_search_results=_get_int(env, "SERVERFS_DEFAULT_SEARCH_RESULTS", 50),
        max_search_results=_get_int(env, "SERVERFS_MAX_SEARCH_RESULTS", 100),
        max_walk_entries=_get_int(env, "SERVERFS_MAX_WALK_ENTRIES", 200_000),
        search_timeout_seconds=_get_float(env, "SERVERFS_SEARCH_TIMEOUT_SECONDS", 15.0),
        search_max_file_bytes=_get_int(env, "SERVERFS_SEARCH_MAX_FILE_BYTES", 52_428_800),
        allow_hidden=_get_strict_bool(env, "SERVERFS_ALLOW_HIDDEN", False),
        extra_deny_globs=_get_deny_globs(env),
        disable_default_deny=_get_strict_bool(env, "SERVERFS_DISABLE_DEFAULT_DENY", False),
        max_write_bytes=_get_positive_int_strict(env, "SERVERFS_MAX_WRITE_BYTES", 1_048_576),
        binary_transfer_enabled=_get_strict_bool(env, "SERVERFS_BINARY_TRANSFER_ENABLED", False),
        max_binary_transfer_bytes=_get_positive_int_strict(
            env, "SERVERFS_MAX_BINARY_TRANSFER_BYTES", 8_388_608
        ),
        file_ingress_enabled=_get_strict_bool(env, "SERVERFS_FILE_INGRESS_ENABLED", False),
        file_ingress_timeout_seconds=_get_float(env, "SERVERFS_FILE_INGRESS_TIMEOUT_SECONDS", 30.0),
        agent_mode=agent_mode,
        agent_runtimes=agent_runtimes,
        max_edits_per_call=_get_int(env, "SERVERFS_MAX_EDITS_PER_CALL", 50),
        agent_bridge_enabled=_get_bool(env, "SERVERFS_AGENT_BRIDGE_ENABLED", False),
        agent_bridge_socket=env.get(
            "SERVERFS_AGENT_BRIDGE_SOCKET", "/run/serverfs-agent-bridge/bridge.sock"
        ).strip()
        or "/run/serverfs-agent-bridge/bridge.sock",
        agent_bridge_timeout_seconds=_get_float(env, "SERVERFS_AGENT_BRIDGE_TIMEOUT_SECONDS", 30.0),
        agent_lock_dir=env.get("SERVERFS_AGENT_LOCK_DIR", "/run/serverfs-agent-locks").strip()
        or "/run/serverfs-agent-locks",
    )
