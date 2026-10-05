"""Native TOML configuration model (v0.10 Phase A, §7.1).

Native (non-Docker) deployments read an explicit TOML file instead of
``WORKDIR_XX_*`` environment variables. Parsing uses Python 3.12 ``tomllib``
so no new parser dependency is required.

Rules enforced here (fail at startup, never at request time):

- ``path`` is operator configuration, never an MCP argument;
- workdir paths must be absolute native paths;
- aliases keep the current public validation rules (workdirs.ALIAS_RE);
- duplicate aliases fail startup;
- ``read_only`` defaults to true — the safe default, like the env adapter;
- configuration is parsed once; the effective policy is frozen at startup;
- no environment interpolation and no secrets in ``serverfs.toml``.

The TOML model resolves into the same platform-neutral ``Workdir`` /
``EffectiveWorkdirPolicy`` objects the legacy env adapter produces, so the
registry, tools and policy layers see one shape regardless of source.

v0.11 Phase D adds the operator-facing ``[agent]`` model (§7, §7.1). Three rules shape it:

- **Absent or disabled is the v0.10 configuration.** A config with no ``[agent]``
  section, or with ``enabled = false``, must parse to the identical filesystem-only
  result it produced before Phase D. That is the hard upgrade gate, so the
  defaults here are the *absence* of behaviour rather than a parallel default set.
- **No secrets, no endpoint.** The proxy booleans below are routing policy. The
  endpoint itself arrives through the dedicated ``SERVERFS_AGENT_PROXY_URL``
  namespace at runtime and is never written to ``serverfs.toml`` (§7.1, §10).
- **One expression per knob.** The lifecycle timeouts map onto the Bridge's
  existing ``LifecycleLimits`` rather than introducing a second vocabulary with
  the same meaning.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .workdirs import (
    AGENT_MODE_DISABLED,
    AGENT_MODE_WORKSPACE_WRITE,
    AGENT_MODES,
    ALIAS_RE,
    PUBLIC_AGENT_RUNTIMES,
    EffectiveWorkdirPolicy,
    Workdir,
)

# Resource guard, not a numbered deployment slot (§7.3): the native format
# has no 16-slot layout, but an unbounded workdir count would let a config
# file exhaust startup resources. This ceiling is deliberately generous.
MAX_NATIVE_WORKDIRS = 64


class NativeConfigError(Exception):
    """Configuration error in the native TOML file; aborts startup."""


@dataclass(frozen=True)
class NativeServerSettings:
    log_level: str = "INFO"
    agent: NativeAgentSettings | None = None

    @property
    def agent_enabled(self) -> bool:
        """Whether this deployment delegates to the Agent Bridge at all.

        ``None`` means the config carried no ``[agent]`` section, which is the
        v0.10 shape and must behave exactly as it did.
        """
        return self.agent is not None and self.agent.enabled


@dataclass(frozen=True)
class NativeCodexSettings:
    enabled: bool = False
    #: Routing policy measured in Phase 0F: on WorkPC ``api.openai.com`` and
    #: ``chatgpt.com`` time out direct, so Codex egress requires the proxy.
    use_proxy: bool = True
    codex_bin: str = "codex"

    @property
    def binary(self) -> str:
        return self.codex_bin


@dataclass(frozen=True)
class NativeClaudeSettings:
    enabled: bool = False
    #: Not required on WorkPC, and not measurable without a real turn, so this
    #: stays a per-deployment choice rather than a global "always proxy".
    use_proxy: bool = False
    claude_bin: str = "claude"

    @property
    def binary(self) -> str:
        return self.claude_bin


@dataclass(frozen=True)
class NativeQoderSettings:
    enabled: bool = False
    #: Measured directly reachable on WorkPC; the SDK honours a proxy but falls
    #: back to direct when it fails, so proxying is policy, not connectivity.
    use_proxy: bool = False
    qoder_bin: str = "qodercli"

    @property
    def binary(self) -> str:
        return self.qoder_bin


@dataclass(frozen=True)
class NativeProxySettings:
    """The dedicated Agent egress proxy policy (§7.1).

    v0.11 supports exactly one source. ``file``, ``registry``, ``winhttp``,
    ``system`` and ``keyring`` are deliberately absent: no such consumer exists,
    and an unused switch is a promise the release cannot keep. The endpoint value
    is never stored here — only the decision to consume it from the environment.
    """

    enabled: bool = False
    source: str = "env"


#: The only proxy source v0.11 supports (§7.1).
PROXY_SOURCES = frozenset({"env"})

#: Mandatory local bypass. Phase 0F measured that an empty or absent ``NO_PROXY``
#: re-enables proxying of loopback, which would route the Bridge's own authenticated
#: Codex control channel through the egress proxy. The operator value merges with
#: this set and can never remove an entry from it.
MANDATORY_NO_PROXY = ("127.0.0.1", "localhost", "::1")


@dataclass(frozen=True)
class NativeAgentSettings:
    """The whole operator-facing Agent model, or ``None`` when the section is absent.

    The lifecycle defaults are the Bridge's own ``LifecycleLimits`` defaults
    (7200 / 1800 / 4 / 168h). Restating them as a second definition would be
    exactly the duplicate semantics §7 forbids, so they are written once in this
    module and the renderer passes them through unchanged.
    """

    enabled: bool = False
    task_timeout_seconds: int = 7200
    interaction_timeout_seconds: int = 1800
    max_active_tasks: int = 4
    retention_seconds: int = 168 * 60 * 60
    codex: NativeCodexSettings = field(default_factory=NativeCodexSettings)
    claude: NativeClaudeSettings = field(default_factory=NativeClaudeSettings)
    qoder: NativeQoderSettings = field(default_factory=NativeQoderSettings)
    proxy: NativeProxySettings = field(default_factory=NativeProxySettings)

    def runtime_enabled(self, name: str) -> bool:
        return self.runtime(name).enabled

    def runtime_use_proxy(self, name: str) -> bool:
        return self.runtime(name).use_proxy

    def runtime_binary(self, name: str) -> str:
        return self.runtime(name).binary

    def runtime(self, name: str):
        """The settings for one public runtime name."""
        return {"codex": self.codex, "claude": self.claude, "qoder": self.qoder}[name]

    @property
    def enabled_runtimes(self) -> frozenset[str]:
        return frozenset(name for name in PUBLIC_AGENT_RUNTIMES if self.runtime(name).enabled)


@dataclass(frozen=True)
class NativeDefaults:
    """Global defaults applied to every native workdir unless overridden."""

    max_read_bytes: int = 524_288
    max_read_lines: int = 500
    max_write_bytes: int = 1_048_576
    allow_hidden: bool = False
    binary_transfer_enabled: bool = False
    max_binary_transfer_bytes: int = 8_388_608


def _require_strict_bool(section: str, key: str, value: object, default: bool) -> bool:
    """Strict boolean parsing: an invalid value must abort, never guess."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raise NativeConfigError(f"{section}.{key} must be true or false, got {value!r}")


