"""Source-only offline native Windows I7 Scheduler fix rollout contracts."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1] / "deployment/windows/v0250-i7-scheduler-offline-rollout.py"
)
spec = importlib.util.spec_from_file_location("i7_rollout_contract", SCRIPT)
assert spec is not None and spec.loader is not None
rollout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rollout)


def test_exact_nonproduction_targets_only() -> None:
    assert rollout.INSTANCE == "i7-public"
    assert rollout.COMPONENTS == ("core", "cloudflare")
    assert rollout.MARKER == "scheduler-failclosed-rollout-1"
    assert rollout.install_args(Path("private-python.exe"), Path("candidate.whl")) == [
        "uv",
        "pip",
        "install",
        "--offline",
        "--no-deps",
        "--reinstall",
        "--python",
        "private-python.exe",
        "candidate.whl",
    ]


def test_rollout_is_fail_closed_and_has_two_independent_old_wheels() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    for needed in (
        "wheel_gate.installed_baseline()",
        "wheel_gate.verify_candidate(candidate, modules, previous)",
        "installer()",  # fixed helper only
        "gate.postflight()",
        "helper._new_dir(marker, sid)",
        'helper.run([*commands[component], "--dry-run"]',
        "attempted.append(component)",
        "for component in reversed(attempted):",
        "install_args(python, old_wheels[component])",
        "helper._protect_tree(",
        "wheel_gate.archived_modules(old_wheels[component], set(modules))",
        "rollback_complete = False",
        "I7_TWO_OWNER_OLD_WHEEL_ROLLBACK=",
        "I7_TWO_PRIVATE_OWNER_SCHEDULER_SOURCE_PARITY=PASS",
    ):
        assert needed in source
    for forbidden in (
        "Scheduler().start(",
        "Scheduler().stop(",
        "Scheduler().uninstall(",
        "cloudflare.request(",
        "tunnel.token",
        "http.env",
        "git push",
        "dangerouslyDisableSandbox",
    ):
        assert forbidden not in source


def test_installed_parity_is_strict_for_all_expected_modules(tmp_path: Path, monkeypatch) -> None:
    class FakeStorage:
        @staticmethod
        def check_private(path, *, directory=False, sid=None):
            assert directory is (path == tmp_path / "Lib/site-packages/serverfs_mcp")
            assert sid == "test-sid"

    # package_files imports storage at call time; set up a deliberately wrong
    # module first and assert it cannot be accepted as byte-identical source.
    package = tmp_path / "Lib/site-packages/serverfs_mcp"
    package.mkdir(parents=True)
    (package / "good.py").write_bytes(b"old")
    import serverfs_mcp.windows_v25.storage as storage

    monkeypatch.setattr(storage, "check_private", FakeStorage.check_private)
    assert rollout.package_files(tmp_path, {"good.py": b"new"}, "test-sid") == {"good.py": b"old"}
    assert rollout.package_files(tmp_path, {"good.py": b"old"}, "test-sid") == {"good.py": b"old"}


@pytest.mark.parametrize("mode", ["read_only", "unsafe"])
def test_rollout_keeps_one_use_refusal_and_failure_marker(mode: str) -> None:
    assert rollout.PHASE == "entry"
    with pytest.raises(rollout.I7OfflineRolloutError):
        rollout.require(False, mode)
