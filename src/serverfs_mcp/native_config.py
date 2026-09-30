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
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from .workdirs import (
    ALIAS_RE,
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


def _parse_workdir(entry: object, index: int, defaults: NativeDefaults) -> Workdir:
    """Validate one [[workdirs]] entry and build a platform-neutral Workdir."""
    where = f"workdirs[{index}]"
    if not isinstance(entry, dict):
        raise NativeConfigError(f"{where} must be a table")
    known = {"alias", "path", "description", "read_only"}
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

    # Native workdirs have no deployment slot; the legacy slot is an
    # input-adapter concept (§6). slot=0 marks "not from the legacy adapter".
    # The root is stored as a plain Path; on POSIX a Windows-shaped root is
    # inert data until the backend that can open it exists.
    return Workdir(
        slot=0,
        alias=alias,
        container_path=Path(raw_path),
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
        ),
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
    server_settings = NativeServerSettings(log_level=_parse_log_level(data.get("server", {})))
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

    workdirs = [_parse_workdir(entry, i, defaults) for i, entry in enumerate(entries)]
    seen: dict[str, int] = {}
    for index, wd in enumerate(workdirs):
        if wd.alias in seen:
            raise NativeConfigError(
                f"workdirs[{index}].alias '{wd.alias}' is a duplicate "
                f"(also configured at workdirs[{seen[wd.alias]}])"
            )
        seen[wd.alias] = index
    return workdirs, server_settings


__all__ = [
    "MAX_NATIVE_WORKDIRS",
    "NativeConfigError",
    "NativeDefaults",
    "NativeServerSettings",
    "load_native_config",
]
