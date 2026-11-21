"""Pure deterministic contracts for I5 seven-module offline Wheel candidate."""

from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "deployment/windows/v0250-i5-owner-seven-module-wheel-stage.py"
)
spec = importlib.util.spec_from_file_location("i5_private_wheel_test", SCRIPT)
assert spec is not None and spec.loader is not None
wheel_gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wheel_gate)

MODULES = (
    "cli.py",
    "isolation.py",
    "readiness.py",
    "runtime.py",
    "spec.py",
    "storage.py",
    "tasks.py",
)


def _make_wheel(
    path: Path,
    *,
    new: bool,
    unrelated: bool = False,
    miss: str | None = None,
    unchanged: str | None = None,
) -> Path:
    with zipfile.ZipFile(path, "w") as wheel:
        for name in MODULES:
            if name == miss:
                continue
            wheel.writestr(
                "serverfs_mcp/windows_v25/" + name,
                b"new-code" if new and name != unchanged else b"old-code",
            )
        wheel.writestr("serverfs_mcp/__init__.py", b"changed" if unrelated else b"identical")
        wheel.writestr("serverfs_mcp-0.25.0.dist-info/METADATA", b"same-metadata")
        wheel.writestr(
            "serverfs_mcp-0.25.0.dist-info/RECORD", b"new-record" if new else b"old-record"
        )
    return path


def test_owner_only_and_exact_one_use_fixed_slot() -> None:
    assert wheel_gate.SLOT == "bridge-owner-seven-module-wheel-1"
    source = SCRIPT.read_text(encoding="utf-8")
    for expected in (
        "gate.native_preflight()",
        "len(old) == 1",
        "builder._new_dir(slot, sid)",
        "builder._wheel(",
        "builder._protect_tree(slot, sid)",
        "verify_archive_delta(old, wheel, allowed=gate.EXPECTED_DELTA)",
        "I5_OWNER_NO_PRIVATE_PACKAGE_INSTALL=PASS",
    ):
        assert expected in source
    for forbidden in (
        "Scheduler().start(",
        "Scheduler().stop(",
        "uv pip install",
        "http.env",
        "tunnel.token",
        "cloudflare.request(",
        "git push",
    ):
        assert forbidden not in source


def test_allow_exact_seven_code_changes_and_record_only(tmp_path: Path) -> None:
    old = _make_wheel(tmp_path / "old.whl", new=False)
    new = _make_wheel(tmp_path / "new.whl", new=True)
    wheel_gate.verify_archive_delta(
        old, new, allowed=frozenset("windows_v25/" + name for name in MODULES)
    )


@pytest.mark.parametrize("unrelated,miss", [(True, None), (False, "spec.py")])
def test_refuses_unrelated_member_or_missing_expected_delta(
    tmp_path: Path, unrelated: bool, miss: str | None
) -> None:
    old = _make_wheel(tmp_path / "old.whl", new=False)
    new = _make_wheel(tmp_path / "new.whl", new=True, unrelated=unrelated, miss=miss)
    with pytest.raises(wheel_gate.I5WheelStageError):
        wheel_gate.verify_archive_delta(
            old, new, allowed=frozenset("windows_v25/" + name for name in MODULES)
        )


def test_refuses_archive_if_only_six_code_modules_changed(tmp_path: Path) -> None:
    old = _make_wheel(tmp_path / "old.whl", new=False)
    new = _make_wheel(tmp_path / "new.whl", new=True, unchanged="tasks.py")
    with pytest.raises(wheel_gate.I5WheelStageError):
        wheel_gate.verify_archive_delta(
            old, new, allowed=frozenset("windows_v25/" + name for name in MODULES)
        )
