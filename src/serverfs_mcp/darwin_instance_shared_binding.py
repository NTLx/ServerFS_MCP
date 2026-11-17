"""Read-only source preflight for an explicitly owner-configured Shared Bridge.

This module defines no persistence format and is not called by Core startup.
Credential provisioning, grant persistence, and Shared Bridge lifecycle remain
separately gated.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

from .instance_identity import DEFAULT_INSTANCE_ID, InstanceIdentity
from .native_config import load_native_config

_ID = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?\Z")
_ALIAS = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class DarwinSharedBindingError(ValueError):
    """The explicit Shared Bridge binding cannot be proven safe."""


@dataclass(frozen=True)
class SharedBridgeGrant:
    """Owner-supplied immutable grant evidence; secret material is never shown."""

    bridge_id: str
    core_id: str
    key_id: str
    secret: bytes = field(repr=False)
    workdir_alias: str
    workdir_path: Path
    policy_revision: str


@dataclass(frozen=True)
class SharedBridgeBinding:
    """Explicit Core-to-Bridge inputs; this is not a persisted manifest."""

    bridge_config_path: Path
    socket_path: Path
    grant: SharedBridgeGrant
    core_policy_revision: str


def preflight_shared_core_binding(
    identity: InstanceIdentity,
    core_config_path: Path,
    binding: SharedBridgeBinding,
) -> Path:
    """Validate a named Core's owner-supplied v2 binding without side effects.

    The Bridge owner must supply already trusted grant metadata and the full
    Bridge policy revision for the selected workdir. No credential is loaded
    from environment files, generated, persisted, or included in diagnostics.
    """
    try:
        if (
            not isinstance(identity, InstanceIdentity)
            or identity.instance_id == DEFAULT_INSTANCE_ID
            or identity.bridge_mode != "shared"
            or not isinstance(identity.bridge_id, str)
            or not _ID.fullmatch(identity.bridge_id)
        ):
            raise DarwinSharedBindingError("named shared Bridge identity required")
        if not isinstance(binding, SharedBridgeBinding) or not isinstance(
            binding.grant, SharedBridgeGrant
        ):
            raise DarwinSharedBindingError("explicit shared Bridge grant required")
        grant = binding.grant
        if (
            grant.bridge_id != identity.bridge_id
            or grant.core_id != identity.instance_id
            or not _ID.fullmatch(grant.bridge_id)
            or not _ID.fullmatch(grant.core_id)
            or not _ID.fullmatch(grant.key_id)
            or not isinstance(grant.secret, bytes)
            or len(grant.secret) != 32
            or not isinstance(grant.workdir_alias, str)
            or not _ALIAS.fullmatch(grant.workdir_alias)
            or not isinstance(grant.policy_revision, str)
            or not _DIGEST.fullmatch(grant.policy_revision)
            or not isinstance(binding.core_policy_revision, str)
            or not _DIGEST.fullmatch(binding.core_policy_revision)
        ):
            raise DarwinSharedBindingError("shared Bridge grant identity is invalid")

        bridge_config_path = _safe_regular_private_file(binding.bridge_config_path)
        bridge_config = json.loads(bridge_config_path.read_text(encoding="utf-8"))
        if (
            not isinstance(bridge_config, dict)
            or bridge_config.get("protocol_mode") != "v2"
            or bridge_config.get("bridge_id") != identity.bridge_id
            or bridge_config.get("socket_path") != str(binding.socket_path)
        ):
            raise DarwinSharedBindingError("shared Bridge configuration identity mismatch")

        socket_path = Path(binding.socket_path)
        if not socket_path.is_absolute() or any(p.is_symlink() for p in socket_path.parents):
            raise DarwinSharedBindingError("shared Bridge socket path is unsafe")
        socket_info = socket_path.stat(follow_symlinks=False)
        if (
            not stat.S_ISSOCK(socket_info.st_mode)
            or socket_info.st_uid != os.getuid()
            or stat.S_IMODE(socket_info.st_mode) & 0o077
        ):
            raise DarwinSharedBindingError("shared Bridge socket type or permissions are unsafe")

        config_path = Path(core_config_path).expanduser()
        core_config_path = _safe_regular_file(config_path)
        workdirs, _ = load_native_config(core_config_path)
        matches = [item for item in workdirs if item.alias == grant.workdir_alias]
        if len(matches) != 1:
            raise DarwinSharedBindingError("shared Bridge workdir alias is not configured")
        core_workdir = Path(matches[0].root).resolve(strict=True)
        bridge_workdir = Path(grant.workdir_path)
        if (
            not bridge_workdir.is_absolute()
            or bridge_workdir.is_symlink()
            or any(parent.is_symlink() for parent in bridge_workdir.parents)
            or bridge_workdir.resolve(strict=True) != core_workdir
            or not core_workdir.is_dir()
        ):
            raise DarwinSharedBindingError("shared Bridge workdir identity mismatch")
        # This revision must come from the trusted owner-side canonical
        # core_workdir_policy_digest calculation. Do not substitute a Core
        # TOML hash: the Bridge digest covers effective policy and host root.
        if grant.policy_revision != binding.core_policy_revision:
            raise DarwinSharedBindingError("shared Bridge policy revision is stale")
        return socket_path
    except DarwinSharedBindingError:
        raise
    except (OSError, ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        raise DarwinSharedBindingError("shared Bridge binding cannot be verified") from None


def _safe_regular_private_file(path: Path) -> Path:
    path = Path(path).expanduser()
    if not path.is_absolute() or any(parent.is_symlink() for parent in path.parents):
        raise DarwinSharedBindingError("shared Bridge input path is unsafe")
    info = path.stat(follow_symlinks=False)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise DarwinSharedBindingError("shared Bridge input file is not private")
    return path


def _safe_regular_file(path: Path) -> Path:
    path = Path(path).expanduser()
    if not path.is_absolute() or any(parent.is_symlink() for parent in path.parents):
        raise DarwinSharedBindingError("shared Bridge input path is unsafe")
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise DarwinSharedBindingError("shared Bridge input file is unavailable")
    return path
