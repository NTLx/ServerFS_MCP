#!/usr/bin/env python3
"""Render a production Agent Bridge JSON config from the ServerFS .env.

This script intentionally implements a small, strict subset of Docker Compose
.env syntax. It does not execute shell syntax, expand variables or source the
file. The normal ServerFS .env is the single source of truth for workdirs,
Agent policy, deployment identity/paths and provider executable locations.
Provider secrets and shell-only environment normally stay outside the repository in the
user-owned provider.env loaded by the systemd user service. The opt-in experimental Jev
advisor suite is the explicit exception: SERVERFS_JEV_API_KEY is read from the untracked repository
.env and rendered only into the private 0600 Bridge config.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import re
import shlex
import sys
import tempfile
from pathlib import Path
from typing import Any

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ALIAS_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}
_AGENT_MODES = {"disabled", "review", "workspace-write"}
_RUNTIMES = {"codex", "claude", "qoder"}
_ACP_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,27}$")
_ACP_FIELDS = {
    "id",
    "command",
    "args",
    "use_proxy",
    "probe_timeout_seconds",
    "shutdown_timeout_seconds",
}
_ACP_MAX_JSON_BYTES = 65_536
_REPO_ACTION_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_REPO_ACTION_FIELDS = {"argv", "mutates", "argument", "integrity"}
_REPO_ACTION_MAX_JSON_BYTES = 65_536
_MAX_SPOOLED_RESULT_BYTES = 8 * 1024 * 1024

_SHARED_PROXY_PATH = (
    Path(__file__).resolve().parents[2] / "src" / "serverfs_mcp" / "shared_proxy.py"
)
_SHARED_PROXY_SPEC = importlib.util.spec_from_file_location(
    "_serverfs_shared_proxy_config",
    _SHARED_PROXY_PATH,
)
assert _SHARED_PROXY_SPEC is not None and _SHARED_PROXY_SPEC.loader is not None
_shared_proxy = importlib.util.module_from_spec(_SHARED_PROXY_SPEC)
sys.modules.setdefault("_serverfs_shared_proxy_config", _shared_proxy)
_SHARED_PROXY_SPEC.loader.exec_module(_shared_proxy)


class ConfigRenderError(ValueError):
    pass


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            raise ConfigRenderError(f"{path}:{lineno}: 'export' syntax is not supported")
        if "=" not in raw:
            raise ConfigRenderError(f"{path}:{lineno}: expected KEY=VALUE")
        key, value = raw.split("=", 1)
        key = key.strip()
        if not _KEY_RE.fullmatch(key):
            raise ConfigRenderError(f"{path}:{lineno}: invalid environment key")
        if key in values:
            raise ConfigRenderError(f"{path}:{lineno}: duplicate key {key}")
        values[key] = _parse_env_value(value.strip(), path, lineno)
    return values


def _parse_env_value(value: str, path: Path, lineno: int) -> str:
    if not value:
        return ""
    if value[0] in {"'", '"'}:
        try:
            parts = shlex.split(value, comments=True, posix=True)
        except ValueError as exc:
            raise ConfigRenderError(f"{path}:{lineno}: invalid quoted value") from exc
        if len(parts) != 1:
            raise ConfigRenderError(f"{path}:{lineno}: quoted value must resolve to one token")
        return parts[0]

    # Compose allows inline comments after whitespace. Preserve '#' when it is
    # part of an unquoted value with no separating whitespace.
    match = re.search(r"\s+#", value)
    if match:
        value = value[: match.start()].rstrip()
    return value


def _required_int(values: dict[str, str], key: str) -> int:
    raw = values.get(key, "").strip()
    if not raw:
        raise ConfigRenderError(f"{key} must be set from measured host credentials")
    try:
        value = int(raw, 10)
    except ValueError as exc:
        raise ConfigRenderError(f"{key} must be an integer") from exc
    if value < 0:
        raise ConfigRenderError(f"{key} must be non-negative")
    return value


def _bool(raw: str, key: str, default: bool) -> bool:
    value = raw.strip().lower()
    if not value:
        return default
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigRenderError(f"{key} must be true or false")


def _positive_float(raw: str, key: str, default: float) -> float:
    value = raw.strip()
    if not value:
        return default
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ConfigRenderError(f"{key} must be numeric") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ConfigRenderError(f"{key} must be positive and finite")
    return parsed


def _positive_int(raw: str, key: str, default: int) -> int:
    value = raw.strip()
    if not value:
        return default
    try:
        parsed = int(value, 10)
    except ValueError as exc:
        raise ConfigRenderError(f"{key} must be an integer") from exc
    if parsed <= 0:
        raise ConfigRenderError(f"{key} must be a positive integer")
    return parsed


def _absolute_path(raw: str, key: str) -> str:
    value = raw.strip()
    if not value:
        raise ConfigRenderError(f"{key} must be set to an absolute path")
    path = Path(value)
    if not path.is_absolute():
        raise ConfigRenderError(f"{key} must be an absolute path")
    return str(path)


def _optional_api_key(raw: str, key: str) -> str | None:
    value = raw.strip()
    if not value:
        return None
    if not value.isascii() or any(char.isspace() or ord(char) < 32 for char in value):
        raise ConfigRenderError(f"{key} has an invalid format")
    return value


def _provider_binary(
    values: dict[str, str],
    key: str,
    *,
    enabled: bool,
) -> str:
    raw = values.get(key, "").strip()
    if not enabled:
        return raw or key.removeprefix("SERVERFS_").removesuffix("_BIN").lower()
    path = Path(raw)
    if not raw or not path.is_absolute():
        raise ConfigRenderError(
            f"{key} must be an absolute executable path when the runtime is enabled"
        )
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ConfigRenderError(f"{key} is not an executable file")
    return str(path)


def _acp_timeout(profile: dict[str, Any], key: str, default: float) -> float:
    value = profile.get(key, default)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0 < value <= 60
        or not math.isfinite(value)
    ):
        raise ConfigRenderError(f"ACP {key} must be a finite number greater than 0 and at most 60")
    return float(value)


def _acp_profiles(raw: str) -> list[dict[str, Any]]:
    if not raw.strip():
        return []
    if len(raw.encode("utf-8")) > _ACP_MAX_JSON_BYTES:
        raise ConfigRenderError("SERVERFS_ACP_PROFILES_JSON exceeds size limit")
    try:

        def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ConfigRenderError(
                        "SERVERFS_ACP_PROFILES_JSON contains duplicate object keys"
                    )
                result[key] = value
            return result

        profiles = json.loads(raw, object_pairs_hook=reject_duplicate_keys)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ConfigRenderError("SERVERFS_ACP_PROFILES_JSON must be valid JSON") from exc
    if not isinstance(profiles, list) or len(profiles) > 16:
        raise ConfigRenderError(
            "SERVERFS_ACP_PROFILES_JSON must be an array of at most 16 profiles"
        )
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, profile in enumerate(profiles):
        label = f"SERVERFS_ACP_PROFILES_JSON[{index}]"
        if not isinstance(profile, dict) or set(profile) - _ACP_FIELDS:
            raise ConfigRenderError(f"{label} has invalid fields")
        identifier = profile.get("id")
        if not isinstance(identifier, str) or not _ACP_ID_RE.fullmatch(identifier):
            raise ConfigRenderError(f"{label}.id is invalid")
        if identifier in seen or identifier in _RUNTIMES:
            raise ConfigRenderError(f"{label}.id duplicates or collides with a reserved runtime")
        seen.add(identifier)
        command = profile.get("command")
        if not isinstance(command, str) or not command.startswith("/") or "\x00" in command:
            raise ConfigRenderError(f"{label}.command must be an absolute POSIX path")
        path = Path(command)
        if not path.is_file() or not os.access(path, os.X_OK):
            raise ConfigRenderError(f"{label}.command must exist as a regular executable file")
        args = profile.get("args", [])
        if (
            not isinstance(args, list)
            or len(args) > 32
            or any(not isinstance(arg, str) or len(arg) > 4096 or "\x00" in arg for arg in args)
        ):
            raise ConfigRenderError(f"{label}.args must be at most 32 bounded strings")
        use_proxy = profile.get("use_proxy", False)
        if not isinstance(use_proxy, bool):
            raise ConfigRenderError(f"{label}.use_proxy must be boolean")
        result.append(
            {
                "id": identifier,
                "command": str(path),
                "args": args,
                "use_proxy": use_proxy,
                "probe_timeout_seconds": _acp_timeout(profile, "probe_timeout_seconds", 5.0),
                "shutdown_timeout_seconds": _acp_timeout(profile, "shutdown_timeout_seconds", 5.0),
            }
        )
    return result


def _repo_actions(raw: str, *, key: str, enabled: bool, read_only: bool) -> dict[str, Any]:
    if not raw.strip():
        return {}
    if len(raw.encode("utf-8")) > _REPO_ACTION_MAX_JSON_BYTES:
        raise ConfigRenderError(f"{key} exceeds size limit")
    try:

        def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for name, value in pairs:
                if name in result:
                    raise ConfigRenderError(f"{key} contains duplicate object keys")
                result[name] = value
            return result

        actions = json.loads(raw, object_pairs_hook=reject_duplicate_keys)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ConfigRenderError(f"{key} must be valid JSON") from exc
    if not isinstance(actions, dict) or len(actions) > 16:
        raise ConfigRenderError(f"{key} must be an object with at most 16 actions")
    if actions and not enabled:
        raise ConfigRenderError(f"{key} requires REPO_OPS_ENABLED=true")
    normalized: dict[str, Any] = {}
    for name, action in actions.items():
        label = f"{key}.{name}"
        if not isinstance(name, str) or not _REPO_ACTION_RE.fullmatch(name):
            raise ConfigRenderError(f"{key} contains an invalid action name")
        if not isinstance(action, dict) or set(action) - _REPO_ACTION_FIELDS:
            raise ConfigRenderError(f"{label} has invalid fields")
        argv = action.get("argv")
        if (
            not isinstance(argv, list)
            or not argv
            or len(argv) > 32
            or any(
                not isinstance(item, str)
                or not item
                or len(item.encode("utf-8")) > 4096
                or any(char in item for char in ("\x00", "\n", "\r"))
                for item in argv
            )
        ):
            raise ConfigRenderError(f"{label}.argv must be 1-32 bounded strings")
        mutates = action.get("mutates", False)
        if not isinstance(mutates, bool):
            raise ConfigRenderError(f"{label}.mutates must be boolean")
        if read_only:
            raise ConfigRenderError(f"{label} cannot be configured on a read-only workdir")
        argument = action.get("argument", "none")
        if argument not in {"none", "path", "basename"}:
            raise ConfigRenderError(f"{label}.argument must be none, path or basename")
        integrity = action.get("integrity")
        if not isinstance(integrity, dict) or not integrity or len(integrity) > 32:
            raise ConfigRenderError(
                f"{label}.integrity must be a non-empty object of at most 32 files"
            )
        normalized_integrity: dict[str, str] = {}
        for path, digest in integrity.items():
            if (
                not isinstance(path, str)
                or not path
                or path.startswith(("/", "-"))
                or "\\" in path
                or "\x00" in path
                or any(part in {"", ".", ".."} for part in path.split("/"))
            ):
                raise ConfigRenderError(f"{label}.integrity contains an invalid relative path")
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)
            ):
                raise ConfigRenderError(f"{label}.integrity digest must be lowercase sha256")
            normalized_integrity[path] = digest
        normalized[name] = {
            "argv": argv,
            "mutates": mutates,
            "argument": argument,
            "integrity": normalized_integrity,
        }
    return normalized


def build_config(values: dict[str, str]) -> dict[str, Any]:
    if not _bool(
        values.get("SERVERFS_AGENT_BRIDGE_ENABLED", ""),
        "SERVERFS_AGENT_BRIDGE_ENABLED",
        False,
    ):
        raise ConfigRenderError("SERVERFS_AGENT_BRIDGE_ENABLED must be true")

    peer_uid = _required_int(values, "SERVERFS_AGENT_PEER_UID")
    peer_gid = _required_int(values, "SERVERFS_AGENT_PEER_GID")
    try:
        container_uid = int(values.get("SERVERFS_UID", "-1"), 10)
        container_gid = int(values.get("SERVERFS_GID", "-1"), 10)
    except ValueError as exc:
        raise ConfigRenderError("SERVERFS_UID/SERVERFS_GID must be integers") from exc
    if container_uid != os.getuid() or peer_uid != os.getuid():
        raise ConfigRenderError(
            "Phase E user deployment requires SERVERFS_UID and "
            "SERVERFS_AGENT_PEER_UID to equal the current user id"
        )
    if container_gid != os.getgid() or peer_gid != os.getgid():
        raise ConfigRenderError(
            "Phase E user deployment requires SERVERFS_GID and "
            "SERVERFS_AGENT_PEER_GID to equal the current primary group id"
        )

    _positive_float(
        values.get("SERVERFS_AGENT_BRIDGE_TIMEOUT_SECONDS", ""),
        "SERVERFS_AGENT_BRIDGE_TIMEOUT_SECONDS",
        30.0,
    )

    socket_dir = Path(
        _absolute_path(
            values.get("SERVERFS_AGENT_BRIDGE_HOST_SOCKET_DIR", ""),
            "SERVERFS_AGENT_BRIDGE_HOST_SOCKET_DIR",
        )
    )
    lock_dir = _absolute_path(
        values.get("SERVERFS_AGENT_LOCK_HOST_DIR", ""),
        "SERVERFS_AGENT_LOCK_HOST_DIR",
    )
    state_dir = _absolute_path(
        values.get("SERVERFS_AGENT_BRIDGE_STATE_DIR", ""),
        "SERVERFS_AGENT_BRIDGE_STATE_DIR",
    )
    socket_path = str(socket_dir / "bridge.sock")

    acp_profiles = _acp_profiles(values.get("SERVERFS_ACP_PROFILES_JSON", ""))
    acp_ids = {f"acp-{profile['id']}" for profile in acp_profiles}
    workdirs: list[dict[str, Any]] = []
    enabled_runtimes: set[str] = set()
    repo_ops_any = False
    seen_aliases: set[str] = set()

    for slot in range(1, 17):
        prefix = f"WORKDIR_{slot:02d}"
        alias = values.get(f"{prefix}_ALIAS", "").strip()
        host_path_raw = values.get(f"{prefix}_PATH", "").strip()
        read_only = _bool(
            values.get(f"{prefix}_READ_ONLY", ""),
            f"{prefix}_READ_ONLY",
            True,
        )
        mode = values.get(f"{prefix}_AGENT_MODE", "disabled").strip().lower()
        raw_runtimes = values.get(f"{prefix}_AGENT_RUNTIMES", "")
        repo_ops_enabled = _bool(
            values.get(f"{prefix}_REPO_OPS_ENABLED", ""),
            f"{prefix}_REPO_OPS_ENABLED",
            False,
        )
        repo_actions_raw = values.get(f"{prefix}_REPO_ACTIONS_JSON", "")
        runtimes = []
        for item in raw_runtimes.split(","):
            runtime = item.strip()
            if not runtime:
                continue
            normalized_native = runtime.lower()
            runtimes.append(normalized_native if normalized_native in _RUNTIMES else runtime)

        if not alias and not host_path_raw:
            if not read_only:
                raise ConfigRenderError(f"{prefix}: disabled slot cannot set READ_ONLY=false")
            if mode not in {"", "disabled"} or runtimes:
                raise ConfigRenderError(
                    f"{prefix}: disabled slot cannot configure Agent delegation"
                )
            if repo_ops_enabled or repo_actions_raw.strip():
                raise ConfigRenderError(f"{prefix}: disabled slot cannot configure Repo Ops")
            continue
        if not alias or not host_path_raw:
            raise ConfigRenderError(f"{prefix}: ALIAS and PATH must be set together")
        if not _ALIAS_RE.fullmatch(alias):
            raise ConfigRenderError(f"{prefix}_ALIAS is invalid")
        if alias in seen_aliases:
            raise ConfigRenderError(f"{prefix}_ALIAS duplicates {alias!r}")
        seen_aliases.add(alias)
        if mode not in _AGENT_MODES:
            raise ConfigRenderError(f"{prefix}_AGENT_MODE is invalid")
        if len(runtimes) != len(set(runtimes)):
            raise ConfigRenderError(f"{prefix}_AGENT_RUNTIMES contains duplicates")
        unknown = set(runtimes) - _RUNTIMES - acp_ids
        if unknown:
            raise ConfigRenderError(
                f"{prefix}_AGENT_RUNTIMES contains unknown runtimes: " + ", ".join(sorted(unknown))
            )
        if mode == "disabled" and runtimes:
            raise ConfigRenderError(f"{prefix}: Agent runtimes require an enabled Agent mode")
        if mode != "disabled" and not runtimes:
            raise ConfigRenderError(f"{prefix}: enabled Agent mode requires Agent runtimes")
        if mode == "workspace-write" and read_only:
            raise ConfigRenderError(f"{prefix}: workspace-write requires READ_ONLY=false")
        if runtimes and mode != "workspace-write":
            raise ConfigRenderError(f"{prefix}: Agent runtimes require workspace-write")
        repo_actions = _repo_actions(
            repo_actions_raw,
            key=f"{prefix}_REPO_ACTIONS_JSON",
            enabled=repo_ops_enabled,
            read_only=read_only,
        )

        host_path = Path(host_path_raw)
        if not host_path.is_absolute():
            raise ConfigRenderError(f"{prefix}_PATH must be absolute")
        try:
            host_path = host_path.resolve(strict=True)
        except OSError as exc:
            raise ConfigRenderError(f"{prefix}_PATH does not exist") from exc
        if not host_path.is_dir():
            raise ConfigRenderError(f"{prefix}_PATH must be a directory")

        workdir_entry: dict[str, Any] = {
            "slot": slot,
            "alias": alias,
            "host_path": str(host_path),
            "read_only": read_only,
            "agent_mode": mode or "disabled",
            "agent_runtimes": runtimes,
        }
        if repo_ops_enabled:
            workdir_entry["repo_ops_enabled"] = True
            workdir_entry["repo_actions"] = repo_actions
        workdirs.append(workdir_entry)
        enabled_runtimes.update(runtimes)
        repo_ops_any = repo_ops_any or repo_ops_enabled

    if not enabled_runtimes and not repo_ops_any:
        raise ConfigRenderError(
            "at least one workdir must explicitly enable an Agent runtime or Repo Ops"
        )

    codex_enabled = "codex" in enabled_runtimes
    claude_enabled = "claude" in enabled_runtimes
    qoder_enabled = "qoder" in enabled_runtimes
    codex_bin = _provider_binary(
        values,
        "SERVERFS_CODEX_BIN",
        enabled=codex_enabled,
    )
    claude_bin = _provider_binary(
        values,
        "SERVERFS_CLAUDE_BIN",
        enabled=claude_enabled,
    )
    qoder_bin = _provider_binary(
        values,
        "SERVERFS_QODER_BIN",
        enabled=qoder_enabled,
    )
    jev_api_key = _optional_api_key(
        values.get("SERVERFS_JEV_API_KEY", ""),
        "SERVERFS_JEV_API_KEY",
    )
    agent_use_proxy = _bool(
        values.get("SERVERFS_AGENT_USE_PROXY", ""),
        "SERVERFS_AGENT_USE_PROXY",
        False,
    )
    jev_use_proxy = _bool(
        values.get("SERVERFS_JEV_USE_PROXY", ""),
        "SERVERFS_JEV_USE_PROXY",
        False,
    )
    shared_proxy = None
    acp_use_proxy = any(profile["use_proxy"] for profile in acp_profiles)
    if agent_use_proxy or jev_use_proxy or acp_use_proxy:
        shared_proxy = _shared_proxy.parse_shared_proxy(values, error_type=ConfigRenderError)
        if shared_proxy is None:
            raise ConfigRenderError(
                "shared proxy HOST/PORT are required when Agent or Jev proxy is enabled"
            )
        if (agent_use_proxy or acp_use_proxy) and shared_proxy.authenticated:
            raise ConfigRenderError(
                "Agent runtime proxy must be credentialless; use a credentialless local broker"
            )

    task_timeout_seconds = _positive_int(
        values.get("SERVERFS_AGENT_TASK_TIMEOUT_SECONDS", ""),
        "SERVERFS_AGENT_TASK_TIMEOUT_SECONDS",
        7200,
    )
    interaction_timeout_seconds = _positive_int(
        values.get("SERVERFS_AGENT_INTERACTION_TIMEOUT_SECONDS", ""),
        "SERVERFS_AGENT_INTERACTION_TIMEOUT_SECONDS",
        1800,
    )
    max_active_tasks = _positive_int(
        values.get("SERVERFS_AGENT_MAX_ACTIVE_TASKS", ""),
        "SERVERFS_AGENT_MAX_ACTIVE_TASKS",
        4,
    )
    retention_hours = _positive_int(
        values.get("SERVERFS_AGENT_TASK_RETENTION_HOURS", ""),
        "SERVERFS_AGENT_TASK_RETENTION_HOURS",
        168,
    )
    result_spool_threshold_bytes = _positive_int(
        values.get("SERVERFS_AGENT_RESULT_SPOOL_THRESHOLD_BYTES", ""),
        "SERVERFS_AGENT_RESULT_SPOOL_THRESHOLD_BYTES",
        262_144,
    )
    if result_spool_threshold_bytes > _MAX_SPOOLED_RESULT_BYTES:
        raise ConfigRenderError(
            "SERVERFS_AGENT_RESULT_SPOOL_THRESHOLD_BYTES must not exceed 8388608"
        )

    config = {
        "socket_path": socket_path,
        "state_dir": state_dir,
        "lock_dir": lock_dir,
        "allowed_peer_uid": peer_uid,
        "allowed_peer_gid": peer_gid,
        "enable_fake_runtime": False,
        "limits": {
            "task_timeout_seconds": task_timeout_seconds,
            "interaction_timeout_seconds": interaction_timeout_seconds,
            "max_active_tasks": max_active_tasks,
            "retention_seconds": retention_hours * 60 * 60,
            "result_spool_threshold_bytes": result_spool_threshold_bytes,
        },
        "codex": {
            "enabled": codex_enabled,
            "autostart": _bool(
                values.get("SERVERFS_CODEX_AUTOSTART", ""),
                "SERVERFS_CODEX_AUTOSTART",
                False,
            ),
            "codex_home": values.get("SERVERFS_CODEX_HOME", "~/.codex").strip() or "~/.codex",
            "codex_bin": codex_bin,
            "use_proxy": agent_use_proxy,
            "request_timeout_seconds": _positive_float(
                values.get("SERVERFS_CODEX_REQUEST_TIMEOUT_SECONDS", ""),
                "SERVERFS_CODEX_REQUEST_TIMEOUT_SECONDS",
                10.0,
            ),
            "event_idle_timeout_seconds": None,
            "max_message_bytes": 134_217_728,
        },
        "claude": {
            "enabled": claude_enabled,
            "claude_bin": claude_bin,
            "use_proxy": agent_use_proxy,
            "probe_timeout_seconds": _positive_float(
                values.get("SERVERFS_CLAUDE_PROBE_TIMEOUT_SECONDS", ""),
                "SERVERFS_CLAUDE_PROBE_TIMEOUT_SECONDS",
                5.0,
            ),
            "event_idle_timeout_seconds": None,
        },
        "qoder": {
            "enabled": qoder_enabled,
            "qoder_bin": qoder_bin,
            "use_proxy": agent_use_proxy,
            "probe_timeout_seconds": _positive_float(
                values.get("SERVERFS_QODER_PROBE_TIMEOUT_SECONDS", ""),
                "SERVERFS_QODER_PROBE_TIMEOUT_SECONDS",
                5.0,
            ),
            "event_idle_timeout_seconds": None,
        },
        "workdirs": workdirs,
    }
    if shared_proxy is not None:
        config["proxy"] = {
            "url": shared_proxy.url,
            "authenticated": shared_proxy.authenticated,
        }
    if acp_profiles:
        config["acp_profiles"] = acp_profiles
    if jev_api_key is not None or jev_use_proxy:
        config["jev"] = {"use_proxy": jev_use_proxy}
        if jev_api_key is not None:
            config["jev"]["api_key"] = jev_api_key
    return config


def write_atomic(path: Path, payload: dict[str, Any]) -> None:
    if not path.is_absolute():
        raise ConfigRenderError("output path must be absolute")
    parent = path.parent
    if not parent.is_dir():
        raise ConfigRenderError("output parent directory does not exist")
    if path.is_symlink():
        raise ConfigRenderError("refusing to replace a symlink output path")

    encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    fd, temp_name = tempfile.mkstemp(prefix=".serverfs-agent-config-", dir=parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    values = load_env_file(args.env_file)
    config = build_config(values)
    write_atomic(args.output, config)


if __name__ == "__main__":
    main()
