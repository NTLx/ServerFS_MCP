"""Read-only bootstrap for an explicitly staged named-owned v2 Agent client."""

from __future__ import annotations

import math
from pathlib import Path

from .core_bridge_client_v2 import CoreBridgeClientV2Error
from .darwin_instance_bridge_identity import named_owned_bridge_resources
from .darwin_owned_v2_agent_adapter import DarwinOwnedV2AgentAdapter
from .darwin_owned_v2_binding import _owned_identity
from .darwin_owned_v2_bootstrap import (
    DarwinOwnedV2BootstrapError,
    load_owned_v2_clients,
)
from .darwin_shared_bridge_readiness import probe_shared_v2_bridge
from .instance_identity import InstanceIdentity


class DarwinOwnedV2AgentFactoryError(ValueError):
    """The named-owned v2 Agent client could not be authenticated."""


async def prepare_owned_v2_agent_adapter(
    identity: InstanceIdentity,
    config_path: Path,
    *,
    home: Path | None = None,
    timeout_seconds: float = 5,
) -> DarwinOwnedV2AgentAdapter:
    if (
        not isinstance(config_path, Path)
        or type(timeout_seconds) not in (int, float)
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= 30
    ):
        raise DarwinOwnedV2AgentFactoryError("owned Bridge client could not be verified")
    try:
        _owned_identity(identity)
        clients = load_owned_v2_clients(
            identity, config_path, home=home, timeout_seconds=timeout_seconds
        )
        socket_path = named_owned_bridge_resources(identity, home=home).socket_path
        for client in clients.values():
            if not await probe_shared_v2_bridge(
                client,
                socket_path,
                timeout_seconds=timeout_seconds,
                require_private_parent=True,
                require_task_list=client.agent_enabled,
                require_repo_ops=client.repo_ops_enabled,
            ):
                raise DarwinOwnedV2AgentFactoryError("owned Bridge client could not be verified")
        return DarwinOwnedV2AgentAdapter(
            clients,
            agent_aliases={alias for alias, client in clients.items() if client.agent_enabled},
            repo_aliases={alias for alias, client in clients.items() if client.repo_ops_enabled},
        )
    except DarwinOwnedV2AgentFactoryError:
        raise
    except (DarwinOwnedV2BootstrapError, CoreBridgeClientV2Error, OSError, ValueError):
        raise DarwinOwnedV2AgentFactoryError("owned Bridge client could not be verified") from None
