"""Runtime settings parsed from environment variables.

Protocol, policy and optional-capability knobs live here. Host paths never enter
this container's environment — Compose maps them to /workdirs/XX bind mounts instead.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    """Tunable limits for reads, listings, searches and mutations."""

    log_level: str = "INFO"
    http_host: str = "0.0.0.0"
    http_port: int = 8000
    http_path: str = "/mcp"
    http_allowed_hosts: tuple[str, ...] = ("serverfs-mcp:8000",)
    http_allowed_origins: tuple[str, ...] = ()
    http_api_key: str = ""
    oauth_enabled: bool = False
    oauth_resource_url: str = ""
    oauth_issuer_url: str = ""
    oauth_required_scopes: tuple[str, ...] = ("serverfs:access",)
    oauth_introspection_url: str = "http://serverfs-auth:8082/internal/introspect"
    oauth_introspection_secret: str = ""
    oauth_introspection_timeout_seconds: float = 2.0
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
    #: When set, the ingress helper serves HTTP over this private AF_UNIX
    # socket instead of a TCP listener (v0.13 macOS native deployments).
    file_ingress_socket: str = ""
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


def _get_positive_float_strict(env: Mapping[str, str], key: str, default: float) -> float:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"invalid {key} value {env.get(key, '')!r}") from exc
    if value <= 0:
        raise ValueError(f"invalid {key} value {env.get(key, '')!r}")
    return value


def _get_bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = env.get(key, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


_TRUE_VALUES = frozenset({"true", "1", "yes", "on"})
_FALSE_VALUES = frozenset({"false", "0", "no", "off"})
_ACP_RUNTIME_ID_RE = re.compile(r"^acp-[a-z][a-z0-9_-]{0,27}$")


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


def _get_http_host(env: Mapping[str, str]) -> str:
    raw = env.get("SERVERFS_HTTP_HOST", "").strip()
    value = raw or "0.0.0.0"
    if any(char.isspace() for char in value) or "://" in value or "/" in value:
        raise ValueError(f"invalid SERVERFS_HTTP_HOST value {env.get('SERVERFS_HTTP_HOST', '')!r}")
    return value


def _get_http_port(env: Mapping[str, str]) -> int:
    raw = env.get("SERVERFS_HTTP_PORT", "").strip()
    if not raw:
        return 8000
    try:
        value = int(raw)
    except ValueError as exc:
        raw_value = env.get("SERVERFS_HTTP_PORT", "")
        raise ValueError(f"invalid SERVERFS_HTTP_PORT value {raw_value!r}") from exc
    if value < 1 or value > 65535:
        raw_value = env.get("SERVERFS_HTTP_PORT", "")
        raise ValueError(f"invalid SERVERFS_HTTP_PORT value {raw_value!r}")
    return value


def _get_http_path(env: Mapping[str, str]) -> str:
    raw = env.get("SERVERFS_HTTP_PATH", "").strip()
    value = raw or "/mcp"
    if (
        not value.startswith("/")
        or value.startswith("//")
        or any(char.isspace() for char in value)
        or "?" in value
        or "#" in value
    ):
        raise ValueError(f"invalid SERVERFS_HTTP_PATH value {env.get('SERVERFS_HTTP_PATH', '')!r}")
    return value


def _get_http_allowed_hosts(env: Mapping[str, str], port: int) -> tuple[str, ...]:
    raw = env.get("SERVERFS_HTTP_ALLOWED_HOSTS")
    if raw is None or not raw.strip():
        return (f"serverfs-mcp:{port}",)
    values = tuple(item for item in (part.strip() for part in raw.split(",")) if item)
    if not values:
        raise ValueError(f"invalid SERVERFS_HTTP_ALLOWED_HOSTS value {raw!r}")
    for item in values:
        invalid_wildcard = "*" in item and not (item.endswith(":*") and item.count("*") == 1)
        if any(char.isspace() for char in item) or "://" in item or "/" in item or invalid_wildcard:
            raise ValueError(f"invalid SERVERFS_HTTP_ALLOWED_HOSTS value {raw!r}")
    return values


def _get_http_allowed_origins(env: Mapping[str, str]) -> tuple[str, ...]:
    raw = env.get("SERVERFS_HTTP_ALLOWED_ORIGINS", "")
    values = tuple(item for item in (part.strip() for part in raw.split(",")) if item)
    for item in values:
        try:
            parsed = urlsplit(item)
            _ = parsed.port
        except ValueError as exc:
            raise ValueError(f"invalid SERVERFS_HTTP_ALLOWED_ORIGINS value {raw!r}") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or any(char.isspace() for char in item)
        ):
            raise ValueError(f"invalid SERVERFS_HTTP_ALLOWED_ORIGINS value {raw!r}")
    return values


def _is_loopback_host(hostname: str | None) -> bool:
    return hostname in {"localhost", "127.0.0.1", "::1"}


def _get_oauth_url(
    env: Mapping[str, str],
    key: str,
    *,
    required: bool,
    default: str = "",
    require_https: bool,
) -> str:
    raw = env.get(key, "").strip() or default
    if not raw:
        if required:
            raise ValueError(f"{key} is required when SERVERFS_OAUTH_ENABLED=true")
        return ""
    try:
        parsed = urlsplit(raw)
        _ = parsed.port
    except ValueError as exc:
        raise ValueError(f"invalid {key} value {env.get(key, '')!r}") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(char.isspace() for char in raw)
    ):
        raise ValueError(f"invalid {key} value {env.get(key, '')!r}")
    if require_https and parsed.scheme != "https" and not _is_loopback_host(parsed.hostname):
        raise ValueError(f"{key} must use https outside loopback")
    return raw


def _get_oauth_scopes(env: Mapping[str, str]) -> tuple[str, ...]:
    raw = env.get("SERVERFS_OAUTH_REQUIRED_SCOPES", "serverfs:access")
    values = tuple(item.strip() for item in raw.split(",") if item.strip())
    if not values or len(values) != len(set(values)):
        raise ValueError(f"invalid SERVERFS_OAUTH_REQUIRED_SCOPES value {raw!r}")
    if any(any(char.isspace() for char in item) for item in values):
        raise ValueError(f"invalid SERVERFS_OAUTH_REQUIRED_SCOPES value {raw!r}")
    return values


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
    values = [item.strip() for item in raw.split(",") if item.strip()]
    values = [
        value.lower() if value.lower() in {"codex", "claude", "qoder"} else value
        for value in values
    ]
    known_native = {"codex", "claude", "qoder"}
    unknown = {
        value
        for value in values
        if value not in known_native and not _ACP_RUNTIME_ID_RE.fullmatch(value)
    }
    if len(values) != len(set(values)) or unknown:
        raise ValueError(f"invalid {key} value {raw!r}")
    return frozenset(values)


def settings_from_env(env: Mapping[str, str] | None = None) -> Settings:
    """Build Settings from an environment mapping (defaults: os.environ)."""
    if env is None:
        env = os.environ
    http_port = _get_http_port(env)
    agent_mode = _get_agent_mode(env)
    agent_runtimes = _get_agent_runtimes(env)
    http_api_key = env.get("SERVERFS_HTTP_API_KEY", "").strip()
    oauth_enabled = _get_strict_bool(env, "SERVERFS_OAUTH_ENABLED", False)
    if http_api_key and oauth_enabled:
        raise ValueError(
            "SERVERFS_HTTP_API_KEY cannot be combined with SERVERFS_OAUTH_ENABLED=true"
        )
    oauth_resource_url = _get_oauth_url(
        env,
        "SERVERFS_OAUTH_RESOURCE_URL",
        required=oauth_enabled,
        require_https=True,
    )
    oauth_issuer_url = _get_oauth_url(
        env,
        "SERVERFS_OAUTH_ISSUER_URL",
        required=oauth_enabled,
        require_https=True,
    )
    oauth_introspection_url = _get_oauth_url(
        env,
        "SERVERFS_OAUTH_INTROSPECTION_URL",
        required=False,
        default="http://serverfs-auth:8082/internal/introspect",
        require_https=False,
    )
    oauth_required_scopes = _get_oauth_scopes(env)
    oauth_introspection_secret = env.get("SERVERFS_OAUTH_INTROSPECTION_SECRET", "").strip()
    if oauth_enabled and not oauth_introspection_secret:
        raise ValueError(
            "SERVERFS_OAUTH_INTROSPECTION_SECRET is required when SERVERFS_OAUTH_ENABLED=true"
        )
    if agent_mode == "disabled" and agent_runtimes:
        raise ValueError("SERVERFS_AGENT_MODE=disabled cannot have Agent runtimes")
    if agent_mode != "disabled" and not agent_runtimes:
        raise ValueError("enabled SERVERFS_AGENT_MODE requires Agent runtimes")
    return Settings(
        log_level=_get_log_level(env),
        http_host=_get_http_host(env),
        http_port=http_port,
        http_path=_get_http_path(env),
        http_allowed_hosts=_get_http_allowed_hosts(env, http_port),
        http_allowed_origins=_get_http_allowed_origins(env),
        http_api_key=http_api_key,
        oauth_enabled=oauth_enabled,
        oauth_resource_url=oauth_resource_url,
        oauth_issuer_url=oauth_issuer_url,
        oauth_required_scopes=oauth_required_scopes,
        oauth_introspection_url=oauth_introspection_url,
        oauth_introspection_secret=oauth_introspection_secret,
        oauth_introspection_timeout_seconds=_get_positive_float_strict(
            env, "SERVERFS_OAUTH_INTROSPECTION_TIMEOUT_SECONDS", 2.0
        ),
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
        file_ingress_socket=env.get("SERVERFS_FILE_INGRESS_SOCKET", "").strip(),
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
