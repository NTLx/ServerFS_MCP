"""Read-only private Shared Bridge binding loader tests; synthetic credentials only."""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import pytest

from serverfs_mcp.darwin_instance_shared_binding import DarwinSharedBindingError
from serverfs_mcp.darwin_shared_binding_private import load_shared_core_binding
from serverfs_mcp.instance_identity import InstanceIdentity

_SECRET = "73" * 32
_REVISION = "a" * 64
_IDENTITY = InstanceIdentity("named-core", "shared", "shared-bridge")


@pytest.fixture
def private_binding(tmp_path: Path):
    if sys.platform != "darwin":
        pytest.skip("requires native Darwin /private/tmp and filesystem semantics")
    # Keep the synthetic socket path short enough for Darwin AF_UNIX limits.
    root = Path("/private/tmp") / f"sfb-{uuid.uuid4().hex[:12]}"
    root.mkdir(mode=0o700)
    core_home = root / "core"
    core_home.mkdir(mode=0o700)
    binding_path = core_home / "shared-bridge-binding.json"
    socket_path = root / "bridge.sock"
    document = {
        "schema_version": 1,
        "bridge_id": "shared-bridge",
        "core_id": "named-core",
        "key_id": "core-key",
        "secret_hex": _SECRET,
        "workdir_alias": "Workspace",
        "workdir_path": str(tmp_path / "workspace"),
        "policy_revision": _REVISION,
        "bridge_config_path": str(root / "bridge.json"),
        "socket_path": str(socket_path),
    }
    binding_path.write_text(json.dumps(document), encoding="utf-8")
    binding_path.chmod(0o600)
    try:
        yield root, core_home, binding_path, socket_path, document
    finally:
        binding_path.unlink(missing_ok=True)
        core_home.rmdir()
        root.rmdir()


def _load(fixture, *, identity=_IDENTITY, binding_path=None, expected_socket_path=None):
    _, home, path, socket_path, _ = fixture
    return load_shared_core_binding(
        identity=identity,
        trusted_core_home=home,
        binding_path=path if binding_path is None else binding_path,
        expected_socket_path=socket_path if expected_socket_path is None else expected_socket_path,
    )


def _write(fixture, value: str) -> None:
    path = fixture[2]
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)


def test_loads_exact_private_binding_without_exposing_secret(private_binding) -> None:
    binding = _load(private_binding)
    assert binding.grant.bridge_id == "shared-bridge"
    assert binding.grant.core_id == "named-core"
    assert binding.grant.secret == bytes.fromhex(_SECRET)
    assert binding.core_policy_revision == _REVISION
    assert _SECRET not in repr(binding)


@pytest.mark.parametrize(
    "identity",
    [
        InstanceIdentity("default", "shared", "shared-bridge"),
        InstanceIdentity("named-core", "owned", "named-core"),
        InstanceIdentity("named-core", "disabled"),
        InstanceIdentity("named-core", "shared", "other-bridge"),
    ],
)
def test_rejects_default_non_shared_and_wrong_bridge(private_binding, identity) -> None:
    with pytest.raises(DarwinSharedBindingError):
        _load(private_binding, identity=identity)


def test_rejects_wrong_core_and_socket(private_binding) -> None:
    _, _, _, socket_path, document = private_binding
    document["core_id"] = "other-core"
    _write(private_binding, json.dumps(document))
    with pytest.raises(DarwinSharedBindingError):
        _load(private_binding)
    document["core_id"] = "named-core"
    _write(private_binding, json.dumps(document))
    with pytest.raises(DarwinSharedBindingError):
        _load(private_binding, expected_socket_path=socket_path.parent / "other.sock")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(schema_version=True),
        lambda d: d.update(unknown="field"),
        lambda d: d.pop("key_id"),
        lambda d: d.update(key_id="Bad_Key"),
        lambda d: d.update(secret_hex="A" * 64),
        lambda d: d.update(secret_hex="0" * 62),
        lambda d: d.update(workdir_alias="9bad"),
        lambda d: d.update(policy_revision="z" * 64),
        lambda d: d.update(workdir_path="relative"),
        lambda d: d.update(bridge_config_path="relative"),
        lambda d: d.update(socket_path="relative"),
    ],
)
def test_rejects_invalid_exact_schema_and_values(private_binding, mutate) -> None:
    document = dict(private_binding[4])
    mutate(document)
    _write(private_binding, json.dumps(document))
    with pytest.raises(DarwinSharedBindingError):
        _load(private_binding)


def test_rejects_duplicate_json_keys_without_leaking_secret(private_binding) -> None:
    document = json.dumps(private_binding[4])[:-1] + f',"secret_hex":"{_SECRET}"}}'
    _write(private_binding, document)
    with pytest.raises(DarwinSharedBindingError) as error:
        _load(private_binding)
    assert _SECRET not in str(error.value)


def test_rejects_symlink_and_non_regular_binding(private_binding, tmp_path: Path) -> None:
    path = private_binding[2]
    target = tmp_path / "target.json"
    target.write_text(json.dumps(private_binding[4]), encoding="utf-8")
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(DarwinSharedBindingError):
        _load(private_binding)
    path.unlink()
    path.mkdir(mode=0o700)
    with pytest.raises(DarwinSharedBindingError):
        _load(private_binding)
    path.rmdir()


def test_rejects_wrong_file_owner_and_permissions(private_binding, monkeypatch) -> None:
    path = private_binding[2]
    path.chmod(0o640)
    with pytest.raises(DarwinSharedBindingError):
        _load(private_binding)
    path.chmod(0o600)

    original_fstat = os.fstat

    class ForeignOwner:
        def __init__(self, info):
            self._info = info

        def __getattr__(self, name):
            return self._info.st_uid + 1 if name == "st_uid" else getattr(self._info, name)

    monkeypatch.setattr(
        os,
        "fstat",
        lambda fd: (
            ForeignOwner(original_fstat(fd))
            if original_fstat(fd).st_mode & 0o170000 == 0o100000
            else original_fstat(fd)
        ),
    )
    with pytest.raises(DarwinSharedBindingError):
        _load(private_binding)


def test_rejects_non_private_home_and_unpinned_filename(private_binding) -> None:
    _, home, path, _, _ = private_binding
    home.chmod(0o750)
    with pytest.raises(DarwinSharedBindingError):
        _load(private_binding)
    home.chmod(0o700)
    with pytest.raises(DarwinSharedBindingError):
        _load(private_binding, binding_path=home / "other.json")


def test_rejects_symlinked_home_component(private_binding) -> None:
    root, home, path, socket_path, _ = private_binding
    alias = root / "core-alias"
    alias.symlink_to(home, target_is_directory=True)
    with pytest.raises(DarwinSharedBindingError):
        load_shared_core_binding(
            identity=_IDENTITY,
            trusted_core_home=alias,
            binding_path=alias / path.name,
            expected_socket_path=socket_path,
        )
    alias.unlink()


def test_rejects_oversized_binding(private_binding) -> None:
    _write(private_binding, " " * (16 * 1024 + 1))
    with pytest.raises(DarwinSharedBindingError):
        _load(private_binding)


def test_file_error_and_repr_never_include_secret(private_binding) -> None:
    _write(private_binding, _SECRET)
    with pytest.raises(DarwinSharedBindingError) as error:
        _load(private_binding)
    assert _SECRET not in str(error.value)
    assert _SECRET not in repr(error.value)
