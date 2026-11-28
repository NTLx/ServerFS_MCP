"""Independent v2 Bridge owner lifecycle: synthetic Windows-only boundaries.

No native process, named Job, lease, Task Scheduler, pipe or provider is created.
All lifecycle dependencies are injected with in-memory fakes.
"""

from __future__ import annotations

import asyncio
import io
from pathlib import Path
from types import SimpleNamespace

import pytest

from serverfs_mcp.windows_v25 import runtime
from serverfs_mcp.windows_v25.bridge_v2_owner import (
    BridgeV2OwnerError,
    wait_for_signed_bridge_v2_ready,
)
from serverfs_mcp.windows_v25.legacy_bridge_ownership import LegacyBridgeOwnerError
from serverfs_mcp.windows_v25.workdir_ownership import WorkdirClaim
from test_windows_v25_bridge_v2_owner import _fixture, _plan


class FakeProcess:
    def __init__(self):
        self.stdin = io.BytesIO()
        self._polls = 0

    def poll(self):
        self._polls += 1
        return None if self._polls < 3 else 0


class FakeJob:
    def __init__(self, name: str, events):
        self.name = name
        self.events = events

    def __enter__(self):
        self.events.append(("job-open", self.name))
        return self

    def __exit__(self, *_args):
        self.events.append(("job-close", self.name))

    def contains_pid(self, pid: int):
        return pid == 99


def _patch_owner(tmp_path, monkeypatch, *, fail_ready=False, proxy=None):
    from serverfs_mcp import agent_lifecycle, native_lifecycle, windows_job
    from serverfs_mcp.windows_v25 import bridge_v2_owner, named_agent_proxy, readiness, storage
    from serverfs_mcp.windows_v25 import cli as native_cli

    manifest, _cfg, _grants, home, _worker = _fixture(tmp_path)
    plan = _plan(_fixture(tmp_path))
    events = []
    process = FakeProcess()

    class FakeLease:
        def __init__(self, *, root, artifact_name):
            events.append(("lease-path", root, artifact_name))
            assert Path(root) == home
            assert artifact_name == plan.lease_artifact

        def acquire(self):
            events.append("lease-acquired")
            return self

        def close(self):
            events.append("lease-released")

    def fake_spawn(*, config_path, child_env, job, argv):
        events.append(("spawn", config_path, tuple(argv)))
        assert config_path == plan.config_path
        assert argv[0] == str(plan.bridge_python)
        assert job.name == plan.job_name
        assert not any("SECRET" in name for name in child_env)
        return process

    async def fake_ready(real_plan, proc, *, server_pid_validator):
        assert real_plan == plan and proc is process
        assert server_pid_validator(99)
        events.append("signed-ping")
        if fail_ready:
            raise BridgeV2OwnerError("synthetic denied health check")

    monkeypatch.setattr(bridge_v2_owner, "load_native_bridge_plan", lambda _: plan)
    monkeypatch.setattr(named_agent_proxy, "read_owned_named_agent_proxy", lambda **_kw: proxy)
    # A read-only Bridge has an explicit WorkdirClaim, but does not need
    # to query or take over the host legacy Bridge writable ownership.
    monkeypatch.setattr(
        bridge_v2_owner,
        "load_owned_bridge_workdir_claims",
        lambda _: (WorkdirClaim("bridge-a", r"C:\FixtureReadOnly", False),),
    )
    monkeypatch.setattr(
        native_cli, "_assert_instance_isolation", lambda _: events.append("isolation-check")
    )
    monkeypatch.setattr(bridge_v2_owner, "wait_for_signed_bridge_v2_ready", fake_ready)
    monkeypatch.setattr(storage, "home", lambda component, iid: home)
    monkeypatch.setattr(native_lifecycle, "LifecycleLease", FakeLease)
    monkeypatch.setattr(windows_job, "WindowsJob", lambda name: FakeJob(name, events))
    monkeypatch.setattr(agent_lifecycle, "spawn_bridge", fake_spawn)

    def fake_bridge_env(selected_proxy):
        events.append(("bridge-env", selected_proxy))
        return {}

    def fake_bootstrap(selected_proxy, *, prior_bridge_execution_stopped=False):
        events.append(("bootstrap-proxy", selected_proxy, prior_bridge_execution_stopped))
        return b"bootstrap\n"

    monkeypatch.setattr(agent_lifecycle, "bridge_child_environment", fake_bridge_env)
    monkeypatch.setattr(agent_lifecycle, "bootstrap_frame_bytes", fake_bootstrap)
    monkeypatch.setattr(
        agent_lifecycle, "request_graceful_shutdown", lambda child: events.append("graceful-stop")
    )

    def fake_exit(child):
        events.append("await-child-exit")
        return True

    monkeypatch.setattr(agent_lifecycle, "await_graceful_exit", fake_exit)
    monkeypatch.setattr(readiness, "publish", lambda _: events.append("publish"))
    monkeypatch.setattr(runtime.time, "sleep", lambda _: None)
    return manifest, plan, process, events


