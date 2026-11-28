"""Native Windows Repo Ops containment, stop proof, and reparse rejection."""

from __future__ import annotations

import asyncio
import ctypes
import subprocess
import sys
import threading
from ctypes import wintypes
from pathlib import Path

import pytest

from serverfs_agent_bridge.errors import BridgeError
from serverfs_agent_bridge.leases import LeaseManager
from serverfs_agent_bridge.repo_ops import (
    RepoCleanupUnproven,
    _validate_host_path,
    run_bounded_command,
)
from test_repo_ops_service import make_service

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="native Windows Job/NTFS contract")

_TREE_SCRIPT = """
import os
import subprocess
import sys
import time
from pathlib import Path

role, mode, parent_exits = sys.argv[1:]
if role == 'parent':
    subprocess.Popen([sys.executable, __file__, 'child', mode, parent_exits])
    if parent_exits == 'yes':
        sys.exit(0)
elif role == 'child':
    subprocess.Popen([sys.executable, __file__, 'leaf', mode, parent_exits])
    Path('child.pid').write_text(str(os.getpid()))
    while not Path('leaf.pid').exists():
        time.sleep(0.01)
    Path('ready').touch()
    if mode.endswith('overflow'):
        while not Path('trigger').exists():
            time.sleep(0.01)
        stream = sys.stdout if mode == 'stdout_overflow' else sys.stderr
        stream.buffer.write(b'x' * 4096)
        stream.flush()
else:
    Path('leaf.pid.tmp').write_text(str(os.getpid()))
    Path('leaf.pid.tmp').replace('leaf.pid')
while True:
    time.sleep(0.02)
"""


@pytest.fixture
def windows_command():
    # Lazy import keeps collection safe on POSIX: the helper loads kernel32.
    from serverfs_agent_bridge import repo_ops_windows

    return repo_ops_windows


@pytest.fixture
def process_handles():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.IsProcessInJob.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.BOOL),
    ]
    kernel32.IsProcessInJob.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handles = []

    def retain(pid: int, job):
        handle = kernel32.OpenProcess(0x00101000, False, pid)
        assert handle, f"could not retain process {pid}"
        handles.append(handle)
        contained = wintypes.BOOL()
        assert kernel32.IsProcessInJob(handle, job.handle, ctypes.byref(contained))
        assert contained.value, "descendant escaped the command Job"
        return handle

    def assert_stopped():
        assert handles
        # Object signalling is eventual teardown evidence, not the writer-safety barrier.
        states = [kernel32.WaitForSingleObject(handle, 5000) for handle in handles]
        assert states == [0] * len(handles), f"descendant wait states: {states}"

    yield retain, assert_stopped
    for handle in handles:
        kernel32.CloseHandle(handle)


