"""Path safety layer: the single entry point for all filesystem access.

Every tool and the resource template resolve paths through
``resolve_workdir_path``. Nothing else in the codebase may join workdir roots
with user input.

Policy (v0.1):
- relative paths only, NUL rejected
- ``..`` segments are normalized, result must stay inside the workdir
- symlinks are not followed: a symlink anywhere on the path is rejected
- hidden path components (leading ``.``) are rejected unless allow_hidden
- credential-like names are always denied (defense in depth)
- only regular files and directories are accessible
"""

from __future__ import annotations

import fnmatch
import os
import stat as stat_module
from pathlib import Path

from .models import WorkdirInfo  # noqa: F401  (re-exported types live in models)
from .workdirs import Workdir

# credential-like patterns that are always denied, even with allow_hidden=true
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


class PathSecurityError(Exception):
    """Base class for anticipated path-policy violations."""

    code = "ACCESS_DENIED"
    message = "path is not accessible"


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


class UnsupportedFileTypeError(PathSecurityError):
    code = "UNSUPPORTED_FILE_TYPE"
    message = "only regular files and directories are supported"


def _is_hidden_component(component: str) -> bool:
    return component.startswith(".") and component not in (".", "..")


def _is_denied(rel_parts: tuple[str, ...]) -> bool:
    """True when any component of the relative path matches deny rules."""
    if not rel_parts:
        return False
    *dirs, base = rel_parts
    for d in dirs:
        if d in DEFAULT_DENY_DIR_NAMES:
            return True
    if base in DEFAULT_DENY_BASENAMES:
        return True
    return any(fnmatch.fnmatchcase(base, g) for g in DEFAULT_DENY_GLOBS)


def _split_relative_path(relative_path: str) -> tuple[str, ...]:
    """Split a client-relative path into normalized components.

    Raises on absolute paths, NUL bytes, and backslash tricks (we treat ``\\``
    as a literal character, not a separator, so it can never produce a
    Windows-style escape).
    """
    if "\x00" in relative_path:
        raise NulPathError()
    p = Path(relative_path)
    if p.is_absolute() or relative_path.startswith("/"):
        raise AbsolutePathError()
    if relative_path.startswith("~"):
        raise AbsolutePathError()
    # split on "/" only; posixPurePath handles this natively for POSIX
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
    """A validated workdir-relative path with its container path."""

    def __init__(self, workdir: Workdir, rel_parts: tuple[str, ...]):
        self._workdir = workdir
        self._rel_parts = rel_parts

    @property
    def workdir(self) -> Workdir:
        return self._workdir

    @property
    def rel_path(self) -> str:
        return "/".join(self._rel_parts)

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
) -> ResolvedPath:
    """Validate a client path against every safety policy.

    Symlink check strategy: walk each existing prefix component with
    ``os.lstat``; if any component is a symlink, reject. This is done before
    any ``stat``/``open`` so symlink targets are never touched. The workdir
    root itself is always a bind mount and never a symlink in production.
    """
    rel_parts = _split_relative_path(relative_path)

    if not allow_hidden and any(_is_hidden_component(c) for c in rel_parts):
        raise HiddenPathNotAllowedError()
    if _is_denied(rel_parts):
        raise DeniedPathError()

    resolved = ResolvedPath(workdir, rel_parts)

    # symlink check, component by component (lstat, never follow)
    current = workdir.container_path
    for seg in rel_parts:
        current = current / seg
        try:
            st = os.lstat(current)
        except FileNotFoundError:
            # missing components are fine here; the caller's stat/open will
            # surface a proper PATH_NOT_FOUND
            return resolved
        except OSError as exc:
            raise PathSecurityError(f"cannot access path: {exc.strerror}") from exc
        if stat_module.S_ISLNK(st.st_mode):
            raise SymlinkNotAllowedError()

    return resolved
