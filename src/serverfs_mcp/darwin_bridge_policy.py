"""macOS native Agent Bridge policy reconciliation.

``serverfs.toml`` is the single operator-facing source for native workdir/runtime/lifecycle policy.
The launchd Bridge also owns a private JSON document because it must persist secrets and native
runtime material outside every exposed workdir.  This module keeps those ownership domains aligned
without reading the private document in the MCP process: a Bridge-owned helper compares or rewrites
only the derived non-secret policy and returns a redacted result.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from .agent_lifecycle import render_request_from_config
from .agent_proxy import AgentProxyError, bridge_environment, parse_agent_proxy
from .native_tunnel import parse_env_value
from .shared_proxy import parse_shared_proxy

_SYNC_TIMEOUT_SECONDS = 30.0
_PRIVATE_ENV_KEYS = frozenset(
    {
        "SERVERFS_AGENT_PROXY_URL",
        "SERVERFS_AGENT_NO_PROXY",
        "SERVERFS_JEV_API_KEY",
        "SERVERFS_JEV_USE_PROXY",
        "SERVERFS_PROXY_HOST",
        "SERVERFS_PROXY_PORT",
        "SERVERFS_PROXY_USERNAME",
        "SERVERFS_PROXY_PASSWORD",
    }
)


class DarwinBridgePolicyError(Exception):
    """The private Bridge policy could not be checked or synchronized."""


def bridge_config_path() -> Path:
    """Return launchd's private config path, or the v0.13 default before install."""
    from .darwin_lifecycle import installed_bridge_config_path
    from .native_endpoint import bridge_home

    installed = installed_bridge_config_path()
    if installed is not None:
        return installed
    return bridge_home() / "bridge.json"


def _bridge_python() -> Path:
    override = os.environ.get("SERVERFS_BRIDGE_PYTHON", "").strip()
    return Path(override) if override else Path(sys.executable)