def test_independent_bridge_lifecycle_owns_only_its_job_and_lease(tmp_path, monkeypatch):
    manifest, plan, process, events = _patch_owner(tmp_path, monkeypatch)
    assert runtime.run_independent_bridge_v2(manifest) == 3
    assert "signed-ping" in events
    assert "publish" in events
    assert events.index("isolation-check") < events.index("lease-acquired")
    assert events.index("lease-acquired") < events.index(("job-open", plan.job_name))
    assert events.index("signed-ping") < events.index("publish")
    assert events.index("graceful-stop") < events.index(("job-close", plan.job_name))
    assert events.index(("job-close", plan.job_name)) < events.index("lease-released")
    assert process.stdin.getvalue() == b"bootstrap\n"
    assert all("Native Tunnel" not in str(item) for item in events)


def test_named_bridge_passes_private_proxy_only_to_clean_child_and_bootstrap(
    tmp_path,
    monkeypatch,
) -> None:
    from serverfs_mcp.agent_proxy import AgentProxyConfig

    proxy = AgentProxyConfig(
        url="http://127.0.0.1:20170",
        no_proxy="127.0.0.1,localhost,::1",
    )
    manifest, _plan_value, process, events = _patch_owner(tmp_path, monkeypatch, proxy=proxy)
    assert runtime.run_independent_bridge_v2(manifest) == 3
    assert ("bridge-env", proxy) in events
    assert ("bootstrap-proxy", proxy, True) in events
    assert process.stdin.getvalue() == b"bootstrap\n"


def test_failed_signed_readiness_cleans_exact_bridge_only(tmp_path, monkeypatch):
    manifest, plan, _process, events = _patch_owner(tmp_path, monkeypatch, fail_ready=True)
    with pytest.raises(BridgeV2OwnerError, match="synthetic denied"):
        runtime.run_independent_bridge_v2(manifest)
    assert "publish" not in events
    assert "graceful-stop" in events
    assert ("job-close", plan.job_name) in events
    assert events[-1] == "lease-released"


def test_writable_v2_owner_denies_unverified_legacy_before_lease(tmp_path, monkeypatch) -> None:
    from serverfs_mcp.windows_v25 import bridge_v2_owner
    from serverfs_mcp.windows_v25 import cli as native_cli

    manifest, _plan_value, _process, events = _patch_owner(tmp_path, monkeypatch)
    monkeypatch.setattr(
        bridge_v2_owner,
        "load_owned_bridge_workdir_claims",
        lambda _: (WorkdirClaim("bridge-a", r"C:\WritableFixture", True),),
    )

    def unverified():
        raise OSError("synthetic legacy state unavailable")

    monkeypatch.setattr(native_cli, "_legacy_bridge_active", unverified)
    with pytest.raises(LegacyBridgeOwnerError, match="legacy Bridge writable"):
        runtime.run_independent_bridge_v2(manifest)
    assert "lease-acquired" not in events
    assert not any(isinstance(e, tuple) and e[0] == "spawn" for e in events)


def test_waiter_requires_os_owner_generation_proof_before_transport(tmp_path) -> None:
    plan = _plan(_fixture(tmp_path))
    with pytest.raises(BridgeV2OwnerError, match="owner-generation proof"):
        asyncio.run(
            wait_for_signed_bridge_v2_ready(
                plan, SimpleNamespace(poll=lambda: None), server_pid_validator=None
            )
        )


def test_waiter_rejects_signed_health_with_unexpected_result(tmp_path, monkeypatch) -> None:
    from serverfs_mcp.agent_core_grant_client import CoreGrantBridgeClient

    plan = _plan(_fixture(tmp_path))
    monkeypatch.setattr(
        CoreGrantBridgeClient,
        "__init__",
        lambda self, *_a, **_k: None,
    )

    async def fake_call(_self, method, params):
        assert method == "bridge.ping" and params == {"workdir": "Repo"}
        return {"status": "unhealthy"}

    monkeypatch.setattr(CoreGrantBridgeClient, "call", fake_call)
    with pytest.raises(BridgeV2OwnerError, match="response is invalid"):
        asyncio.run(
            wait_for_signed_bridge_v2_ready(
                plan, SimpleNamespace(poll=lambda: None), server_pid_validator=lambda _: True
            )
        )
