"""Scoped macOS lifecycle for an already provisioned named Shared Bridge Core.

A Shared Core owns only its HTTP origin. Its Bridge, grants, signing keys,
listeners and any peer Cores remain exclusively under their own owner.
No staging, secret generation, hot reload or implicit grant changes occur here.
"""

from __future__ import annotations

import asyncio
import os
import stat
from contextlib import contextmanager
from pathlib import Path

from .darwin_instance_runtime import (
    _assert_available_port,
    _manager,
    _manager_path,
    _wait_component_stopped,
    darwin_service,
)
from .darwin_instance_shared_manifest import (
    DarwinSharedManifestError,
    resolve_shared_core_manifest,
)
from .darwin_instances import (
    _read_plist,
    _validate_shared_core_python,
    instance_app_path,
    instance_service_home,
)
from .darwin_shared_core_bootstrap import SharedCoreBootstrapError, prepare_shared_v2_client
from .darwin_shared_runtime import (
    DarwinSharedRuntimeError,
    verify_managed_shared_core_python,
)
from .instance_identity import InstanceIdentity


class DarwinSharedLifecycleError(ValueError):
    """A named Shared Core cannot be operated without a verified owner."""


def _shared_launchd_job_absent(label: str, *, context: str) -> bool:
    """Require strict launchd presence evidence; only literal False is absent."""
    try:
        present = darwin_service._launchd_job_present(label)
    except Exception:
        raise DarwinSharedLifecycleError(
            f"named shared Core {context} launchd presence could not be verified"
        ) from None
    if present is not False:
        raise DarwinSharedLifecycleError(
            f"named shared Core {context} launchd presence is indeterminate"
        )
    return True


@contextmanager
def _shared_start_lock(identity: InstanceIdentity, home: Path | None):
    """Lock shared-start callers through the existing private manifest inode."""
    import fcntl

    path = instance_service_home(identity, home=home) / "services.json"
    fd: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(path, flags)
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or stat.S_IMODE(opened.st_mode) & 0o077
        ):
            raise OSError("unsafe shared manifest lock inode")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        current = os.stat(path, follow_symlinks=False)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_dev != opened.st_dev
            or current.st_ino != opened.st_ino
        ):
            raise OSError("shared manifest lock inode changed")
    except Exception:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        raise DarwinSharedLifecycleError("named shared Core start lock is unavailable") from None
    try:
        yield fd
    finally:
        os.close(fd)


def _verify_shared_start_lock(identity: InstanceIdentity, home: Path | None, fd: int) -> None:
    """Require services.json to remain the private inode held by the caller."""
    path = instance_service_home(identity, home=home) / "services.json"
    try:
        opened = os.fstat(fd)
        current = os.stat(path, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or stat.S_IMODE(opened.st_mode) & 0o077
            or not stat.S_ISREG(current.st_mode)
            or current.st_dev != opened.st_dev
            or current.st_ino != opened.st_ino
        ):
            raise OSError("shared manifest lock inode changed")
    except Exception:
        raise DarwinSharedLifecycleError("named shared Core start lock is unavailable") from None


def _rollback_registered_core(identity: InstanceIdentity, home: Path | None) -> None:
    try:
        state = _manager(identity, "unregister", "http-origin", home=home)
    except Exception:
        raise DarwinSharedLifecycleError(
            "named shared Core rollback could not unregister safely"
        ) from None
    if not isinstance(state, str) or state not in {"not-registered", "not-found"}:
        raise DarwinSharedLifecycleError("named shared Core did not reach unregistered state")
    try:
        _wait_component_stopped(identity, "core", timeout=12.0)
        terminal = _manager(identity, "status", "http-origin", home=home)
        pid = darwin_service._launchd_job_pid(identity.macos_label("core"))
        _shared_launchd_job_absent(identity.macos_label("core"), context="rollback")
    except Exception:
        raise DarwinSharedLifecycleError(
            "named shared Core rollback state could not be verified"
        ) from None
    if (
        not isinstance(terminal, str)
        or terminal not in {"not-registered", "not-found"}
        or pid is not None
    ):
        raise DarwinSharedLifecycleError("named shared Core rollback state is indeterminate")


def _require_shared(identity: InstanceIdentity) -> None:
    if (
        not isinstance(identity, InstanceIdentity)
        or identity.instance_id == "default"
        or identity.bridge_mode != "shared"
        or identity.bridge_id is None
    ):
        raise DarwinSharedLifecycleError("named shared Core identity required")


def shared_core_status(
    identity: InstanceIdentity, *, home: Path | None = None
) -> dict[str, object]:
    """Read only this Core's signed ServiceManager registration and launchd PID."""
    _require_shared(identity)
    return {
        "instance_id": identity.instance_id,
        "bridge_mode": "shared",
        "bridge_id": identity.bridge_id,
        "core_registration": _manager(identity, "status", "http-origin", home=home),
        "core_pid": darwin_service._launchd_job_pid(identity.macos_label("core")),
    }


