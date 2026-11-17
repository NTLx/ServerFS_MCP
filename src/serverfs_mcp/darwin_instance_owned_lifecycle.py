"""macOS v0.25 named-owned ServiceManagement lifecycle (no implicit deployment).

A Core owns exactly one supervised Bridge. Explicit start: Bridge -> Core.
Explicit stop: Core -> Bridge. Registration is scoped to signed named labels,
and rollback touches only components newly registered by this invocation.
Default and shared Bridge service modes are never addressed.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import stat
import time
from pathlib import Path

from .agent_client import (
    AgentBridgeClient,
    AgentBridgeRemoteError,
    AgentBridgeUnavailable,
)
from .darwin_instance_bridge_identity import named_owned_bridge_resources
from .darwin_instance_owned_binding import resolve_owned_core_binding
from .darwin_instance_runtime import (
    DarwinInstanceRuntimeError,
    _anonymous_core_401,
    _assert_available_port,
    _core_listener_owned_by,
    _load_connector_owner,
    _manager,
    _wait_component_stopped,
    darwin_service,
)
from .darwin_instances import instance_service_home
from .instance_identity import InstanceIdentity


class DarwinOwnedLifecycleError(ValueError):
    """A named owned component cannot be safely started or stopped."""


def _owned(identity: InstanceIdentity) -> None:
    if (
        identity.instance_id == "default"
        or identity.bridge_mode != "owned"
        or identity.bridge_id != identity.instance_id
    ):
        raise DarwinOwnedLifecycleError("only independently named owned instances are supported")


def _verify(
    identity: InstanceIdentity,
    config: Path,
    port: int,
    *,
    home: Path | None = None,
    default_bridge_config: Path | None = None,
) -> Path:
    _owned(identity)
    resolve_owned_core_binding(
        identity,
        config,
        port,
        home=home,
        default_bridge_config=default_bridge_config,
    )
    return named_owned_bridge_resources(identity, home=home).socket_path


async def _probe_bridge_ipc(path: Path) -> bool:
    client = AgentBridgeClient(path, timeout_seconds=1.0)
    try:
        await client.call("task.get", {"task_id": "agt_" + secrets.token_hex(12)})
    except AgentBridgeRemoteError as exc:
        return exc.code == "AGENT_TASK_NOT_FOUND"
    except (AgentBridgeUnavailable, OSError, ValueError):
        return False
    return False


def _bridge_ready(identity: InstanceIdentity, path: Path, *, timeout: float = 12.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pid = darwin_service._launchd_job_pid(identity.macos_label("bridge"))
        try:
            info = path.stat(follow_symlinks=False)
            valid = (
                pid is not None
                and stat.S_ISSOCK(info.st_mode)
                and info.st_uid == os.getuid()
                and stat.S_IMODE(info.st_mode) == 0o600
                and asyncio.run(_probe_bridge_ipc(path))
            )
            if valid:
                return pid
        except (OSError, RuntimeError):
            pass
        time.sleep(0.2)
    raise DarwinOwnedLifecycleError("named owned Bridge not ready with verified IPC")


def _core_ready(identity: InstanceIdentity, port: int, *, timeout: float = 15.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pid = darwin_service._launchd_job_pid(identity.macos_label("core"))
        if pid is not None and _core_listener_owned_by(pid, port) and _anonymous_core_401(port):
            return pid
        time.sleep(0.2)
    raise DarwinOwnedLifecycleError("named Core did not own authenticated loopback listener")


def _status(identity: InstanceIdentity, component: str, home: Path | None) -> str:
    return _manager(
        identity,
        "status",
        "agent-bridge" if component == "bridge" else "http-origin",
        home=home,
    )


def _load_owned_manifest(identity: InstanceIdentity, home: Path | None) -> dict:
    try:
        _, document = _load_connector_owner(identity, home=home)
    except (DarwinInstanceRuntimeError, OSError, ValueError) as exc:
        raise DarwinOwnedLifecycleError("named owned manifest is untrusted") from exc
    core = document.get("core")
    if not isinstance(core, dict) or type(core.get("port")) is not int:
        raise DarwinOwnedLifecycleError("named owned manifest identity mismatch")
    return document


def _refuse_registered_connectors(identity: InstanceIdentity, home: Path | None) -> None:
    document = _load_owned_manifest(identity, home)
    keys = [key for key in ("cloudflare", "tunnel") if key in document["services"]]
    keys.extend(key for key in document["services"] if key.startswith(("cloudflare.", "tunnel.")))
    for key in keys:
        if key == "cloudflare":
            provider, connector_id = "cloudflare", None
        elif key == "tunnel":
            provider, connector_id = "openai", None
        else:
            provider, connector_id = key.split(".", 1)
            if provider == "tunnel":
                provider = "openai"
        status = _manager(identity, "status", key, home=home)
        pid = darwin_service._launchd_job_pid(identity.macos_label(provider, connector_id))
        if status not in {"not-registered", "not-found"} or pid is not None:
            raise DarwinOwnedLifecycleError("stop Connector before its owned Core or Bridge")


def _rollback_one(identity: InstanceIdentity, component: str, *, home: Path | None) -> None:
    name = "agent-bridge" if component == "bridge" else "http-origin"
    _manager(identity, "unregister", name, home=home)
    _wait_component_stopped(identity, component, timeout=12.0)


def owned_instance_status(
    identity: InstanceIdentity, *, home: Path | None = None
) -> dict[str, object]:
    """Read-only owned Core/Bridge lifecycle snapshot, without any repair."""
    _owned(identity)
    doc = _load_owned_manifest(identity, home)
    statuses = {component: _status(identity, component, home) for component in ("bridge", "core")}
    pids = {
        component: darwin_service._launchd_job_pid(identity.macos_label(component))
        for component in ("bridge", "core")
    }
    for provider, service in (("cloudflare", "cloudflare"), ("openai", "tunnel")):
        if service in doc["services"]:
            statuses[provider] = _manager(identity, "status", service, home=home)
            pids[provider] = darwin_service._launchd_job_pid(identity.macos_label(provider))
    for service in doc["services"]:
        if service.startswith("cloudflare."):
            provider, connector_id = "cloudflare", service.removeprefix("cloudflare.")
        elif service.startswith("tunnel."):
            provider, connector_id = "openai", service.removeprefix("tunnel.")
        else:
            continue
        statuses[service] = _manager(identity, "status", service, home=home)
        pids[service] = darwin_service._launchd_job_pid(
            identity.macos_label(provider, connector_id)
        )
    path = named_owned_bridge_resources(identity, home=home).socket_path
    try:
        info = path.stat(follow_symlinks=False)
        private_socket = (
            stat.S_ISSOCK(info.st_mode)
            and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == 0o600
        )
    except OSError:
        private_socket = False
    return {
        "instance_id": identity.instance_id,
        "bridge_mode": "owned",
        "core_port": doc["core"]["port"],
        "registrations": statuses,
        "pids": pids,
        "bridge_socket_private": private_socket,
    }


def start_owned_bridge(
    identity: InstanceIdentity,
    config: Path,
    port: int,
    *,
    home: Path | None = None,
    default_bridge_config: Path | None = None,
) -> dict[str, str | int]:
    path = _verify(
        identity,
        config,
        port,
        home=home,
        default_bridge_config=default_bridge_config,
    )
    if _status(identity, "bridge", home) not in {"not-registered", "not-found"}:
        raise DarwinOwnedLifecycleError("named Bridge already registered; no replacement")
    if path.exists() or path.is_symlink():
        raise DarwinOwnedLifecycleError("named Bridge IPC namespace is occupied")
    root = instance_service_home(identity, home=home)
    _assert_available_port(root.parents[2], identity.instance_id, port, allow_candidate=True)
    state = _manager(identity, "register", "agent-bridge", home=home)
    try:
        if state != "enabled":
            raise DarwinOwnedLifecycleError("named Bridge needs explicit macOS approval")
        pid = _bridge_ready(identity, path)
    except Exception as exc:
        try:
            _rollback_one(identity, "bridge", home=home)
        except Exception as rollback:
            raise DarwinOwnedLifecycleError(
                "named Bridge start failed and scoped rollback failed"
            ) from rollback
        raise DarwinOwnedLifecycleError("named Bridge start readiness failed") from exc
    return {
        "instance_id": identity.instance_id,
        "component": "bridge",
        "registration": "enabled",
        "pid": pid,
    }


def start_owned_core(
    identity: InstanceIdentity,
    config: Path,
    port: int,
    *,
    home: Path | None = None,
    default_bridge_config: Path | None = None,
) -> dict[str, str | int]:
    path = _verify(
        identity,
        config,
        port,
        home=home,
        default_bridge_config=default_bridge_config,
    )
    if _status(identity, "core", home) not in {"not-registered", "not-found"}:
        raise DarwinOwnedLifecycleError("named Core already registered; no replacement")
    if _status(identity, "bridge", home) != "enabled":
        raise DarwinOwnedLifecycleError("named Core requires enabled owned Bridge")
    _bridge_ready(identity, path, timeout=3.0)
    root = instance_service_home(identity, home=home)
    _assert_available_port(root.parents[2], identity.instance_id, port, allow_candidate=True)
    state = _manager(identity, "register", "http-origin", home=home)
    try:
        if state != "enabled":
            raise DarwinOwnedLifecycleError("named Core needs explicit macOS approval")
        pid = _core_ready(identity, port)
    except Exception as exc:
        try:
            _rollback_one(identity, "core", home=home)
        except Exception as rollback:
            raise DarwinOwnedLifecycleError(
                "named Core start failed and scoped rollback failed"
            ) from rollback
        raise DarwinOwnedLifecycleError("named Core start readiness failed") from exc
    return {
        "instance_id": identity.instance_id,
        "component": "core",
        "registration": "enabled",
        "pid": pid,
    }


def start_owned_pair(
    identity: InstanceIdentity,
    config: Path,
    port: int,
    *,
    home: Path | None = None,
    default_bridge_config: Path | None = None,
) -> dict[str, str | int]:
    _verify(identity, config, port, home=home, default_bridge_config=default_bridge_config)
    if _status(identity, "bridge", home) not in {"not-registered", "not-found"}:
        raise DarwinOwnedLifecycleError("owned pair requires an unregistered Bridge")
    if _status(identity, "core", home) not in {"not-registered", "not-found"}:
        raise DarwinOwnedLifecycleError("owned pair requires an unregistered Core")
    bridge = start_owned_bridge(
        identity,
        config,
        port,
        home=home,
        default_bridge_config=default_bridge_config,
    )
    try:
        core = start_owned_core(
            identity,
            config,
            port,
            home=home,
            default_bridge_config=default_bridge_config,
        )
    except Exception as exc:
        try:
            _rollback_one(identity, "bridge", home=home)
        except Exception as rollback:
            raise DarwinOwnedLifecycleError(
                "owned pair Core start failed and Bridge rollback failed"
            ) from rollback
        raise DarwinOwnedLifecycleError("owned pair Core start failed; Bridge rolled back") from exc
    return {
        "instance_id": identity.instance_id,
        "bridge_pid": bridge["pid"],
        "core_pid": core["pid"],
        "state": "enabled",
    }


def stop_owned_core(
    identity: InstanceIdentity,
    *,
    home: Path | None = None,
) -> dict[str, str]:
    _owned(identity)
    _refuse_registered_connectors(identity, home)
    status = _status(identity, "core", home)
    if status in {"not-registered", "not-found"}:
        raise DarwinOwnedLifecycleError("named Core is already unregistered")
    state = _manager(identity, "unregister", "http-origin", home=home)
    if state not in {"not-registered", "not-found"}:
        raise DarwinOwnedLifecycleError("named Core did not unregister")
    _wait_component_stopped(identity, "core")
    return {"instance_id": identity.instance_id, "component": "core", "state": state}


def stop_owned_bridge(
    identity: InstanceIdentity,
    *,
    home: Path | None = None,
) -> dict[str, str]:
    _owned(identity)
    if _status(identity, "core", home) not in {"not-registered", "not-found"}:
        raise DarwinOwnedLifecycleError("stop Core before its owned Bridge")
    _refuse_registered_connectors(identity, home)
    if _status(identity, "bridge", home) in {"not-registered", "not-found"}:
        raise DarwinOwnedLifecycleError("named Bridge is already unregistered")
    state = _manager(identity, "unregister", "agent-bridge", home=home)
    if state not in {"not-registered", "not-found"}:
        raise DarwinOwnedLifecycleError("named Bridge did not unregister")
    _wait_component_stopped(identity, "bridge")
    return {"instance_id": identity.instance_id, "component": "bridge", "state": state}


def stop_owned_pair(
    identity: InstanceIdentity,
    *,
    home: Path | None = None,
) -> dict[str, str]:
    _owned(identity)
    _refuse_registered_connectors(identity, home)
    if _status(identity, "core", home) not in {"not-registered", "not-found"}:
        stop_owned_core(identity, home=home)
    if _status(identity, "bridge", home) not in {"not-registered", "not-found"}:
        stop_owned_bridge(identity, home=home)
    return {"instance_id": identity.instance_id, "state": "not-registered"}
