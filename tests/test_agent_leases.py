"""Cross-process mutation lease integration tests."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import Any

from helpers import call_concurrently, call_error, call_success, error_code, outcomes
from serverfs_mcp.config import Settings
from serverfs_mcp.main import create_server
from serverfs_mcp.workdirs import Workdir, WorkdirRegistry


class NoopAgentClient:
    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        del method, params
        return {"runtimes": []}


def make_mutation_server(tmp_path: Path) -> tuple[Any, Workdir, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir()
    lock_file = lock_dir / "01.lock"
    lock_file.touch(mode=0o640)

    wd = Workdir(
        slot=1,
        alias="repo",
        container_path=repo,
        description=None,
        read_only=False,
    )
    settings = Settings(
        agent_bridge_enabled=True,
        agent_bridge_socket="/tmp/test-agent-bridge.sock",
        agent_lock_dir=str(lock_dir),
    )
    server = create_server(
        settings,
        WorkdirRegistry([wd]),
        NoopAgentClient(),  # type: ignore[arg-type]
    )
    return server, wd, lock_file


def test_mutation_succeeds_when_shared_agent_lease_is_free(tmp_path: Path) -> None:
    server, wd, _ = make_mutation_server(tmp_path)
    result = call_success(
        server,
        "create_text_file",
        {"workdir": "repo", "path": "ok.txt", "content": "ok\n"},
    )
    assert result["created"] is True
    assert (wd.container_path / "ok.txt").read_text() == "ok\n"


def test_two_serverfs_mutations_still_serialize_instead_of_reporting_busy(
    tmp_path: Path,
) -> None:
    server, wd, _ = make_mutation_server(tmp_path)
    results = call_concurrently(
        server,
        [
            (
                "create_text_file",
                {"workdir": "repo", "path": "a.txt", "content": "a\n"},
            ),
            (
                "create_text_file",
                {"workdir": "repo", "path": "b.txt", "content": "b\n"},
            ),
        ],
    )
    assert outcomes(results) == ["ok", "ok"]
    assert (wd.container_path / "a.txt").read_text() == "a\n"
    assert (wd.container_path / "b.txt").read_text() == "b\n"


def test_mutation_returns_workdir_busy_when_agent_holds_lease(tmp_path: Path) -> None:
    server, wd, lock_file = make_mutation_server(tmp_path)
    holder = os.open(lock_file, os.O_RDWR | os.O_CLOEXEC)
    try:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        message = call_error(
            server,
            "create_text_file",
            {"workdir": "repo", "path": "blocked.txt", "content": "no\n"},
        )
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)

    assert error_code(message) == "WORKDIR_BUSY"
    assert not (wd.container_path / "blocked.txt").exists()


def test_live_agent_guard_keeps_workdir_busy_precedence(tmp_path: Path) -> None:
    server, wd, lock_file = make_mutation_server(tmp_path)
    active_dir = lock_file.parent / "active"
    active_dir.mkdir()
    (active_dir / "01").write_text('{"schema_version":1}\n')

    holder = os.open(lock_file, os.O_RDWR | os.O_CLOEXEC)
    try:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        message = call_error(
            server,
            "create_text_file",
            {"workdir": "repo", "path": "blocked.txt", "content": "no\n"},
        )
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)

    assert error_code(message) == "WORKDIR_BUSY"
    assert not (wd.container_path / "blocked.txt").exists()


def test_mutation_fails_closed_when_recovery_guard_survives_agent_crash(
    tmp_path: Path,
) -> None:
    server, wd, lock_file = make_mutation_server(tmp_path)
    active_dir = lock_file.parent / "active"
    active_dir.mkdir()
    (active_dir / "01").write_text('{"schema_version":1}\n')

    message = call_error(
        server,
        "create_text_file",
        {"workdir": "repo", "path": "blocked.txt", "content": "no\n"},
    )
    assert error_code(message) == "WORKDIR_RECOVERY_REQUIRED"
    assert not (wd.container_path / "blocked.txt").exists()


def test_mutation_fails_closed_when_shared_lock_file_is_missing(tmp_path: Path) -> None:
    server, wd, lock_file = make_mutation_server(tmp_path)
    lock_file.unlink()
    message = call_error(
        server,
        "create_text_file",
        {"workdir": "repo", "path": "blocked.txt", "content": "no\n"},
    )
    assert error_code(message) == "AGENT_LOCK_UNAVAILABLE"
    assert not (wd.container_path / "blocked.txt").exists()


def test_agent_disabled_preserves_v02_mutation_without_lock_dir(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    wd = Workdir(
        slot=1,
        alias="repo",
        container_path=repo,
        description=None,
        read_only=False,
    )
    server = create_server(Settings(), WorkdirRegistry([wd]))
    result = call_success(
        server,
        "create_text_file",
        {"workdir": "repo", "path": "legacy.txt", "content": "legacy\n"},
    )
    assert result["created"] is True


def test_shared_lease_contends_on_a_read_only_lock_file(tmp_path: Path) -> None:
    """A read-only lock file must still arbitrate, as Phase E's read-only bind mount assumes.

    The container opens the lock file O_RDONLY, so file mode 0444 must not
    change the outcome: flock is inode-based and needs no write access.
    """
    server, wd, lock_file = make_mutation_server(tmp_path)
    lock_file.chmod(0o444)
    result = call_success(
        server,
        "create_text_file",
        {"workdir": "repo", "path": "read-only.txt", "content": "ok\n"},
    )
    assert result["created"] is True

    holder = os.open(lock_file, os.O_RDONLY | os.O_CLOEXEC)
    try:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        message = call_error(
            server,
            "create_text_file",
            {"workdir": "repo", "path": "blocked.txt", "content": "no\n"},
        )
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)

    assert error_code(message) == "WORKDIR_BUSY"
    assert not (wd.container_path / "blocked.txt").exists()
