"""Contract for I5 two *genuinely Core-bound* nonproduction Windows connectors."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SOURCE = (
    Path(__file__).resolve().parents[1]
    / "deployment/windows/v0250-i5-bridge-public-connector-stage.py"
)
spec = importlib.util.spec_from_file_location("i5_native_cloudflare_stage_contract", SOURCE)
assert spec is not None and spec.loader is not None
stage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stage)


def test_cores_bind_two_distinct_local_ports_and_nonprod_token_sources() -> None:
    assert stage.CORES == (
        ("i5-core-a", "i5-edge-a", "i7-edge-a", 18873),
        ("i5-core-b", "i5-edge-b", "i7-edge-b", 18874),
    )
    assert len({i for i, _, _, _ in stage.CORES}) == 2
    assert len({c for _, c, _, _ in stage.CORES}) == 2
    assert len({t for _, _, t, _ in stage.CORES}) == 2
    assert stage.SLOT == "i5-public-cloudflare-owner-1"


def test_create_only_private_token_and_binary_copy_without_secret_logging() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    for item in (
        'storage.prepare("cloudflare", core, connector)',
        "i7.private_token_record(source, sid=sid)",
        "installer._new_private_file(target, data, sid)",
        "digest == new_digest and account == new_account and uuid == new_uuid",
        "installer._protect_tree(binfile.parent, sid)",
        "i7binary.sha(binfile) == i7binary.CLOUDFLARED_SHA256",
        "storage.stage(manifest)",
        "Scheduler().install(manifest)",
        "manifest.task_name() == InstanceIdentity(core).windows_task",
        "_assert_instance_isolation(manifest)",
        "i7.postflight()",
        "I5_PUBLIC_TWO_DISABLED_CORE_BOUND_TASKS=PASS",
    ):
        assert item in source
    for unsafe in (
        "print(data)",
        "print(token)",
        "print(source.read_bytes())",
        "git push",
        "cloudflare.request(",
        "Scheduler().start(",
        "Scheduler().stop(",
        "os.system(",
        "dangerouslyDisableSandbox",
    ):
        assert unsafe not in source


def test_connector_owner_never_claims_i7_core_or_prod_owner_role() -> None:
    source = SOURCE.read_text("utf-8")
    assert 'storage.load("core", core)' in source
    assert '"port": port' in source
    assert '"hostname": name' in source
    assert 'manifest.settings["hostname"] == core + ".local"' in source
    assert 'storage.home("cloudflare", "i7-public", old_name)' in source
    assert 'storage.home("cloudflare", core, connector)' in source


def test_sanitized_refusal() -> None:
    with pytest.raises(stage.I5PublicConnectorError):
        stage.require(False, "bad-test-state")
    stage.require(True, "not-an-error")


def test_nonprod_stage_has_no_cloudflare_control_plane_or_model_execution() -> None:
    source = SOURCE.read_text("utf-8")
    for forbidden in (
        "send_agent_task",
        "task.submit",
        "submit_agent_task",
        "claude --",
        "SERVERFS_HTTP_API_KEY=",
        "CreateTunnel",
        "PUT /accounts",
    ):
        assert forbidden not in source
