"""Pure contracts for read-only I5 owner Wheel/installed-state drift gate."""

from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1] / "deployment/windows/v0250-i5-owner-private-wheel-audit.py"
)
spec = importlib.util.spec_from_file_location("i5_private_owner_audit_test", SCRIPT)
assert spec is not None and spec.loader is not None
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def test_exact_i5_seven_module_scope() -> None:
    assert audit.INSTANCE == "i5-shared"
    assert audit.CORES == ("i5-core-a", "i5-core-b")
    assert audit.EXPECTED_DELTA == {
        "windows_v25/" + name + ".py"
        for name in ("cli", "isolation", "readiness", "runtime", "spec", "storage", "tasks")
    }


def test_retained_wheel_contents_are_bytewise_not_name_only(tmp_path: Path) -> None:
    wheel = tmp_path / "serverfs_mcp-history.whl"
    with zipfile.ZipFile(wheel, "w") as z:
        z.writestr("serverfs_mcp/__init__.py", b"original")
        z.writestr("serverfs_mcp/windows_v25/tasks.py", b"old-owned-task")
    assert audit.wheel_modules(wheel, {"__init__.py", "windows_v25/tasks.py"}) == {
        "__init__.py": b"original",
        "windows_v25/tasks.py": b"old-owned-task",
    }
    with pytest.raises(KeyError):
        audit.wheel_modules(wheel, {"windows_v25/not-packaged.py"})


def test_refusal_does_not_suppress_source_or_installed_mismatch() -> None:
    with pytest.raises(audit.I5OwnerAuditError, match="test-only"):
        audit.require(False, "test-only")
    audit.require(True, "no issue")


def test_script_never_changes_service_or_credential_state() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    for allowed in (
        "i7.postflight()",
        "i5.verify_candidates(i5.candidates(), installed=True)",
        'state.get("state") == "Disabled"',
        "differences == EXPECTED_DELTA",
        "wheel_modules(wheel, set(installed)) == installed",
        "storage.check_private(wheel, directory=False, sid=sid)",
    ):
        assert allowed in source
    for forbidden in (
        "Scheduler().start(",
        "Scheduler().stop(",
        "Scheduler().install(",
        "uv pip install",
        "http.env",
        "tunnel.token",
        "cloudflare.request(",
        "git push",
        "subprocess.run(",
    ):
        assert forbidden not in source
