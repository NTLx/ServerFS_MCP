"""End-to-end tool-layer security tests through the MCP tool surface.

These exercise the tools the way the agent would: via mcp.call_tool with
flat arguments matching the published input schema, verifying that security
errors surface as recoverable ToolError messages and that no internal paths
leak.
"""

from __future__ import annotations

import asyncio
import os
import socket

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from serverfs_mcp.config import Settings
from serverfs_mcp.main import create_server


@pytest.fixture()
def server(registry, settings):
    return create_server(settings, registry)


def call_error(server, name: str, args: dict) -> str:
    """Call a tool expecting a ToolError; return its message."""

    async def _call():
        try:
            await server.call_tool(name, args)
        except ToolError as e:
            return str(e)
        raise AssertionError(f"expected ToolError from {name}, got success")

    return asyncio.run(_call())


def call_success(server, name: str, args: dict) -> dict:
    """Call a tool expecting success; return structured content."""

    async def _call():
        result = await server.call_tool(name, args)
        assert not result.is_error, result
        return result.structured_content

    return asyncio.run(_call())


def _registry_for(workdir):
    from serverfs_mcp.workdirs import WorkdirRegistry

    return WorkdirRegistry([workdir])


class TestToolErrors:
    def test_traversal_blocked(self, server) -> None:
        msg = call_error(server, "read_text_file", {"workdir": "test", "path": "../../etc/passwd"})
        assert "PATH_OUTSIDE_WORKDIR" in msg
        assert "/workdirs" not in msg

    def test_absolute_path_blocked(self, server) -> None:
        msg = call_error(server, "read_text_file", {"workdir": "test", "path": "/etc/passwd"})
        assert "PATH_OUTSIDE_WORKDIR" in msg

    def test_workdir_not_found(self, server) -> None:
        msg = call_error(server, "list_directory", {"workdir": "nope", "path": ""})
        assert "WORKDIR_NOT_FOUND" in msg

    def test_hidden_read_blocked(self, server, workdir) -> None:
        (workdir.container_path / ".env").write_text("SECRET=1")
        msg = call_error(server, "read_text_file", {"workdir": "test", "path": ".env"})
        assert "HIDDEN_PATH_NOT_ALLOWED" in msg

    def test_denied_read_blocked_even_with_allow_hidden(self, workdir) -> None:
        (workdir.container_path / "id_rsa").write_text("KEY")
        settings = Settings(allow_hidden=True)
        srv = create_server(settings, _registry_for(workdir))
        msg = call_error(srv, "read_text_file", {"workdir": "test", "path": "id_rsa"})
        assert "DENIED_PATH" in msg

    def test_symlink_escape_blocked(self, server, workdir) -> None:
        os.symlink("/etc", workdir.container_path / "etclink")
        msg = call_error(server, "read_text_file", {"workdir": "test", "path": "etclink/passwd"})
        assert "SYMLINK_NOT_ALLOWED" in msg

    def test_path_not_found(self, server) -> None:
        msg = call_error(server, "read_text_file", {"workdir": "test", "path": "ghost.txt"})
        assert "PATH_NOT_FOUND" in msg

    def test_nul_rejected(self, server) -> None:
        msg = call_error(server, "read_text_file", {"workdir": "test", "path": "a\x00b"})
        assert "ACCESS_DENIED" in msg


