"""Native Windows ACP process launch and task-level tree stop behavior."""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from serverfs_agent_bridge.adapters.acp_process import spawn_acp_child, stop_acp_child

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows ACP process behavior")

_REAL_PYTHON = getattr(sys, "_base_executable", sys.executable)

_TREE_AGENT = """
import os, subprocess, sys, time

pid_file = sys.argv[1]
descendant = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
with open(pid_file, "w", encoding="ascii") as stream:
    stream.write(f"{os.getpid()} {descendant.pid}\\n")
    stream.flush()
while True:
    time.sleep(1)
"""


def _windows_pid_running(pid: int) -> bool:
    """Check a PID through a synchronization handle, without signaling it."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.CloseHandle.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

    handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        error = ctypes.get_last_error()
        if error in (6, 87):  # ERROR_INVALID_HANDLE / ERROR_INVALID_PARAMETER
            return False
        raise OSError(error, "OpenProcess failed while checking test process")
    try:
        result = kernel32.WaitForSingleObject(handle, 0)
        if result == 0x00000102:  # WAIT_TIMEOUT
            return True
        if result == 0:  # WAIT_OBJECT_0
            return False
        raise OSError("WaitForSingleObject returned an uncertain process state")
    finally:
        kernel32.CloseHandle(handle)


async def _wait_for_file(path: Path, timeout: float) -> str:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            value = path.read_text(encoding="ascii").strip()
        except (FileNotFoundError, PermissionError):
            await asyncio.sleep(0.02)
            continue
        if value:
            return value
        await asyncio.sleep(0.02)
    raise AssertionError("ACP fixture did not publish its child process IDs")


@pytest.mark.asyncio
async def test_direct_launch_preserves_argv_and_paths_with_spaces(tmp_path: Path) -> None:
    cwd = tmp_path / "working directory with spaces"
    script_dir = tmp_path / "script directory with spaces"
    cwd.mkdir()
    script_dir.mkdir()
    script = script_dir / "echo argv.py"
    script.write_text(
        "import json, sys, time; "
        "payload = json.dumps(sys.argv[1:], ensure_ascii=False).encode('utf-8') + b'\\n'; "
        "sys.stdout.buffer.write(payload); sys.stdout.buffer.flush(); "
        "time.sleep(300)\n",
        encoding="utf-8",
    )
    expected = ["", "two words", 'an "embedded quote"', "trailing\\", "雪だるま🙂"]

    child = await spawn_acp_child(
        _REAL_PYTHON,
        (str(script), *expected),
        cwd=cwd,
        env=os.environ.copy(),
    )
    try:
        assert child.process.stdout is not None
        raw = await asyncio.wait_for(child.process.stdout.readline(), timeout=5)
        if not raw:
            assert child.process.stderr is not None
            stderr = await asyncio.wait_for(child.process.stderr.read(), timeout=5)
            raise AssertionError(
                f"ACP argv fixture exited without stdout: returncode={child.process.returncode}, "
                f"stderr={stderr.decode('utf-8', errors='replace')!r}"
            )
        assert json.loads(raw.decode("utf-8")) == expected
        assert await asyncio.wait_for(stop_acp_child(child, timeout=2), timeout=6)
    finally:
        if child.process.returncode is None:
            await asyncio.wait_for(stop_acp_child(child, timeout=2), timeout=6)


@pytest.mark.asyncio
async def test_stop_kills_acp_tree_idempotently_and_preserves_unrelated_sibling(
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "tree pids.txt"
    sibling = subprocess.Popen([_REAL_PYTHON, "-c", "import time; time.sleep(300)"])
    child = await spawn_acp_child(
        _REAL_PYTHON,
        ("-c", _TREE_AGENT, str(pid_file)),
        cwd=tmp_path,
        env=os.environ.copy(),
    )
    try:
        direct_pid, descendant_pid = map(int, (await _wait_for_file(pid_file, 5)).split())
        assert direct_pid == child.process.pid
        assert _windows_pid_running(direct_pid)
        assert _windows_pid_running(descendant_pid)
        assert sibling.poll() is None

        assert await asyncio.wait_for(stop_acp_child(child, timeout=2), timeout=6)
        assert child.process.returncode is not None
        assert not _windows_pid_running(direct_pid)
        assert not _windows_pid_running(descendant_pid)
        assert sibling.poll() is None

        assert await asyncio.wait_for(stop_acp_child(child, timeout=2), timeout=6)
        assert sibling.poll() is None
        assert _windows_pid_running(sibling.pid)
    finally:
        if child.process.returncode is None:
            await asyncio.wait_for(stop_acp_child(child, timeout=2), timeout=6)
        if sibling.poll() is None:
            sibling.terminate()
            sibling.wait(timeout=5)
