"""Pure source/phase guards for one-use installed I3 HMAC key rotation.

No real installed bridge, task, grant or credentials are read/modified here.
"""

from __future__ import annotations

import importlib.util
from copy import deepcopy
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "deployment"
    / "windows"
    / "v0250-i3-private-hmac-rotation.py"
)


def _module():
    spec = importlib.util.spec_from_file_location("i3_private_rotation_source", _SCRIPT)
    assert spec is not None and spec.loader is not None
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def _original():
    def record(core, key, hexbyte):
        return {
            "core_id": core,
            "key_id": key,
            "secret_hex": hexbyte * 32,
            "methods": ["bridge.ping", "runtime.list"],
            "workdirs": [{"alias": "Fixture", "policy_sha256": "a" * 64}],
        }

    a = record("i3-core-a", "key-old-a", "ab")
    b = record("i3-core-b", "key-b", "cd")
    grants = {"schema_version": 1, "bridge_id": "i3-shared", "grants": [a, b]}

    def binding(grant):
        return {
            "schema_version": 1,
            "instance_id": grant["core_id"],
            "bridge_mode": "shared",
            "bridge_id": "i3-shared",
            "key_id": grant["key_id"],
            "secret_hex": grant["secret_hex"],
            "workdirs": deepcopy(grant["workdirs"]),
        }

    return grants, binding(a), binding(b)


def test_strict_original_only_state_and_distinct_keys() -> None:
    rotator = _module()
    grants, a, b = _original()
    ga, gb = rotator._sealed_documents(grants, a, b)
    assert ga["core_id"] == "i3-core-a"
    assert gb["core_id"] == "i3-core-b"
    assert "ab" * 32 not in repr(ga.keys())
    for mismatch in (
        lambda g, a, b: g["grants"].append(deepcopy(g["grants"][0])),
        lambda g, a, b: g["grants"][0].update(key_id="old-key-now-different"),
        lambda g, a, b: g["grants"][0].update(methods=["repo.action"]),
        lambda g, a, b: g["grants"][1].update(
            workdirs=[{"alias": "Outside", "policy_sha256": "a" * 64}]
        ),
        lambda g, a, b: a.update(secret_hex="ee" * 32),
        lambda g, a, b: b.update(bridge_id="other"),
        lambda g, a, b: g["grants"][1].update(core_id="i3-core-a"),
    ):
        clone = (deepcopy(grants), deepcopy(a), deepcopy(b))
        mismatch(*clone)
        with pytest.raises(rotator.I3RotateError) as denied:
            rotator._sealed_documents(*clone)
        assert "ab" * 32 not in str(denied.value)
        assert "cd" * 32 not in str(denied.value)


def test_core_binding_grant_pair_is_exact() -> None:
    rotator = _module()
    grants, a, b = _original()
    assert rotator._binding_grant_consistent(a, grants["grants"][0], core_id="i3-core-a")
    assert rotator._binding_grant_consistent(b, grants["grants"][1], core_id="i3-core-b")
    b["workdirs"][0]["policy_sha256"] = "b" * 64
    assert not rotator._binding_grant_consistent(b, grants["grants"][1], core_id="i3-core-b")


def test_duplicate_private_json_keys_are_denied() -> None:
    rotator = _module()
    with pytest.raises(rotator.I3RotateError, match="ambiguous"):
        rotator.unique_pairs([("grant", "first"), ("grant", "second")])


def test_source_orders_overlap_cutover_revoke_and_scoped_final_stop() -> None:
    src = _SCRIPT.read_text(encoding="utf-8")
    assert 'BRIDGE = "i3-shared"' in src
    assert 'CORE_A = "i3-core-a"' in src and 'CORE_B = "i3-core-b"' in src
    assert 'EXPECTED_METHODS = frozenset({"bridge.ping", "runtime.list"})' in src
    assert (
        src.index("provision([old_a, new_a_grant, old_b])")
        < src.index("_private_replace(binding_path")
        < src.index("provision([new_a_grant, old_b])")
    )
    assert src.index("verify_overlap()") < src.index("_private_replace(binding_path")
    assert src.index("private_helper._new_private_file(") < src.index(
        "provision([old_a, new_a_grant, old_b])"
    )
    assert "I3_ROTATE_ONE_USE_MARKER=PRIVATE" in src
    assert src.index("        verify_revoke()") > src.index("provision([new_a_grant, old_b])")
    assert "expected_prior = original_grants if len(entries) == 3 else overlap_document" in src
    assert "storage.check_private(grants_path" in src
    assert "storage.check_private(path, directory=False, sid=sid)" in src
    assert "probe.three_task_states(preflight)" in src
    assert "I3_ROTATE_OVERLAP_OLD_NEW_AND_B=PASS" in src
    assert "I3_ROTATE_OLD_A_KEY=REVOKED" in src
    assert "I3_ROTATE_FIXTURE_STOP=" in src
    assert "I3_ROTATE_PRODUCTION_TASKS_AND_CORE=UNCHANGED" in src
    assert "I3_INSTALLED_HMAC_ROTATION=PASS" in src
    for bad in (
        'probe.cli("core"',
        "Scheduler().install(",
        "Scheduler().uninstall(",
        "storage.stage(",
        "Stop-Process",
        "Remove-Item",
        "git push",
        "print(document)",
        "print(new_a_binding)",
        "print(new_a_grant)",
        "print(secret_hex)",
    ):
        assert bad not in src


def test_new_bridge_grant_is_replaced_atomically_only_after_acl_verification() -> None:
    src = _SCRIPT.read_text("utf-8")
    assert src.index("protect_new_file(temp, sid)") < src.index("os.replace(temp, path)")
    assert src.index("storage.check_private(temp, directory=False, sid=sid)") < src.index(
        "os.replace(temp, path)"
    )
    assert src.index("os.replace(temp, path)") < src.index(
        "storage.check_private(path, directory=False, sid=sid)", src.index("os.replace(temp, path)")
    )
