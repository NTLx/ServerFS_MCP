"""Bind Shared Core file mutations to the live owner's actual writer lease.

No Core-created lock, credential provisioning or owner lifecycle operation.
An older owner can still serve read/Agent calls; file mutations require the
new authenticated lease proof and fail closed otherwise.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Iterator
from pathlib import Path

from .agent_leases import mutation_agent_lease
from .core_bridge_client_v2 import CoreBridgeClientV2, CoreBridgeClientV2Error
from .darwin_instance_shared_binding import (
    DarwinSharedBindingError,
    _safe_regular_private_file,
)
from .darwin_instances import instance_service_home
from .darwin_shared_agent_adapter import DarwinSharedAgentAdapter
from .darwin_shared_binding_private import load_shared_core_binding
from .darwin_shared_multi_agent_adapter import DarwinSharedMultiAgentAdapter
from .errors import AgentLeaseError
from .instance_identity import InstanceIdentity
from .lease_identity import alias_lease_id, writer_lease_proof


class SharedMutationLease:
    def __init__(self, lock_dir: Path | None, clients: dict[str, CoreBridgeClientV2]):
        self.lock_dir = lock_dir
        self._clients = clients

    @contextlib.contextmanager
    def hold(self, lease_id: str, workdir_root: Path) -> Iterator[None]:
        client = self._clients.get(lease_id)
        if client is None or self.lock_dir is None:
            raise AgentLeaseError("shared writer lease is unavailable")

        def verify(fd: int) -> None:
            try:
                proof = writer_lease_proof(
                    self.lock_dir, lease_id, workdir_root, opened=os.fstat(fd)
                )
                client.verify_writer_lease(proof)
                # Detect replacement during the authenticated exchange. The
                # owner must not relocate the lease namespace while running.
                if (
                    writer_lease_proof(self.lock_dir, lease_id, workdir_root, opened=os.fstat(fd))
                    != proof
                ):
                    raise ValueError("writer lease changed")
            except (OSError, ValueError, CoreBridgeClientV2Error):
                raise AgentLeaseError("shared writer lease cannot be verified") from None

        with mutation_agent_lease(self.lock_dir, lease_id, enabled=True, verify=verify):
            yield


def prepare_shared_mutation_lease(
    identity: InstanceIdentity,
    expected_socket: Path,
    adapter: DarwinSharedAgentAdapter | DarwinSharedMultiAgentAdapter,
) -> SharedMutationLease:
    core_home = instance_service_home(identity)
    binding = load_shared_core_binding(
        identity=identity,
        trusted_core_home=core_home,
        binding_path=core_home / "shared-bridge-binding.json",
        expected_socket_path=expected_socket,
    )
    # Use the same independently authenticated adapter, not a new key or a
    # caller-supplied alias. The signed request binds each proof to its grant.
    if isinstance(adapter, DarwinSharedAgentAdapter):
        adapters = {adapter._workdir_alias: adapter}
    elif isinstance(adapter, DarwinSharedMultiAgentAdapter):
        adapters = adapter._adapters
    else:
        raise DarwinSharedBindingError("invalid shared mutation adapter")
    grants = binding.grants or (binding.grant,)
    if set(adapters) != {grant.workdir_alias for grant in grants}:
        raise DarwinSharedBindingError("shared mutation grant mismatch")
    clients = {}
    for alias, selected in adapters.items():
        client = selected._client
        if not isinstance(client, CoreBridgeClientV2):
            raise DarwinSharedBindingError("invalid shared mutation client")
        clients[alias_lease_id(alias)] = client

    source = _safe_regular_private_file(binding.bridge_config_path)
    document = json.loads(source.read_text(encoding="utf-8"))
    if (
        not isinstance(document, dict)
        or document.get("protocol_mode") != "v2"
        or document.get("bridge_id") != identity.bridge_id
        or document.get("socket_path") != str(expected_socket)
    ):
        raise DarwinSharedBindingError("shared mutation owner mismatch")
    raw = document.get("lock_dir")
    lock_dir = Path(raw) if isinstance(raw, str) and Path(raw).is_absolute() else None
    # No guessed default or local fallback: only explicit owner configuration
    # can be proven. The live signed proof verifies its real artifacts on use.
    return SharedMutationLease(lock_dir, clients)
