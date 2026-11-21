"""Pure phase classifier for native I3 HMAC rotation recovery evidence.

Never loads an installed user's Bridge, Core, private credentials or Scheduler.
"""

from __future__ import annotations

import importlib.util
from copy import deepcopy
from pathlib import Path

import pytest

_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "deployment"
    / "windows"
    / "v0250-i3-private-rotation-readonly.py"
)


def _module():
    spec = importlib.util.spec_from_file_location("i3_rotated_state_source", _SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fixture():
    def grant(core, key, secret):
        return {
            "core_id": core,
            "key_id": key,
            "secret_hex": secret * 32,
            "methods": ["bridge.ping", "runtime.list"],
            "workdirs": [{"alias": "Fixture", "policy_sha256": "a" * 64}],
        }

    old = grant("i3-core-a", "key-old", "aa")
    new = grant("i3-core-a", "key-new", "bb")
    other = grant("i3-core-b", "key-b", "cc")

    def bind(g):
        return {
            "schema_version": 1,
            "instance_id": g["core_id"],
            "bridge_mode": "shared",
            "bridge_id": "i3-shared",
            "key_id": g["key_id"],
            "secret_hex": g["secret_hex"],
            "workdirs": deepcopy(g["workdirs"]),
        }

    def document(grants):
        return {"schema_version": 1, "bridge_id": "i3-shared", "grants": grants}

    marker = {"schema_version": 1, "old_core_a_key_id": "key-old"}
    return old, new, other, bind, document, marker


@pytest.mark.parametrize(
    "phase",
    [
        "READY_ORIGINAL",
        "MARKER_ONLY",
        "OVERLAP_OLD_A_BINDING",
        "OVERLAP_NEW_A_BINDING",
        "REVOKED_NEW_A_BINDING",
    ],
)
def test_exact_phase_classification(phase) -> None:
    classifier = _module()
    old, new, b, bind, document, marker = _fixture()
    by_phase = {
        "READY_ORIGINAL": (document([old, b]), bind(old), None),
        "MARKER_ONLY": (document([old, b]), bind(old), marker),
        "OVERLAP_OLD_A_BINDING": (document([old, new, b]), bind(old), marker),
        "OVERLAP_NEW_A_BINDING": (document([old, new, b]), bind(new), marker),
        "REVOKED_NEW_A_BINDING": (document([new, b]), bind(new), marker),
    }
    grants, a_binding, marker_state = by_phase[phase]
    assert classifier.classify(grants, a_binding, bind(b), marker_state) == phase


@pytest.mark.parametrize(
    "kind", ["unmarked-overlap", "wrong-b", "unknown-a", "bad-methods", "three-a", "foreign"]
)
def test_invalid_states_are_fail_closed(kind) -> None:
    classifier = _module()
    old, new, b, bind, document, marker = _fixture()
    doc = document([old, new, b])
    a_binding, b_binding = bind(old), bind(b)
    if kind == "unmarked-overlap":
        marker = None
    elif kind == "wrong-b":
        b_binding["secret_hex"] = "dd" * 32
    elif kind == "unknown-a":
        a_binding["key_id"] = "key-unknown"
    elif kind == "bad-methods":
        doc["grants"][0]["methods"] = ["task.submit", "bridge.ping"]
    elif kind == "three-a":
        doc["grants"].append(deepcopy(new))
    else:
        doc["grants"][0]["core_id"] = "production"
    with pytest.raises(classifier.I3AuditError) as err:
        classifier.classify(doc, a_binding, b_binding, marker)
    assert "aa" * 32 not in str(err.value)
    assert "bb" * 32 not in str(err.value)


def test_readonly_inspector_does_not_provision_keys_or_lifecycle() -> None:
    source = _SOURCE.read_text("utf-8")
    assert "I3_ROTATION_AUDIT_STATE=" in source
    assert "I3_ROTATION_AUDIT_FIXTURES=DISABLED" in source
    assert "I3_ROTATION_AUDIT_PRODUCTION=UNCHANGED" in source
    assert "I3_ROTATION_READONLY=PASS" in source
    for forbidden in (
        "storage.stage(",
        "Scheduler().install(",
        'helper.cli("bridge", BRIDGE, "start")',
        'helper.cli("bridge", BRIDGE, "stop")',
        "_private_replace(",
        "provision_private_core_grants(",
        "print(grant_doc)",
        "print(a)",
        "print(marker)",
    ):
        assert forbidden not in source
