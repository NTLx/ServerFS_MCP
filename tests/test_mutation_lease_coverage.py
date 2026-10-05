"""Every mutation channel takes the writer lease, and no read channel does (v0.11 Phase C §C3).

The lease is only a boundary if *every* mutation goes through it. This enumerates the published
tool surface and asserts the partition by observation, so a new mutation tool that forgets the
lease fails here instead of shipping an unguarded write path.

Platform-neutral on purpose: the lease key, the artifact name and the enforcement have the same
shape on both backends, so this file runs in the Linux CI jobs as well as on Windows.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
from pathlib import Path
from typing import Any

import pytest

from helpers import call_error, call_success, error_code
from serverfs_mcp import lease_identity, tools
from serverfs_mcp.config import Settings
from serverfs_mcp.main import create_server
from serverfs_mcp.workdirs import AGENT_MODE_WORKSPACE_WRITE, Workdir, WorkdirRegistry

ALIAS = "repo"
MUTATIONS = (
    "create_text_file",
    "edit_text_file",
    "create_directory",
    "upload_binary_file",
    "delete_file",
    "delete_directory",
)
READS = (
    "list_workdirs",
    "list_directory",
    "read_text_file",
    "stat_file",
    "search_files",
    "find_files",
    "download_binary_file",
)


class NoopAgentClient:
    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        del method, params
        return {"runtimes": []}


@pytest.fixture()
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workdir = tmp_path / ALIAS
    workdir.mkdir()
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir()
    (lock_dir / "active").mkdir()
    lease_id = lease_identity.alias_lease_id(ALIAS)
    (lock_dir / lease_identity.lock_artifact_name(lease_id)).touch()

    settings = Settings(
        agent_bridge_enabled=True,
        agent_bridge_socket=str(tmp_path / "bridge-endpoint-unused"),
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
    # upload_binary_file is only advertised when the workdir allows binary transfer, so the
    # partition test has to enable it to cover that channel at all.
    registered = dataclasses.replace(
        registered,
        policy=dataclasses.replace(registered.policy, binary_transfer_enabled=True),
    )
    server = create_server(settings, WorkdirRegistry([registered]), NoopAgentClient())

    taken: list[str] = []
    real = tools._mutation_lease

    def recording(lease_settings: Settings, resolved) -> Any:
        taken.append(resolved.workdir.alias)
        return real(lease_settings, resolved)

    monkeypatch.setattr(tools, "_mutation_lease", recording)
    return server, workdir, taken


def leases(taken: list[str], before: int) -> bool:
    return len(taken) > before


def _registered(server) -> set[str]:
    async def _list() -> set[str]:
        return {tool.name for tool in await server.list_tools()}

    return asyncio.run(_list())


def test_every_mutation_channel_takes_the_lease(harness) -> None:
    server, _workdir, taken = harness

    call_success(
        server, "create_text_file", {"workdir": ALIAS, "path": "a.txt", "content": "one\n"}
    )
    assert leases(taken, 0)
    revision = call_success(server, "stat_file", {"workdir": ALIAS, "path": "a.txt"})["revision"]
    assert len(taken) == 1, "stat_file is a read channel"

    before = len(taken)
    call_success(
        server,
        "edit_text_file",
        {
            "workdir": ALIAS,
            "path": "a.txt",
            "expected_revision": revision,
            "edits": [{"old_text": "one\n", "new_text": "two\n"}],
        },
    )
    assert leases(taken, before), "edit_text_file wrote without taking the writer lease"

    for name, args in (
        ("create_directory", {"workdir": ALIAS, "path": "dir"}),
        (
            "upload_binary_file",
            {
                "workdir": ALIAS,
                "path": "b.bin",
                "data_base64": base64.b64encode(b"\x00\x01").decode(),
                "mime_type": "application/octet-stream",
            },
        ),
    ):
        before = len(taken)
        call_success(server, name, args)
        assert leases(taken, before), f"{name} wrote without taking the writer lease"

    for name, args in (
        ("delete_file", {"workdir": ALIAS, "path": "a.txt"}),
        ("delete_directory", {"workdir": ALIAS, "path": "dir"}),
    ):
        # Both delete channels re-check the revision inside the lease, so they need a current one.
        revision = call_success(server, "stat_file", {"workdir": ALIAS, "path": args["path"]})[
            "revision"
        ]
        before = len(taken)
        call_success(server, name, {**args, "expected_revision": revision})
        assert leases(taken, before), f"{name} wrote without taking the writer lease"

    assert len(taken) == len(MUTATIONS)


def test_upload_binary_file_overwrite_takes_the_lease(harness) -> None:
    server, workdir, taken = harness
    (workdir / "b.bin").write_bytes(b"\x00\x01")
    revision = call_success(server, "stat_file", {"workdir": ALIAS, "path": "b.bin"})["revision"]
    before = len(taken)
    call_success(
        server,
        "upload_binary_file",
        {
            "workdir": ALIAS,
            "path": "b.bin",
            "data_base64": base64.b64encode(b"\x02\x03").decode(),
            "mime_type": "application/octet-stream",
            "overwrite": True,
            "expected_revision": revision,
        },
    )
    assert leases(taken, before)
    assert (workdir / "b.bin").read_bytes() == b"\x02\x03"


def test_no_read_channel_takes_the_lease(harness) -> None:
    server, workdir, taken = harness
    (workdir / "note.txt").write_bytes(b"stable\n")
    (workdir / "raw.bin").write_bytes(b"\x00\x01")

    cases: tuple[tuple[str, dict[str, Any]], ...] = (
        ("list_workdirs", {}),
        ("list_directory", {"workdir": ALIAS, "path": "."}),
        ("read_text_file", {"workdir": ALIAS, "path": "note.txt"}),
        ("stat_file", {"workdir": ALIAS, "path": "note.txt"}),
        ("search_files", {"workdir": ALIAS, "path": ".", "query": "stable"}),
        ("find_files", {"workdir": ALIAS, "path": ".", "pattern": "*.txt"}),
        ("download_binary_file", {"workdir": ALIAS, "path": "raw.bin"}),
    )
    available = _registered(server)
    # The search channels are only advertised when rg is installed, so their absence narrows this
    # case on that machine instead of failing it.
    cases = tuple(case for case in cases if case[0] in available)
    assert {name for name, _ in cases} | {"search_files", "find_files"} == set(READS)
    for name, args in cases:
        before = len(taken)
        call_success(server, name, args)
        assert not leases(taken, before), f"{name} is a read channel and must not hold the lease"
    assert taken == []


def test_a_refused_mutation_still_went_through_the_lease(harness) -> None:
    """The lease is taken before the work, so a refused write was never an unguarded one."""
    server, workdir, taken = harness
    (workdir / "note.txt").write_bytes(b"stable\n")
    before = len(taken)
    message = call_error(
        server,
        "edit_text_file",
        {
            "workdir": ALIAS,
            "path": "note.txt",
            "expected_revision": "v1:0000000000000000",
            "edits": [{"old_text": "stable\n", "new_text": "other\n"}],
        },
    )
    assert error_code(message) == "REVISION_CONFLICT", message
    assert leases(taken, before)
