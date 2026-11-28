"""Shared Core instances participate in the native macOS resource inventory.

The inventory must accept strict, port-owning shared manifests, but not
invent shared Bridge ownership or accept unknown/malformed instance records.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from serverfs_mcp import darwin_instance_runtime as runtime


def _base(tmp_path: Path) -> Path:
    base = tmp_path / "Library/Application Support/ServerFS"
    instances = base / "instances"
    instances.mkdir(parents=True, mode=0o700)
    instances.chmod(0o700)
    return base


def _shared(base: Path, name: str, port: int) -> Path:
    home = base / "instances" / name
    root = home / "service-manager"
    root.mkdir(parents=True, mode=0o700)
    home.chmod(0o700)
    root.chmod(0o700)
    manifest = root / "services.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "owner": "v0.25-named-shared-core",
                "instance_id": name,
                "bridge_mode": "shared",
                "bridge_id": "test-owner",
                "core": {
                    "config": str(base / f"{name}.toml"),
                    "sha256": "a" * 64,
                    "port": port,
                    "hostname": "core.example.test",
                },
                "services": {"http-origin": {}},
            }
        ),
        encoding="utf-8",
    )
    manifest.chmod(0o600)
    return manifest


def test_two_shared_core_manifests_reserve_distinct_ports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = _base(tmp_path)
    _shared(base, "core-a", 18081)
    _shared(base, "core-b", 18082)
    monkeypatch.setattr(runtime.darwin_service, "_loopback_port_available", lambda _: True)
    documents = runtime._read_collision_manifests(base, "core-c")
    assert {name: doc["core"]["port"] for name, doc in documents} == {
        "core-a": 18081,
        "core-b": 18082,
    }
    runtime._assert_available_port(base, "core-c", 18083)
    for duplicate in (18081, 18082):
        with pytest.raises(runtime.DarwinInstanceRuntimeError, match="already claimed"):
            runtime._assert_available_port(base, "core-c", duplicate)
    with pytest.raises(runtime.DarwinInstanceRuntimeError, match="already occupies"):
        runtime._read_collision_manifests(base, "core-a")
    assert len(runtime._read_collision_manifests(base, "core-a", allow_candidate=True)) == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("owner", "unknown-owner"),
        ("bridge_mode", "owned"),
        ("bridge_id", None),
        ("bridge_id", "INVALID_CAPS"),
        ("instance_id", "unrelated"),
        ("schema_version", True),
        ("core", {"port": 18081}),
        ("services", {"http-origin": {}, "agent-bridge": {}}),
        ("services", {"http-origin": {}, "tunnel": {}}),
    ],
)
def test_unknown_or_malformed_shared_owner_still_blocks_allocation(
    tmp_path: Path, field: str, value: object
) -> None:
    base = _base(tmp_path)
    manifest = _shared(base, "core-a", 18081)
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document[field] = value
    manifest.write_text(json.dumps(document), encoding="utf-8")
    manifest.chmod(0o600)
    with pytest.raises(
        runtime.DarwinInstanceRuntimeError,
        match="unknown named instance blocks resource allocation",
    ):
        runtime._read_collision_manifests(base, "core-b")


def test_shared_registry_owner_cannot_claim_bridge_or_connector_resources(tmp_path: Path) -> None:
    base = _base(tmp_path)
    manifest = _shared(base, "core-a", 18081)
    doc = json.loads(manifest.read_text(encoding="utf-8"))
    for illegal in ("agent-bridge", "cloudflare", "tunnel"):
        doc["services"][illegal] = {}
        manifest.write_text(json.dumps(doc), encoding="utf-8")
        manifest.chmod(0o600)
        with pytest.raises(runtime.DarwinInstanceRuntimeError):
            runtime._read_collision_manifests(base, "core-b")
        del doc["services"][illegal]


def test_connector_credential_scanners_accept_shared_peer_without_connector_ownership(
    tmp_path: Path,
) -> None:
    base = _base(tmp_path)
    _shared(base, "core-a", 18081)
    other = base / "instances" / "candidate"
    root = other / "service-manager"
    root.mkdir(parents=True, mode=0o700)
    other.chmod(0o700)
    root.chmod(0o700)
    candidate_manifest = root / "services.json"
    candidate_manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "owner": "v0.25-named-file-core",
                "instance_id": "candidate",
                "bridge_mode": "disabled",
                "bridge_id": None,
                "core": {"port": 18083},
                "services": {"http-origin": {}},
            }
        ),
        encoding="utf-8",
    )
    candidate_manifest.chmod(0o600)
    token = tmp_path / "sandbox-token"
    control = tmp_path / "sandbox-control"
    bearer = tmp_path / "sandbox-bearer"
    for file in (token, control, bearer):
        file.write_text("unit-only", encoding="utf-8")
        file.chmod(0o600)
    runtime._reject_named_cloudflare_token_collision(root, token)
    runtime._reject_openai_credential_reuse(root, control, bearer)

    # A Shared Core can never silently smuggle a Connector declaration into
    # the inventory when another peer checks credential file ownership.
    shared_manifest = base / "instances" / "core-a" / "service-manager" / "services.json"
    illegal = json.loads(shared_manifest.read_text(encoding="utf-8"))
    illegal["services"]["cloudflare"] = {"arguments": []}
    shared_manifest.write_text(json.dumps(illegal), encoding="utf-8")
    shared_manifest.chmod(0o600)
    with pytest.raises(runtime.DarwinInstanceRuntimeError, match="unverifiable"):
        runtime._reject_named_cloudflare_token_collision(root, token)
    with pytest.raises(runtime.DarwinInstanceRuntimeError, match="unverifiable"):
        runtime._reject_openai_credential_reuse(root, control, bearer)
