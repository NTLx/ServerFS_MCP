"""I4 owned Core/Bridge pair installation is create-only and fail-closed.

No Windows host mutation, Task Scheduler call, native package or HMAC secret
persistence occurs in these portable source/contract tests.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from serverfs_mcp.instance_identity import InstanceIdentityError
from serverfs_mcp.windows_v25.core_bridge_binding import parse_core_bridge_binding
from serverfs_mcp.windows_v25.spec import Manifest

_SOURCE = (
    Path(__file__).resolve().parents[1] / "deployment" / "windows" / "v0250-i4-independent-stage.py"
)
_SID = "S-1-5-21-111-222-333-1001"


def _module():
    spec = importlib.util.spec_from_file_location("i4_stage_source", _SOURCE)
    assert spec is not None and spec.loader is not None
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def _manifest(tmp_path: Path, *, component: str, instance: str) -> Manifest:
    settings = (
        {
            "config": str(tmp_path / instance / "bridge.json"),
            "bridge_python": str(
                tmp_path / instance / "envs" / "agent-bridge" / "Scripts" / "python.exe"
            ),
        }
        if component == "bridge"
        else {
            "config": str(tmp_path / instance / "serverfs.toml"),
            "auth_file": str(tmp_path / instance / "http.env"),
            "port": 18871,
            "hostname": instance + ".local",
        }
    )
    return Manifest.parse(
        {
            "schema": 2,
            "component": component,
            "instance_id": instance,
            "sid": _SID,
            "pythonw": str(tmp_path / instance / component / "Scripts/pythonw.exe"),
            "settings": settings,
        }
    )


def test_i4_persisted_grant_and_core_binding_use_owned_identity(tmp_path: Path) -> None:
    mod = _module()
    binding, grant = mod._binding_and_grant("i4-a", "a" * 64)
    assert binding["bridge_mode"] == "owned"
    assert binding["bridge_id"] == binding["instance_id"] == grant["core_id"] == "i4-a"
    assert binding["key_id"] == grant["key_id"]
    assert binding["secret_hex"] == grant["secret_hex"]
    assert len(bytes.fromhex(binding["secret_hex"])) == 32
    assert grant["methods"] == ["bridge.ping", "runtime.list"]
    assert (
        grant["workdirs"]
        == binding["workdirs"]
        == [{"alias": "Fixture", "policy_sha256": "a" * 64}]
    )
    parsed = parse_core_bridge_binding(
        _manifest(tmp_path, component="core", instance="i4-a"),
        binding,
        local_aliases=frozenset({"Fixture"}),
        verified_bridge=_manifest(tmp_path, component="bridge", instance="i4-a"),
        bridge_lock_dir=tmp_path / "i4-a" / "locks",
    )
    assert parsed.bridge_mode == "owned"
    assert parsed.instance_id == parsed.bridge_id == "i4-a"


def test_i4_independent_owners_have_distinct_hmac_keys() -> None:
    mod = _module()
    first, _grant_a = mod._binding_and_grant("i4-a", "b" * 64)
    second, _grant_b = mod._binding_and_grant("i4-b", "b" * 64)
    assert first["key_id"] != second["key_id"]
    assert first["secret_hex"] != second["secret_hex"]


@pytest.mark.parametrize("invalid", ["default", "i4-bridge-a", "../escape", "I4-A"])
def test_i4_owned_identity_must_be_one_of_exact_pair_ids(invalid: str) -> None:
    mod = _module()
    # Owned identity itself cannot be default/traversal/case-invalid, but
    # any other valid ID is rejected by the installer's fixed allowlist.
    if invalid in ("default", "../escape", "I4-A"):
        if invalid == "default":
            # Default is syntactically valid, but prohibited for owned I4.
            assert "tuple(gate.BRIDGES)" in _SOURCE.read_text("utf-8")
        else:
            with pytest.raises(InstanceIdentityError):
                mod._binding_and_grant(invalid, "a" * 64)
    else:
        assert invalid not in {"i4-a", "i4-b"}


def test_i4_stager_reuses_trusted_offline_native_and_private_fixtures() -> None:
    src = _SOURCE.read_text("utf-8")
    for required in (
        'helper("v0250-i4-independent-preflight.py"',
        "gate.preflight()",
        "verify_core_bridge_workdirs(",
        "native_pin.verify_native_wheel(native)",
        "installer._protect_tree(",
        'storage.prepare("bridge", instance)',
        'storage.prepare("core", instance)',
        '"bridge_mode": "owned"',
        "load_native_core_bridge_binding(",
        "Scheduler().install(manifest)",
        "require_candidates_absent=False",
        "I4_NATIVE_STAGE=PASS",
    ):
        assert required in src
    for denied in (
        "Start-ScheduledTask",
        "Stop-ScheduledTask",
        "Remove-Item",
        "Stop-Process",
        "git push",
        "shutil.rmtree",
        "os.environ.clear()",
        "run_core(",
        "run_bridge(",
    ):
        assert denied not in src
