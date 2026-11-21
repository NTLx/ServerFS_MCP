"""Source-only I5 exact-state recovery contracts, no host modifications."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from serverfs_mcp.native_config import NativeConfigError, load_native_config

_ROOT = Path(__file__).resolve().parents[1] / "deployment" / "windows"
_RESUME = _ROOT / "v0250-i5-resume-core-policy.py"
_STAGE = _ROOT / "v0250-i5-provider-shared-stage.py"
_PARTIAL = _ROOT / "v0250-i5-postfail-readonly.py"


def _load(source: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_i5_original_error_reproduces_but_corrected_core_policy_parses(tmp_path: Path) -> None:
    stage = _load(_STAGE, "i5_stage_fixed_generator")
    partial = _load(_PARTIAL, "i5_historical_failed_generator")
    root = tmp_path / "private-fixture"
    root.mkdir()

    historical = tmp_path / "historical.toml"
    historical.write_bytes(partial.historical_core_toml(root))
    with pytest.raises(NativeConfigError, match="agent.claude.enabled is false"):
        load_native_config(historical)

    repaired = tmp_path / "repaired.toml"
    repaired.write_bytes(stage._core_toml(root))
    assert repaired.read_bytes() != historical.read_bytes()
    workdirs, settings = load_native_config(repaired)
    assert settings.bridge_required
    assert settings.agent is not None and settings.agent.claude.enabled
    assert len(workdirs) == 1
    assert workdirs[0].read_only is False
    assert workdirs[0].agent_runtimes == frozenset({"claude"})
    assert workdirs[0].agent_mode == "workspace-write"


def test_i5_recovery_has_one_exact_state_preflight_and_private_original_backup() -> None:
    src = _RESUME.read_text("utf-8")
    for required in (
        'helper("v0250-i5-postfail-readonly.py"',
        "diagnostic.inspect()",
        "diagnostic.historical_core_toml(fixture)",
        "stage._core_toml(fixture)",
        'backup = first_home / "serverfs.original-invalid.toml"',
        'replacement = first_home / "serverfs.new-verified.toml"',
        "i2._new_private_file(backup, old, sid)",
        "i2._new_private_file(replacement, correct, sid)",
        "os.replace(replacement, policy_file)",
        "load_native_config(replacement)",
        "verify_core_bridge_workdirs(",
        "stage._DIGEST",
        "issue_shared_core_capabilities(",
        "load_native_core_bridge_binding(",
        "Scheduler().install(manifest)",
        "gate.protected_tasks()",
        "gate.verify_candidates(gate.candidates(), installed=True)",
        "I5_NATIVE_STAGE_RECOVERED=PASS",
    ):
        assert required in src
    assert src.index("diagnostic.inspect()") < src.index("os.replace(replacement, policy_file)")
    assert src.index("load_native_config(replacement)") < src.index(
        "os.replace(replacement, policy_file)"
    )
    assert src.index("gate.verify_candidates(gate.candidates(), installed=True)") > src.index(
        "Scheduler().install(manifest)"
    )


def test_i5_recovery_never_reinstalls_or_starts_any_existing_component() -> None:
    src = _RESUME.read_text("utf-8")
    for forbidden in (
        "gate.preflight()",
        "storage.prepare(",
        "installer._offline_install(",
        "i2._offline_install(",
        "uv pip",
        "uv venv",
        "Scheduler().start(",
        "Scheduler().stop(",
        "Scheduler().uninstall(",
        "shutil.rmtree(",
        "git push",
        "git commit",
        "subprocess.Popen(",
        "claude_agent_sdk",
        "ANTHROPIC_API_KEY",
        "print(grants)",
    ):
        assert forbidden not in src