class TestExtraDenyGlobsViaTools:
    """SERVERFS_EXTRA_DENY_GLOBS applies to every channel uniformly."""

    def _srv(self, workdir, **settings_kw):
        settings = Settings(**settings_kw)
        return create_server(settings, _registry_for(workdir))

    def test_extra_glob_blocks_read(self, workdir) -> None:
        (workdir.container_path / "db.sqlite").write_text("NEEDLE\n")
        srv = self._srv(workdir, extra_deny_globs=("*.sqlite",))
        msg = call_error(srv, "read_text_file", {"workdir": "test", "path": "db.sqlite"})
        assert "DENIED_PATH" in msg

    def test_extra_glob_blocks_stat(self, workdir) -> None:
        (workdir.container_path / "db.sqlite").write_text("NEEDLE\n")
        srv = self._srv(workdir, extra_deny_globs=("*.sqlite",))
        msg = call_error(srv, "stat_file", {"workdir": "test", "path": "db.sqlite"})
        assert "DENIED_PATH" in msg

    def test_extra_glob_hides_from_list(self, workdir) -> None:
        (workdir.container_path / "db.sqlite").write_text("NEEDLE\n")
        (workdir.container_path / "ok.txt").write_text("x\n")
        srv = self._srv(workdir, extra_deny_globs=("*.sqlite",))
        data = call_success(srv, "list_directory", {"workdir": "test", "path": ""})
        names = [e["name"] for e in data["entries"]]
        assert "db.sqlite" not in names
        assert "ok.txt" in names

    def test_extra_glob_skips_find(self, workdir) -> None:
        (workdir.container_path / "db.sqlite").write_text("x\n")
        srv = self._srv(workdir, extra_deny_globs=("*.sqlite",))
        data = call_success(srv, "find_files", {"workdir": "test", "pattern": "*.sqlite"})
        assert data["returned"] == 0

    def test_extra_glob_filters_search(self, workdir) -> None:
        (workdir.container_path / "db.sqlite").write_text("NEEDLE\n")
        (workdir.container_path / "note.txt").write_text("NEEDLE\n")
        srv = self._srv(workdir, extra_deny_globs=("*.sqlite",))
        data = call_success(srv, "search_text", {"workdir": "test", "query": "NEEDLE"})
        assert data["returned"] == 1
        assert data["matches"][0]["path"] == "note.txt"

    def test_extra_dir_glob_blocks_nested_read_and_search(self, workdir) -> None:
        (workdir.container_path / "internal").mkdir()
        (workdir.container_path / "internal" / "doc.md").write_text("NEEDLE\n")
        srv = self._srv(workdir, extra_deny_globs=("internal/**",))
        msg = call_error(srv, "read_text_file", {"workdir": "test", "path": "internal/doc.md"})
        assert "DENIED_PATH" in msg
        data = call_success(srv, "search_text", {"workdir": "test", "query": "NEEDLE"})
        assert data["returned"] == 0


class TestDisableDefaultDenyViaTools:
    """SERVERFS_DISABLE_DEFAULT_DENY=true releases the built-in rules only."""

    def _srv(self, workdir):
        settings = Settings(allow_hidden=True, disable_default_deny=True)
        return create_server(settings, _registry_for(workdir))

    def test_env_file_readable_when_disabled(self, workdir) -> None:
        (workdir.container_path / "app.env").write_text("VALUE=1\n")
        data = call_success(
            self._srv(workdir), "read_text_file", {"workdir": "test", "path": "app.env"}
        )
        assert data["content"] == "VALUE=1\n"

    def test_id_rsa_readable_when_disabled_and_hidden_allowed(self, workdir) -> None:
        (workdir.container_path / "id_rsa").write_text("KEY\n")
        data = call_success(
            self._srv(workdir), "read_text_file", {"workdir": "test", "path": "id_rsa"}
        )
        assert data["content"] == "KEY\n"

    def test_extra_globs_survive_disable(self, workdir) -> None:
        (workdir.container_path / "db.sqlite").write_text("x\n")
        settings = Settings(
            allow_hidden=True, disable_default_deny=True, extra_deny_globs=("*.sqlite",)
        )
        srv = create_server(settings, _registry_for(workdir))
        msg = call_error(srv, "read_text_file", {"workdir": "test", "path": "db.sqlite"})
        assert "DENIED_PATH" in msg


class TestNoInternalLeak:
    @pytest.mark.parametrize(
        "tool,args",
        [
            ("list_directory", {"workdir": "test", "path": "missing-dir"}),
            ("read_text_file", {"workdir": "test", "path": "missing.txt"}),
            ("stat_file", {"workdir": "test", "path": "missing.txt"}),
        ],
    )
    def test_error_messages_never_leak_container_paths(self, server, workdir, tool, args) -> None:
        msg = call_error(server, tool, args)
        assert "/workdirs" not in msg
        assert str(workdir.container_path.parent) not in msg

    def test_list_workdirs_exposes_no_slots_or_paths(self, server, workdir) -> None:
        data = call_success(server, "list_workdirs", {})
        assert "workdirs" in data
        for wd in data["workdirs"]:
            assert set(wd.keys()) <= {"alias", "description"}
            assert "/workdirs" not in str(wd)

    def test_happy_path_reads_via_tool(self, server, workdir) -> None:
        (workdir.container_path / "ok.txt").write_text("hello world\n")
        data = call_success(server, "read_text_file", {"workdir": "test", "path": "ok.txt"})
        assert data["content"] == "hello world\n"
        assert "/workdirs" not in str(data)


class TestSpecialFilesViaTool:
    def test_fifo_never_blocks(self, server, workdir) -> None:
        os.mkfifo(workdir.container_path / "apipe")
        msg = call_error(server, "read_text_file", {"workdir": "test", "path": "apipe"})
        assert "UNSUPPORTED_FILE_TYPE" in msg

    def test_unix_socket_never_blocks(self, server, workdir) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(str(workdir.container_path / "asock"))
        s.close()
        msg = call_error(server, "read_text_file", {"workdir": "test", "path": "asock"})
        assert "UNSUPPORTED_FILE_TYPE" in msg
