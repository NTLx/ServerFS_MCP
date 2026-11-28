"""Opt-in, read-only bootstrap for a named Core's Shared Bridge v2 client."""

from __future__ import annotations

import math
from pathlib import Path

from .core_bridge_client_v2 import CoreBridgeClientV2, CoreBridgeClientV2Error
from .darwin_instance_shared_binding import (
    DarwinSharedBindingError,
    preflight_shared_core_binding,
)
from .darwin_instances import instance_service_home
from .darwin_shared_binding_private import load_shared_core_binding
from .darwin_shared_bridge_readiness import probe_shared_v2_bridge
from .instance_identity import DEFAULT_INSTANCE_ID, InstanceIdentity


class SharedCoreBootstrapError(Exception):
    """The owner-provisioned Shared Bridge client could not be verified."""


async def prepare_shared_v2_client(
    identity: InstanceIdentity,
    trusted_core_config_path: Path,
    expected_socket_path: Path,
    *,
    home: Path | None = None,
    timeout_seconds: float = 5,
) -> CoreBridgeClientV2 | dict[str, CoreBridgeClientV2]:
    """Load and authenticate an explicitly configured named Shared Bridge.

    This performs no service lifecycle or credential provisioning operations.
    """
    if (
        not isinstance(identity, InstanceIdentity)
        or identity.instance_id == DEFAULT_INSTANCE_ID
        or identity.bridge_mode != "shared"
        or identity.bridge_id is None
        or not isinstance(trusted_core_config_path, Path)
        or not isinstance(expected_socket_path, Path)
        or type(timeout_seconds) not in (int, float)
        or not 0 < timeout_seconds <= 30
        or not math.isfinite(timeout_seconds)
        or (home is not None and not isinstance(home, Path))
    ):
        raise SharedCoreBootstrapError("shared Bridge client could not be verified")

    try:
        core_home = instance_service_home(identity, home=home)
        binding_path = core_home / "shared-bridge-binding.json"
        binding = load_shared_core_binding(
            identity=identity,
            trusted_core_home=core_home,
            binding_path=binding_path,
            expected_socket_path=expected_socket_path,
        )
        socket_path = preflight_shared_core_binding(identity, trusted_core_config_path, binding)
        grants = binding.grants or (binding.grant,)
        clients = {
            grant.workdir_alias: CoreBridgeClientV2(
                socket_path,
                bridge_id=grant.bridge_id,
                core_id=grant.core_id,
                key_id=grant.key_id,
                secret=grant.secret,
                workdir_alias=grant.workdir_alias,
                policy_revision=grant.policy_revision,
                timeout_seconds=timeout_seconds,
            )
            for grant in grants
        }
        # Every alias must independently prove live owner authorization.
        for client in clients.values():
            if not await probe_shared_v2_bridge(
                client,
                expected_socket_path,
                timeout_seconds=timeout_seconds,
                require_private_parent=True,
            ):
                raise SharedCoreBootstrapError("shared Bridge client could not be verified")
        return clients if len(clients) > 1 else next(iter(clients.values()))
    except SharedCoreBootstrapError:
        raise
    except (
        DarwinSharedBindingError,
        CoreBridgeClientV2Error,
        OSError,
        TimeoutError,
    ):
        raise SharedCoreBootstrapError("shared Bridge client could not be verified") from None
