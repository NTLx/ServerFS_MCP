"""Read-only loader for an explicitly provisioned macOS Shared Bridge binding.

This private file is not an independent trust source for the policy digest and
does not prove that the Bridge owner has installed the matching grant. The
authenticated v2 IPC exchange is required for that proof. This module neither
provisions credentials nor participates in Core startup.
"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any

from .darwin_instance_shared_binding import (
    DarwinSharedBindingError,
    SharedBridgeBinding,
    SharedBridgeGrant,
)
from .instance_identity import DEFAULT_INSTANCE_ID, InstanceIdentity, valid_id

_FILENAME = "shared-bridge-binding.json"
_MAX_BYTES = 16 * 1024
_FIELDS = frozenset(
    {
        "schema_version",
        "bridge_id",
        "core_id",
        "key_id",
        "secret_hex",
        "workdir_alias",
        "workdir_path",
        "policy_revision",
        "bridge_config_path",
        "socket_path",
    }
)
_FIELDS_MULTI = _FIELDS - {"workdir_alias", "workdir_path", "policy_revision"} | {"workdirs"}
_WORKDIR_FIELDS = frozenset({"alias", "path", "policy_revision"})
_ALIAS = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def _deny() -> DarwinSharedBindingError:
    return DarwinSharedBindingError("owner-provisioned shared Bridge binding is invalid or unsafe")


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise _deny()
        result[key] = value
    return result


def _open_directory(path: Path) -> int:
    """Open an absolute directory one component at a time without following links."""
    if not path.is_absolute():
        raise _deny()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path.anchor, flags)
    try:
        for component in path.parts[1:]:
            next_fd = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        info = os.fstat(fd)
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise _deny()
        return fd
    except Exception:
        os.close(fd)
        raise


def load_shared_core_binding(
    *,
    identity: InstanceIdentity,
    trusted_core_home: Path,
    binding_path: Path,
    expected_socket_path: Path,
) -> SharedBridgeBinding:
    """Load a fixed, existing Core-owner binding without creating or repairing it.

    ``identity`` and ``expected_socket_path`` are independently supplied by the
    caller. ``trusted_core_home`` must also be independently pinned by the
    caller. This function checks its actual owner/mode and safe traversal, but
    does not prove it is the canonical service home for ``identity``.
    ``binding_path`` must name the one supported filename directly beneath it.
    """
    fd: int | None = None
    home_fd: int | None = None
    try:
        if (
            not isinstance(identity, InstanceIdentity)
            or identity.instance_id == DEFAULT_INSTANCE_ID
            or identity.bridge_mode != "shared"
            or identity.bridge_id is None
        ):
            raise _deny()
        valid_id(identity.instance_id)
        valid_id(identity.bridge_id)
        if not isinstance(trusted_core_home, Path) or not trusted_core_home.is_absolute():
            raise _deny()
        if (
            not isinstance(binding_path, Path)
            or binding_path != trusted_core_home / _FILENAME
            or binding_path.name != _FILENAME
        ):
            raise _deny()
        if not isinstance(expected_socket_path, Path) or not expected_socket_path.is_absolute():
            raise _deny()

        home_fd = _open_directory(trusted_core_home)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(_FILENAME, flags, dir_fd=home_fd)
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or stat.S_IMODE(opened.st_mode) != 0o600
            or not 2 <= opened.st_size <= _MAX_BYTES
        ):
            raise _deny()
        chunks = bytearray()
        while len(chunks) <= _MAX_BYTES:
            chunk = os.read(fd, min(4096, _MAX_BYTES + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
        if len(chunks) != opened.st_size or len(chunks) > _MAX_BYTES:
            raise _deny()
        document = json.loads(bytes(chunks).decode("utf-8"), object_pairs_hook=_pairs)
        if not isinstance(document, dict) or type(document.get("schema_version")) is not int:
            raise _deny()
        version = document["schema_version"]
        if (
            (version == 1 and set(document) != _FIELDS)
            or (version == 2 and set(document) != _FIELDS_MULTI)
            or version not in (1, 2)
        ):
            raise _deny()
        bridge_id = valid_id(document["bridge_id"])
        core_id = valid_id(document["core_id"])
        key_id = valid_id(document["key_id"])
        secret_hex = document["secret_hex"]
        if (
            bridge_id != identity.bridge_id
            or core_id != identity.instance_id
            or not isinstance(secret_hex, str)
            or not re.fullmatch(r"[0-9a-f]{64}", secret_hex)
        ):
            raise _deny()
        bridge_config_path = Path(document["bridge_config_path"])
        socket_path = Path(document["socket_path"])
        if (
            not bridge_config_path.is_absolute()
            or not socket_path.is_absolute()
            or socket_path != expected_socket_path
        ):
            raise _deny()
        if version == 1:
            entries = [
                {
                    "alias": document["workdir_alias"],
                    "path": document["workdir_path"],
                    "policy_revision": document["policy_revision"],
                }
            ]
        else:
            entries = document["workdirs"]
            if not isinstance(entries, list) or not 2 <= len(entries) <= 16:
                raise _deny()
        grants: list[SharedBridgeGrant] = []
        seen_aliases: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != _WORKDIR_FIELDS:
                raise _deny()
            alias, raw_path, revision = (
                entry["alias"],
                entry["path"],
                entry["policy_revision"],
            )
            if (
                not isinstance(alias, str)
                or not _ALIAS.fullmatch(alias)
                or alias in seen_aliases
                or not isinstance(raw_path, str)
                or not isinstance(revision, str)
                or not _DIGEST.fullmatch(revision)
            ):
                raise _deny()
            seen_aliases.add(alias)
            workdir_path = Path(raw_path)
            if not workdir_path.is_absolute():
                raise _deny()
            grants.append(
                SharedBridgeGrant(
                    bridge_id=bridge_id,
                    core_id=core_id,
                    key_id=key_id,
                    secret=bytes.fromhex(secret_hex),
                    workdir_alias=alias,
                    workdir_path=workdir_path,
                    policy_revision=revision,
                )
            )
        return SharedBridgeBinding(
            bridge_config_path=bridge_config_path,
            socket_path=socket_path,
            grant=grants[0],
            core_policy_revision=grants[0].policy_revision,
            grants=tuple(grants) if version == 2 else (),
        )
    except DarwinSharedBindingError:
        raise
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, json.JSONDecodeError):
        raise _deny() from None
    finally:
        if fd is not None:
            os.close(fd)
        if home_fd is not None:
            os.close(home_fd)
