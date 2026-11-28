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
from .linux_v0250_bridge_adapter import LinuxV0250BridgeAdapter, LinuxV026MultiBridgeAdapter

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
_MULTI_FIELDS = _FIELDS - {"workdir_alias", "workdir_path", "policy_revision"} | {"workdirs"}
_WORKDIR_FIELDS = frozenset({"alias", "workdir_path", "policy_revision"})
_CONTAINER_SLOT = re.compile(r"/workdirs/(0[1-9]|1[0-6])\Z")


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
) -> tuple[CoreBridgeClientV2 | dict[str, CoreBridgeClientV2], str | None]:
    """Open a private v1 single-alias or v2 multi-alias Core grant, fail closed."""
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
        if not isinstance(doc, dict) or type(doc.get("schema_version")) is not int:
            raise _deny(stage)
        version = doc["schema_version"]
        if (
            (version == 1 and set(doc) != _FIELDS)
            or (version == 2 and set(doc) != _MULTI_FIELDS)
            or version not in (1, 2)
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
        secret_hex = doc["secret_hex"]
        if not isinstance(secret_hex, str) or not re.fullmatch(r"[0-9a-f]{64}", secret_hex):
            raise _deny(stage)
        if version == 1:
            candidates = [
                {
                    "alias": doc["workdir_alias"],
                    "workdir_path": doc["workdir_path"],
                    "policy_revision": doc["policy_revision"],
                }
            ]
        else:
            candidates = doc["workdirs"]
            if not isinstance(candidates, list) or not 1 <= len(candidates) <= 16:
                raise _deny(stage)
        validated: dict[str, tuple[Path, str]] = {}
        slots: set[str] = set()
        for entry in candidates:
            if not isinstance(entry, dict) or set(entry) != _WORKDIR_FIELDS:
                raise _deny(stage)
            alias, path_value, revision = (
                entry["alias"],
                entry["workdir_path"],
                entry["policy_revision"],
            )
            if (
                not isinstance(alias, str)
                or not _ALIAS.fullmatch(alias)
                or alias in validated
                or not isinstance(path_value, str)
                or not isinstance(revision, str)
                or not _DIGEST.fullmatch(revision)
            ):
                raise _deny(stage)
            grant_path = Path(path_value)
            if not grant_path.is_absolute():
                raise _deny(stage)
            if version == 2:
                if not _CONTAINER_SLOT.fullmatch(path_value) or path_value in slots:
                    raise _deny(stage)
                slots.add(path_value)
            validated[alias] = (grant_path, revision)
        stage = "socket-binding"
        if Path(doc["socket_path"]) != expected_socket:
            raise _deny(stage)
        stage = "workdir-binding"
        enabled = [w for w in workdirs if w.agent_mode != "disabled" or w.policy.repo_ops_enabled]
        if len(enabled) != len(validated) or {w.alias for w in enabled} != set(validated):
            raise _deny(stage)
        if any(Path(w.root) != validated[w.alias][0] for w in enabled):
            raise _deny(stage)
        stage = "client-construction"
        clients = {
            alias: CoreBridgeClientV2(
                expected_socket,
                bridge_id=bridge_id,
                core_id=instance_id,
                key_id=doc["key_id"],
                secret=bytes.fromhex(secret_hex),
                workdir_alias=alias,
                policy_revision=revision,
                timeout_seconds=5,
            )
            for alias, (_, revision) in validated.items()
        }
        if len(clients) == 1:
            alias, client = next(iter(clients.items()))
            return client, alias
        return clients, None
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
    client: CoreBridgeClientV2 | dict[str, CoreBridgeClientV2],
    expected_socket: Path,
    *,
    timeout_seconds: float = 3.0,
) -> None:
    """Require signed bridge.ping within a finite startup deadline."""
    clients = tuple(client.values()) if isinstance(client, dict) else (client,)
    if not clients or any(c._socket_path != expected_socket for c in clients):
        raise _deny("socket-binding")
    try:
        async with asyncio.timeout(timeout_seconds):
            for signed_client in clients:
                if await signed_client.call("bridge.ping", {}) != {"status": "ok"}:
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
    client: CoreBridgeClientV2 | dict[str, CoreBridgeClientV2], alias: str | None
) -> LinuxV0250BridgeAdapter | LinuxV026MultiBridgeAdapter:
    if isinstance(client, dict):
        if alias is not None:
            raise _deny("client-construction")
        return LinuxV026MultiBridgeAdapter(client)
    if alias is None:
        raise _deny("client-construction")
    return LinuxV0250BridgeAdapter(client, alias)
