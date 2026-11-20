"""Windows user-private v25 component manifests; no legacy lifecycle state."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

from serverfs_mcp.instance_identity import (
    DEFAULT_INSTANCE_ID,
    InstanceIdentityError,
    valid_id,
)

from .spec import COMPONENTS, Manifest, ManifestError

_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class StorageError(ValueError):
    """Fail closed on untrusted native component file ownership."""


def _windows() -> None:
    if sys.platform != "win32" or os.name != "nt":
        raise StorageError("native Windows 11 is required")


def root(instance_id: str = DEFAULT_INSTANCE_ID) -> Path:
    _windows()
    from serverfs_mcp.native_lifecycle import window_local_app_data

    valid_id(instance_id)
    base = window_local_app_data() / "ServerFS" / "v25"
    return base if instance_id == DEFAULT_INSTANCE_ID else base / "instances" / instance_id


def home(
    component: str,
    instance_id: str = DEFAULT_INSTANCE_ID,
    connector_id: str | None = None,
) -> Path:
    if component not in COMPONENTS:
        raise StorageError("unknown component")
    if connector_id is not None:
        if component not in ("cloudflare", "openai") or instance_id == DEFAULT_INSTANCE_ID:
            raise StorageError("only independent connectors may have Connector IDs")
        valid_id(connector_id)
    base = root() if instance_id == DEFAULT_INSTANCE_ID else root(instance_id)
    component_root = base / "services" / component
    return component_root if connector_id is None else component_root / "connectors" / connector_id


def pinned_pythonw(component: str, instance_id: str = DEFAULT_INSTANCE_ID) -> Path:
    if component not in COMPONENTS:
        raise StorageError("unknown component")
    base = root() if instance_id == DEFAULT_INSTANCE_ID else root(instance_id)
    return base / "envs" / component / "Scripts" / "pythonw.exe"


def _sid() -> str:
    _windows()
    from serverfs_mcp.native_endpoint import current_user_sid

    return current_user_sid()


def check_private(path: Path, *, directory: bool, sid: str | None = None) -> None:
    """Trusted current-user/SYSTEM/Admins DACL with no reparse traversal."""
    _windows()
    from serverfs_mcp.windows_cloudflare_security import (
        _no_reparse_components,
        _read_windows_file_sddl,
        _validate_private_sddl,
    )

    try:
        _no_reparse_components(path)
        info = path.lstat()
        if info.st_file_attributes & _REPARSE or stat.S_ISLNK(info.st_mode):
            raise StorageError("private component path is redirected")
        if directory and not stat.S_ISDIR(info.st_mode):
            raise StorageError("private component directory is not regular")
        if not directory and not stat.S_ISREG(info.st_mode):
            raise StorageError("private component artifact is not regular")
        _validate_private_sddl(_read_windows_file_sddl(path), sid or _sid())
    except StorageError:
        raise
    except Exception as exc:  # noqa: BLE001 - do not leak SID/path
        raise StorageError("Windows component file owner/DACL is unverified") from exc


def _private_home(
    component: str,
    sid: str,
    instance_id: str = DEFAULT_INSTANCE_ID,
    connector_id: str | None = None,
) -> Path:
    if instance_id == DEFAULT_INSTANCE_ID:
        path = home(component, connector_id=connector_id)
        parents = (root(), root() / "services", path)
    else:
        base = root(instance_id)
        path = home(component, instance_id, connector_id)
        parents = (root(), root() / "instances", base, base / "services")
        if connector_id is None:
            parents += (path,)
        else:
            parent = base / "services" / component
            parents += (parent, parent / "connectors", path)
    for item in parents:
        check_private(item, directory=True, sid=sid)
    return path


def prepare(
    component: str,
    instance_id: str = DEFAULT_INSTANCE_ID,
    connector_id: str | None = None,
) -> None:
    """Provision only new per-user v25 directories, without touching legacy homes.

    Existing paths are checked, never re-ACL'd or replaced. Executables,
    credentials and Scheduler tasks are deliberately not created here.
    """
    _windows()
    if component not in COMPONENTS:
        raise StorageError("unknown component")
    if connector_id is not None:
        if component not in ("cloudflare", "openai") or instance_id == DEFAULT_INSTANCE_ID:
            raise StorageError("only independent connectors may have Connector IDs")
        valid_id(connector_id)
    from serverfs_mcp.windows_cloudflare_security import _no_reparse_components
    from serverfs_mcp.windows_private_artifacts import protect_new_file

    sid = _sid()
    default_root = root()
    base = default_root if instance_id == DEFAULT_INSTANCE_ID else root(instance_id)
    origin = default_root.parent.parent  # Shell-resolved per-user LocalAppData.
    try:
        _no_reparse_components(origin)
        if not origin.is_dir():
            raise StorageError("Windows user profile storage is unavailable")
    except StorageError:
        raise
    except Exception as exc:
        raise StorageError("Windows user profile storage is unverified") from exc

    prefix = (default_root.parent, default_root)
    if instance_id != DEFAULT_INSTANCE_ID:
        prefix += (default_root / "instances", base)
    directories = prefix + (
        base / "services",
        base / "services" / component,
        base / "envs",
        base / "envs" / component,
    )
    if connector_id is not None:
        directories += (
            base / "services" / component / "connectors",
            base / "services" / component / "connectors" / connector_id,
        )
    if component == "bridge" and instance_id != DEFAULT_INSTANCE_ID:
        # Independent Bridge-specific state, workdir locks and worker venv.
        # Existing default Bridge directories and their ACLs stay untouched.
        directories += (
            base / "services" / "bridge" / "state",
            base / "services" / "bridge" / "locks",
            base / "envs" / "agent-bridge",
        )
    for path in directories:
        if path.exists() or path.is_symlink():
            if path == default_root.parent:
                try:
                    _no_reparse_components(path)
                    if not path.is_dir():
                        raise StorageError("existing ServerFS data directory is invalid")
                except StorageError:
                    raise
                except Exception as exc:
                    raise StorageError("existing ServerFS data directory is unverified") from exc
            else:
                check_private(path, directory=True, sid=sid)
            continue
        created = False
        try:
            _no_reparse_components(path.parent)
            path.mkdir()
            created = True
            protect_new_file(path, sid)
            check_private(path, directory=True, sid=sid)
        except Exception as exc:
            # Never delete a directory created by a concurrent owner.
            if created:
                try:
                    path.rmdir()
                except OSError:
                    pass
            raise StorageError("Windows private service directory creation failed") from exc


def load(
    component: str,
    instance_id: str = DEFAULT_INSTANCE_ID,
    connector_id: str | None = None,
) -> Manifest:
    """No implicit creation, repair or registration on read."""
    _windows()
    sid = _sid()
    base = _private_home(component, sid, instance_id, connector_id)
    path = base / "service.json"
    check_private(path, directory=False, sid=sid)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        manifest = Manifest.parse(raw)
        expected = (
            pinned_pythonw(component)
            if instance_id == DEFAULT_INSTANCE_ID
            else pinned_pythonw(component, instance_id)
        )
        if (
            manifest.component != component
            or manifest.instance_id != instance_id
            or manifest.connector_id != connector_id
            or manifest.sid != sid
            or os.path.normcase(manifest.pythonw) != os.path.normcase(str(expected))
        ):
            raise StorageError("service manifest identity mismatches current user")
        return manifest
    except (OSError, ValueError, UnicodeError, ManifestError) as exc:
        raise StorageError("service manifest content is invalid") from exc


def component_inventory(component: str) -> tuple[Manifest, ...]:
    """Read exact protected manifests; never silently skip malformed peers.

    This is an early collision refusal, not an atomic port reservation.
    Actual listener ownership remains enforced by Windows/HTTP readiness.
    """
    _windows()
    if component not in COMPONENTS:
        raise StorageError("unsupported component inventory")
    base = root()
    sid = _sid()
    check_private(base, directory=True, sid=sid)
    profiles: list[Manifest] = []
    default = base / "services" / component / "service.json"
    if default.exists() or default.is_symlink():
        profiles.append(load(component))
    instances = base / "instances"
    if not instances.exists() and not instances.is_symlink():
        return tuple(profiles)
    check_private(instances, directory=True, sid=sid)
    try:
        entries = sorted(instances.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        raise StorageError("instance inventory is unavailable") from exc
    for entry in entries:
        try:
            valid_id(entry.name)
        except InstanceIdentityError as exc:
            raise StorageError("instance directory name is invalid") from exc
        if entry.name == DEFAULT_INSTANCE_ID:
            raise StorageError("default instance cannot shadow the legacy installation")
        check_private(entry, directory=True, sid=sid)
        path = entry / "services" / component / "service.json"
        if path.exists() or path.is_symlink():
            profiles.append(load(component, entry.name))
        if component not in ("cloudflare", "openai"):
            continue
        connectors = entry / "services" / component / "connectors"
        if not connectors.exists() and not connectors.is_symlink():
            continue
        check_private(connectors, directory=True, sid=sid)
        try:
            identities = sorted(connectors.iterdir(), key=lambda p: p.name)
        except OSError as exc:
            raise StorageError("connector inventory is unavailable") from exc
        for connector in identities:
            try:
                valid_id(connector.name)
            except InstanceIdentityError as exc:
                raise StorageError("connector directory name is invalid") from exc
            check_private(connector, directory=True, sid=sid)
            source = connector / "service.json"
            if source.exists() or source.is_symlink():
                profiles.append(load(component, entry.name, connector.name))
    return tuple(profiles)


def core_inventory() -> tuple[Manifest, ...]:
    return component_inventory("core")


def stage(manifest: Manifest) -> None:
    """One-time exclusive publish into a PREPROVISIONED protected directory.

    The installer deliberately cannot auto-create a private service directory
    with an insecure inherited DACL. It must already be owner-protected.
    """
    _windows()
    manifest = Manifest.parse(manifest.to_dict())
    sid = _sid()
    runtime = (
        pinned_pythonw(manifest.component)
        if manifest.instance_id == DEFAULT_INSTANCE_ID
        else pinned_pythonw(manifest.component, manifest.instance_id)
    )
    if manifest.sid != sid or os.path.normcase(manifest.pythonw) != os.path.normcase(str(runtime)):
        raise StorageError("manifest targets a foreign user or interpreter")
    # Refuse a manifest before install if its pinned runtime interpreter is
    # missing, redirected or not protected for this Windows account.
    check_private(runtime, directory=False, sid=sid)
    base = _private_home(manifest.component, sid, manifest.instance_id, manifest.connector_id)
    target = base / "service.json"
    if target.exists() or target.is_symlink():
        raise StorageError("service manifest already exists")
    payload = json.dumps(manifest.to_dict(), sort_keys=True, separators=(",", ":")).encode()
    temp = base / (".manifest-" + str(os.getpid()) + ".tmp")
    if temp.exists() or temp.is_symlink():
        raise StorageError("temporary manifest name is occupied")
    try:
        with temp.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        from serverfs_mcp.windows_private_artifacts import protect_new_file

        protect_new_file(temp, sid)
        check_private(temp, directory=False, sid=sid)
        os.link(temp, target)
        check_private(target, directory=False, sid=sid)
    except OSError as exc:
        raise StorageError("service manifest could not be published privately") from exc
    finally:
        temp.unlink(missing_ok=True)
