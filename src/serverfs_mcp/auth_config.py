"""Configuration for the optional ServerFS OAuth authorization service.

This module is deliberately separate from ``serverfs_mcp.config`` so upstream
provider credentials never need to enter the MCP Resource Server process.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePath
from urllib.parse import urlsplit

from .shared_proxy import SharedProxyConfig, parse_shared_proxy

_TRUE_VALUES = frozenset({"true", "1", "yes", "on"})
_FALSE_VALUES = frozenset({"false", "0", "no", "off"})
_SUPPORTED_PROVIDERS = frozenset({"github", "dingtalk", "feishu", "wechat"})


@dataclass(frozen=True)
class AuthServiceSettings:
    """Runtime settings owned only by the optional ``serverfs-auth`` process."""

    oauth_enabled: bool = False
    host: str = "0.0.0.0"
    port: int = 8082
    public_url: str = ""
    state_dir: str = "/var/lib/serverfs-auth"
    allowed_principals: tuple[str, ...] = ()
    access_token_ttl_seconds: int = 3600
    refresh_token_ttl_seconds: int = 2_592_000
    dcr_enabled: bool = False
    use_proxy: bool = False
    proxy: SharedProxyConfig | None = None
    introspection_secret: str = ""
    github_enabled: bool = False
    github_client_id: str = ""
    github_client_secret: str = ""
    dingtalk_enabled: bool = False
    dingtalk_client_id: str = ""
    dingtalk_client_secret: str = ""
    feishu_enabled: bool = False
    feishu_client_id: str = ""
    feishu_client_secret: str = ""
    wechat_enabled: bool = False
    wechat_client_id: str = ""
    wechat_client_secret: str = ""

    @property
    def enabled_providers(self) -> tuple[str, ...]:
        values = []
        for provider in ("github", "dingtalk", "feishu", "wechat"):
            if getattr(self, f"{provider}_enabled"):
                values.append(provider)
        return tuple(values)


def _get_strict_bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = env.get(key, "").strip().lower()
    if not raw:
        return default
    if raw in _TRUE_VALUES:
        return True
    if raw in _FALSE_VALUES:
        return False
    raise ValueError(f"invalid {key} value {env.get(key, '')!r}")


def _get_positive_int(env: Mapping[str, str], key: str, default: int) -> int:
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


def _get_port(env: Mapping[str, str]) -> int:
    value = _get_positive_int(env, "SERVERFS_AUTH_PORT", 8082)
    if value > 65535:
        raise ValueError(f"invalid SERVERFS_AUTH_PORT value {env.get('SERVERFS_AUTH_PORT', '')!r}")
    return value


def _get_host(env: Mapping[str, str]) -> str:
    value = env.get("SERVERFS_AUTH_HOST", "").strip() or "0.0.0.0"
    if any(char.isspace() for char in value) or "://" in value or "/" in value:
        raise ValueError(f"invalid SERVERFS_AUTH_HOST value {env.get('SERVERFS_AUTH_HOST', '')!r}")
    return value


def _is_loopback_host(hostname: str | None) -> bool:
    return hostname in {"localhost", "127.0.0.1", "::1"}


def _get_public_url(env: Mapping[str, str], *, required: bool) -> str:
    key = "SERVERFS_AUTH_PUBLIC_URL"
    raw = env.get(key, "").strip()
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
    if parsed.scheme != "https" and not _is_loopback_host(parsed.hostname):
        raise ValueError(f"{key} must use https outside loopback")
    return raw.rstrip("/")


def _get_state_dir(env: Mapping[str, str]) -> str:
    value = env.get("SERVERFS_AUTH_STATE_DIR", "").strip() or "/var/lib/serverfs-auth"
    if not PurePath(value).is_absolute():
        raise ValueError("SERVERFS_AUTH_STATE_DIR must be absolute")
    return value


def _get_allowed_principals(env: Mapping[str, str]) -> tuple[str, ...]:
    raw = env.get("SERVERFS_AUTH_ALLOWED_PRINCIPALS", "")
    values = tuple(item.strip() for item in raw.split(",") if item.strip())
    if len(values) != len(set(values)):
        raise ValueError("SERVERFS_AUTH_ALLOWED_PRINCIPALS contains duplicates")
    for item in values:
        if "*" in item or any(char.isspace() for char in item):
            raise ValueError(f"invalid SERVERFS_AUTH_ALLOWED_PRINCIPALS entry {item!r}")
        provider, separator, subject = item.partition(":")
        if not separator or provider not in _SUPPORTED_PROVIDERS or not subject:
            raise ValueError(f"invalid SERVERFS_AUTH_ALLOWED_PRINCIPALS entry {item!r}")
    return values


def _provider_values(
    env: Mapping[str, str], provider: str, *, require_credentials: bool
) -> tuple[bool, str, str]:
    prefix = f"SERVERFS_OAUTH_{provider.upper()}"
    enabled = _get_strict_bool(env, f"{prefix}_ENABLED", False)
    client_id = env.get(f"{prefix}_CLIENT_ID", "").strip()
    client_secret = env.get(f"{prefix}_CLIENT_SECRET", "").strip()
    if require_credentials and enabled and (not client_id or not client_secret):
        raise ValueError(f"{prefix}_ENABLED=true requires CLIENT_ID and CLIENT_SECRET")
    return enabled, client_id, client_secret


def auth_settings_from_env(env: Mapping[str, str]) -> AuthServiceSettings:
    """Parse settings for ``serverfs-auth`` without exposing secrets to MCP config."""

    oauth_enabled = _get_strict_bool(env, "SERVERFS_OAUTH_ENABLED", False)
    github_enabled, github_client_id, github_client_secret = _provider_values(
        env, "github", require_credentials=oauth_enabled
    )
    dingtalk_enabled, dingtalk_client_id, dingtalk_client_secret = _provider_values(
        env, "dingtalk", require_credentials=oauth_enabled
    )
    feishu_enabled, feishu_client_id, feishu_client_secret = _provider_values(
        env, "feishu", require_credentials=oauth_enabled
    )
    wechat_enabled, wechat_client_id, wechat_client_secret = _provider_values(
        env, "wechat", require_credentials=oauth_enabled
    )

    enabled_providers = tuple(
        provider
        for provider, enabled in (
            ("github", github_enabled),
            ("dingtalk", dingtalk_enabled),
            ("feishu", feishu_enabled),
            ("wechat", wechat_enabled),
        )
        if enabled
    )
    if oauth_enabled and not enabled_providers:
        raise ValueError("SERVERFS_OAUTH_ENABLED=true requires at least one enabled OAuth provider")

    introspection_secret = env.get("SERVERFS_OAUTH_INTROSPECTION_SECRET", "").strip()
    if oauth_enabled and not introspection_secret:
        raise ValueError(
            "SERVERFS_OAUTH_INTROSPECTION_SECRET is required when SERVERFS_OAUTH_ENABLED=true"
        )

    use_proxy = _get_strict_bool(env, "SERVERFS_AUTH_USE_PROXY", False)
    proxy = parse_shared_proxy(env) if use_proxy else None
    if use_proxy and proxy is None:
        raise ValueError("SERVERFS_AUTH_USE_PROXY=true requires SERVERFS_PROXY_HOST/PORT")

    return AuthServiceSettings(
        oauth_enabled=oauth_enabled,
        host=_get_host(env),
        port=_get_port(env),
        public_url=_get_public_url(env, required=oauth_enabled),
        state_dir=_get_state_dir(env),
        allowed_principals=_get_allowed_principals(env),
        access_token_ttl_seconds=_get_positive_int(
            env, "SERVERFS_AUTH_ACCESS_TOKEN_TTL_SECONDS", 3600
        ),
        refresh_token_ttl_seconds=_get_positive_int(
            env, "SERVERFS_AUTH_REFRESH_TOKEN_TTL_SECONDS", 2_592_000
        ),
        dcr_enabled=_get_strict_bool(env, "SERVERFS_AUTH_DCR_ENABLED", False),
        use_proxy=use_proxy,
        proxy=proxy if use_proxy else None,
        introspection_secret=introspection_secret,
        github_enabled=github_enabled,
        github_client_id=github_client_id,
        github_client_secret=github_client_secret,
        dingtalk_enabled=dingtalk_enabled,
        dingtalk_client_id=dingtalk_client_id,
        dingtalk_client_secret=dingtalk_client_secret,
        feishu_enabled=feishu_enabled,
        feishu_client_id=feishu_client_id,
        feishu_client_secret=feishu_client_secret,
        wechat_enabled=wechat_enabled,
        wechat_client_id=wechat_client_id,
        wechat_client_secret=wechat_client_secret,
    )
