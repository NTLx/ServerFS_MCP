"""Fail-closed loader and bootstrap for an owner-provisioned Linux Core grant."""

from __future__ import annotations

import asyncio
import json
import os
import re
import stat
from pathlib import Path
from typing import Any

from .core_bridge_client_v2 import (
    CoreBridgeClientV2,
    CoreBridgeClientV2Error,
    CoreBridgeRemoteError,
    CoreBridgeUnavailable,
)
from .instance_identity import DEFAULT_INSTANCE_ID, valid_id
from .linux_v0250_bridge_adapter import LinuxV0250BridgeAdapter

_MAX_BYTES = 16 * 1024
_FILENAME = "core-bridge-binding.json"
_FIELDS = frozenset(
    {
        "schema_version",
        "instance_id",
        "core_id",
        "bridge_id",
        "key_id",
        "secret_hex",
        "workdir_alias",
        "workdir_path",
        "policy_revision",
        "socket_path",
    }
)
_ALIAS = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class LinuxCoreBindingError(ValueError):
    """Private Linux v2 binding could not be validated or authenticated."""


class LinuxCoreBridgeUnavailable(LinuxCoreBindingError):
    """A valid local binding cannot currently reach its Bridge."""


def _deny(stage: str) -> LinuxCoreBindingError:
    # Stages are fixed local labels, never paths, credentials or remote error text.
    return LinuxCoreBindingError(f"Linux Core Bridge binding is invalid or unavailable ({stage})")


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise _deny("binding-document")
        result[key] = value
    return result


def _open_private_parent(path: Path) -> int:
    """Walk every parent component without following links; trust only a private leaf."""
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    fd = os.open(path.anchor, flags)
    try:
        for component in path.parts[1:-1]:
            next_fd = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        info = os.fstat(fd)
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise _deny("private-directory")
        return fd
    except Exception:
        os.close(fd)
        raise


def load_linux_core_client(
    binding_file: str, *, instance_id: str, bridge_id: str, socket_path: str, workdirs: list[Any]
) -> tuple[CoreBridgeClientV2, str]:
    """Open one existing private grant and bind it to one enabled workdir."""
    fd: int | None = None
    parent_fd: int | None = None
    stage = "binding-path"
    try:
        path = Path(binding_file)
        if not path.is_absolute() or path.name != _FILENAME or ".." in path.parts:
            raise _deny(stage)
        stage = "instance-identity"
        if instance_id == DEFAULT_INSTANCE_ID or bridge_id == DEFAULT_INSTANCE_ID:
            raise _deny(stage)
        valid_id(instance_id)
        valid_id(bridge_id)
        stage = "socket-path"
        expected_socket = Path(socket_path)
        if not expected_socket.is_absolute():
            raise _deny(stage)
        stage = "private-directory"
        parent_fd = _open_private_parent(path)
        stage = "private-file"
        fd = os.open(
            path.name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=parent_fd,
        )
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) not in {0o400, 0o600}
            or info.st_nlink != 1
            or not 2 <= info.st_size <= _MAX_BYTES
        ):
            raise _deny(stage)
        raw = bytearray()
        while len(raw) <= _MAX_BYTES:
            part = os.read(fd, min(4096, _MAX_BYTES + 1 - len(raw)))
            if not part:
                break
            raw.extend(part)
        if len(raw) != info.st_size or len(raw) > _MAX_BYTES:
            raise _deny(stage)
        stage = "binding-document"
        doc = json.loads(bytes(raw).decode("utf-8"), object_pairs_hook=_pairs)
        if (
            not isinstance(doc, dict)
            or set(doc) != _FIELDS
            or type(doc["schema_version"]) is not int
            or doc["schema_version"] != 1
        ):
            raise _deny(stage)
        stage = "grant-identity"
        if (
            valid_id(doc["instance_id"]) != instance_id
            or valid_id(doc["core_id"]) != instance_id
            or valid_id(doc["bridge_id"]) != bridge_id
            or not isinstance(doc["key_id"], str)
            or valid_id(doc["key_id"]) != doc["key_id"]
        ):
            raise _deny(stage)
        stage = "grant-fields"
        alias, secret_hex, revision = (
            doc["workdir_alias"],
            doc["secret_hex"],
            doc["policy_revision"],
        )
        if (
            not isinstance(alias, str)
            or not _ALIAS.fullmatch(alias)
            or not isinstance(secret_hex, str)
            or not re.fullmatch(r"[0-9a-f]{64}", secret_hex)
            or not isinstance(revision, str)
            or not _DIGEST.fullmatch(revision)
        ):
            raise _deny(stage)
        stage = "socket-binding"
        grant_socket = Path(doc["socket_path"])
        if grant_socket != expected_socket:
            raise _deny(stage)
        stage = "workdir-binding"
        grant_path = Path(doc["workdir_path"])
        if not grant_path.is_absolute():
            raise _deny(stage)
        matches = [w for w in workdirs if (w.agent_mode != "disabled" or w.policy.repo_ops_enabled)]
        if len(matches) != 1 or matches[0].alias != alias or Path(matches[0].root) != grant_path:
            raise _deny(stage)
        stage = "client-construction"
        client = CoreBridgeClientV2(
            expected_socket,
            bridge_id=bridge_id,
            core_id=instance_id,
            key_id=doc["key_id"],
            secret=bytes.fromhex(secret_hex),
            workdir_alias=alias,
            policy_revision=revision,
            timeout_seconds=5,
        )
        return client, alias
    except LinuxCoreBindingError:
        raise
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, json.JSONDecodeError):
        raise _deny(stage) from None
    finally:
        if fd is not None:
            os.close(fd)
        if parent_fd is not None:
            os.close(parent_fd)


async def verify_linux_core_client(
    client: CoreBridgeClientV2, expected_socket: Path, *, timeout_seconds: float = 3.0
) -> None:
    """Require signed bridge.ping within a finite startup deadline."""
    if client._socket_path != expected_socket:
        raise _deny("socket-binding")
    try:
        async with asyncio.timeout(timeout_seconds):
            if await client.call("bridge.ping", {}) != {"status": "ok"}:
                raise _deny("ping-response")
    except CoreBridgeRemoteError:
        raise _deny("ping-rejected") from None
    except (CoreBridgeUnavailable, OSError, TimeoutError):
        raise LinuxCoreBridgeUnavailable(
            "Linux Core Bridge binding is invalid or unavailable (ping-unavailable)"
        ) from None
    except LinuxCoreBindingError:
        raise
    except (CoreBridgeClientV2Error, ValueError, TypeError):
        raise _deny("ping-response") from None


def create_linux_v0250_agent_adapter(
    client: CoreBridgeClientV2, alias: str
) -> LinuxV0250BridgeAdapter:
    return LinuxV0250BridgeAdapter(client, alias)
