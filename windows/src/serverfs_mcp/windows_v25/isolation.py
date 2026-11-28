"""Fail-closed collision checks for independent native Windows MCP Cores.

No host scanning or resource allocation: callers pass the authenticated,
private manifests loaded by Windows storage. This cannot replace the actual
kernel listener ownership proof when a service starts.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

from .spec import Manifest


class InstanceCollisionError(ValueError):
    """A Core identity, private input, or loopback port is already allocated."""


def _path_key(value: str) -> str:
    # Windows paths are case-insensitive even when tests run on other hosts.
    return os.path.normpath(str(Path(value))).replace("/", "\\").casefold()


def assert_core_isolation(candidate: Manifest, peers: Iterable[Manifest]) -> None:
    """Reject cross-installation resource reuse before configuration/start.

    An existing copy of the same installation is not a competing Core; the
    private Manifest loader and Task Scheduler XML validator still enforce
    exact ownership of that installation.
    """
    if candidate.component != "core":
        raise InstanceCollisionError("Core isolation needs a Core manifest")
    for other in peers:
        if other.component != "core":
            raise InstanceCollisionError("installed Core manifest inventory is invalid")
        if other.instance_id == candidate.instance_id:
            if other != candidate:
                raise InstanceCollisionError("Core instance identity is already allocated")
            continue  # Exact installed self is not a competing owner.
        if other.task_name().casefold() == candidate.task_name().casefold():
            raise InstanceCollisionError("Core task identity is already allocated")
        if other.settings["port"] == candidate.settings["port"]:
            raise InstanceCollisionError("Core loopback port is already allocated")
        if _path_key(other.pythonw) == _path_key(candidate.pythonw):
            raise InstanceCollisionError("Core interpreter is already allocated")
        for name in ("config", "auth_file"):
            if _path_key(str(other.settings[name])) == _path_key(str(candidate.settings[name])):
                raise InstanceCollisionError("Core private inputs must be independent")


def assert_connector_isolation(candidate: Manifest, peers: Iterable[Manifest]) -> None:
    """Reject duplicate Connector identities or private credentials.

    The manifest hostname is the *local Core HTTP Host*, not a Cloudflare
    public DNS name. Different Cores can legitimately use the same local Host
    on different loopback ports. Public hostname ownership must be checked
    separately against the configured Tunnel, not inferred from this field.
    """
    if candidate.component not in ("cloudflare", "openai"):
        raise InstanceCollisionError("connector isolation needs an ingress manifest")
    keys = {
        "cloudflare": ("token_file",),
        "openai": ("control_plane_key_file", "core_auth_header_file"),
    }
    candidate_secrets = {
        _path_key(str(candidate.settings[key])) for key in keys[candidate.component]
    }
    for other in peers:
        if other.component not in ("cloudflare", "openai"):
            raise InstanceCollisionError("installed connector manifest inventory is invalid")
        if (
            other.instance_id == candidate.instance_id
            and other.component == candidate.component
            and other.connector_id == candidate.connector_id
        ):
            if other != candidate:
                raise InstanceCollisionError("Connector instance identity is already allocated")
            continue  # Exact installed Connector is not a competing owner.
        if other.task_name().casefold() == candidate.task_name().casefold():
            raise InstanceCollisionError("Connector Scheduler task identity is allocated")
        if candidate.component == other.component == "openai":
            if candidate.settings["tunnel_id"] == other.settings["tunnel_id"]:
                raise InstanceCollisionError("OpenAI tunnel identity is already allocated")
        other_secrets = {_path_key(str(other.settings[key])) for key in keys[other.component]}
        if candidate_secrets.intersection(other_secrets):
            raise InstanceCollisionError("Connector credential file is already allocated")