def _require_positive_int(section: str, key: str, value: object, default: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise NativeConfigError(f"{section}.{key} must be a positive integer, got {value!r}")
    return value


def _parse_log_level(server: dict) -> str:
    raw = server.get("log_level", "INFO")
    if not isinstance(raw, str):
        raise NativeConfigError(f"server.log_level must be a string, got {raw!r}")
    level = raw.strip().upper()
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        raise NativeConfigError(f"server.log_level value {raw!r} is not a valid level")
    return level


def _require_section(data: dict, name: str) -> dict | None:
    """One TOML table, or None when absent. Absence is never an error."""
    section = data.get(name)
    if section is None:
        return None
    if not isinstance(section, dict):
        raise NativeConfigError(f"[{name}] must be a table")
    return section


def _reject_unknown(section: dict, known: set[str], label: str) -> None:
    unknown = set(section) - known
    if unknown:
        raise NativeConfigError(f"unknown {label} keys: " + ", ".join(sorted(unknown)))


def _parse_proxy(section: dict | None) -> NativeProxySettings:
    defaults = NativeProxySettings()
    if section is None:
        return defaults
    _reject_unknown(section, {"enabled", "source"}, "[agent.proxy]")
    enabled = _require_strict_bool(
        "agent.proxy", "enabled", section.get("enabled"), defaults.enabled
    )
    source = section.get("source", defaults.source)
    if not isinstance(source, str):
        raise NativeConfigError("agent.proxy.source must be a string")
    if source not in PROXY_SOURCES:
        # v0.11 supports exactly one source. Naming the others would imply a
        # consumer that does not exist.
        raise NativeConfigError(
            f"agent.proxy.source must be one of {sorted(PROXY_SOURCES)}, got {source!r}"
        )
    return NativeProxySettings(enabled=enabled, source=source)


def _parse_runtime(
    section: dict | None,
    *,
    label: str,
    bin_key: str,
    default_bin: str,
    default_use_proxy: bool,
) -> tuple[bool, bool, str]:
    """``(enabled, use_proxy, binary)`` for one runtime table."""
    if section is None:
        return (False, default_use_proxy, default_bin)
    _reject_unknown(section, {"enabled", "use_proxy", bin_key}, f"[agent.{label}]")
    enabled = _require_strict_bool(f"agent.{label}", "enabled", section.get("enabled"), False)
    use_proxy = _require_strict_bool(
        f"agent.{label}", "use_proxy", section.get("use_proxy"), default_use_proxy
    )
    binary = section.get(bin_key, default_bin)
    if not isinstance(binary, str) or not binary.strip():
        raise NativeConfigError(f"agent.{label}.{bin_key} must be a non-empty string")
    binary = binary.strip()
    if any(char in binary for char in ("\x00", "\n", "\r")):
        raise NativeConfigError(f"agent.{label}.{bin_key} contains an invalid character")
    return (enabled, use_proxy, binary)


def _parse_agent(data: dict) -> NativeAgentSettings | None:
    """The ``[agent]`` model, or ``None`` for the v0.10 shape with no such section."""
    section = _require_section(data, "agent")
    if section is None:
        return None

    _reject_unknown(
        section,
        {
            "enabled",
            "task_timeout_seconds",
            "interaction_timeout_seconds",
            "max_active_tasks",
            "retention_seconds",
            "codex",
            "claude",
            "qoder",
            "proxy",
        },
        "[agent]",
    )
    defaults = NativeAgentSettings()
    codex_enabled, codex_proxy, codex_bin = _parse_runtime(
        _require_section(section, "codex"),
        label="codex",
        bin_key="codex_bin",
        default_bin=defaults.codex.codex_bin,
        default_use_proxy=defaults.codex.use_proxy,
    )
    claude_enabled, claude_proxy, claude_bin = _parse_runtime(
        _require_section(section, "claude"),
        label="claude",
        bin_key="claude_bin",
        default_bin=defaults.claude.claude_bin,
        default_use_proxy=defaults.claude.use_proxy,
    )
    qoder_enabled, qoder_proxy, qoder_bin = _parse_runtime(
        _require_section(section, "qoder"),
        label="qoder",
        bin_key="qoder_bin",
        default_bin=defaults.qoder.qoder_bin,
        default_use_proxy=defaults.qoder.use_proxy,
    )
    return NativeAgentSettings(
        enabled=_require_strict_bool("agent", "enabled", section.get("enabled"), False),
        task_timeout_seconds=_require_positive_int(
            "agent",
            "task_timeout_seconds",
            section.get("task_timeout_seconds"),
            defaults.task_timeout_seconds,
        ),
        interaction_timeout_seconds=_require_positive_int(
            "agent",
            "interaction_timeout_seconds",
            section.get("interaction_timeout_seconds"),
            defaults.interaction_timeout_seconds,
        ),
        max_active_tasks=_require_positive_int(
            "agent", "max_active_tasks", section.get("max_active_tasks"), defaults.max_active_tasks
        ),
        retention_seconds=_require_positive_int(
            "agent",
            "retention_seconds",
            section.get("retention_seconds"),
            defaults.retention_seconds,
        ),
        codex=NativeCodexSettings(
            enabled=codex_enabled, use_proxy=codex_proxy, codex_bin=codex_bin
        ),
        claude=NativeClaudeSettings(
            enabled=claude_enabled, use_proxy=claude_proxy, claude_bin=claude_bin
        ),
        qoder=NativeQoderSettings(
            enabled=qoder_enabled, use_proxy=qoder_proxy, qoder_bin=qoder_bin
        ),
        proxy=_parse_proxy(_require_section(section, "proxy")),
    )


def _parse_defaults(data: dict) -> NativeDefaults:
    section = data.get("defaults")
    if section is None:
        return NativeDefaults()
    if not isinstance(section, dict):
        raise NativeConfigError("[defaults] must be a table")
    known = {
        "max_read_bytes",
        "max_read_lines",
        "max_write_bytes",
        "allow_hidden",
        "binary_transfer_enabled",
        "max_binary_transfer_bytes",
    }
    unknown = set(section) - known
    if unknown:
        raise NativeConfigError("unknown [defaults] keys: " + ", ".join(sorted(unknown)))
    return NativeDefaults(
        max_read_bytes=_require_positive_int(
            "defaults", "max_read_bytes", section.get("max_read_bytes"), 524_288
        ),
        max_read_lines=_require_positive_int(
            "defaults", "max_read_lines", section.get("max_read_lines"), 500
        ),
        max_write_bytes=_require_positive_int(
            "defaults", "max_write_bytes", section.get("max_write_bytes"), 1_048_576
        ),
        allow_hidden=_require_strict_bool(
            "defaults", "allow_hidden", section.get("allow_hidden"), False
        ),
        binary_transfer_enabled=_require_strict_bool(
            "defaults", "binary_transfer_enabled", section.get("binary_transfer_enabled"), False
        ),
        max_binary_transfer_bytes=_require_positive_int(
            "defaults",
            "max_binary_transfer_bytes",
            section.get("max_binary_transfer_bytes"),
            8_388_608,
        ),
    )


def _is_absolute_native_path(raw_path: str) -> bool:
    """Lexical absoluteness for either platform's native root syntax.

    Accepts ``/posix/roots``, ``D:\\Windows\\roots`` and UNC
    (``\\\\server\\share`` or the ``\\\\?\\`` prefix form). Rejects
    drive-relative (``D:file``) and bare-relative shapes — those resolve
    against the process CWD, which would make the trusted anchor depend on
    where the service happens to start.
    """
    if raw_path.startswith("/"):
        return True
    # drive-absolute D:\ or D:/ — but NOT D:relative
    if len(raw_path) >= 3 and raw_path[1] == ":" and raw_path[2] in "\\/":
        return True
    # UNC: \\server\share... or \\?\D:\... (both start with two separators)
    return raw_path.startswith("\\\\") and len(raw_path) > 2


def _has_parent_component(raw_path: str) -> bool:
    """True when either separator flavor splits a ``..`` out of the path."""
    return ".." in raw_path.replace("\\", "/").split("/")


def _parse_agent_mode(where: str, value: object) -> str:
    """The frozen Agent mode vocabulary (§15 Phase D).

    Same three values the env adapter and the Bridge already share; no native-only mode is
    introduced, because the mode set is the authorization contract rather than a config detail.
    """
    if value is None:
        return AGENT_MODE_DISABLED
    if not isinstance(value, str) or value not in AGENT_MODES:
        raise NativeConfigError(
            f"{where}.agent_mode must be one of {sorted(AGENT_MODES)}, got {value!r}"
        )
    return value


def _parse_agent_runtimes(where: str, value: object) -> frozenset[str]:
    """The public runtime allowlist for one workdir.

    An unknown or duplicated runtime fails startup rather than being dropped: a silently
    ignored allowlist entry would leave the operator believing a runtime is permitted when the
    Bridge will refuse it.
    """
    if value is None:
        return frozenset()
    if not isinstance(value, list):
        raise NativeConfigError(f"{where}.agent_runtimes must be an array of strings")
    runtimes: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise NativeConfigError(f"{where}.agent_runtimes entries must be strings")
        if item not in PUBLIC_AGENT_RUNTIMES:
            raise NativeConfigError(
                f"{where}.agent_runtimes has unknown runtime {item!r}; "
                f"known runtimes are {sorted(PUBLIC_AGENT_RUNTIMES)}"
            )
        if item in runtimes:
            raise NativeConfigError(f"{where}.agent_runtimes has duplicate runtime {item!r}")
        runtimes.add(item)
    return frozenset(runtimes)


def _parse_workdir(
    entry: object,
    index: int,
    defaults: NativeDefaults,
    *,
    agent: NativeAgentSettings | None,
) -> Workdir:
    """Validate one [[workdirs]] entry and build a platform-neutral Workdir."""
    where = f"workdirs[{index}]"
    if not isinstance(entry, dict):
        raise NativeConfigError(f"{where} must be a table")
    known = {
        "alias",
        "path",
        "description",
        "read_only",
        "agent_mode",
        "agent_runtimes",
    }
    unknown = set(entry) - known
    if unknown:
        raise NativeConfigError(f"{where} has unknown keys: " + ", ".join(sorted(unknown)))

    alias = entry.get("alias")
    if not isinstance(alias, str) or not ALIAS_RE.fullmatch(alias):
        raise NativeConfigError(
            f"{where}.alias {alias!r} is invalid. Aliases must match "
            "^[A-Za-z][A-Za-z0-9_-]{0,31}$ (letter first, up to 32 chars, "
            "no slashes or spaces)."
        )

    raw_path = entry.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise NativeConfigError(f"{where}.path is required and must be a non-empty string")
    # Platform-neutral absoluteness: POSIX roots start '/', Windows roots
    # start with a drive letter (D:\...), a drive-relative form is refused,
    # and both UNC forms are accepted. ``Path.is_absolute`` on POSIX rejects
    # Windows shapes (and vice versa), so the lexical check is spelled out
    # rather than delegated to the running platform's Path flavor.
    if not _is_absolute_native_path(raw_path):
        raise NativeConfigError(f"{where}.path {raw_path!r} must be an absolute native path")
    if _has_parent_component(raw_path):
        # The root is a trusted anchor (§10.2); a `..` component would make
        # the effective anchor depend on resolution order. Fail closed.
        # Both separators are split: a POSIX flavor Path must still see the
        # `..` inside a Windows-shaped root, so the check cannot rely on the
        # running platform's separator rules.
        raise NativeConfigError(f"{where}.path {raw_path!r} must not contain '..'")

    description = entry.get("description")
    if description is not None and not isinstance(description, str):
        raise NativeConfigError(f"{where}.description must be a string")
    description = (description or "").strip() or None

    read_only = _require_strict_bool(where, "read_only", entry.get("read_only"), True)

    agent_mode = _parse_agent_mode(where, entry.get("agent_mode"))
    agent_runtimes = _parse_agent_runtimes(where, entry.get("agent_runtimes"))

    # A workspace-write workdir is only meaningful against a writable root. This is checked here
    # rather than left to the Bridge so the operator sees it as a configuration error at startup.
    if agent_mode == AGENT_MODE_WORKSPACE_WRITE and read_only:
        raise NativeConfigError(
            f"{where}.agent_mode = 'workspace-write' requires read_only = false"
        )
    if agent_mode == AGENT_MODE_DISABLED and agent_runtimes:
        raise NativeConfigError(
            f"{where}.agent_runtimes requires an agent_mode other than 'disabled'"
        )

    # Native workdirs carry no legacy slot (§6): the slot is an input-adapter
    # concept belonging to the Compose/env adapter alone.
    # The root is stored as a plain Path; on POSIX a Windows-shaped root is
    # inert data until the backend that can open it exists.
    return Workdir(
        alias=alias,
        root=Path(raw_path),
        description=description,
        read_only=read_only,
        policy=EffectiveWorkdirPolicy(
            allow_hidden=defaults.allow_hidden,
            disable_default_deny=False,
            extra_deny_globs=(),
            max_read_bytes=defaults.max_read_bytes,
            max_read_lines=defaults.max_read_lines,
            max_write_bytes=defaults.max_write_bytes,
            binary_transfer_enabled=defaults.binary_transfer_enabled,
            max_binary_transfer_bytes=defaults.max_binary_transfer_bytes,
            agent_mode=agent_mode,
            agent_runtimes=agent_runtimes,
        ),
    )


def _cross_validate_agent(
    agent: NativeAgentSettings | None,
    workdirs: list[Workdir],
) -> None:
    """Startup-time consistency between ``[agent]`` and the per-workdir policy (§9).

    Only contradictions are rejected. An enabled runtime that no workdir allowlists is
    *not* an error: the operator may be staging a runtime before assigning it, and demanding
    usage would add a restriction no frozen contract requires.
    """
    if agent is None or not agent.enabled:
        # A disabled [agent] section must not leave Agent policy behind on a workdir, or the
        # operator would have configured authorization that nothing will ever honour.
        for wd in workdirs:
            if wd.agent_mode != AGENT_MODE_DISABLED or wd.agent_runtimes:
                raise NativeConfigError(
                    f"workdir {wd.alias!r} configures agent_mode/agent_runtimes but "
                    "[agent] is not enabled"
                )
        return

    for wd in workdirs:
        for runtime in sorted(wd.agent_runtimes):
            if not agent.runtime_enabled(runtime):
                raise NativeConfigError(
                    f"workdir {wd.alias!r} allowlists runtime {runtime!r} but "
                    f"agent.{runtime}.enabled is false"
                )
        # The frozen native-mode rule: a native runtime may only submit with workspace-write,
        # so any workdir allowing one must itself be workspace-write.
        if wd.agent_runtimes and wd.agent_mode != AGENT_MODE_WORKSPACE_WRITE:
            raise NativeConfigError(
                f"workdir {wd.alias!r} allowlists native runtimes {sorted(wd.agent_runtimes)} "
                f"and therefore requires agent_mode = 'workspace-write', not {wd.agent_mode!r}"
            )


def load_native_config(path: Path) -> tuple[list[Workdir], NativeServerSettings]:
    """Parse serverfs.toml into (platform-neutral Workdirs, server settings).

    Raises NativeConfigError on any rule violation; the caller aborts
    startup. Root existence/typing checks (directory, not a reparse point on
    Windows) belong to the backend's root acquisition, not to parsing —
    parsing must stay platform-neutral.
    """
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError as exc:
        raise NativeConfigError(f"configuration file not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise NativeConfigError(f"invalid TOML in {path}: {exc}") from exc
    except OSError as exc:
        raise NativeConfigError(f"cannot read configuration file {path}: {exc}") from exc

    if not isinstance(data.get("server", {}), dict):
        raise NativeConfigError("[server] must be a table")
    agent = _parse_agent(data)
    server_settings = NativeServerSettings(
        log_level=_parse_log_level(data.get("server", {})), agent=agent
    )
    defaults = _parse_defaults(data)

    entries = data.get("workdirs", [])
    if not isinstance(entries, list):
        raise NativeConfigError("[[workdirs]] must be an array of tables")
    if not entries:
        raise NativeConfigError("at least one [[workdirs]] entry is required")
    if len(entries) > MAX_NATIVE_WORKDIRS:
        raise NativeConfigError(
            f"too many workdirs: {len(entries)} exceeds the {MAX_NATIVE_WORKDIRS} limit"
        )

    workdirs = [_parse_workdir(entry, i, defaults, agent=agent) for i, entry in enumerate(entries)]
    seen: dict[str, int] = {}
    for index, wd in enumerate(workdirs):
        if wd.alias in seen:
            raise NativeConfigError(
                f"workdirs[{index}].alias '{wd.alias}' is a duplicate "
                f"(also configured at workdirs[{seen[wd.alias]}])"
            )
        seen[wd.alias] = index
    _cross_validate_agent(agent, workdirs)
    return workdirs, server_settings


__all__ = [
    "MANDATORY_NO_PROXY",
    "MAX_NATIVE_WORKDIRS",
    "PROXY_SOURCES",
    "NativeAgentSettings",
    "NativeClaudeSettings",
    "NativeCodexSettings",
    "NativeConfigError",
    "NativeDefaults",
    "NativeProxySettings",
    "NativeQoderSettings",
    "NativeServerSettings",
    "load_native_config",
]
