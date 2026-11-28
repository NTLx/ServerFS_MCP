"""Create-only Core-side binding for a named owned Bridge v2 grant set."""

from __future__ import annotations

import re
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .darwin_instance_runtime import _check_dir, _check_private_file, _new_private_file
from .darwin_instances import instance_service_home
from .instance_identity import DEFAULT_INSTANCE_ID, InstanceIdentity

_ALIAS = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}\Z")
_ID = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_FILENAME = "owned-bridge-v2-binding.json"


@dataclass(frozen=True)
class OwnedV2GrantMaterial:
    alias: str
    key_id: str
    secret: bytes = field(repr=False)

    def bridge_input(self) -> dict[str, str]:
        return {"alias": self.alias, "key_id": self.key_id, "secret_hex": self.secret.hex()}


def _owned_identity(identity: InstanceIdentity) -> None:
    if (
        not isinstance(identity, InstanceIdentity)
        or identity.instance_id == DEFAULT_INSTANCE_ID
        or identity.bridge_mode != "owned"
        or identity.bridge_id != identity.instance_id
    ):
        raise ValueError("named owned Bridge identity is invalid")


def generate_owned_v2_grant_materials(
    identity: InstanceIdentity, aliases: Sequence[str]
) -> tuple[OwnedV2GrantMaterial, ...]:
    """Generate one independent key per exact Agent-enabled alias."""
    _owned_identity(identity)
    if not isinstance(aliases, Sequence) or isinstance(aliases, (str, bytes)):
        raise ValueError("owned Bridge aliases are invalid")
    if not 1 <= len(aliases) <= 16 or any(
        not isinstance(alias, str) or not _ALIAS.fullmatch(alias) for alias in aliases
    ):
        raise ValueError("owned Bridge aliases are invalid")
    if len(set(aliases)) != len(aliases):
        raise ValueError("owned Bridge aliases are invalid")
    return tuple(
        OwnedV2GrantMaterial(
            alias=alias,
            key_id="own-" + secrets.token_hex(8),
            secret=secrets.token_bytes(32),
        )
        for alias in aliases
    )


def write_owned_v2_binding(
    identity: InstanceIdentity,
    *,
    home: Path | None,
    bridge_config_path: Path,
    socket_path: Path,
    agent_workdirs: Mapping[str, Path],
    materials: Sequence[OwnedV2GrantMaterial],
    grant_scopes: Sequence[Mapping[str, str]],
) -> Path:
    """Persist matching per-alias private material without replacing an owner file."""
    _owned_identity(identity)
    if not isinstance(agent_workdirs, Mapping) or not isinstance(materials, Sequence):
        raise ValueError("owned Bridge binding scope is invalid")
    if not isinstance(grant_scopes, Sequence) or len(materials) != len(grant_scopes):
        raise ValueError("owned Bridge binding scope is invalid")
    aliases = set(agent_workdirs)
    if not 1 <= len(aliases) <= 16 or aliases != {item.alias for item in materials}:
        raise ValueError("owned Bridge binding aliases do not match Agent policy")
    if (
        not isinstance(bridge_config_path, Path)
        or not bridge_config_path.is_absolute()
        or not isinstance(socket_path, Path)
        or not socket_path.is_absolute()
    ):
        raise ValueError("owned Bridge binding paths are invalid")

    material_by_alias: dict[str, OwnedV2GrantMaterial] = {}
    seen_keys: set[str] = set()
    seen_secrets: set[bytes] = set()
    for item in materials:
        if (
            not isinstance(item, OwnedV2GrantMaterial)
            or not _ALIAS.fullmatch(item.alias)
            or not _ID.fullmatch(item.key_id)
            or len(item.secret) != 32
            or item.alias in material_by_alias
            or item.key_id in seen_keys
            or item.secret in seen_secrets
        ):
            raise ValueError("owned Bridge binding grant material is invalid")
        material_by_alias[item.alias] = item
        seen_keys.add(item.key_id)
        seen_secrets.add(item.secret)
    scopes_by_alias: dict[str, Mapping[str, str]] = {}
    for item in grant_scopes:
        if (
            not isinstance(item, Mapping)
            or set(item) != {"alias", "key_id", "policy_revision"}
            or not isinstance(item.get("alias"), str)
            or not isinstance(item.get("key_id"), str)
            or not isinstance(item.get("policy_revision"), str)
        ):
            raise ValueError("owned Bridge binding grant scope is invalid")
        alias = item["alias"]
        material = material_by_alias.get(alias)
        if (
            not _ALIAS.fullmatch(alias)
            or alias in scopes_by_alias
            or material is None
            or item["key_id"] != material.key_id
        ):
            raise ValueError("owned Bridge binding grant scope is invalid")
        if not _DIGEST.fullmatch(item["policy_revision"]):
            raise ValueError("owned Bridge binding grant scope is invalid")
        scopes_by_alias[alias] = item
    if set(scopes_by_alias) != aliases:
        raise ValueError("owned Bridge binding grant scopes do not match Agent policy")

    workdirs: list[dict[str, str]] = []
    for alias in agent_workdirs:
        if not isinstance(alias, str) or not _ALIAS.fullmatch(alias):
            raise ValueError("owned Bridge binding alias is invalid")
        path = agent_workdirs[alias]
        if not isinstance(path, Path) or not path.is_absolute() or path.is_symlink():
            raise ValueError("owned Bridge workdir binding path is invalid")
        resolved = path.resolve(strict=True)
        if not resolved.is_dir() or resolved != path:
            raise ValueError("owned Bridge workdir binding path is invalid")
        material = material_by_alias[alias]
        scope = scopes_by_alias[alias]
        workdirs.append(
            {
                "alias": alias,
                "path": str(resolved),
                "key_id": material.key_id,
                "secret_hex": material.secret.hex(),
                "policy_revision": scope["policy_revision"],
            }
        )

    root = instance_service_home(identity, home=home)
    _check_dir(root, private=True)
    binding_path = root / _FILENAME
    import json

    payload = {
        "schema_version": 1,
        "owner": "v0.26-named-owned-core-v2",
        "instance_id": identity.instance_id,
        "core_id": identity.instance_id,
        "bridge_id": identity.instance_id,
        "bridge_config_path": str(bridge_config_path),
        "socket_path": str(socket_path),
        "workdirs": workdirs,
    }
    _new_private_file(
        binding_path,
        (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(),
    )
    _check_private_file(binding_path)
    return binding_path
