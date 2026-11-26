"""Read-only factory for a named Core's grant-scoped Shared Agent client."""

from __future__ import annotations

from pathlib import Path

from .darwin_shared_agent_adapter import DarwinSharedAgentAdapter
from .darwin_shared_core_bootstrap import SharedCoreBootstrapError, prepare_shared_v2_client
from .darwin_shared_multi_agent_adapter import DarwinSharedMultiAgentAdapter
from .instance_identity import DEFAULT_INSTANCE_ID, InstanceIdentity


async def prepare_shared_agent_adapter(
    identity: InstanceIdentity,
    trusted_core_config_path: Path,
    expected_socket_path: Path,
    *,
    home: Path | None = None,
    timeout_seconds: float = 5,
) -> DarwinSharedAgentAdapter | DarwinSharedMultiAgentAdapter:
    """Authenticate an existing Shared v2 Bridge and return its narrow Agent adapter."""
    if (
        not isinstance(identity, InstanceIdentity)
        or identity.instance_id == DEFAULT_INSTANCE_ID
        or identity.bridge_mode != "shared"
        or identity.bridge_id is None
    ):
        raise SharedCoreBootstrapError("shared Bridge client could not be verified")
    client = await prepare_shared_v2_client(
        identity,
        trusted_core_config_path,
        expected_socket_path,
        home=home,
        timeout_seconds=timeout_seconds,
    )
    # Both the alias map and its grants have been authenticated at bootstrap.
    try:
        if isinstance(client, dict):
            return DarwinSharedMultiAgentAdapter(client)
        alias = getattr(client, "_workdir_alias", None)
        return DarwinSharedAgentAdapter(client, alias)
    except (TypeError, ValueError):
        raise SharedCoreBootstrapError("shared Bridge client could not be verified") from None
