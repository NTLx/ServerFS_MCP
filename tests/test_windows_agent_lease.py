r"""Windows mutation-side lease reader — the §37 matrix (v0.11 Phase C).

Every case here is driven through the published MCP surface, because the defect class this covers is
a channel behaviour, not a helper behaviour. The foreign lease holder is a **real second process**
that holds the artifact through ``serverfs_mcp.windows_lease``, which is the same ``LockFileEx``
shape the Bridge uses, so the contention is the production contention (§5.5 item 2: both sides
acquire identically).

``reader never creates`` is checked as an observable outcome: after a refused mutation the artifact
is still absent.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from helpers import call_error, call_success, error_code
from serverfs_mcp import lease_identity
from serverfs_mcp.config import Settings
from serverfs_mcp.main import create_server
from serverfs_mcp.workdirs import AGENT_MODE_WORKSPACE_WRITE, Workdir, WorkdirRegistry

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="LockFileEx is the Windows backend")

ALIAS = "repo"
LEASE_ID = lease_identity.alias_lease_id(ALIAS)

HOLDER = """
import sys
from pathlib import Path
from serverfs_mcp import windows_lease

lock_dir, lease_id = Path(sys.argv[1]), sys.argv[2]
with windows_lease.hold(lock_dir, lease_id):
    print("HELD", flush=True)
    sys.stdin.read()
