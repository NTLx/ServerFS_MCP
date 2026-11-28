"""Conservative cleanup for an inactive macOS Shared Core candidate.

Never unregister a service, rotate a grant, or touch the Bridge owner. Preserve
runtime data, logs, unexpected files, and any candidate with a damaged signature.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .darwin_instance_runtime import (
    _check_dir,
    _check_private_file,
    _manager,
    _manager_path,
    darwin_service,
)
from .darwin_instance_shared_lifecycle import (
    DarwinSharedLifecycleError,
    _require_shared,
    _shared_start_lock,
    _verify_shared_start_lock,
)
from .darwin_instance_shared_manifest import _read_json
from .darwin_instances import APP_NAME, instance_service_home
from .instance_identity import InstanceIdentity


class DarwinSharedUnstageError(ValueError):
    """Named Shared Core state is active or cannot be safely discarded."""


def _require_inactive_core(identity: InstanceIdentity, home: Path | None) -> None:
    """Require a known unregistered state and no exact Core launchd job."""
    try:
        status = _manager(identity, "status", "http-origin", home=home)
    except Exception:
        raise DarwinSharedUnstageError(
            "named Shared Core registration state is unavailable"
        ) from None
    if type(status) is not str:
        raise DarwinSharedUnstageError("named Shared Core registration state is malformed")
    if status not in {"not-registered", "not-found"}:
        raise DarwinSharedUnstageError("unregister the named Core before unstage")
    try:
        present = darwin_service._launchd_job_present(identity.macos_label("core"))
    except Exception:
        raise DarwinSharedUnstageError("named Shared Core process state is unavailable") from None
    if present is not False:
        raise DarwinSharedUnstageError("named Shared Core process remains active")


def unstage_shared_core(identity: InstanceIdentity, *, home: Path | None = None) -> dict[str, str]:
    """Unstage only a pristine, unregistered named Core and no other resources."""
    _require_shared(identity)
    root = instance_service_home(identity, home=home)
    try:
        lock = _shared_start_lock(identity, home)
        with lock as lock_fd:
            _check_dir(root.parent, private=True)
            _check_dir(root, private=True)
            manifest = _read_json(root / "services.json")
            services = manifest.get("services")
            if (
                type(manifest.get("schema_version")) is not int
                or manifest["schema_version"] != 1
                or manifest.get("owner") != "v0.25-named-shared-core"
                or manifest.get("instance_id") != identity.instance_id
                or manifest.get("bridge_mode") != "shared"
                or manifest.get("bridge_id") != identity.bridge_id
                or not isinstance(services, dict)
                or set(services) != {"http-origin"}
            ):
                raise DarwinSharedUnstageError("named Shared Core manifest is untrusted")
            _require_inactive_core(identity, home)
            expected = {
                "services.json",
                "shared-bridge-binding.json",
                "http-origin-environment.json",
                "logs",
                "data",
                APP_NAME,
            }
            if {item.name for item in root.iterdir()} != expected:
                raise DarwinSharedUnstageError("unknown Core state must be retained")
            if {item.name for item in root.parent.iterdir()} != {"service-manager"}:
                raise DarwinSharedUnstageError("unknown instance files must be retained")
            for directory in (root / "logs", root / "data"):
                _check_dir(directory, private=True)
                if any(directory.iterdir()):
                    raise DarwinSharedUnstageError("Core logs or runtime state must be retained")
            for name in (
                "services.json",
                "shared-bridge-binding.json",
                "http-origin-environment.json",
            ):
                _check_private_file(root / name)
            _manager_path(identity, home)
            _require_inactive_core(identity, home)
            _verify_shared_start_lock(identity, home, lock_fd)
            (root / "services.json").unlink()
            (root / "shared-bridge-binding.json").unlink()
            (root / "http-origin-environment.json").unlink()
            (root / "logs").rmdir()
            (root / "data").rmdir()
            shutil.rmtree(root / APP_NAME)
            root.rmdir()
            root.parent.rmdir()
    except DarwinSharedLifecycleError:
        raise DarwinSharedUnstageError("named Shared Core start lock is unavailable") from None
    return {
        "instance_id": identity.instance_id,
        "bridge_mode": "shared",
        "state": "unstaged",
    }
