"""Offline-only I7 Scheduler wheel staging safeguards (never starts services)."""

from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "deployment/windows/v0250-i7-scheduler-failclosed-wheel-stage.py"
)
spec = importlib.util.spec_from_file_location("i7_scheduler_wheel", SCRIPT)
assert spec is not None and spec.loader is not None
stage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stage)


def _wheel(path: Path, *, tasks: bytes, other: bytes, metadata: bytes = b"meta") -> Path:
    files = {
        "serverfs_mcp/windows_v25/tasks.py": tasks,
        "serverfs_mcp/windows_v25/runtime.py": other,
        "serverfs_mcp/__init__.py": b"package",
        "serverfs_mcp-0.25.0.dist-info/METADATA": metadata,
        "serverfs_mcp-0.25.0.dist-info/RECORD": b"record",
    }
    with zipfile.ZipFile(path, "w") as archive:
        for name, contents in files.items():
            archive.writestr(name, contents)
    return path


def test_i7_only_no_installer_or_service_runtime_mutation() -> None:
    assert stage.INSTANCE == "i7-public"
    assert stage.SLOT == "scheduler-failclosed-wheel-1"
    assert stage.CHANGED == "windows_v25/tasks.py"
    assert stage.TARGETS == ("core", "cloudflare")
    source = SCRIPT.read_text(encoding="utf-8")
    for expected in (
        "gate.host()",
        "gate.postflight()",
        "mismatches == {CHANGED}",
        "archived_modules(wheel, set(modules)) == installed",
        "installer._new_dir(slot, sid)",
        "installer._wheel(",
        "installer._protect_tree(slot, sid)",
        "wheel_metadata_delta(previous, wheel)",
        "I7_SCHEDULER_NO_PRIVATE_PACKAGE_REINSTALL=PASS",
    ):
        assert expected in source
    for forbidden in (
        "uv pip install",
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


def test_new_wheel_may_change_only_scheduler_and_record(tmp_path: Path) -> None:
    old = _wheel(tmp_path / "old.whl", tasks=b"old", other=b"stable")
    candidate = _wheel(tmp_path / "new.whl", tasks=b"new", other=b"stable")
    stage.wheel_metadata_delta(old, candidate)
    assert stage.archived_modules(
        candidate, {"__init__.py", "windows_v25/runtime.py", "windows_v25/tasks.py"}
    ) == {
        "__init__.py": b"package",
        "windows_v25/runtime.py": b"stable",
        "windows_v25/tasks.py": b"new",
    }


@pytest.mark.parametrize(
    ("other", "metadata"),
    [(b"unreviewed-runtime-drift", b"meta"), (b"stable", b"changed-metadata")],
)
def test_wheel_refuses_unrelated_code_or_metadata_delta(
    tmp_path: Path, other: bytes, metadata: bytes
) -> None:
    old = _wheel(tmp_path / "old.whl", tasks=b"old", other=b"stable")
    new = _wheel(tmp_path / "new.whl", tasks=b"new", other=other, metadata=metadata)
    with pytest.raises(stage.I7WheelStageError):
        stage.wheel_metadata_delta(old, new)


def test_wheel_verification_refuses_stale_source(tmp_path: Path) -> None:
    old = _wheel(tmp_path / "old.whl", tasks=b"old", other=b"stable")
    new = _wheel(tmp_path / "new.whl", tasks=b"new", other=b"stable")
    stage.verify_candidate(
        new,
        {
            "__init__.py": b"package",
            "windows_v25/runtime.py": b"stable",
            "windows_v25/tasks.py": b"new",
        },
        {"core": old},
    )
    with pytest.raises(stage.I7WheelStageError):
        stage.verify_candidate(
            new,
            {
                "__init__.py": b"package",
                "windows_v25/runtime.py": b"stable",
                "windows_v25/tasks.py": b"unexpected",
            },
            {"core": old},
        )