def _signed_shared_core_python(identity: InstanceIdentity, home: Path | None) -> Path:
    """Resolve Core interpreter from the identity-checked signed app bundle.

    The operator invokes shared-start with an arbitrary management Python;
    only the actual Core's sys.executable must equal the signed manifest pin.
    Never trust an interpreter path supplied by operator argv or manifest alone.
    """
    _manager_path(identity, home)  # Identity + codesign --verify --deep --strict.
    app = instance_app_path(identity, home=home)
    info = _read_plist(app / "Contents/Info.plist")
    raw = info.get("ServerFSSharedCorePythonPath")
    if not isinstance(raw, str) or not raw.startswith("/"):
        raise DarwinSharedLifecycleError("signed Shared Core Python pin is unavailable")
    python = _validate_shared_core_python(Path(raw))
    try:
        verify_managed_shared_core_python(python)
    except DarwinSharedRuntimeError:
        raise DarwinSharedLifecycleError(
            "signed Shared Core runtime provenance is invalid"
        ) from None
    return python


def preflight_shared_core(
    identity: InstanceIdentity,
    config_path: Path,
    port: int,
    *,
    home: Path | None = None,
) -> dict[str, object]:
    """Verify signed Core, manifest, live owner grant and namespace without registration.

    This is the same fail-closed read-only gate used by shared-start. No
    ServiceManagement registration, grant changes or Core/Bridge launch occurs.
    """
    _require_shared(identity)
    try:
        core_python = _signed_shared_core_python(identity, home)
        alias, socket_path = resolve_shared_core_manifest(
            identity,
            config_path,
            port,
            home=home,
            expected_executable=core_python,
        )
        # A v2 authenticated challenge is mandatory: filesystem metadata
        # cannot establish Bridge identity or grant authorization.
        client = asyncio.run(
            prepare_shared_v2_client(identity, Path(config_path), socket_path, home=home)
        )
    except (DarwinSharedManifestError, SharedCoreBootstrapError, OSError, ValueError):
        raise DarwinSharedLifecycleError("shared Core preflight failed") from None
    bound = set(client) if isinstance(client, dict) else {getattr(client, "_workdir_alias", None)}
    expected = {alias} if isinstance(alias, str) else set(alias)
    if bound != expected:
        raise DarwinSharedLifecycleError("shared Bridge grant alias mismatch")

    try:
        registration = _manager(identity, "status", "http-origin", home=home)
    except Exception:
        raise DarwinSharedLifecycleError(
            "named shared Core registration state is unavailable"
        ) from None
    if not isinstance(registration, str):
        raise DarwinSharedLifecycleError("named shared Core registration state is malformed")
    if registration not in {"not-registered", "not-found"}:
        if registration not in {"enabled", "requires-approval"}:
            raise DarwinSharedLifecycleError("named shared Core registration state is unknown")
        raise DarwinSharedLifecycleError("named shared Core is already registered")
    label = identity.macos_label("core")
    try:
        pid = darwin_service._launchd_job_pid(label)
    except Exception:
        raise DarwinSharedLifecycleError(
            "named shared Core launchd namespace could not be verified"
        ) from None
    if pid is not None:
        raise DarwinSharedLifecycleError("named shared Core launchd namespace is occupied")
    _shared_launchd_job_absent(label, context="preflight")

    root = instance_service_home(identity, home=home)
    _assert_available_port(root.parents[2], identity.instance_id, port, allow_candidate=True)
    return {
        "instance_id": identity.instance_id,
        "bridge_mode": "shared",
        "bridge_id": identity.bridge_id,
        "core_port": port,
        "core_registration": "not-registered",
        "bridge_authenticated": True,
        "state": "ready-to-register",
    }


def start_shared_core(
    identity: InstanceIdentity,
    config_path: Path,
    port: int,
    *,
    home: Path | None = None,
) -> dict[str, object]:
    """Register only a Shared Core after the identical read-only owner gate.

    The manifest-inode lock coordinates cooperative shared-start and unstage
    callers. It is held through preflight, registration, readiness and rollback.
    Direct manager calls and stop remain outside that lock. Any newly registered
    Core failed during readiness is rolled back without changing or contacting
    the Bridge owner's lifecycle.
    """
    _require_shared(identity)
    with _shared_start_lock(identity, home):
        return _start_shared_core_locked(identity, config_path, port, home=home)


