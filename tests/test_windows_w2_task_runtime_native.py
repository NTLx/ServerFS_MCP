"""Opt-in Windows W2 real Scheduler lifecycle with TWO harmless test-only tasks.

Each task runs a disposable Python sleeper from the current test interpreter.
Unique names; no ServerFS component names, Bridge, tunnel, network, or secrets.
The finally block limits termination/removal to exact registered identities.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest

from serverfs_mcp.native_endpoint import current_user_sid
from serverfs_mcp.windows_component_task import ComponentTask, build_xml, validate_registered
from serverfs_mcp.windows_process import process_identity
from serverfs_mcp.windows_service import _decode_process_output, _schtasks_executable

pytestmark = [
    pytest.mark.skipif(sys.platform != "win32", reason="Windows Scheduler only"),
    pytest.mark.skipif(
        os.environ.get("SERVERFS_W2_TASK_RUNTIME") != "1",
        reason="explicit W2 task runtime opt-in required",
    ),
]

_MISSING = frozenset({0x80070002, 0x80070003})


def _call(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [str(_schtasks_executable()), *args],
        check=False,
        capture_output=True,
        timeout=25,
    )


def _definition(name: str) -> str | None:
    result = _call("/Query", "/TN", name, "/XML", "/HResult")
    if result.returncode == 0:
        return _decode_process_output(result.stdout).lstrip("\ufeff\x00 \t\r\n")
    if result.returncode & 0xFFFFFFFF in _MISSING:
        return None
    pytest.fail("W2 runtime Scheduler query failed or was denied")


def _must_succeed(*args: str) -> None:
    result = _call(*args)
    assert result.returncode == 0, f"W2 test-owned Scheduler {args[0]} action failed"


def _present(pid: int) -> bool:
    return process_identity(pid) is not None


def _read_started(path: Path) -> int:
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        if path.is_file():
            pid = int(path.read_text(encoding="ascii").strip())
            if _present(pid):
                return pid
        time.sleep(0.1)
    pytest.fail("W2 test-owned task did not start a verifiable child PID")


def _await_gone(pid: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not _present(pid):
            return
        time.sleep(0.1)
    pytest.fail("W2 test-owned child remains after explicit task stop")


def _spec(name: str, mark: Path) -> ComponentTask:
    source = (
        "import os,time; from pathlib import Path; "
        f"Path({str(mark)!r}).write_text(str(os.getpid()),encoding='ascii'); "
        "time.sleep(35)"
    )
    return ComponentTask(
        name=name,
        sid=current_user_sid(),
        executable=Path(sys.executable),
        arguments=("-I", "-B", "-c", source),
        enabled=False,
    )


def test_scheduler_stop_restart_is_scoped_to_one_task(tmp_path: Path) -> None:
    suffix = uuid4().hex[:16]
    a = _spec(f"ServerFS W2 Runtime A {suffix}", tmp_path / "a.pid")
    b = _spec(f"ServerFS W2 Runtime B {suffix}", tmp_path / "b.pid")
    registered: list[ComponentTask] = []
    try:
        for task in (a, b):
            assert _definition(task.name) is None, "test task name already exists"
            xml = tmp_path / (task.name + ".xml")
            xml.write_bytes(build_xml(task, boundary="2099-01-01T00:00:00+00:00"))
            _must_succeed("/Create", "/TN", task.name, "/XML", str(xml))
            registered.append(task)
            normalized = _definition(task.name)
            assert normalized is not None
            assert validate_registered(normalized, task)[0]

        for task in (a, b):
            _must_succeed("/Change", "/TN", task.name, "/ENABLE")
            _must_succeed("/Run", "/TN", task.name)
        a_pid = _read_started(tmp_path / "a.pid")
        b_pid = _read_started(tmp_path / "b.pid")
        assert a_pid != b_pid

        # Disable A's triggers, end only A, then prove B's PID/creation time
        # is unchanged. The second task has no lifecycle dependency on A.
        identity_a = process_identity(a_pid)
        identity_b = process_identity(b_pid)
        assert identity_a is not None and identity_b is not None
        _must_succeed("/Change", "/TN", a.name, "/DISABLE")
        _must_succeed("/End", "/TN", a.name)
        _await_gone(a_pid)
        assert process_identity(b_pid) == identity_b

        _must_succeed("/Change", "/TN", b.name, "/DISABLE")
        _must_succeed("/End", "/TN", b.name)
        _await_gone(b_pid)

        # Restart only B with an actual new Scheduler generation. A remains
        # stopped even when B is enabled and started a second time.
        (tmp_path / "b.pid").unlink()
        _must_succeed("/Change", "/TN", b.name, "/ENABLE")
        _must_succeed("/Run", "/TN", b.name)
        restarted_pid = _read_started(tmp_path / "b.pid")
        restarted_identity = process_identity(restarted_pid)
        assert restarted_identity is not None and restarted_identity != identity_b
        assert process_identity(a_pid) != identity_a
        _must_succeed("/Change", "/TN", b.name, "/DISABLE")
        _must_succeed("/End", "/TN", b.name)
        _await_gone(restarted_pid)
        assert process_identity(a_pid) != identity_a
    finally:
        for task in reversed(registered):
            current = _definition(task.name)
            if current is None:
                continue
            valid, _ = validate_registered(current, task)
            if not valid:
                pytest.fail("W2 test task identity drifted: cleanup refused")
            # Only test-owned names, exact task identity, no wildcard delete.
            _must_succeed("/Change", "/TN", task.name, "/DISABLE")
            _call("/End", "/TN", task.name)  # May already have stopped.
            _must_succeed("/Delete", "/TN", task.name, "/F")
            assert _definition(task.name) is None


def test_scheduler_ends_the_real_pythonw_interpreter_behind_a_venv_launcher(
    tmp_path: Path,
) -> None:
    """W2: /End must kill the real interpreter, not just its venv redirector.

    Explicitly opt-in only. A failure forbids Core/Connector active lifecycle
    cutover until an actual watchdog/contained child is implemented.
    """
    exe = Path(sys.executable).with_name("pythonw.exe")
    if not exe.is_file():
        pytest.skip("test Python environment has no pythonw.exe launcher")
    marker = tmp_path / "pythonw-actual.pid"
    name = f"ServerFS W2 Pythonw Launcher {uuid4().hex[:16]}"
    code = (
        "import os,time; from pathlib import Path; "
        f"Path({str(marker)!r}).write_text(str(os.getpid()),encoding='ascii'); "
        "time.sleep(40)"
    )
    task = ComponentTask(
        name=name,
        sid=current_user_sid(),
        executable=exe,
        arguments=("-I", "-B", "-c", code),
        enabled=False,
    )
    registered = False
    child_pid: int | None = None
    try:
        assert _definition(name) is None, "test-only scheduled task already exists"
        xml = tmp_path / "pythonw.xml"
        xml.write_bytes(build_xml(task))
        _must_succeed("/Create", "/TN", name, "/XML", str(xml))
        registered = True
        current = _definition(name)
        assert current is not None and validate_registered(current, task)[0]
        _must_succeed("/Change", "/TN", name, "/ENABLE")
        _must_succeed("/Run", "/TN", name)
        child_pid = _read_started(marker)
        child = process_identity(child_pid)
        assert child is not None
        _must_succeed("/Change", "/TN", name, "/DISABLE")
        _must_succeed("/End", "/TN", name)
        _await_gone(child_pid)
        assert process_identity(child_pid) != child
    finally:
        if registered:
            current = _definition(name)
            if current is None or not validate_registered(current, task)[0]:
                pytest.fail("test-only pythonw task identity drifted; manual cleanup required")
            _call("/Change", "/TN", name, "/DISABLE")
            _call("/End", "/TN", name)
            _must_succeed("/Delete", "/TN", name, "/F")
            assert _definition(name) is None
        if child_pid is not None and _present(child_pid):
            pytest.fail("test-owned pythonw child survived cleanup")
