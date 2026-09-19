"""Configuration loader for the standalone Agent Bridge."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import KNOWN_RUNTIME_NAMES, AgentMode
from .policy import PolicyRegistry, WorkdirAgentPolicy

_CONFIG_KEYS = frozenset(
    {
        "socket_path",
        "state_dir",
        "lock_dir",
        "allowed_peer_uid",
        "allowed_peer_gid",
        "enable_fake_runtime",
        "workdirs",
    }
)
_WORKDIR_KEYS = frozenset(
    {"slot", "alias", "host_path", "read_only", "agent_mode", "agent_runtimes"}
)
_ALIAS_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
_MAX_WORKDIR_SLOTS = 16


@dataclass(frozen=True)
class BridgeConfig:
    socket_path: Path
    state_dir: Path
    lock_dir: Path
    allowed_peer_uid: int | None
    allowed_peer_gid: int | None
    enable_fake_runtime: bool
    policies: PolicyRegistry

    @classmethod
    def load(cls, path: Path) -> BridgeConfig:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("bridge config root must be an object")
        _reject_unknown_keys(data, _CONFIG_KEYS, "bridge config")

        policies: list[WorkdirAgentPolicy] = []
        workdirs = data.get("workdirs", [])
        if not isinstance(workdirs, list):
            raise ValueError("workdirs must be an array")
        for item in workdirs:
            if not isinstance(item, dict):
                raise ValueError("workdirs entries must be objects")
            _reject_unknown_keys(item, _WORKDIR_KEYS, "workdir")
            slot = _strict_int(item.get("slot"), "workdir.slot")
            if not 1 <= slot <= _MAX_WORKDIR_SLOTS:
                raise ValueError("workdir.slot must be between 1 and 16")
            alias = _strict_string(item.get("alias"), "workdir.alias")
            if _ALIAS_RE.fullmatch(alias) is None:
                raise ValueError("workdir.alias has an invalid format")
            host_path = Path(
                _strict_string(item.get("host_path"), "workdir.host_path")
            ).expanduser()
            if not host_path.is_absolute():
                raise ValueError("workdir.host_path must be absolute")
            try:
                host_path = host_path.resolve(strict=True)
            except OSError as exc:
                raise ValueError("workdir.host_path must exist") from exc
            if not host_path.is_dir():
                raise ValueError("workdir.host_path must be a directory")

            mode_value = item.get("agent_mode", AgentMode.DISABLED.value)
            mode = AgentMode(_strict_string(mode_value, "workdir.agent_mode"))
            runtimes_value = item.get("agent_runtimes", [])
            if not isinstance(runtimes_value, list):
                raise ValueError("workdir.agent_runtimes must be an array")
            runtimes: list[str] = []
            for runtime in runtimes_value:
                runtime_name = _strict_string(runtime, "workdir.agent_runtimes entry")
                if runtime_name not in KNOWN_RUNTIME_NAMES:
                    raise ValueError(f"unknown agent runtime: {runtime_name}")
                if runtime_name in runtimes:
                    raise ValueError(f"duplicate agent runtime: {runtime_name}")
                runtimes.append(runtime_name)
            if mode is AgentMode.DISABLED and runtimes:
                raise ValueError("disabled agent mode cannot allow runtimes")
            read_only = _strict_bool(item.get("read_only", True), "workdir.read_only")
            if mode is AgentMode.WORKSPACE_WRITE and read_only:
                raise ValueError("workspace-write agent mode requires a writable workdir")
            policies.append(
                WorkdirAgentPolicy(
                    slot=slot,
                    alias=alias,
                    host_path=host_path,
                    mode=mode,
                    runtimes=frozenset(runtimes),
                    read_only=read_only,
                )
            )

        enable_fake_runtime = _strict_bool(
            data.get("enable_fake_runtime", False), "enable_fake_runtime"
        )
        if not enable_fake_runtime and any("fake" in policy.runtimes for policy in policies):
            raise ValueError("fake runtime is allowlisted but enable_fake_runtime is false")

        return cls(
            socket_path=_config_path(
                data.get("socket_path", "/run/serverfs-agent-bridge/bridge.sock"),
                "socket_path",
            ),
            state_dir=_config_path(
                data.get("state_dir", "~/.local/state/serverfs-agent-bridge"),
                "state_dir",
                expand_user=True,
            ),
            lock_dir=_config_path(data.get("lock_dir", "/run/serverfs-agent-locks"), "lock_dir"),
            allowed_peer_uid=_optional_int(data.get("allowed_peer_uid")),
            allowed_peer_gid=_optional_int(data.get("allowed_peer_gid")),
            enable_fake_runtime=enable_fake_runtime,
            policies=PolicyRegistry(policies),
        )


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return _strict_int(value, "peer credential")


def _reject_unknown_keys(data: dict[str, Any], allowed: frozenset[str], label: str) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(f"unknown {label} field: {sorted(unknown)[0]}")


def _strict_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _strict_bool(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{label} must be a JSON boolean")
    return value


def _strict_int(value: Any, label: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{label} must be a JSON integer")
    if value < 0:
        raise ValueError(f"{label} must not be negative")
    return value


def _config_path(value: Any, label: str, *, expand_user: bool = False) -> Path:
    path = Path(_strict_string(value, label))
    if expand_user:
        path = path.expanduser()
    if not path.is_absolute():
        raise ValueError(f"{label} must be absolute")
    return path
