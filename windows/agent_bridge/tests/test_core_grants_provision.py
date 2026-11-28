"""Explicit owner-side provisioning of private v2 grants is atomic and fail-closed.

Uses only temporary protected Bridge state roots; no production task,
connector, IPC endpoint, provider or background service is modified.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from serverfs_agent_bridge.core_grants_private import (
    load_private_core_grants,
    provision_private_core_grants,
)
from test_core_grants import _request
from test_core_grants_private import _fixture

_NOW = 1_800_000_000


def _install(store, policies, document):
    provision_private_core_grants(
        store=store, bridge_id="bridge-v2", policies=policies, document=document
    )


def _load(store, policies):
    return load_private_core_grants(store=store, bridge_id="bridge-v2", policies=policies)


def test_explicit_provision_creates_private_file_and_verifiable_grants(
    tmp_path: Path,
) -> None:
    store, policies, document = _fixture(tmp_path)
    path = store.state_dir / "core-grants.json"
    assert not path.exists()
    _install(store, policies, document)
    assert path.is_file()
    assert not list(store.state_dir.glob(".core-grants-*.tmp"))
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0
    verifier = _load(store, policies)
    assert verifier.durable_replay_protection
    grant = next(iter(verifier._grants.values()))
    request = _request(
        grant,
        bridge_id="bridge-v2",
        workdir="Repo",
        policy=document["grants"][0]["workdirs"][0]["policy_sha256"],
    )
    assert verifier.authorize(request, now=_NOW).core_id == "core-a"


def test_provisioning_invalid_document_leaves_previous_file_untouched(
    tmp_path: Path,
) -> None:
    store, policies, document = _fixture(tmp_path)
    _install(store, policies, document)
    path = store.state_dir / "core-grants.json"
    original = path.read_bytes()
    invalid = json.loads(json.dumps(document))
    invalid["grants"][0]["secret_hex"] = "../not-a-secret"
    with pytest.raises(ValueError, match="invalid or not private"):
        _install(store, policies, invalid)
    assert path.read_bytes() == original
    assert not list(store.state_dir.glob(".core-grants-*.tmp"))


def test_key_rotation_replaces_file_but_not_previous_runtime_verifier(
    tmp_path: Path,
) -> None:
    store, policies, document = _fixture(tmp_path)
    _install(store, policies, document)
    prior = _load(store, policies)
    old = next(iter(prior._grants.values()))
    next_document = json.loads(json.dumps(document))
    next_document["grants"][0]["key_id"] = "key-b"
    next_document["grants"][0]["secret_hex"] = "ef" * 32
    _install(store, policies, next_document)
    reloaded = _load(store, policies)
    new = next(iter(reloaded._grants.values()))
    assert new.key_id == "key-b"
    assert old.key_id == "key-a"
    assert ("core-a", "key-a") in prior._grants
    assert ("core-a", "key-a") not in reloaded._grants
    assert ("core-a", "key-b") in reloaded._grants
    # The runtime map does not silently hot-reload a disk edit; the owner
    # must coordinate a restart or a separately authenticated reload.
    assert ("core-a", "key-b") not in prior._grants


def test_persisted_revoke_all_is_explicit_and_survives_reload(tmp_path: Path) -> None:
    store, policies, document = _fixture(tmp_path)
    _install(store, policies, document)
    document["grants"] = []
    _install(store, policies, document)
    assert _load(store, policies)._grants == {}
    assert (store.state_dir / "core-grants.json").is_file()


def test_preplanted_nonprivate_grant_file_is_not_replaced(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("Windows ACL preplanting is validated by private-state tests")
    store, policies, document = _fixture(tmp_path)
    target = store.state_dir / "core-grants.json"
    target.write_text("preplanted", encoding="utf-8")
    target.chmod(0o644)
    with pytest.raises(ValueError, match="invalid or not private"):
        _install(store, policies, document)
    assert target.read_text() == "preplanted"


def test_failed_atomic_replace_preserves_previous_content_and_cleans_staging(
    tmp_path: Path, monkeypatch
) -> None:
    from serverfs_agent_bridge import core_grants_private as module

    store, policies, document = _fixture(tmp_path)
    _install(store, policies, document)
    old = (store.state_dir / "core-grants.json").read_bytes()

    def reject_replace(_src, _dst):
        raise OSError("simulated write failure")

    monkeypatch.setattr(module.os, "replace", reject_replace)
    document["grants"][0]["key_id"] = "next-key"
    with pytest.raises(ValueError, match="invalid or not private"):
        _install(store, policies, document)
    assert (store.state_dir / "core-grants.json").read_bytes() == old
    assert not list(store.state_dir.glob(".core-grants-*.tmp"))
