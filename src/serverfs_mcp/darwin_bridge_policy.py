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
import plistlib
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
    """Return the persistent Bridge config path across legacy and v0.13.2 service layouts."""
    from .native_endpoint import bridge_home

    # v0.13.2: the bundle-owned launcher records the active absolute Bridge config path in the
    # private service manifest.  Reading it preserves non-default deployments after the legacy
    # ~/Library/LaunchAgents plist has been removed.
    try:
        from .darwin_service import manifest_path

        service_manifest = manifest_path()
        if service_manifest.is_file():
            document = json.loads(service_manifest.read_text(encoding="utf-8"))
            bridge = document.get("services", {}).get("agent-bridge", {})
            argv = bridge.get("arguments")
            if (
                isinstance(argv, list)
                and len(argv) >= 2
                and argv[0] == "--config"
                and isinstance(argv[1], str)
            ):
                candidate = Path(argv[1]).expanduser()
                if candidate.is_absolute():
                    return candidate
    except (OSError, TypeError, ValueError):
        pass

    # v0.13.0/v0.13.1 migration source.
    from .darwin_lifecycle import installed_bridge_config_path

    installed = installed_bridge_config_path()
    if installed is not None:
        return installed
    return bridge_home() / "bridge.json"


def _bridge_python_from_env_file(path: Path) -> Path | None:
    """Read only the deployment-time Bridge interpreter selector from a dotenv file."""
    if not path.is_file():
        raise DarwinBridgePolicyError("the Agent Bridge env file does not exist")
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as exc:
        raise DarwinBridgePolicyError("the Agent Bridge env file cannot be read") from exc
    selected: Path | None = None
    seen = False
    for line_number, line in enumerate(lines, 1):
        candidate = line.strip()
        if not candidate or candidate.startswith("#"):
            continue
        if candidate.startswith("export "):
            candidate = candidate[7:].lstrip()
        key, sep, raw = candidate.partition("=")
        if key.strip() != "SERVERFS_BRIDGE_PYTHON":
            continue
        if seen:
            raise DarwinBridgePolicyError(
                f"duplicate SERVERFS_BRIDGE_PYTHON at line {line_number}"
            )
        seen = True
        if not sep:
            raise DarwinBridgePolicyError(
                f"invalid SERVERFS_BRIDGE_PYTHON assignment at line {line_number}"
            )
        try:
            value = parse_env_value(raw.strip(), "SERVERFS_BRIDGE_PYTHON", line_number).strip()
        except ValueError as exc:
            raise DarwinBridgePolicyError(str(exc)) from exc
        if value:
            selected = Path(value).expanduser()
    return selected


def _python_from_console_script(executable: Path) -> Path | None:
    """Recover the absolute interpreter from an installed console-script shebang."""
    if not executable.is_absolute() or not executable.is_file():
        return None
    try:
        first_line = executable.open("r", encoding="utf-8").readline(4096).strip()
    except (OSError, UnicodeError):
        return None
    if not first_line.startswith("#!"):
        return None
    value = first_line[2:].strip()
    if not value or any(character.isspace() for character in value):
        return None
    candidate = Path(value).expanduser()
    return candidate if candidate.is_absolute() and candidate.is_file() else None


def _installed_bridge_python() -> Path | None:
    """Recover the Bridge interpreter from v0.13.2 manifest or v0.13.1 legacy plist."""
    try:
        from .darwin_service import manifest_path

        path = manifest_path()
        if path.is_file():
            document = json.loads(path.read_text(encoding="utf-8"))
            bridge = document.get("services", {}).get("agent-bridge", {})
            executable = bridge.get("executable")
            if isinstance(executable, str):
                candidate = _python_from_console_script(Path(executable).expanduser())
                if candidate is not None:
                    return candidate
    except (OSError, TypeError, ValueError):
        pass

    try:
        from .darwin_lifecycle import launch_agent_plist_path

        payload = plistlib.loads(launch_agent_plist_path().read_bytes())
        argv = payload.get("ProgramArguments") if isinstance(payload, dict) else None
        if isinstance(argv, list) and argv and isinstance(argv[0], str):
            return _python_from_console_script(Path(argv[0]).expanduser())
    except (OSError, TypeError, ValueError):
        pass
    return None


def _bridge_python(*, env_file: Path | None = None) -> Path:
    candidate = _bridge_python_from_env_file(env_file) if env_file is not None else None
    if candidate is None:
        candidate = _installed_bridge_python()
    if candidate is None:
        override = os.environ.get("SERVERFS_BRIDGE_PYTHON", "").strip()
        candidate = Path(override).expanduser() if override else Path(sys.executable)
    if not candidate.is_absolute() or not candidate.is_file():
        raise DarwinBridgePolicyError(
            "the configured Agent Bridge interpreter must be an available absolute path"
        )
    return candidate


def _run_helper(payload: dict, *extra_args: str, env_file: Path | None = None) -> bool:
    candidate = _bridge_python(env_file=env_file)
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
    if not _run_helper(payload, "--configure", env_file=env_file):  # pragma: no cover - helper promises True
        raise DarwinBridgePolicyError(
            "the Agent Bridge configure helper did not confirm publication"
        )
