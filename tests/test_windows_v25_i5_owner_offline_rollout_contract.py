"""Pure verification for the single-owner I5 offline upgrade lifecycle."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "deployment/windows/v0250-i5-owner-seven-module-offline-rollout.py"
)
spec = importlib.util.spec_from_file_location("i5_owner_rollout_contract", SCRIPT)
assert spec is not None and spec.loader is not None
rollout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rollout)


def test_one_use_upgrade_is_only_i5_bridge_owner() -> None:
    assert rollout.INSTANCE == "i5-shared"
    assert rollout.MARKER == "bridge-owner-seven-module-rollout-1"
    assert rollout.install_args(Path("owner-python.exe"), Path("wheel.whl")) == [
        "uv",
        "pip",
        "install",
        "--offline",
        "--no-deps",
        "--reinstall",
        "--python",
        "owner-python.exe",
        "wheel.whl",
    ]


def test_protects_other_installed_cores_and_wheel_delta() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    for clause in (
        "gate.native_preflight()",
        "len(previous) == 1",
        "candidate_gate.verify_archive_delta(",
        "gate.wheel_modules(wheel, set(source)) == source",
        "i7.postflight()",
        "i5.verify_candidates(i5.candidates(), installed=True)",
        'builder.run([*command, "--dry-run"]',
        "builder._new_dir(marker, sid)",
        "builder._protect_tree(env, sid)",
        "current_modules(env, source, sid) == source",
        "current_modules(env, source, sid) == old_bytes",
        "I5_OWNER_OLD_WHEEL_ROLLBACK=",
        "I5_OWNER_ONLY_OFFLINE_ROLLOUT=PASS",
    ):
        assert clause in source
    for forbidden in (
        "Scheduler().start(",
        "Scheduler().stop(",
        "Core A",
        "Core B",
        "cloudflare.request(",
        "tunnel.token",
        "http.env",
        "git push",
    ):
        assert forbidden not in source


@pytest.mark.parametrize("reason", ["some-refusal", "other-refusal"])
def test_fail_closed_exception(reason: str) -> None:
    with pytest.raises(rollout.I5OwnerRolloutError, match=reason):
        rollout.require(False, reason)


def test_reinstall_is_offline_no_deps() -> None:
    args = rollout.install_args(Path("trusted/py.exe"), Path("trusted/build.whl"))
    assert args.count("--offline") == 1
    assert "--no-deps" in args
    assert "--reinstall" in args
    assert "--python" in args
    assert "force" not in args
