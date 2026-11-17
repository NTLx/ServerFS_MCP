"""Conservative cleanup of an inactive macOS v0.25 named owned candidate.

No auto-stop and no wildcard deletion. Refuse cleanup on any registered job,
live PID, active/stale socket, nonempty task state, log or unknown file.
The default installation and other instances are never addressed.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .darwin_instance_bridge_identity import named_owned_bridge_resources
from .darwin_instance_owned_lifecycle import _load_owned_manifest, _owned, _status
from .darwin_instance_runtime import (
    _check_dir,
    _check_private_file,
    _manager,
    darwin_service,
)
from .darwin_instances import APP_NAME, instance_service_home
from .instance_identity import InstanceIdentity


class DarwinOwnedUnstageError(ValueError):
    """Named Bridge state must be preserved until it is proven safe to remove."""


def unstage_owned_candidate(
    identity: InstanceIdentity, *, home: Path | None = None
) -> dict[str, str]:
    """Remove only an untouched, completely stopped owned installation."""
    _owned(identity)
    root = instance_service_home(identity, home=home)
    _check_dir(root, private=True)
    _check_private_file(root / "services.json")
    try:
        document = _load_owned_manifest(identity, home)
    except ValueError as exc:
        raise DarwinOwnedUnstageError("owned manifest identity is untrusted") from exc
    for component in ("core", "bridge"):
        if _status(identity, component, home) not in {"not-registered", "not-found"}:
            raise DarwinOwnedUnstageError("all named jobs must be unregistered before unstage")
        if darwin_service._launchd_job_pid(identity.macos_label(component)) is not None:
            raise DarwinOwnedUnstageError("named service process remains active")
    for key in document["services"]:
        if key.startswith("cloudflare."):
            provider, connector_id = "cloudflare", key.removeprefix("cloudflare.")
        elif key.startswith("tunnel."):
            provider, connector_id = "openai", key.removeprefix("tunnel.")
        else:
            continue
        if _manager(identity, "status", key, home=home) not in {
            "not-registered",
            "not-found",
        }:
            raise DarwinOwnedUnstageError(
                "all named Connector jobs must be unregistered before unstage"
            )
        if (
            darwin_service._launchd_job_pid(identity.macos_label(provider, connector_id))
            is not None
        ):
            raise DarwinOwnedUnstageError("named Connector process remains active")
    socket = named_owned_bridge_resources(identity, home=home).socket_path
    if socket.exists() or socket.is_symlink() or socket.parent.exists():
        raise DarwinOwnedUnstageError("named Bridge socket namespace is still present")
    if {entry.name for entry in root.iterdir()} != {
        "services.json",
        "http-origin-environment.json",
        "logs",
        "data",
        APP_NAME,
    }:
        raise DarwinOwnedUnstageError("unexpected named service state; cleanup refused")
    _check_dir(root / "logs", private=True)
    _check_dir(root / "data", private=True)
    _check_dir(root / "data/agent-bridge", private=True)
    _check_dir(root / "data/agent-bridge/state", private=True)
    _check_dir(root / "data/agent-bridge/locks", private=True)
    _check_private_file(root / "http-origin-environment.json")
    _check_private_file(root / "data/agent-bridge/bridge.json")
    if (
        any((root / "logs").iterdir())
        or {entry.name for entry in (root / "data").iterdir()} != {"agent-bridge"}
        or {entry.name for entry in (root / "data/agent-bridge").iterdir()}
        != {"bridge.json", "state", "locks"}
        or any((root / "data/agent-bridge/state").iterdir())
        or any((root / "data/agent-bridge/locks").iterdir())
        or {entry.name for entry in root.parent.iterdir()} != {"service-manager"}
    ):
        raise DarwinOwnedUnstageError(
            "owned Bridge state, logs or unexpected files must be retained"
        )
    # Verify the signed app is exactly the intended instance, before any
    # deletion; no service-manager operation other than the statuses above.
    from .darwin_instance_runtime import _manager_path

    _manager_path(identity, home=home)
    (root / "data/agent-bridge/bridge.json").unlink()
    (root / "data/agent-bridge/locks").rmdir()
    (root / "data/agent-bridge/state").rmdir()
    (root / "data/agent-bridge").rmdir()
    (root / "data").rmdir()
    (root / "logs").rmdir()
    (root / "services.json").unlink()
    (root / "http-origin-environment.json").unlink()
    shutil.rmtree(root / APP_NAME)
    root.rmdir()
    root.parent.rmdir()
    return {"instance_id": identity.instance_id, "state": "unstaged"}