async def wait_until(predicate, command: asyncio.Task | None = None) -> None:
    async with asyncio.timeout(10):
        while not predicate():
            if command is not None and command.done():
                await command
                pytest.fail("command finished before the test handshake")
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_exits", [False, True], ids=["leader-alive", "leader-exited"])
@pytest.mark.parametrize("mode", ["timeout", "stdout_overflow", "stderr_overflow", "cancel"])
async def test_descendants_stop_before_writer_lease_release(
    tmp_path: Path, monkeypatch, windows_command, process_handles, mode: str, parent_exits: bool
) -> None:
    service, policy = make_service(tmp_path)
    repo = policy.host_path
    script = repo / "tree.py"
    script.write_text(_TREE_SCRIPT, encoding="utf-8")
    retain, assert_stopped = process_handles
    processes = []
    jobs = []
    proven = []
    releases = []
    original_assign = windows_command.WindowsCommandJob.assign
    original_stop = windows_command.WindowsCommandJob.stop
    original_acquire = service.lease_manager.acquire_exclusive

    def assign(job, process):
        processes.append(process)
        jobs.append(job)
        return original_assign(job, process)

    def stop(job, process):
        original_stop(job, process)
        assert job.active_processes() == 0
        proven.append(True)

    class ObservedLease:
        def __init__(self, lease):
            self.lease = lease

        def release(self):
            try:
                assert proven
                releases.append(True)
            finally:
                self.lease.release()

    monkeypatch.setattr(windows_command.WindowsCommandJob, "assign", assign)
    monkeypatch.setattr(windows_command.WindowsCommandJob, "stop", stop)
    monkeypatch.setattr(
        service.lease_manager, "acquire_exclusive", lambda key: ObservedLease(original_acquire(key))
    )

    async def run():
        async with service._repo_write_lease(policy):
            return await run_bounded_command(
                (sys.executable, str(script), "parent", mode, "yes" if parent_exits else "no"),
                cwd=repo,
                timeout_seconds=3 if mode == "timeout" else 20,
                max_output_bytes=1024,
            )

    command = asyncio.create_task(run())
    try:
        await wait_until(lambda: (repo / "ready").exists(), command)
        for name in ("child.pid", "leaf.pid"):
            retain(int((repo / name).read_text()), jobs[0])
        if parent_exits:
            await wait_until(lambda: processes[0].poll() is not None, command)
            assert not command.done()  # Descendants still own inherited stdout/stderr.
        other = LeaseManager(tmp_path / "locks", lease_ids=[policy.lease_id])
        with pytest.raises(BridgeError, match="busy"):
            other.acquire_exclusive(policy.lease_id)
        if mode == "cancel":
            command.cancel()
            with pytest.raises(asyncio.CancelledError):
                await command
        else:
            (repo / "trigger").touch()
            with pytest.raises(BridgeError) as exc:
                await command
            assert exc.value.code == (
                "REPO_TIMEOUT" if mode == "timeout" else "REPO_OUTPUT_TOO_LARGE"
            )
        assert_stopped()
        assert releases == [True]
        assert service._repo_cleanup_leases == []
        other.acquire_exclusive(policy.lease_id).release()
    finally:
        if not command.done():
            command.cancel()
        await asyncio.gather(command, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["assign", "resume"])
async def test_startup_failure_never_runs_uncontained_code(
    tmp_path: Path, monkeypatch, windows_command, stage: str
) -> None:
    marker = tmp_path / "ran"

    def fail(*_args):
        assert not marker.exists()
        raise OSError("injected startup failure")

    monkeypatch.setattr(windows_command.WindowsCommandJob, stage, fail)
    with pytest.raises(BridgeError) as exc:
        await run_bounded_command(
            (sys.executable, "-c", "from pathlib import Path; Path('ran').touch()"),
            cwd=tmp_path,
            timeout_seconds=5,
        )
    assert exc.value.code == "REPO_EXEC_FAILED"
    assert not marker.exists()


@pytest.mark.asyncio
async def test_repeated_cancellation_waits_for_pipe_drain(
    tmp_path: Path, monkeypatch, windows_command
) -> None:
    entered = asyncio.Event()
    unblock = asyncio.Event()
    original_read = windows_command._read_limited
    reader_gate = threading.Event()

    def read(stream, limit):
        result = original_read(stream, limit)
        reader_gate.wait(10)
        return result

    original_drain = windows_command._drain

    async def drain(tasks, completion):
        entered.set()
        await original_drain(tasks, completion)
        unblock.set()

    monkeypatch.setattr(windows_command, "_read_limited", read)
    monkeypatch.setattr(windows_command, "_drain", drain)
    command = asyncio.create_task(
        run_bounded_command(
            (sys.executable, "-c", "import time; time.sleep(60)"),
            cwd=tmp_path,
            timeout_seconds=20,
        )
    )
    try:
        await asyncio.sleep(0)
        command.cancel()
        await wait_until(entered.is_set)
        command.cancel()
        await asyncio.sleep(0)
        assert not command.done()
        assert not unblock.is_set()
        reader_gate.set()
        with pytest.raises(asyncio.CancelledError):
            await command
    finally:
        reader_gate.set()
        await asyncio.gather(command, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_type",
    [
        None,
        RepoCleanupUnproven,
        BridgeError,
        asyncio.CancelledError,
        KeyboardInterrupt,
        SystemExit,
        BaseException,
    ],
)
async def test_repo_writer_retains_only_unproven_cleanup(tmp_path: Path, error_type) -> None:
    service, policy = make_service(tmp_path)
    error = (
        BridgeError("TEST_FAILURE", "injected")
        if error_type is BridgeError
        else error_type()
        if error_type
        else None
    )
    other = LeaseManager(tmp_path / "locks", lease_ids=[policy.lease_id])
    try:
        if error is None:
            async with service._repo_write_lease(policy):
                pass
        else:
            with pytest.raises(error_type) as exc:
                async with service._repo_write_lease(policy):
                    raise error
            assert exc.value is error
        if error_type is RepoCleanupUnproven:
            assert len(service._repo_cleanup_leases) == 1
            with pytest.raises(BridgeError, match="busy"):
                other.acquire_exclusive(policy.lease_id)
        else:
            assert service._repo_cleanup_leases == []
            other.acquire_exclusive(policy.lease_id).release()
    finally:
        for lease in service._repo_cleanup_leases:
            lease.release()


