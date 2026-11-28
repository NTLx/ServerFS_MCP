"""Shared fixtures: temp workdir tree with slot sentinels, registry, settings.

Collection classification (v0.11 Phase A): the files listed below import the Linux
kernel layer at module scope — `fdio`'s `O_DIRECTORY`/`O_NOFOLLOW` FD walk, the
`flock`-based shared lease, or `rg`'s `/proc/self/fd` cwd — so on another platform
they cannot even be imported. They are not skipped to hide a failure: each one is the
positive contract of a POSIX mechanism that `serverfs` keeps unchanged on Linux, and
the Windows native kernel has its own coverage in `test_windows_backend`,
`test_native_windows` and `test_windows_path_acceptance`. Everything that speaks only
to the MCP surface stays collected and running on every platform.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from serverfs_mcp.config import Settings
from serverfs_mcp.workdirs import SLOT_COUNT, Workdir, WorkdirRegistry, build_registry

LINUX_ONLY_TEST_FILES = [
    "test_agent_leases.py",
    "test_binary_download.py",
    "test_binary_overwrite.py",
    "test_binary_upload.py",
    "test_fdio.py",
    "test_find_files.py",
    "test_list_directory.py",
    "test_search_text.py",
    "test_stat_file.py",
]

if not sys.platform.startswith("linux"):
    collect_ignore = LINUX_ONLY_TEST_FILES


@pytest.fixture()
def workdir_root(tmp_path: Path) -> Path:
    """A workdir root with slot 01 mounted as a real tree, others disabled."""
    root = tmp_path / "workdirs"
    root.mkdir()
    real = root / "01"
    real.mkdir()
    for slot in range(2, SLOT_COUNT + 1):
        d = root / f"{slot:02d}"
        d.mkdir()
        (d / ".serverfs-disabled").touch()
    return root


@pytest.fixture()
def registry(workdir_root: Path) -> WorkdirRegistry:
    return build_registry(
        {1: "test", **{s: "" for s in range(2, SLOT_COUNT + 1)}},
        {1: "A test workdir", **{s: "" for s in range(2, SLOT_COUNT + 1)}},
        workdir_root=workdir_root,
    )


@pytest.fixture()
def workdir(registry) -> Workdir:
    wd = registry.get("test")
    assert wd is not None
    return wd


@pytest.fixture()
def settings() -> Settings:
    return Settings()
