"""Source-only I5 interactive-method grant repair contracts; no Windows writes."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1] / "deployment" / "windows"
_SCRIPT = _ROOT / "v0250-i5-interactive-grants-upgrade.py"


def _source():
    spec = importlib.util.spec_from_file_location("i5_interactive_grants", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fixture():
    src = _source()
    records = []
    bindings = {}
    for core, index in zip(src.CORES, (1, 2), strict=True):
        common = {
            "key_id": f"key-{index}",
            "secret_hex": f"{index:064x}",
            "workdirs": [{"alias": "Fixture", "policy_sha256": f"{index + 5:064x}"}],
        }
        records.append({"core_id": core, **common, "methods": list(src.OLD_METHODS)})
        bindings[core] = {
            "schema_version": 1,
            "instance_id": core,
            "bridge_mode": "shared",
            "bridge_id": src.BRIDGE,
            **common,
        }
    return src, {"schema_version": 1, "bridge_id": src.BRIDGE, "grants": records}, bindings


def test_interactive_extension_is_exact_two_methods() -> None:
    source, old, bindings = _fixture()
    assert source.ADDED_METHODS == ("task.approval.respond", "task.question.answer")
    source.validate(old, bindings, source.OLD_METHODS)
    extended = {
        **old,
        "grants": [{**item, "methods": list(source.NEW_METHODS)} for item in old["grants"]],
    }
    source.validate(extended, bindings, source.NEW_METHODS)
    with pytest.raises(source.I5GrantUpgradeError):
        source.validate(extended, bindings, source.OLD_METHODS)


def test_interactive_extension_rejects_wrong_core_keys_methods_or_extra_grants() -> None:
    source, old, bindings = _fixture()
    wrong_method = {
        **old,
        "grants": [
            {**old["grants"][0], "methods": [*source.OLD_METHODS, "repo.push"]},
            old["grants"][1],
        ],
    }
    with pytest.raises(source.I5GrantUpgradeError):
        source.validate(wrong_method, bindings, source.OLD_METHODS)
    wrong_key = {
        **old,
        "grants": [{**old["grants"][0], "secret_hex": "f" * 64}, old["grants"][1]],
    }
    with pytest.raises(source.I5GrantUpgradeError):
        source.validate(wrong_key, bindings, source.OLD_METHODS)
    with pytest.raises(source.I5GrantUpgradeError):
        source.validate(
            {**old, "grants": [*old["grants"], old["grants"][0]]},
            bindings,
            source.OLD_METHODS,
        )


def test_oneuse_source_contains_private_backup_atomic_worker_and_offline_guards() -> None:
    source = _SCRIPT.read_text("utf-8")
    for needed in (
        'MARKER = ".i5-interactive-grants-v1.attempted.json"',
        'BACKUP = ".i5-interactive-grants-v1.old.json"',
        "smoke.candidates_disabled(gate)",
        "smoke.protected_snapshot(gate)",
        "storage.check_private(",
        "load_native_bridge_plan(",
        "reader.read_private_json(",
        "validate(current, bindings, OLD_METHODS)",
        "validate(upgraded, bindings, NEW_METHODS)",
        "i2._new_private_file(marker,",
        "i2._new_private_file(backup,",
        "i3._PROVISION",
        "input_data=stage._json(upgraded)",
        "protected_after(",
    ):
        assert needed in source
    assert source.index("i2._new_private_file(marker,") < source.index("i2.run(")
    for forbidden in (
        "Scheduler().start(",
        "Scheduler().stop(",
        "Scheduler().install(",
        "Scheduler().uninstall(",
        "os.system(",
        "shutil.rmtree(",
        "print(upgraded)",
        "print(current)",
        "git push",
        "git commit",
        "ANTHROPIC_API_KEY",
    ):
        assert forbidden not in source