def _run_helper(payload: dict, *extra_args: str) -> bool:
    candidate = _bridge_python()
    if not candidate.is_file():
        raise DarwinBridgePolicyError("the configured Agent Bridge interpreter is unavailable")
    config_path = bridge_config_path()
    command = [
        str(candidate),
        "-m",
        "serverfs_agent_bridge.sync_config",
        "--config",
        str(config_path),
        *extra_args,
    ]
    try:
        completed = subprocess.run(  # noqa: S603
            command,
            input=json.dumps(payload, separators=(",", ":")),
            capture_output=True,
            text=True,
            timeout=_SYNC_TIMEOUT_SECONDS,
            env=bridge_environment(),
            cwd=str(Path.cwd()),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DarwinBridgePolicyError(
            f"the Agent Bridge policy helper could not run ({type(exc).__name__})"
        ) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip().splitlines()
        reason = detail[0] if detail else f"exit {completed.returncode}"
        raise DarwinBridgePolicyError(f"the Agent Bridge policy helper refused: {reason}")
    try:
        result = json.loads(completed.stdout)
    except (TypeError, ValueError) as exc:
        raise DarwinBridgePolicyError(
            "the Agent Bridge policy helper returned invalid output"
        ) from exc
    if not isinstance(result, dict) or type(result.get("synced")) is not bool:
        raise DarwinBridgePolicyError("the Agent Bridge policy helper returned invalid output")
    return bool(result["synced"])


def _invoke(workdirs, settings, *, check_only: bool) -> bool:
    request = render_request_from_config(workdirs, settings)
    return _run_helper(request, *(("--check",) if check_only else ()))


def policy_is_synced(workdirs, settings) -> bool:
    """Read-only comparison of TOML-owned policy against the launchd Bridge config."""
    return _invoke(workdirs, settings, check_only=True)


def sync_policy(workdirs, settings) -> None:
    """Atomically synchronize only TOML-derived policy, preserving private proxy/Jev material."""
    if not _invoke(workdirs, settings, check_only=False):  # pragma: no cover - helper promises True
        raise DarwinBridgePolicyError(
            "the Agent Bridge policy helper did not confirm synchronization"
        )


def _read_private_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise DarwinBridgePolicyError("the Agent Bridge env file does not exist")
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as exc:
        raise DarwinBridgePolicyError("the Agent Bridge env file cannot be read") from exc
    for line_number, line in enumerate(lines, 1):
        candidate = line.strip()
        if not candidate or candidate.startswith("#"):
            continue
        key, sep, raw = candidate.partition("=")
        key = key.strip()
        if key not in _PRIVATE_ENV_KEYS:
            continue
        if key in values:
            raise DarwinBridgePolicyError(f"duplicate {key} at line {line_number}")
        if not sep:
            raise DarwinBridgePolicyError(f"invalid {key} assignment at line {line_number}")
        try:
            values[key] = parse_env_value(raw.strip(), key, line_number)
        except ValueError as exc:
            raise DarwinBridgePolicyError(str(exc)) from exc
    return values


def _env_bool(values: Mapping[str, str], name: str, *, default: bool = False) -> bool:
    raw = values.get(name, "").strip().lower()
    if not raw:
        return default
    if raw == "true":
        return True
    if raw == "false":
        return False
    raise DarwinBridgePolicyError(f"{name} must be true or false")


def _private_overlay(settings, values: Mapping[str, str]) -> dict:
    agent = settings.agent
    if agent is None or not agent.enabled:
        raise DarwinBridgePolicyError("the native configuration does not enable Agent delegation")

    runtime_needs_proxy = any(
        agent.runtime_enabled(name) and agent.runtime_use_proxy(name)
        for name in agent.enabled_runtimes
    )
    agent_proxy = None
    if runtime_needs_proxy:
        if not agent.proxy.enabled:
            raise DarwinBridgePolicyError(
                "an enabled Agent runtime has use_proxy=true but [agent.proxy] is disabled"
            )
        try:
            agent_proxy = parse_agent_proxy(agent.proxy, values)
        except AgentProxyError as exc:
            raise DarwinBridgePolicyError(str(exc)) from exc
        if agent_proxy is None:  # defensive: enabled policy above must produce a proxy
            raise DarwinBridgePolicyError("the Agent runtime proxy is not configured")

    jev_key = values.get("SERVERFS_JEV_API_KEY", "").strip()
    jev_use_proxy = _env_bool(values, "SERVERFS_JEV_USE_PROXY")
    if jev_use_proxy and not jev_key:
        raise DarwinBridgePolicyError("SERVERFS_JEV_USE_PROXY=true requires SERVERFS_JEV_API_KEY")
    shared_proxy = None
    if jev_use_proxy:
        shared_proxy = parse_shared_proxy(values, error_type=DarwinBridgePolicyError)
        if shared_proxy is None:
            raise DarwinBridgePolicyError(
                "SERVERFS_JEV_USE_PROXY=true requires SERVERFS_PROXY_HOST/PORT"
            )

    proxy_document = None
    if agent_proxy is not None:
        proxy_document = {"url": agent_proxy.url, "authenticated": False}
    if shared_proxy is not None:
        shared_document = {
            "url": shared_proxy.url,
            "authenticated": shared_proxy.authenticated,
        }
        if proxy_document is not None and proxy_document != shared_document:
            raise DarwinBridgePolicyError(
                "Agent and Jev proxy settings resolve to different endpoints; the native Bridge "
                "requires one shared private proxy endpoint"
            )
        proxy_document = shared_document

    private: dict[str, object] = {"jev": {"api_key": jev_key or None, "use_proxy": jev_use_proxy}}
    if proxy_document is not None:
        private["proxy"] = proxy_document
    return private


def configure_policy(workdirs, settings, *, env_file: Path | None = None) -> None:
    """Create/update the private Bridge config from TOML policy plus private environment values."""
    values: Mapping[str, str] = (
        _read_private_env_file(env_file) if env_file is not None else os.environ
    )
    payload = {
        "policy": render_request_from_config(workdirs, settings),
        "private": _private_overlay(settings, values),
    }
    if not _run_helper(payload, "--configure"):  # pragma: no cover - helper promises True
        raise DarwinBridgePolicyError(
            "the Agent Bridge configure helper did not confirm publication"
        )
