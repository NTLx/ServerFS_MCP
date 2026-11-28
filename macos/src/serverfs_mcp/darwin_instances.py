"""Isolated v0.25 macOS ServiceManagement bundle identities (source/staging only).

Each non-default installation needs its own signed app identity, associated
LaunchAgents and manifest root. This module stages bundles; it never registers,
launches, stops or upgrades a service. Predeclared Connector IDs are staged-only
and are not operable through Python lifecycle commands. The legacy default app is untouched.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

from .instance_identity import (
    DEFAULT_INSTANCE_ID,
    InstanceIdentity,
    InstanceIdentityError,
    valid_id,
)

APP_NAME = "ServerFSServiceManager.app"
_DEFAULT_BUNDLE_ID = "com.ntlx.serverfs.service-manager"
_COMPONENTS = {
    "openai": "tunnel",
    "bridge": "agent-bridge",
    "core": "http-origin",
    "cloudflare": "cloudflare",
}


class DarwinInstanceError(ValueError):
    """Invalid or unsafe macOS instance bundle preparation."""


def instance_service_home(identity: InstanceIdentity, *, home: Path | None = None) -> Path:
    """Instance-owned state under the existing per-user ServerFS data root."""
    user_home = Path.home() if home is None else home
    return (
        user_home / "Library" / "Application Support" / "ServerFS" / identity.macos_data_relative()
    )


def instance_app_path(identity: InstanceIdentity, *, home: Path | None = None) -> Path:
    return instance_service_home(identity, home=home) / APP_NAME


def instance_bundle_id(identity: InstanceIdentity) -> str:
    if identity.instance_id == DEFAULT_INSTANCE_ID:
        return _DEFAULT_BUNDLE_ID
    return f"{_DEFAULT_BUNDLE_ID}.{identity.instance_id}"


def _read_plist(path: Path) -> dict:
    try:
        value = plistlib.loads(path.read_bytes())
    except (OSError, ValueError, TypeError) as exc:
        raise DarwinInstanceError("instance bundle contains an invalid plist") from exc
    if not isinstance(value, dict):
        raise DarwinInstanceError("instance bundle plist must be a dictionary")
    return value


def _no_symlinks(path: Path) -> None:
    if path.is_symlink() or any(part.is_symlink() for part in path.parents):
        raise DarwinInstanceError("instance staging path must not contain symlinks")


def _verify_source_app(source: Path) -> None:
    _no_symlinks(source)
    if not source.is_dir() or any(child.is_symlink() for child in source.rglob("*")):
        raise DarwinInstanceError("source app bundle is absent or contains symlinks")
    info = _read_plist(source / "Contents" / "Info.plist")
    if info.get("CFBundleIdentifier") != _DEFAULT_BUNDLE_ID or "ServerFSInstanceID" in info:
        raise DarwinInstanceError("source app must be an unmodified default bundle")
    agents = source / "Contents" / "Library" / "LaunchAgents"
    expected = {f"{InstanceIdentity().macos_label(component)}.plist" for component in _COMPONENTS}
    if not agents.is_dir() or {item.name for item in agents.iterdir()} != expected:
        raise DarwinInstanceError("source app LaunchAgent set is not canonical")
    for component, service_id in _COMPONENTS.items():
        label = InstanceIdentity().macos_label(component)
        spec = _read_plist(agents / f"{label}.plist")
        if (
            spec.get("Label") != label
            or spec.get("BundleProgram") != "Contents/Resources/serverfs-service-launcher"
            or spec.get("ProgramArguments") != ["serverfs-service-launcher", service_id]
        ):
            raise DarwinInstanceError("source app LaunchAgent identity mismatch")


def _sign_and_verify(app: Path) -> None:
    for command in (
        ["codesign", "--force", "--sign", "-", "--timestamp=none", str(app)],
        ["codesign", "--verify", "--deep", "--strict", str(app)],
    ):
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, check=False, timeout=30
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise DarwinInstanceError("macOS instance bundle codesign unavailable") from exc
        if result.returncode != 0:
            raise DarwinInstanceError("macOS instance bundle codesign verification failed")


def _validate_shared_core_python(path: Path) -> Path:
    if not path.is_absolute():
        raise DarwinInstanceError("shared Core Python must be an absolute canonical path")
    try:
        resolved = path.resolve(strict=True)
        if resolved != path:
            raise DarwinInstanceError("shared Core Python must be an absolute canonical path")
        for item in (path, *path.parents):
            if item.is_symlink():
                raise DarwinInstanceError("shared Core Python path must not contain symlinks")
            if item != path and not item.is_dir():
                raise DarwinInstanceError("shared Core Python ancestors must be directories")
        info = path.lstat()
    except OSError as exc:
        raise DarwinInstanceError("shared Core Python is unavailable") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise DarwinInstanceError("shared Core Python must be a user-owned private regular file")
    if not os.access(path, os.X_OK):
        raise DarwinInstanceError("shared Core Python must be executable")
    return path


def stage_instance_app(
    identity: InstanceIdentity,
    source_app: Path,
    destination: Path,
    *,
    connector_ids: dict[str, list[str] | tuple[str, ...]] | None = None,
    shared_core_python: Path | None = None,
) -> Path:
    """Stage one app with an instance-qualified bundle and launchd identity.

    Caller must supply a freshly built default template and an unused staging
    destination. This never changes the current user's installed default app
    or invokes SMAppService. Bridge plist is present only for owned mode.
    """
    if identity.instance_id == DEFAULT_INSTANCE_ID:
        raise DarwinInstanceError("default installation must use its legacy bundle")
    if identity.bridge_mode == "shared":
        if shared_core_python is None:
            raise DarwinInstanceError("shared bridge mode requires an explicit Core Python pin")
        shared_core_python = _validate_shared_core_python(Path(shared_core_python))
    elif shared_core_python is not None:
        raise DarwinInstanceError("Core Python pin is only valid for shared bridge mode")
    connector_ids = {} if connector_ids is None else connector_ids
    if not isinstance(connector_ids, dict) or not set(connector_ids).issubset(
        {"cloudflare", "openai"}
    ):
        raise DarwinInstanceError("connector IDs must map cloudflare/openai providers")
    extra_services: list[tuple[str, str, str]] = []
    for provider, ids in connector_ids.items():
        if not isinstance(ids, (list, tuple)):
            raise DarwinInstanceError("connector IDs must be lists")
        validated_ids: list[str] = []
        for connector_id in ids:
            try:
                valid_id(connector_id)
            except InstanceIdentityError as exc:
                raise DarwinInstanceError("invalid Connector ID") from exc
            validated_ids.append(connector_id)
        if len(validated_ids) != len(set(validated_ids)):
            raise DarwinInstanceError("connector IDs must be unique")
        component = provider
        service = "cloudflare" if provider == "cloudflare" else "tunnel"
        for connector_id in validated_ids:
            key = f"{service}.{connector_id}"
            label = identity.macos_label(component, connector_id)
            extra_services.append((key, label, service))
    if len({key for key, _, _ in extra_services}) != len(extra_services) or len(
        {label for _, label, _ in extra_services}
    ) != len(extra_services):
        raise DarwinInstanceError("Connector service identity collision")
    source_app = Path(source_app)
    destination = Path(destination)
    _no_symlinks(destination)
    if destination.name != APP_NAME:
        raise DarwinInstanceError("instance app filename must be canonical")
    if destination.exists() or destination.is_symlink():
        raise DarwinInstanceError("instance app destination already exists")
    if not destination.parent.is_dir():
        raise DarwinInstanceError("instance app destination parent does not exist")
    _verify_source_app(source_app)

    with tempfile.TemporaryDirectory(prefix=".serverfs-instance-", dir=destination.parent) as tmp:
        staged = Path(tmp) / APP_NAME
        shutil.copytree(source_app, staged, symlinks=False)
        info_path = staged / "Contents" / "Info.plist"
        info = _read_plist(info_path)
        info["CFBundleIdentifier"] = instance_bundle_id(identity)
        info["ServerFSInstanceID"] = identity.instance_id
        if shared_core_python is not None:
            info["ServerFSSharedCorePythonPath"] = str(shared_core_python)
        info_path.write_bytes(plistlib.dumps(info, sort_keys=True))

        agents = staged / "Contents" / "Library" / "LaunchAgents"
        for component, service_id in _COMPONENTS.items():
            old_label = InstanceIdentity().macos_label(component)
            old_path = agents / f"{old_label}.plist"
            if component == "bridge" and identity.bridge_mode != "owned":
                old_path.unlink()
                continue
            spec = _read_plist(old_path)
            new_label = identity.macos_label(component)
            spec["Label"] = new_label
            spec["ProgramArguments"] = [
                "serverfs-service-launcher",
                identity.instance_id,
                service_id,
            ]
            new_path = agents / f"{new_label}.plist"
            new_path.write_bytes(plistlib.dumps(spec, sort_keys=True))
            old_path.unlink()

        for service_key, label, provider in extra_services:
            component = "cloudflare" if provider == "cloudflare" else "openai"
            base_spec_path = agents / f"{identity.macos_label(component)}.plist"
            spec = _read_plist(base_spec_path)
            spec["Label"] = label
            spec["ProgramArguments"] = [
                "serverfs-service-launcher",
                identity.instance_id,
                service_key,
            ]
            path = agents / f"{label}.plist"
            if path.exists():
                raise DarwinInstanceError("Connector LaunchAgent identity collision")
            path.write_bytes(plistlib.dumps(spec, sort_keys=True))

        _sign_and_verify(staged)
        # Do not replace a previous installation on retry or on a collision.
        if destination.exists() or destination.is_symlink():
            raise DarwinInstanceError("instance app destination appeared during staging")
        os.rename(staged, destination)
    return destination