"""


class NoopAgentClient:
    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        del method, params
        return {"runtimes": []}


def mutation_server(lock_dir: Path, workdir: Path, *, enabled: bool = True):
    settings = Settings(
        agent_bridge_enabled=enabled,
        agent_bridge_socket=rf"\\.\pipe\serverfs-agent-bridge-unused-{time.time_ns()}",
        agent_lock_dir=str(lock_dir),
    )
    registered = Workdir(
        alias=ALIAS,
        root=workdir,
        description=None,
        read_only=False,
        agent_mode=AGENT_MODE_WORKSPACE_WRITE,
        agent_runtimes=frozenset({"codex"}),
    )
    return create_server(
        settings,
        WorkdirRegistry([registered]),
        NoopAgentClient(),  # type: ignore[arg-type]
    )


def prepare(tmp_path: Path) -> tuple[Path, Path, Path]:
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir()
    (lock_dir / "active").mkdir()
    workdir = tmp_path / "repo"
    workdir.mkdir()
    return lock_dir, workdir, lock_dir / lease_identity.lock_artifact_name(LEASE_ID)


def start_holder(lock_dir: Path) -> subprocess.Popen[str]:
    process = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(lock_dir), LEASE_ID],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert process.stdout is not None
    line = process.stdout.readline()
    assert line.startswith("HELD"), line
    return process


def stop(process: subprocess.Popen[str]) -> None:
    process.terminate()
    process.wait(timeout=15)


def test_mutation_is_refused_while_another_process_holds_the_lease(tmp_path: Path) -> None:
    lock_dir, workdir, artifact = prepare(tmp_path)
    artifact.touch()
    server = mutation_server(lock_dir, workdir)
    holder = start_holder(lock_dir)
    try:
        message = call_error(
            server, "create_text_file", {"workdir": ALIAS, "path": "x.txt", "content": "hi\n"}
        )
    finally:
        stop(holder)
    assert error_code(message) == "WORKDIR_BUSY", message
    assert not (workdir / "x.txt").exists()


def test_live_lease_beats_the_recovery_guard(tmp_path: Path) -> None:
    """§5.4: a busy workdir reports busy, not recovery, because a task may still own it."""
    lock_dir, workdir, artifact = prepare(tmp_path)
    artifact.touch()
    (lock_dir / "active" / lease_identity.guard_artifact_name(LEASE_ID)).write_text(
        "{}\n", encoding="utf-8"
    )
    server = mutation_server(lock_dir, workdir)
    holder = start_holder(lock_dir)
    try:
        message = call_error(
            server, "create_text_file", {"workdir": ALIAS, "path": "x.txt", "content": "hi\n"}
        )
    finally:
        stop(holder)
    assert error_code(message) == "WORKDIR_BUSY", message


def test_read_channels_stay_available_while_the_lease_is_held(tmp_path: Path) -> None:
    """§7 of the C0 decision: reads must still coexist with an Agent workspace-write turn."""
    lock_dir, workdir, artifact = prepare(tmp_path)
    artifact.touch()
    (workdir / "note.txt").write_bytes(b"stable\n")
    server = mutation_server(lock_dir, workdir)
    holder = start_holder(lock_dir)
    try:
        assert (
            call_success(server, "read_text_file", {"workdir": ALIAS, "path": "note.txt"})[
                "content"
            ]
            == "stable\n"
        )
        assert (
            call_success(server, "stat_file", {"workdir": ALIAS, "path": "note.txt"})["type"]
            == "file"
        )
        assert any(
            entry["name"] == "note.txt"
            for entry in call_success(server, "list_directory", {"workdir": ALIAS, "path": "."})[
                "entries"
            ]
        )
    finally:
        stop(holder)


def test_reader_never_creates_an_absent_artifact(tmp_path: Path) -> None:
    lock_dir, workdir, artifact = prepare(tmp_path)
    server = mutation_server(lock_dir, workdir)
    message = call_error(
        server, "create_text_file", {"workdir": ALIAS, "path": "x.txt", "content": "hi\n"}
    )
    assert error_code(message) == "AGENT_LOCK_UNAVAILABLE", message
    assert not artifact.exists()
    assert not (workdir / "x.txt").exists()


def test_a_non_file_artifact_is_refused(tmp_path: Path) -> None:
    lock_dir, workdir, artifact = prepare(tmp_path)
    artifact.mkdir()
    server = mutation_server(lock_dir, workdir)
    message = call_error(
        server, "create_text_file", {"workdir": ALIAS, "path": "x.txt", "content": "hi\n"}
    )
    assert error_code(message) == "AGENT_LOCK_UNAVAILABLE", message


def test_a_remaining_guard_after_a_free_lease_is_recovery_state(tmp_path: Path) -> None:
    lock_dir, workdir, artifact = prepare(tmp_path)
    artifact.touch()
    (lock_dir / "active" / lease_identity.guard_artifact_name(LEASE_ID)).write_text(
        '{"schema_version": 1}\n', encoding="utf-8"
    )
    server = mutation_server(lock_dir, workdir)
    message = call_error(
        server, "create_text_file", {"workdir": ALIAS, "path": "x.txt", "content": "hi\n"}
    )
    assert error_code(message) == "WORKDIR_RECOVERY_REQUIRED", message
    assert not (workdir / "x.txt").exists()


def test_a_guard_that_is_not_a_file_is_refused(tmp_path: Path) -> None:
    lock_dir, workdir, artifact = prepare(tmp_path)
    artifact.touch()
    (lock_dir / "active" / lease_identity.guard_artifact_name(LEASE_ID)).mkdir()
    server = mutation_server(lock_dir, workdir)
    message = call_error(
        server, "create_text_file", {"workdir": ALIAS, "path": "x.txt", "content": "hi\n"}
    )
    assert error_code(message) == "AGENT_LOCK_UNAVAILABLE", message


def test_mutation_succeeds_when_the_lease_is_free_and_no_guard_remains(tmp_path: Path) -> None:
    lock_dir, workdir, artifact = prepare(tmp_path)
    artifact.touch()
    server = mutation_server(lock_dir, workdir)
    call_success(server, "create_text_file", {"workdir": ALIAS, "path": "x.txt", "content": "hi\n"})
    assert (workdir / "x.txt").read_bytes() == b"hi\n"


def test_agent_disabled_deployment_never_opens_the_lease(tmp_path: Path) -> None:
    """The v0.2 no-op path stays exactly as it was: no lock directory, no lease, no failure."""
    _, workdir, _ = prepare(tmp_path)
    server = mutation_server(tmp_path / "absent", workdir, enabled=False)
    call_success(server, "create_text_file", {"workdir": ALIAS, "path": "x.txt", "content": "hi\n"})
    assert (workdir / "x.txt").read_bytes() == b"hi\n"
