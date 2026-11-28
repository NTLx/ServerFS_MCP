"""Linux v2 Core and Bridge writer leases share alias identities."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent_bridge/src"))

from serverfs_agent_bridge.leases import LeaseManager  # noqa: E402

from serverfs_mcp import lease_identity  # noqa: E402
from serverfs_mcp.agent_leases import AgentLeaseError  # noqa: E402
from serverfs_mcp.config import Settings  # noqa: E402
from serverfs_mcp.errors import WorkdirBusyError  # noqa: E402
from serverfs_mcp.tools import _mutation_lease  # noqa: E402
from serverfs_mcp.workdirs import Workdir  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux-only writer lease test")


def _resolved(alias: str) -> SimpleNamespace:
    # This models the Docker workdir mapping: Core still has legacy slot 01.
    return SimpleNamespace(workdir=Workdir(alias, Path("/workdirs/01"), None, legacy_slot=1))


def test_linux_v2_core_mutation_shares_bridge_alias_lock(tmp_path: Path) -> None:
    lock_dir = tmp_path / "locks"
    first = lease_identity.alias_lease_id("Repo")
    second = lease_identity.alias_lease_id("Docs")
    bridge = LeaseManager(lock_dir, lease_ids=(first, second))
    settings = Settings(
        agent_bridge_enabled=True,
        linux_v0250_bridge_binding_file="/private/core-bridge-binding.json",
        agent_lock_dir=str(lock_dir),
    )

    with bridge.acquire_exclusive(first):
        with pytest.raises(WorkdirBusyError):
            with _mutation_lease(settings, _resolved("Repo")):
                pytest.fail("Core mutation entered while Bridge held the Repo alias lock")
        with _mutation_lease(settings, _resolved("Docs")):
            pass

    # A legacy 01.lock alone cannot substitute for the required alias artifact.
    (lock_dir / lease_identity.lock_artifact_name(lease_identity.slot_lease_id(1))).touch()
    (lock_dir / lease_identity.lock_artifact_name(first)).unlink()
    with pytest.raises(AgentLeaseError):
        with _mutation_lease(settings, _resolved("Repo")):
            pytest.fail("Core mutation entered without its Bridge-created alias lock")


def test_legacy_core_and_linux_v1_keep_slot_lease(tmp_path: Path) -> None:
    lock_dir = tmp_path / "locks"
    slot = lease_identity.slot_lease_id(1)
    bridge = LeaseManager(lock_dir, lease_ids=(slot,))
    settings = Settings(agent_bridge_enabled=True, agent_lock_dir=str(lock_dir))
    with bridge.acquire_exclusive(slot):
        with pytest.raises(AgentLeaseError):
            with _mutation_lease(settings, _resolved("Repo")):
                pytest.fail("legacy Core mutation entered while slot 01 was held")
