"""I5 first-failure exact-state postflight contracts; source-only."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SOURCE = (
    Path(__file__).resolve().parents[1] / "deployment" / "windows" / "v0250-i5-postfail-readonly.py"
)
_STAGE = _SOURCE.with_name("v0250-i5-provider-shared-stage.py")


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_i5_exact_historical_failing_toml_matches_first_stager(tmp_path: Path) -> None:
    partial = _load(_SOURCE, "i5_partial_contract")
    stage = _load(_STAGE, "i5_old_stage_contract")
    fixture = tmp_path / "private-fixture"
    assert partial.historical_core_toml(fixture) != stage._core_toml(fixture)
    text = partial.historical_core_toml(fixture).decode("utf-8")
    assert "[agent]\nenabled = true" in text
    assert "[agent.claude]" not in text
    assert "[agent.claude]" in stage._core_toml(fixture).decode("utf-8")
    assert json.loads(text.split("path = ", 1)[1].splitlines()[0]) == str(fixture)


def test_i5_exact_postfail_absence_and_refusal(tmp_path: Path) -> None:
    module = _load(_SOURCE, "i5_partial_absence")
    missing = tmp_path / "missing"
    assert module.absent(missing) is True
    missing.write_text("present", encoding="utf-8")
    assert module.absent(missing) is False
    with pytest.raises(module.I5PostfailError):
        module.require(False, "bad")
    module.require(True, "ok")


def test_i5_postfail_is_strictly_read_only_and_scope_bounded() -> None:
    source = _SOURCE.read_text("utf-8")
    for required in (
        "gate.protected_tasks()",
        "gate.verify_candidates(gate.candidates(), installed=False)",
        "storage.check_private(",
        "first_toml.read_bytes() == historical_core_toml(fixture)",
        'bridge_home / "state" / "core-grants.json"',
        'first_home / "serverfs.original-invalid.toml"',
        'second_home / "serverfs.toml"',
        "I5_EXACT_PARTIAL_STATE_READONLY=PASS",
        "sys.flags.isolated",
        "IsUserAnAdmin()",
    ):
        assert required in source
    for forbidden in (
        "Scheduler().install(",
        "Scheduler().start(",
        "Scheduler().stop(",
        "storage.prepare(",
        "storage.stage(",
        ".write_bytes(",
        ".write_text(",
        ".unlink(",
        ".replace(",
        ".mkdir(",
        "os.system(",
        "subprocess.run(",
        "git push",
        "secret_hex",
        "ANTHROPIC_API_KEY",
    ):
        assert forbidden not in source
