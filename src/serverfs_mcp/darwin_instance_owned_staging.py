"""Source-only staging of an independent macOS owned Bridge policy.

The Core distribution does not import the separate Bridge distribution or
duplicate its JSON policy renderer. It sends only derived non-secret policy
via stdin to a pinned Bridge Python executable and independently checks the
redacted result. This is NOT an SMAppService installation or activation.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

from .agent_lifecycle import render_request_from_config
from .darwin_instance_bridge_identity import (
    DarwinBridgeIdentityError,
    named_owned_bridge_resources,
    preflight_named_owned_bridge,
    workdir_claims_from_private_bridge_config,
)
from .darwin_instances import instance_service_home
from .instance_identity import InstanceIdentity
from .native_config import load_native_config
from .workdirs import Workdir


class DarwinOwnedPolicyError(DarwinBridgeIdentityError):
    """The separately owned Bridge config cannot be safely staged."""


def _private_directory(path: Path) -> None:
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise DarwinOwnedPolicyError("named Bridge staging path includes a symlink")
    try:
        item = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise DarwinOwnedPolicyError("named Bridge staging directory is unavailable") from exc
    if not stat.S_ISDIR(item.st_mode) or item.st_uid != os.getuid() or item.st_mode & 0o077:
        raise DarwinOwnedPolicyError("named Bridge staging directory is not private")


def stage_owned_bridge_policy(
    identity: InstanceIdentity,
    config_path: Path,
    bridge_python: Path,
    *,
    home: Path | None = None,
    default_bridge_config: Path | None = None,
) -> dict[str, str | int]:
    """Stage only a private named Bridge config using the Bridge's own package.

    Must be called on a new, unregistered candidate whose service state/data
    root was already allocated by a caller. Never stages over a file-only
    manifest or a pre-existing Bridge config; never touches default jobs.
    """
    if sys.platform != "darwin":
        raise DarwinOwnedPolicyError("owned Bridge staging requires macOS")
    if identity.bridge_mode != "owned" or identity.bridge_id != identity.instance_id:
        raise DarwinOwnedPolicyError("named Bridge must be independently owned")

    root = instance_service_home(identity, home=home)
    data = root / "data"
    for directory in (
        root.parent.parent,
        root.parent,
        root,
        data,
    ):
        _private_directory(directory)
    if (root / "services.json").exists() or (root / "services.json").is_symlink():
        raise DarwinOwnedPolicyError("owned Bridge cannot overwrite an existing Core instance")

    resources = named_owned_bridge_resources(identity, home=home)
    if resources.config_path.exists() or resources.config_path.is_symlink():
        raise DarwinOwnedPolicyError("owned Bridge private config already exists")

    preflight_named_owned_bridge(
        identity, config_path, home=home, default_bridge_config=default_bridge_config
    )
    try:
        workdirs, settings = load_native_config(Path(config_path))
        request = render_request_from_config(workdirs, settings)
    except Exception as exc:
        raise DarwinOwnedPolicyError("owned Bridge policy projection is invalid") from exc

    python = Path(bridge_python).expanduser()
    if not python.is_absolute():
        raise DarwinOwnedPolicyError("Bridge interpreter must have an absolute path")
    try:
        info = python.stat()
    except OSError as exc:
        raise DarwinOwnedPolicyError("Bridge interpreter cannot be inspected") from exc
    if not stat.S_ISREG(info.st_mode) or not os.access(python, os.X_OK):
        raise DarwinOwnedPolicyError("Bridge interpreter is not executable")
    payload = json.dumps(request, separators=(",", ":"), ensure_ascii=False)
    if len(payload.encode("utf-8")) > 262_144:
        raise DarwinOwnedPolicyError("owned Bridge policy exceeds the size budget")

    try:
        process = subprocess.run(
            [
                str(python),
                "-B",
                "-m",
                "serverfs_agent_bridge.macos_owned_config",
                "--instance",
                identity.instance_id,
                "--bridge-home",
                str(resources.config_path.parent),
            ],
            input=payload,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DarwinOwnedPolicyError("owned Bridge configuration subprocess failed") from exc
    if process.returncode != 0:
        # stderr may contain private path/exception details: never echo it.
        raise DarwinOwnedPolicyError("owned Bridge configuration subprocess refused")

    try:
        result = json.loads(process.stdout)
        if (
            not isinstance(result, dict)
            or result.get("instance_id") != identity.instance_id
            or result.get("state") != "staged-not-registered"
            or result.get("private_config") != "verified"
            or result.get("workdirs")
            != len(
                [wd for wd in workdirs if wd.agent_mode != "disabled" or wd.policy.repo_ops_enabled]
            )
        ):
            raise ValueError("invalid staged Bridge response")
        claims = workdir_claims_from_private_bridge_config(resources.config_path)
        expected = {
            (wd.alias, str(Path(wd.root).resolve(strict=True)), wd.read_only)
            for wd in workdirs
            if wd.agent_mode != "disabled" or wd.policy.repo_ops_enabled
        }
        observed = {
            (wd.alias, str(Path(wd.root).resolve(strict=True)), wd.read_only) for wd in claims
        }
        if observed != expected:
            raise ValueError("staged workdir policy differs from TOML")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise DarwinOwnedPolicyError(
            "owned Bridge policy was staged but postcondition verification failed; retain state"
        ) from exc
    return {
        "instance_id": identity.instance_id,
        "state": "staged-not-registered",
        "bridge_mode": "owned",
        "workdirs": len(claims),
        "private_config": "verified",
    }


def stage_owned_v2_grant_binding(
    identity: InstanceIdentity,
    config_path: Path,
    bridge_python: Path,
    workdirs: list[Workdir] | tuple[Workdir, ...],
    *,
    home: Path | None = None,
    default_bridge_config: Path | None = None,
) -> dict[str, str | int]:
    """Stage a self-only v2 grant/binding pair without registering either service.

    This create-only foundation is deliberately separate from the current v1
    owned runtime until Core tool routing and compatibility gates are integrated.
    """
    if sys.platform != "darwin":
        raise DarwinOwnedPolicyError("owned v2 grant staging requires macOS")
    if identity.bridge_mode != "owned" or identity.bridge_id != identity.instance_id:
        raise DarwinOwnedPolicyError("named owned Bridge must be independently owned")
    if not isinstance(workdirs, (list, tuple)) or not workdirs:
        raise DarwinOwnedPolicyError("owned v2 Agent workdirs are invalid")
    agent_workdirs = {
        item.alias: Path(item.root).resolve(strict=True)
        for item in workdirs
        if item.agent_mode != "disabled" or item.policy.repo_ops_enabled
    }
    if not 1 <= len(agent_workdirs) <= 16:
        raise DarwinOwnedPolicyError("owned v2 staging requires Bridge-enabled workdirs")

    from .darwin_instance_bridge_identity import named_owned_bridge_resources
    from .darwin_owned_v2_binding import (
        generate_owned_v2_grant_materials,
        write_owned_v2_binding,
    )

    resources = named_owned_bridge_resources(identity, home=home)
    owner_root = instance_service_home(identity, home=home)
    if (owner_root / "services.json").exists() or (owner_root / "services.json").is_symlink():
        raise DarwinOwnedPolicyError("owned v2 staging requires a new Core instance")
    preflight_named_owned_bridge(
        identity, config_path, home=home, default_bridge_config=default_bridge_config
    )
    try:
        configured_workdirs, settings = load_native_config(Path(config_path))
        configured_scope = render_request_from_config(configured_workdirs, settings)
        supplied_scope = render_request_from_config(workdirs, settings)
        if supplied_scope != configured_scope:
            raise ValueError("supplied workdirs differ from the native config")
        materials = generate_owned_v2_grant_materials(identity, tuple(agent_workdirs))
        request = configured_scope
    except Exception as exc:
        raise DarwinOwnedPolicyError("owned v2 grant scope cannot be prepared") from exc
    bridge_python = Path(bridge_python).expanduser()
    if (
        not bridge_python.is_absolute()
        or not bridge_python.is_file()
        or not os.access(bridge_python, os.X_OK)
    ):
        raise DarwinOwnedPolicyError("owned Bridge executable identity is invalid")
    envelope = {
        "policy": request,
        "grant_materials": [material.bridge_input() for material in materials],
    }
    payload = json.dumps(envelope, separators=(",", ":"), ensure_ascii=False)
    if len(payload.encode("utf-8")) > 262_144:
        raise DarwinOwnedPolicyError("owned v2 grant request exceeds the size budget")

    try:
        process = subprocess.run(
            [
                str(bridge_python),
                "-B",
                "-m",
                "serverfs_agent_bridge.macos_owned_config",
                "--instance",
                identity.instance_id,
                "--bridge-home",
                str(resources.config_path.parent),
            ],
            input=payload,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DarwinOwnedPolicyError("owned v2 grant staging subprocess failed") from exc
    if process.returncode != 0:
        raise DarwinOwnedPolicyError("owned v2 grant staging refused")

    try:
        result = json.loads(process.stdout)
        if (
            not isinstance(result, dict)
            or result.get("instance_id") != identity.instance_id
            or result.get("state") != "staged-not-registered"
            or result.get("private_config") != "verified"
            or result.get("protocol_mode") != "v2"
            or not isinstance(result.get("grant_scopes"), list)
        ):
            raise ValueError("invalid owned v2 grant response")
        scopes = result["grant_scopes"]
        if {item.get("alias") for item in scopes if isinstance(item, dict)} != set(agent_workdirs):
            raise ValueError("owned v2 grant aliases differ from Core policy")
        bridge_claims = workdir_claims_from_private_bridge_config(resources.config_path)
        expected = {
            (item.alias, str(Path(item.root).resolve(strict=True)), item.read_only)
            for item in workdirs
            if item.agent_mode != "disabled" or item.policy.repo_ops_enabled
        }
        observed = {
            (item.alias, str(Path(item.root).resolve(strict=True)), item.read_only)
            for item in bridge_claims
            if item.alias in agent_workdirs
        }
        if observed != expected:
            raise ValueError("owned v2 Bridge workdir grants differ from Core policy")
        bridge_document = json.loads(resources.config_path.read_text(encoding="utf-8"))
        if (
            bridge_document.get("protocol_mode") != "v2"
            or bridge_document.get("bridge_id") != identity.instance_id
            or bridge_document.get("socket_path") != str(resources.socket_path)
        ):
            raise ValueError("owned v2 Bridge identity differs from Core")
        binding_path = write_owned_v2_binding(
            identity,
            home=home,
            bridge_config_path=resources.config_path,
            socket_path=resources.socket_path,
            agent_workdirs=agent_workdirs,
            materials=materials,
            grant_scopes=scopes,
        )
        if not binding_path.is_file():
            raise ValueError("owned v2 Core binding was not persisted")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise DarwinOwnedPolicyError(
            "owned v2 grants were staged but Core binding verification failed; retain state"
        ) from exc
    return {
        "instance_id": identity.instance_id,
        "state": "staged-not-registered",
        "bridge_mode": "owned",
        "protocol_mode": "v2",
        "agent_workdirs": len(agent_workdirs),
        "private_binding": "verified",
    }


def check_owned_bridge_policy(
    identity: InstanceIdentity,
    config_path: Path,
    bridge_python: Path,
    *,
    home: Path | None = None,
    default_bridge_config: Path | None = None,
) -> dict[str, str | int]:
    """Verify an existing named Bridge config through its owner's own checker.

    Source-only: no service registration, config rewriting, Agent process or
    permission repair. Any drift or unknown peer Bridge fails closed.
    """
    if sys.platform != "darwin":
        raise DarwinOwnedPolicyError("owned Bridge policy checking requires macOS")
    if identity.bridge_mode != "owned" or identity.bridge_id != identity.instance_id:
        raise DarwinOwnedPolicyError("named Bridge must be independently owned")
    resources = named_owned_bridge_resources(identity, home=home)
    _private_directory(resources.config_path.parent)
    preflight_named_owned_bridge(
        identity, config_path, home=home, default_bridge_config=default_bridge_config
    )
    try:
        workdirs, settings = load_native_config(Path(config_path))
        request = render_request_from_config(workdirs, settings)
        python = Path(bridge_python).expanduser()
        if not python.is_absolute() or not python.is_file() or not os.access(python, os.X_OK):
            raise DarwinOwnedPolicyError("Bridge interpreter is not executable")
        payload = json.dumps(request, separators=(",", ":"), ensure_ascii=False)
        if len(payload.encode("utf-8")) > 262_144:
            raise DarwinOwnedPolicyError("owned Bridge policy exceeds the size budget")
        result = subprocess.run(
            [
                str(python),
                "-B",
                "-m",
                "serverfs_agent_bridge.macos_owned_config",
                "--instance",
                identity.instance_id,
                "--bridge-home",
                str(resources.config_path.parent),
                "--check",
            ],
            input=payload,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise DarwinOwnedPolicyError("named Bridge policy verification unavailable") from exc
    if result.returncode != 0:
        raise DarwinOwnedPolicyError("named Bridge policy is stale or its identity is invalid")
    try:
        response = json.loads(result.stdout)
        if (
            not isinstance(response, dict)
            or response.get("instance_id") != identity.instance_id
            or response.get("state") != "verified-not-registered"
            or response.get("policy") != "synced"
            or response.get("workdirs")
            != len(
                [wd for wd in workdirs if wd.agent_mode != "disabled" or wd.policy.repo_ops_enabled]
            )
        ):
            raise ValueError("invalid Bridge check response")
    except (ValueError, TypeError) as exc:
        raise DarwinOwnedPolicyError("named Bridge checker returned an invalid response") from exc
    return {
        "instance_id": identity.instance_id,
        "bridge_mode": "owned",
        "policy": "synced",
        "workdirs": response["workdirs"],
        "result": "PASS_READ_ONLY",
    }
