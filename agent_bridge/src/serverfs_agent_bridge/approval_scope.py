"""Conservative, structured write-target screening for remote Agent approvals.

This is an approval admission boundary, not a provider sandbox. It cannot
infer the effects of arbitrary commands, MCP tools or provider free text.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

_FILE_TARGET_KEYS = ("path", "file_path", "notebook_path", "notebookPath", "grantRoot")
_WRITE_SCOPE_KEYS = ("write", "writableRoots", "writable_roots", "writeRoots")


def _inside_root(raw: object, root: Path, cwd: Path) -> bool:
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        return False
    try:
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = cwd / candidate
        resolved = candidate.resolve(strict=False)
        return resolved == root or root in resolved.parents
    except (OSError, ValueError, RuntimeError):
        return False


def _file_targets(payload: dict[str, Any]) -> list[object]:
    targets: list[object] = []
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict):
        targets.extend(tool_input[key] for key in _FILE_TARGET_KEYS if key in tool_input)
    files = payload.get("file_changes")
    if isinstance(files, list):
        for item in files:
            if not isinstance(item, dict):
                return [None]
            targets.extend(item[key] for key in _FILE_TARGET_KEYS if key in item)
            if not any(key in item for key in _FILE_TARGET_KEYS):
                targets.append(None)
    elif files is not None:
        return [None]
    return targets


def outside_workdir_write(payload: dict[str, Any], root: Path, relative_cwd: str) -> bool:
    """True only for explicit file mutations or filesystem write grants.

    Missing/unparseable file targets fail closed. Read-only grants, terminal
    command requests and other provider operations retain their existing
    native approval semantics; these cannot be safely classified by string
    matching against arbitrary command text.
    """
    category = payload.get("category")
    if category not in {"file_change", "file_changes", "provider_permission"}:
        return False
    try:
        trusted_root = root.resolve(strict=True)
        cwd = (trusted_root / relative_cwd).resolve(strict=False)
        if cwd != trusted_root and trusted_root not in cwd.parents:
            return True
    except (OSError, RuntimeError, ValueError):
        return True
    if category in {"file_change", "file_changes"}:
        targets = _file_targets(payload)
        return not targets or any(not _inside_root(path, trusted_root, cwd) for path in targets)
    permissions = payload.get("requested_permissions")
    if not isinstance(permissions, list):
        return True
    for permission in permissions:
        if not isinstance(permission, dict):
            return True
        details = permission.get("details")
        if not isinstance(details, dict):
            continue
        for key in _WRITE_SCOPE_KEYS:
            if key not in details:
                continue
            locations = details[key]
            if isinstance(locations, str):
                locations = [locations]
            if not isinstance(locations, list):
                return True
            if any(not _inside_root(path, trusted_root, cwd) for path in locations):
                return True
    return False
