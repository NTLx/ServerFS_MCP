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
import json
import math
import os
import re
import shlex
import tempfile
from pathlib import Path
from typing import Any

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ALIAS_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}
_AGENT_MODES = {"disabled", "review", "workspace-write"}
_RUNTIMES = {"codex", "claude"}


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

    workdirs: list[dict[str, Any]] = []
    enabled_runtimes: set[str] = set()
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
        runtimes = [item.strip().lower() for item in raw_runtimes.split(",") if item.strip()]

        if not alias and not host_path_raw:
            if not read_only:
                raise ConfigRenderError(f"{prefix}: disabled slot cannot set READ_ONLY=false")
            if mode not in {"", "disabled"} or runtimes:
                raise ConfigRenderError(
                    f"{prefix}: disabled slot cannot configure Agent delegation"
                )
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
        unknown = set(runtimes) - _RUNTIMES
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
            raise ConfigRenderError(f"{prefix}: native Codex/Claude require workspace-write")

        host_path = Path(host_path_raw)
        if not host_path.is_absolute():
            raise ConfigRenderError(f"{prefix}_PATH must be absolute")
        try:
            host_path = host_path.resolve(strict=True)
        except OSError as exc:
            raise ConfigRenderError(f"{prefix}_PATH does not exist") from exc
        if not host_path.is_dir():
            raise ConfigRenderError(f"{prefix}_PATH must be a directory")

        workdirs.append(
            {
                "slot": slot,
                "alias": alias,
                "host_path": str(host_path),
                "read_only": read_only,
                "agent_mode": mode or "disabled",
                "agent_runtimes": runtimes,
            }
        )
        enabled_runtimes.update(runtimes)

    if not enabled_runtimes:
        raise ConfigRenderError("at least one workdir must explicitly enable codex or claude")

    codex_enabled = "codex" in enabled_runtimes
    claude_enabled = "claude" in enabled_runtimes
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
    jev_api_key = _optional_api_key(
        values.get("SERVERFS_JEV_API_KEY", ""),
        "SERVERFS_JEV_API_KEY",
    )

    config = {
        "socket_path": socket_path,
        "state_dir": state_dir,
        "lock_dir": lock_dir,
        "allowed_peer_uid": peer_uid,
        "allowed_peer_gid": peer_gid,
        "enable_fake_runtime": False,
        "codex": {
            "enabled": codex_enabled,
            "autostart": _bool(
                values.get("SERVERFS_CODEX_AUTOSTART", ""),
                "SERVERFS_CODEX_AUTOSTART",
                False,
            ),
            "codex_home": values.get("SERVERFS_CODEX_HOME", "~/.codex").strip() or "~/.codex",
            "codex_bin": codex_bin,
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
            "probe_timeout_seconds": _positive_float(
                values.get("SERVERFS_CLAUDE_PROBE_TIMEOUT_SECONDS", ""),
                "SERVERFS_CLAUDE_PROBE_TIMEOUT_SECONDS",
                5.0,
            ),
            "event_idle_timeout_seconds": None,
        },
        "workdirs": workdirs,
    }
    if jev_api_key is not None:
        config["jev"] = {"api_key": jev_api_key}
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