def _start_shared_core_locked(
    identity: InstanceIdentity,
    config_path: Path,
    port: int,
    *,
    home: Path | None = None,
) -> dict[str, object]:
    preflight_shared_core(identity, config_path, port, home=home)

    try:
        state = _manager(identity, "register", "http-origin", home=home)
    except Exception as exc:
        # SMAppService may throw after changing its registration state. A
        # status/PID pair cannot prove this call owns that registration.
        try:
            registered = _manager(identity, "status", "http-origin", home=home)
            pid = darwin_service._launchd_job_pid(identity.macos_label("core"))
        except Exception as inspect_error:
            raise DarwinSharedLifecycleError(
                "named shared Core registration outcome is unknown and could not be "
                "inspected; preserve state and inspect before retrying"
            ) from inspect_error
        known_status = (
            registered
            if isinstance(registered, str)
            and registered in {"enabled", "requires-approval", "not-registered", "not-found"}
            else "unknown"
        )
        pid_state = "present" if isinstance(pid, int) and pid > 0 else "absent"
        raise DarwinSharedLifecycleError(
            "named shared Core registration outcome is ambiguous "
            f"(status={known_status}, core_pid={pid_state}); "
            "no unregister was attempted; inspect this exact Core before retrying"
        ) from exc

    if state == "requires-approval":
        try:
            _rollback_registered_core(identity, home)
        except Exception as rollback:
            raise DarwinSharedLifecycleError(
                "named shared Core approval was denied and scoped rollback failed"
            ) from rollback
        raise DarwinSharedLifecycleError("named shared Core requires explicit macOS approval")
    if state != "enabled":
        raise DarwinSharedLifecycleError(
            "named shared Core registration returned an unknown state; no rollback attempted"
        )

    # Reuse the existing owned-Core HTTP listener authentication and PID
    # acceptance; it checks the named Core label, never the Bridge label.
    from .darwin_instance_owned_lifecycle import _core_ready

    try:
        pid = _core_ready(identity, port)
    except Exception as exc:
        try:
            _rollback_registered_core(identity, home)
        except Exception as rollback:
            raise DarwinSharedLifecycleError(
                "named shared Core start failed and scoped rollback failed"
            ) from rollback
        raise DarwinSharedLifecycleError(
            "named shared Core failed readiness and was rolled back"
        ) from exc

    return {
        "instance_id": identity.instance_id,
        "bridge_mode": "shared",
        "bridge_id": identity.bridge_id,
        "core_registration": "enabled",
        "core_pid": pid,
        "core_port": port,
    }


def stop_shared_core(identity: InstanceIdentity, *, home: Path | None = None) -> dict[str, str]:
    """Unregister exactly this Core; never unregister or stop the shared Bridge.

    This operation intentionally does not require a valid grant/manifest so
    broken or revoked Core installations can still be safely stopped.
    """
    _require_shared(identity)
    try:
        state = _manager(identity, "status", "http-origin", home=home)
    except Exception:
        raise DarwinSharedLifecycleError(
            "named shared Core registration state is unavailable"
        ) from None
    if isinstance(state, str) and state in {"not-registered", "not-found"}:
        label = identity.macos_label("core")
        try:
            pid = darwin_service._launchd_job_pid(label)
            present = darwin_service._launchd_job_present(label)
        except Exception:
            raise DarwinSharedLifecycleError(
                "named shared Core registration/PID state is inconsistent "
                f"({label}, pid=unavailable)"
            ) from None
        if pid is not None or present is not False:
            raise DarwinSharedLifecycleError(
                f"named shared Core registration/PID state is inconsistent ({label}, pid={pid})"
            )
        raise DarwinSharedLifecycleError("named shared Core is already unregistered")
    if not isinstance(state, str) or state not in {"enabled", "requires-approval"}:
        # Preserve read-only evidence for an indeterminate manager response.
        label = identity.macos_label("core")
        try:
            pid = darwin_service._launchd_job_pid(label)
        except Exception:
            pid = "unavailable"
        status_class = "string" if isinstance(state, str) else type(state).__name__
        raise DarwinSharedLifecycleError(
            "named shared Core registration state is indeterminate "
            f"({status_class}, {label}, pid={pid})"
        )
    label = identity.macos_label("core")
    try:
        stopped = _manager(identity, "unregister", "http-origin", home=home)
    except Exception:
        raise DarwinSharedLifecycleError(
            "named shared Core could not be unregistered safely"
        ) from None
    if not isinstance(stopped, str) or stopped not in {"not-registered", "not-found"}:
        raise DarwinSharedLifecycleError("named shared Core did not unregister")
    # The bounded wait allows launchd to complete asynchronous process teardown.
    try:
        _wait_component_stopped(identity, "core")
    except Exception:
        raise DarwinSharedLifecycleError(
            "named shared Core remains running after unregister"
        ) from None
    try:
        terminal = _manager(identity, "status", "http-origin", home=home)
        final_pid = darwin_service._launchd_job_pid(label)
        present = darwin_service._launchd_job_present(label)
    except Exception:
        raise DarwinSharedLifecycleError(
            "named shared Core stop state could not be verified"
        ) from None
    if (
        not isinstance(terminal, str)
        or terminal not in {"not-registered", "not-found"}
        or final_pid is not None
        or present is not False
    ):
        raise DarwinSharedLifecycleError("named shared Core stop state is indeterminate")
    return {
        "instance_id": identity.instance_id,
        "bridge_mode": "shared",
        "bridge_id": identity.bridge_id,
        "core_registration": stopped,
    }
