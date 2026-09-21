"""Text mutation limits use each selected workdir's effective policy."""

from __future__ import annotations

import dataclasses
from pathlib import Path

from helpers import call_error, call_success, error_code, registry_for
from serverfs_mcp.config import Settings
from serverfs_mcp.main import create_server
from serverfs_mcp.workdirs import EffectiveWorkdirPolicy, Workdir


def make_limit_server(tmp_path: Path):
    override_path = tmp_path / "override"
    inherited_path = tmp_path / "inherited"
    override_path.mkdir()
    inherited_path.mkdir()
    override = Workdir(
        slot=1,
        alias="override",
        container_path=override_path,
        description=None,
        read_only=False,
        policy=dataclasses.replace(EffectiveWorkdirPolicy(), max_write_bytes=4),
    )
    inherited = Workdir(
        slot=2,
        alias="inherited",
        container_path=inherited_path,
        description=None,
        read_only=False,
        policy=dataclasses.replace(EffectiveWorkdirPolicy(), max_write_bytes=100),
    )
    server = create_server(
        Settings(max_write_bytes=100),
        registry_for(override, inherited),
    )
    return server, override_path, inherited_path


def test_create_uses_selected_workdir_write_limit(tmp_path: Path) -> None:
    server, _, inherited_path = make_limit_server(tmp_path)

    message = call_error(
        server,
        "create_text_file",
        {"workdir": "override", "path": "too-big.txt", "content": "12345"},
    )
    assert error_code(message) == "WRITE_TOO_LARGE"

    result = call_success(
        server,
        "create_text_file",
        {"workdir": "inherited", "path": "global.txt", "content": "12345"},
    )
    assert result["bytes_written"] == 5
    assert (inherited_path / "global.txt").read_bytes() == b"12345"


def test_edit_uses_selected_workdir_write_limit_for_source_and_result(tmp_path: Path) -> None:
    server, override_path, inherited_path = make_limit_server(tmp_path)
    (override_path / "source-too-big.txt").write_bytes(b"12345")
    (override_path / "result-too-big.txt").write_bytes(b"aaaa")
    (inherited_path / "global.txt").write_bytes(b"hello")

    source_revision = call_success(
        server,
        "stat_file",
        {"workdir": "override", "path": "source-too-big.txt"},
    )["revision"]
    message = call_error(
        server,
        "edit_text_file",
        {
            "workdir": "override",
            "path": "source-too-big.txt",
            "expected_revision": source_revision,
            "edits": [{"old_text": "x", "new_text": "y"}],
        },
    )
    assert error_code(message) == "WRITE_TOO_LARGE"

    result_revision = call_success(
        server,
        "stat_file",
        {"workdir": "override", "path": "result-too-big.txt"},
    )["revision"]
    message = call_error(
        server,
        "edit_text_file",
        {
            "workdir": "override",
            "path": "result-too-big.txt",
            "expected_revision": result_revision,
            "edits": [{"old_text": "a", "new_text": "123", "expected_count": 4}],
        },
    )
    assert error_code(message) == "WRITE_TOO_LARGE"
    assert (override_path / "result-too-big.txt").read_bytes() == b"aaaa"

    inherited_revision = call_success(
        server,
        "stat_file",
        {"workdir": "inherited", "path": "global.txt"},
    )["revision"]
    result = call_success(
        server,
        "edit_text_file",
        {
            "workdir": "inherited",
            "path": "global.txt",
            "expected_revision": inherited_revision,
            "edits": [{"old_text": "hello", "new_text": "world"}],
        },
    )
    assert result["bytes_after"] == 5
    assert (inherited_path / "global.txt").read_bytes() == b"world"
