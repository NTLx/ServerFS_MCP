"""Ephemeral health evidence, never a second lifecycle authority.

Task Scheduler owns enable/disable/run/end. A private runtime stamp only ties
an observed kernel PID+creation time to a service generation and (for Core)
to the actual Win32 TCP listener plus an unauthenticated 401 response.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from . import storage
from .spec import Manifest


class ReadinessError(Exception):
    """No usable or safely attributable service readiness evidence."""


def identity_path(
    component: str,
    instance_id: str = "default",
    connector_id: str | None = None,
) -> Path:
    root = (
        storage.home(component, instance_id, connector_id)
        if connector_id is not None
        else storage.home(component, instance_id)
        if instance_id != "default"
        else storage.home(component)
    )
    return root / "identity.json"


def publish(manifest: Manifest) -> None:
    """Publish own live kernel identity under the component's private DACL."""
    from serverfs_mcp.windows_private_artifacts import protect_new_file
    from serverfs_mcp.windows_process import process_identity

    if manifest.connector_id is not None:
        storage.load(manifest.component, manifest.instance_id, manifest.connector_id)
    elif manifest.instance_id != "default":
        storage.load(manifest.component, manifest.instance_id)
    else:
        storage.load(manifest.component)
    identity = process_identity(os.getpid())
    if identity is None or identity.creation_time <= 0 or not identity.image_path.is_file():
        raise ReadinessError("native Windows service process identity is unverified")
    path = (
        identity_path(manifest.component, manifest.instance_id, manifest.connector_id)
        if manifest.connector_id is not None
        else identity_path(manifest.component, manifest.instance_id)
    )
    if path.exists() or path.is_symlink():
        storage.check_private(path, directory=False, sid=manifest.sid)
    temp = path.parent / (".identity-" + str(os.getpid()) + ".tmp")
    record = {
        "schema": manifest.schema,
        "component": manifest.component,
        "pid": identity.pid,
        "created": identity.creation_time,
        "image": str(identity.image_path),
    }
    if manifest.schema == 2:
        record["instance_id"] = manifest.instance_id
        if manifest.connector_id is not None:
            record["connector_id"] = manifest.connector_id
    payload = json.dumps(
        record,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    try:
        with temp.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        protect_new_file(temp, manifest.sid)
        storage.check_private(temp, directory=False, sid=manifest.sid)
        os.replace(temp, path)
    except OSError as exc:
        raise ReadinessError("private process identity could not be published") from exc
    finally:
        temp.unlink(missing_ok=True)


def _current_identity(manifest: Manifest):
    from serverfs_mcp.windows_process import process_identity

    path = (
        identity_path(manifest.component, manifest.instance_id, manifest.connector_id)
        if manifest.connector_id is not None
        else identity_path(manifest.component, manifest.instance_id)
    )
    try:
        storage.check_private(path, directory=False, sid=manifest.sid)
        saved = json.loads(path.read_text(encoding="utf-8"))
        expected_keys = {"schema", "component", "pid", "created", "image"}
        if manifest.schema == 2:
            expected_keys.add("instance_id")
            if manifest.connector_id is not None:
                expected_keys.add("connector_id")
        if (
            set(saved) != expected_keys
            or saved["schema"] != manifest.schema
            or saved["component"] != manifest.component
            or (manifest.schema == 2 and saved["instance_id"] != manifest.instance_id)
            or (
                manifest.connector_id is not None and saved["connector_id"] != manifest.connector_id
            )
            or type(saved["pid"]) is not int
            or saved["pid"] <= 0
            or type(saved["created"]) is not int
            or saved["created"] <= 0
            or not isinstance(saved["image"], str)
            or not Path(saved["image"]).is_absolute()
        ):
            return None
        actual = process_identity(saved["pid"])
        if (
            actual is None
            or actual.creation_time != saved["created"]
            or os.path.normcase(str(actual.image_path)) != os.path.normcase(saved["image"])
        ):
            return None
        return actual
    except (OSError, ValueError, TypeError, storage.StorageError):
        return None


def _401(port: int) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/mcp",
        headers={"Host": f"127.0.0.1:{port}"},
    )
    try:
        response = opener.open(request, timeout=1.5)
        response.close()
        return False  # HTTP 2xx without credentials is not a healthy secure Core.
    except urllib.error.HTTPError as exc:
        return exc.code == 401
    except (OSError, urllib.error.URLError):
        return False


def core_ready(manifest: Manifest) -> bool:
    """Proof is owned process generation + exact loopback listener + 401."""
    if manifest.component != "core":
        raise ReadinessError("Core readiness called for a non-Core component")
    identity = _current_identity(manifest)
    if identity is None:
        return False
    from serverfs_mcp.windows_core_listener import _windows_ipv4_tcp_listeners

    port = manifest.settings["port"]
    assert type(port) is int
    try:
        matches = [row for row in _windows_ipv4_tcp_listeners() if row.port == port]
    except Exception:  # noqa: BLE001 - unavailable TCP owner proof is not readiness
        return False
    if len(matches) != 1:
        return False
    return matches[0].pid == identity.pid and matches[0].address == "127.0.0.1" and _401(port)


def status_readiness(manifest: Manifest, *, task_running: bool) -> str:
    if not task_running:
        return "offline"
    if manifest.component == "core":
        return "verified" if core_ready(manifest) else "not_verified"
    if manifest.component == "bridge":
        return "owner_alive" if _current_identity(manifest) else "not_verified"
    # Connector processes have an owner and child-process health separately;
    # a live owner is not evidence that the public Tunnel has connected.
    return "owner_alive" if _current_identity(manifest) else "not_verified"