@pytest.mark.asyncio
async def test_windows_command_result_and_environment(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SERVERFS_TEST_SECRET", "must-not-leak")
    result = await run_bounded_command(
        (
            sys.executable,
            "-c",
            "import os,sys; print(os.getenv('SERVERFS_TEST_SECRET')); "
            "print('error', file=sys.stderr); sys.exit(7)",
        ),
        cwd=tmp_path,
        timeout_seconds=5,
    )
    assert result.returncode == 7
    assert result.stdout.strip() == "None"
    assert result.stderr.strip() == "error"


@pytest.mark.parametrize("require_file", [False, True])
@pytest.mark.parametrize("dangling", [False, True])
def test_host_path_rejects_junctions(tmp_path: Path, require_file: bool, dangling: bool) -> None:
    repo = tmp_path / "repo"
    target = tmp_path / "outside"
    repo.mkdir()
    target.mkdir()
    (target / "pin.txt").write_text("outside", encoding="utf-8")
    junction = repo / "junction"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
        capture_output=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode:
        pytest.skip("this host cannot create NTFS junctions")
    if dangling:
        (target / "pin.txt").unlink()
        target.rmdir()
    try:
        for value in ("junction", "junction/pin.txt"):
            with pytest.raises(BridgeError) as exc:
                _validate_host_path(repo, value, require_existing_file=require_file)
            assert exc.value.code == "INVALID_REPO_PATH"
        with pytest.raises(BridgeError) as exc:
            _validate_host_path(junction, "pin.txt", require_existing_file=require_file)
        assert exc.value.code == "INVALID_REPO_PATH"
    finally:
        junction.rmdir()  # Remove only the junction itself, never its target.


@pytest.mark.parametrize("directory", [False, True])
def test_host_path_rejects_symlinks_where_supported(tmp_path: Path, directory: bool) -> None:
    target = tmp_path / "target"
    if directory:
        target.mkdir()
        (target / "pin.txt").write_text("data", encoding="utf-8")
    else:
        target.write_text("data", encoding="utf-8")
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError:
        pytest.skip("this host cannot create symlinks without elevation")
    for require_file in (False, True):
        with pytest.raises(BridgeError) as exc:
            _validate_host_path(
                tmp_path,
                "link/pin.txt" if directory else "link",
                require_existing_file=require_file,
            )
        assert exc.value.code == "INVALID_REPO_PATH"


def test_host_path_accepts_regular_and_missing_commit_entries(tmp_path: Path) -> None:
    (tmp_path / "regular.txt").write_text("data", encoding="utf-8")
    _validate_host_path(tmp_path, "regular.txt", require_existing_file=True)
    _validate_host_path(tmp_path, "deleted.txt", require_existing_file=False)
    with pytest.raises(BridgeError, match="does not exist"):
        _validate_host_path(tmp_path, "deleted.txt", require_existing_file=True)
