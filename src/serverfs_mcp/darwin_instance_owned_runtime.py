"""macOS named owned Core+Bridge source staging. No service registration.

An owned candidate is created atomically under a new private instance root:
signed app, Core auth snapshot, Bridge-owned 0600 policy and a pinned service
manifest. The native ServiceManager still refuses named Bridge registration.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from .darwin_instance_bridge_identity import preflight_named_owned_bridge
from .darwin_instance_owned_binding import (
    DarwinOwnedBindingError,
    resolve_owned_core_binding,
)
from .darwin_instance_owned_staging import stage_owned_bridge_policy
from .darwin_instance_runtime import (
    _assert_available_port,
    _check_dir,
    _check_path,
    _new_private_file,
    _prepare_file_only,
)
from .darwin_instances import (
    APP_NAME,
    _verify_source_app,
    instance_service_home,
    stage_instance_app,
)
from .instance_identity import InstanceIdentity


class DarwinOwnedStageError(ValueError):
    """The named owned installation could not be safely staged."""


def stage_owned_core_bridge(
    identity: InstanceIdentity,
    config_path: Path,
    env_file: Path,
    port: int,
    hostname: str,
    template_app: Path,
    bridge_python: Path,
    *,
    connector_ids: dict[str, list[str] | tuple[str, ...]] | None = None,
    agent_protocol: str = "v1",
    home: Path | None = None,
    default_bridge_config: Path | None = None,
) -> dict[str, str | int]:
    """Stage a new owned candidate, without installing or activating jobs."""
    if (
        identity.instance_id == "default"
        or identity.bridge_mode != "owned"
        or identity.bridge_id != identity.instance_id
    ):
        raise DarwinOwnedStageError("independent named owned Bridge is required")
    if agent_protocol not in {"v1", "v2"}:
        raise DarwinOwnedStageError("owned Bridge protocol is invalid")
    root = instance_service_home(identity, home=home)
    base = (Path.home() if home is None else Path(home)) / ("Library/Application Support/ServerFS")
    _check_dir(base)
    instances = base / "instances"
    if instances.exists() or instances.is_symlink():
        _check_dir(instances, private=True)
    _check_path(root, kind="named owned private root")
    _assert_available_port(base, identity.instance_id, port)
    document, snapshot = _prepare_file_only(
        identity, config_path, env_file, port, hostname, root, owned=True
    )
    preflight_named_owned_bridge(
        identity, config_path, home=home, default_bridge_config=default_bridge_config
    )
    python = Path(bridge_python).expanduser()
    if not python.is_absolute() or not python.is_file() or not os.access(python, os.X_OK):
        raise DarwinOwnedStageError("independent Bridge Python interpreter is unavailable")
    _verify_source_app(Path(template_app))
    created_instances = False
    created_candidate = False
    try:
        if not instances.exists():
            instances.mkdir(mode=0o700)
            created_instances = True
        owner = instances / identity.instance_id
        owner.mkdir(mode=0o700)
        created_candidate = True
        root.mkdir(mode=0o700)
        logs = root / "logs"
        logs.mkdir(mode=0o700)
        (root / "data").mkdir(mode=0o700)
        stage_instance_app(
            identity,
            Path(template_app),
            root / APP_NAME,
            **({"connector_ids": connector_ids} if connector_ids else {}),
        )
        _new_private_file(root / "http-origin-environment.json", snapshot)
        if agent_protocol == "v2":
            from .darwin_instance_owned_staging import stage_owned_v2_grant_binding
            from .native_config import load_native_config

            configured_workdirs, _settings = load_native_config(Path(config_path))
            stage_owned_v2_grant_binding(
                identity,
                config_path,
                python,
                configured_workdirs,
                home=home,
                default_bridge_config=default_bridge_config,
            )
            document["services"]["http-origin"]["arguments"].extend(
                ["--macos-agent-protocol", "v2"]
            )
        else:
            stage_owned_bridge_policy(
                identity,
                config_path,
                python,
                home=home,
                default_bridge_config=default_bridge_config,
            )
        from .darwin_instance_bridge_identity import named_owned_bridge_resources

        resources = named_owned_bridge_resources(identity, home=home)
        document["services"]["agent-bridge"] = {
            "executable": str(Path(sys.executable).absolute()),
            "arguments": [
                "-B",
                "-m",
                "serverfs_mcp.darwin_owned_bridge_supervisor",
                "--instance",
                identity.instance_id,
            ],
            "bridge_python": str(python),
            "bridge_config": str(resources.config_path),
            **({"protocol_mode": "v2"} if agent_protocol == "v2" else {}),
            "environment_file": None,
            "working_directory": str(root / "data"),
            "stdout_log": str(logs / "bridge.stdout.log"),
            "stderr_log": str(logs / "bridge.stderr.log"),
            "origin_owner": "independent-bridge",
        }
        _new_private_file(
            root / "services.json",
            json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        )
        try:
            if agent_protocol == "v2":
                from .darwin_owned_v2_bootstrap import load_owned_v2_clients

                load_owned_v2_clients(identity, Path(config_path), home=home)
            else:
                resolve_owned_core_binding(
                    identity,
                    config_path,
                    port,
                    home=home,
                    default_bridge_config=default_bridge_config,
                    environ={
                        "SERVERFS_MACOS_INSTANCE_ID": identity.instance_id,
                        "SERVERFS_DATA_HOME": str(root / "data"),
                        "SERVERFS_FILE_INGRESS_ENABLED": "false",
                    },
                )
        except (DarwinOwnedBindingError, ValueError, OSError) as exc:
            raise DarwinOwnedStageError("owned Core/Bridge staged identity failed") from exc
    except BaseException:
        if created_candidate:
            shutil.rmtree(instances / identity.instance_id)
        if created_instances:
            try:
                instances.rmdir()
            except OSError:
                pass
        raise
    return {
        "instance_id": identity.instance_id,
        "bridge_mode": "owned",
        "core_port": port,
        "state": "staged-not-registered",
    }
