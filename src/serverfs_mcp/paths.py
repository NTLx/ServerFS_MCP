"""Path safety layer: the single policy gate for all filesystem access.

``resolve_workdir_path`` performs pure policy validation (normalization,
traversal, hidden, deny) and never touches the filesystem. Filesystem
operations then walk components via file descriptors (see fdio.py), so
symlink enforcement and object identity are checked atomically at open
time — there is no separate lstat→open TOCTOU window.

Policy:
- relative paths only, NUL rejected
- ``..`` segments are normalized, result must stay inside the workdir
- hidden path components (leading ``.``) are rejected unless allow_hidden
- credential-like names are denied by the built-in rules (defense in depth);
  admins can append their own globs (SERVERFS_EXTRA_DENY_GLOBS) or opt out
  of the built-in set entirely (SERVERFS_DISABLE_DEFAULT_DENY=true)
- the reserved internal namespace (``.serverfs-tmp-*``) is denied on every
  channel unconditionally — it is not a policy knob, so allow_hidden, the
  default rules and the extra globs cannot release it
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path

from .workdirs import Workdir

# credential-like patterns denied by the built-in rules
DEFAULT_DENY_BASENAMES = {
    ".env",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
}
DEFAULT_DENY_GLOBS = (
    ".env",
    ".env.*",
    "*.env",
    "*.pem",
    "*.key",
)
DEFAULT_DENY_DIR_NAMES = {
    ".ssh",
    ".aws",
    ".gnupg",
    ".kube",
}

# Internal namespace for the same-directory temp files that back atomic
# create/edit. Reserved: never visible, readable or mutable through any
# channel, in any configuration. See is_reserved_path().
RESERVED_TEMP_PREFIX = ".serverfs-tmp-"


class PathSecurityError(Exception):
    """Base class for anticipated path-policy violations."""

    code = "ACCESS_DENIED"
    message = "path is not accessible"

    def __init__(self, message: str | None = None):
        super().__init__(message or self.message)
        if message is not None:
            self.message = message


class AbsolutePathError(PathSecurityError):
    code = "PATH_OUTSIDE_WORKDIR"
    message = "paths must be relative to the workdir"


class NulPathError(PathSecurityError):
    code = "ACCESS_DENIED"
    message = "path contains NUL bytes"


class PathOutsideWorkdirError(PathSecurityError):
    code = "PATH_OUTSIDE_WORKDIR"
    message = "resolved path escapes the workdir"


class SymlinkNotAllowedError(PathSecurityError):
    code = "SYMLINK_NOT_ALLOWED"
    message = "symlinks are not allowed"


class HiddenPathNotAllowedError(PathSecurityError):
    code = "HIDDEN_PATH_NOT_ALLOWED"
    message = "hidden paths are not allowed"


class DeniedPathError(PathSecurityError):
    code = "DENIED_PATH"
    message = "path is denied by credential-protection policy"


class ReservedPathError(PathSecurityError):
    code = "RESERVED_PATH"
    message = "path uses the reserved ServerFS internal namespace"


class UnsupportedFileTypeError(PathSecurityError):
    code = "UNSUPPORTED_FILE_TYPE"
    message = "only regular files and directories are supported"


def is_hidden_component(component: str) -> bool:
    """A path component is hidden when it starts with ``.`` (except . and ..)."""
    return component.startswith(".") and component not in (".", "..")


def is_reserved_component(component: str) -> bool:
    """True for a name inside the reserved internal temp namespace."""
    return component.startswith(RESERVED_TEMP_PREFIX)


def is_reserved_path(rel_parts: tuple[str, ...]) -> bool:
    """True when any component of a workdir-relative path is reserved."""
    return any(is_reserved_component(c) for c in rel_parts)


@dataclass(frozen=True)
class DenyPolicy:
    """Filename deny rules applied on every channel (read/list/find/search).

    Built-in credential rules run unless disabled; admin-provided globs
    always run. Glob semantics:
    - plain glob (``*.pem``) matches the final path component only
    - ``name/**`` (``.ssh/**``) matches that name as ANY path component —
      the directory itself and everything below it

    The reserved internal namespace is denied here too, unconditionally:
    entry filtering (list/find/search) and path resolution both consult
    this one matcher, so no configuration can expose a temp artifact.
    """

    extra_globs: tuple[str, ...] = ()
    default_deny_enabled: bool = True

    def is_denied(self, rel_parts: tuple[str, ...]) -> bool:
        """True when the relative path (or a single basename) is denied."""
        if not rel_parts:
            return False
        if is_reserved_path(rel_parts):
            return True
        *dirs, base = rel_parts
        components = rel_parts
        if self.default_deny_enabled:
            if any(c in DEFAULT_DENY_DIR_NAMES for c in components):
                return True
            if base in DEFAULT_DENY_BASENAMES:
                return True
            if any(fnmatch.fnmatchcase(base, g) for g in DEFAULT_DENY_GLOBS):
                return True
        for g in self.extra_globs:
            if g.endswith("/**"):
                head = g[:-3]
                if head and any(fnmatch.fnmatchcase(c, head) for c in components):
                    return True
            elif fnmatch.fnmatchcase(base, g):
                return True
        return False


def _split_relative_path(relative_path: str) -> tuple[str, ...]:
    """Split a client-relative path into normalized components.

    Raises on absolute paths, NUL bytes, and backslash tricks (we treat
    ``\\`` as a literal character, not a separator, so it can never produce
    a Windows-style escape).
    """
    if "\x00" in relative_path:
        raise NulPathError()
    p = Path(relative_path)
    if p.is_absolute() or relative_path.startswith("/"):
        raise AbsolutePathError()
    if relative_path.startswith("~"):
        raise AbsolutePathError()
    # split on "/" only; backslash is a literal filename character on Linux
    raw_parts = relative_path.split("/")
    parts: list[str] = []
    for seg in raw_parts:
        if seg in ("", "."):
            continue
        if seg == "..":
            if not parts:
                raise PathOutsideWorkdirError()
            parts.pop()
            continue
        parts.append(seg)
    return tuple(parts)


class ResolvedPath:
    """A policy-validated workdir-relative path.

    Carries the access policy it was validated under (allow_hidden + deny
    rules); operations and listings below it must consult the same policy
    so every channel filters identically.
    """

    def __init__(
        self,
        workdir: Workdir,
        rel_parts: tuple[str, ...],
        *,
        allow_hidden: bool,
        deny_policy: DenyPolicy,
    ):
        self._workdir = workdir
        self._rel_parts = rel_parts
        self._allow_hidden = allow_hidden
        self._deny_policy = deny_policy

    @property
    def workdir(self) -> Workdir:
        return self._workdir

    @property
    def rel_parts(self) -> tuple[str, ...]:
        return self._rel_parts

    @property
    def rel_path(self) -> str:
        return "/".join(self._rel_parts)

    @property
    def allow_hidden(self) -> bool:
        return self._allow_hidden

    @property
    def deny_policy(self) -> DenyPolicy:
        return self._deny_policy

    @property
    def container_path(self) -> Path:
        path = self._workdir.container_path
        for seg in self._rel_parts:
            path = path / seg
        return path

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ResolvedPath {self._workdir.alias}:{self.rel_path!r}>"


def resolve_workdir_path(
    workdir: Workdir,
    relative_path: str,
    *,
    allow_hidden: bool,
    deny_policy: DenyPolicy | None = None,
) -> ResolvedPath:
    """Validate a client path against every policy rule (no FS access).

    Symlink enforcement is NOT done here: it happens atomically at open
    time via fdio's O_NOFOLLOW component walk.
    """
    policy = deny_policy if deny_policy is not None else DenyPolicy()
    rel_parts = _split_relative_path(relative_path)

    if is_reserved_path(rel_parts):
        raise ReservedPathError()
    if not allow_hidden and any(is_hidden_component(c) for c in rel_parts):
        raise HiddenPathNotAllowedError()
    if policy.is_denied(rel_parts):
        raise DeniedPathError()

    return ResolvedPath(workdir, rel_parts, allow_hidden=allow_hidden, deny_policy=policy)
