"""Configuration loader for the standalone Agent Bridge."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
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
        "codex",
        "claude",
        "workdirs",
    }
)
_WORKDIR_KEYS = frozenset(
    {"slot", "alias", "host_path", "read_only", "agent_mode", "agent_runtimes"}
)
_CLAUDE_KEYS = frozenset(
    {
        "enabled",
        "claude_bin",
        "probe_timeout_seconds",
        "event_idle_timeout_seconds",
    }
)
_CODEX_KEYS = frozenset(
    {
        "enabled",
        "autostart",
        "codex_home",
        "codex_bin",
        "request_timeout_seconds",
        "event_idle_timeout_seconds",
        "max_message_bytes",
    }
)
_ALIAS_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
_MAX_WORKDIR_SLOTS = 16


def _default_codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".codex"


@dataclass(frozen=True)
class CodexSettings:
    enabled: bool = False
    autostart: bool = False
    codex_home: Path = field(default_factory=_default_codex_home)
    codex_bin: str = "codex"
    request_timeout_seconds: float = 10.0
    event_idle_timeout_seconds: float | None = None
    max_message_bytes: int = 128 * 1024 * 1024

    @property
    def control_socket(self) -> Path:
        return self.codex_home / "app-server-control" / "app-server-control.sock"


@dataclass(frozen=True)
class ClaudeSettings:
    enabled: bool = False
    claude_bin: str = "claude"
    probe_timeout_seconds: float = 5.0
    event_idle_timeout_seconds: float | None = None


@dataclass(frozen=True)
class BridgeConfig:
    socket_path: Path
    state_dir: Path
    lock_dir: Path
    allowed_peer_uid: int | None
    allowed_peer_gid: int | None
    enable_fake_runtime: bool
    codex: CodexSettings
    claude: ClaudeSettings
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

        codex = _load_codex_settings(data.get("codex"))
        claude = _load_claude_settings(data.get("claude"))
        codex_policies = [policy for policy in policies if "codex" in policy.runtimes]
        if not codex.enabled and codex_policies:
            raise ValueError("codex runtime is allowlisted but codex.enabled is false")
        if any(policy.mode is not AgentMode.WORKSPACE_WRITE for policy in codex_policies):
            raise ValueError(
                "codex native mode requires agent_mode=workspace-write "
                "for every allowlisted workdir"
            )

        claude_policies = [policy for policy in policies if "claude" in policy.runtimes]
        if not claude.enabled and claude_policies:
            raise ValueError("claude runtime is allowlisted but claude.enabled is false")
        if any(policy.mode is not AgentMode.WORKSPACE_WRITE for policy in claude_policies):
            raise ValueError(
                "claude native mode requires agent_mode=workspace-write "
                "for every allowlisted workdir"
            )

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
            codex=codex,
            claude=claude,
            policies=PolicyRegistry(policies),
        )


def _load_claude_settings(value: Any) -> ClaudeSettings:
    if value is None:
        return ClaudeSettings()
    if not isinstance(value, dict):
        raise ValueError("claude must be an object")
    _reject_unknown_keys(value, _CLAUDE_KEYS, "claude")

    enabled = _strict_bool(value.get("enabled", False), "claude.enabled")
    claude_bin = _strict_string(value.get("claude_bin", "claude"), "claude.claude_bin")
    if any(char in claude_bin for char in ("\x00", "\n", "\r")):
        raise ValueError("claude.claude_bin contains an invalid character")
    probe_timeout = _strict_positive_number(
        value.get("probe_timeout_seconds", 5.0),
        "claude.probe_timeout_seconds",
    )
    event_idle_timeout_value = value.get("event_idle_timeout_seconds")
    event_idle_timeout = (
        None
        if event_idle_timeout_value is None
        else _strict_positive_number(
            event_idle_timeout_value,
            "claude.event_idle_timeout_seconds",
        )
    )
    return ClaudeSettings(
        enabled=enabled,
        claude_bin=claude_bin,
        probe_timeout_seconds=probe_timeout,
        event_idle_timeout_seconds=event_idle_timeout,
    )


def _load_codex_settings(value: Any) -> CodexSettings:
    if value is None:
        return CodexSettings()
    if not isinstance(value, dict):
        raise ValueError("codex must be an object")
    _reject_unknown_keys(value, _CODEX_KEYS, "codex")

    enabled = _strict_bool(value.get("enabled", False), "codex.enabled")
    autostart = _strict_bool(value.get("autostart", False), "codex.autostart")
    if autostart and not enabled:
        raise ValueError("codex.autostart requires codex.enabled=true")

    home_value = value.get("codex_home", str(_default_codex_home()))
    codex_home = _config_path(home_value, "codex.codex_home", expand_user=True)
    if enabled:
        try:
            home_stat = codex_home.resolve(strict=True)
        except OSError as exc:
            raise ValueError("codex.codex_home must exist when Codex is enabled") from exc
        if not home_stat.is_dir():
            raise ValueError("codex.codex_home must be a directory")
        codex_home = home_stat

    codex_bin = _strict_string(value.get("codex_bin", "codex"), "codex.codex_bin")
    if any(char in codex_bin for char in ("\x00", "\n", "\r")):
        raise ValueError("codex.codex_bin contains an invalid character")

    request_timeout = _strict_positive_number(
        value.get("request_timeout_seconds", 10.0),
        "codex.request_timeout_seconds",
    )
    event_idle_timeout_value = value.get("event_idle_timeout_seconds")
    event_idle_timeout = (
        None
        if event_idle_timeout_value is None
        else _strict_positive_number(
            event_idle_timeout_value,
            "codex.event_idle_timeout_seconds",
        )
    )
    max_message_bytes = _strict_int(
        value.get("max_message_bytes", 128 * 1024 * 1024),
        "codex.max_message_bytes",
    )
    if max_message_bytes < 1024:
        raise ValueError("codex.max_message_bytes must be at least 1024")

    return CodexSettings(
        enabled=enabled,
        autostart=autostart,
        codex_home=codex_home,
        codex_bin=codex_bin,
        request_timeout_seconds=request_timeout,
        event_idle_timeout_seconds=event_idle_timeout,
        max_message_bytes=max_message_bytes,
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


def _strict_positive_number(value: Any, label: str) -> float:
    import math

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a JSON number")
    number = float(value)
    if not math.isfinite(number) or not number > 0:
        raise ValueError(f"{label} must be a finite positive number")
    return number


def _config_path(value: Any, label: str, *, expand_user: bool = False) -> Path:
    path = Path(_strict_string(value, label))
    if expand_user:
        path = path.expanduser()
    if not path.is_absolute():
        raise ValueError(f"{label} must be absolute")
    return path
