r"""Windows MCP E2E over the native read kernel (Phase B acceptance).

Every case exercises the published MCP surface (server.call_tool /
read_resource) against the real Rust kernel on real NTFS: the product
layer, policy layer, pagination, limits and audit mapping are the same
code Linux runs — only the backend session differs.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest

from helpers import call_error, call_success, error_code
from serverfs_mcp.config import Settings
from serverfs_mcp.workdirs import Workdir

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows native kernel")

pytest.importorskip(
    "serverfs_windows_native", reason="serverfs-windows-native wheel/pyd not installed"
)

from serverfs_mcp.main import create_server  # noqa: E402
from serverfs_mcp.workdirs import WorkdirRegistry  # noqa: E402


@pytest.fixture()
def wd_root(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    return root


@pytest.fixture()
def server(wd_root):
    from serverfs_mcp.workdirs import EffectiveWorkdirPolicy

    wd = Workdir(
        "test",
        wd_root,
        None,
        read_only=True,
        policy=EffectiveWorkdirPolicy(binary_transfer_enabled=True),
    )
    return create_server(Settings(binary_transfer_enabled=True), WorkdirRegistry([wd]))


def make_junction(link, target) -> bool:
    status = (
        subprocess.run(
            ["cmd", "/C", "mklink", "/J", str(link), str(target)],
            capture_output=True,
        ).returncode
        == 0
    )
    return status


class TestStatE2E:
    def test_stat_file_fields(self, server, wd_root) -> None:
        (wd_root / "a.txt").write_bytes(b"hello\n")
        result = call_success(server, "stat_file", {"workdir": "test", "path": "a.txt"})
        assert result["type"] == "file"
        assert result["size"] == 6
        assert result["revision"].startswith("v1:")
        assert result["modified_at"].endswith("Z")

    def test_stat_directory_and_root(self, server, wd_root) -> None:
        (wd_root / "sub").mkdir()
        result = call_success(server, "stat_file", {"workdir": "test", "path": "sub"})
        assert result["type"] == "directory"
        assert "size" not in result or result["size"] is None
        root = call_success(server, "stat_file", {"workdir": "test", "path": ""})
        assert root["type"] == "directory"

    def test_stat_missing_and_denied(self, server, wd_root) -> None:
        assert (
            error_code(call_error(server, "stat_file", {"workdir": "test", "path": "nope.txt"}))
            == "PATH_NOT_FOUND"
        )
        (wd_root / "server.pem").write_bytes(b"key\n")
        assert (
            error_code(call_error(server, "stat_file", {"workdir": "test", "path": "server.pem"}))
            == "DENIED_PATH"
        )
        # the hidden axis is independent and wins the code on a dot-file
        (wd_root / ".env").write_bytes(b"SECRET=1\n")
        assert (
            error_code(call_error(server, "stat_file", {"workdir": "test", "path": ".env"}))
            == "HIDDEN_PATH_NOT_ALLOWED"
        )

    def test_stat_case_variant_credential_denied(self, server, wd_root) -> None:
        # the Windows name-comparison axis: SECRET.PEM names the same kind
        # of credential object as secret.pem and must hit the same rule
        (wd_root / "SECRET.PEM").write_bytes(b"x\n")
        assert (
            error_code(call_error(server, "stat_file", {"workdir": "test", "path": "SECRET.PEM"}))
            == "DENIED_PATH"
        )

    def test_stat_reparse_reports_type_not_error(self, server, wd_root) -> None:
        (wd_root / "real").mkdir()
        if not make_junction(wd_root / "jlink", wd_root / "real"):
            pytest.fail("junction creation with mklink /J failed on this host")
        result = call_success(server, "stat_file", {"workdir": "test", "path": "jlink"})
        assert result["type"] == "reparse_point"

    def test_stat_unicode_and_long_component(self, server, wd_root) -> None:
        (wd_root / "文档.txt").write_bytes(b"ok\n")
        big = "x" * 200 + ".txt"
        (wd_root / big).write_bytes(b"ok\n")
        for path in ("文档.txt", big):
            result = call_success(server, "stat_file", {"workdir": "test", "path": path})
            assert result["type"] == "file", path


class TestListE2E:
    def _seed(self, wd_root) -> None:
        (wd_root / "b.txt").write_bytes(b"b\n")
        (wd_root / "a.txt").write_bytes(b"a\n")
        (wd_root / "sub").mkdir()
        (wd_root / ".hidden").write_bytes(b"h\n")
        (wd_root / ".env").write_bytes(b"x\n")
        (wd_root / ".serverfs-tmp-race").write_bytes(b"x\n")

    def test_list_sorted_filtered(self, server, wd_root) -> None:
        self._seed(wd_root)
        result = call_success(server, "list_directory", {"workdir": "test", "path": ""})
        names = [e["name"] for e in result["entries"]]
        assert names == ["a.txt", "b.txt", "sub"]
        assert result["returned"] == 3

    def test_list_pagination_has_more(self, server, wd_root) -> None:
        self._seed(wd_root)
        result = call_success(server, "list_directory", {"workdir": "test", "path": "", "limit": 2})
        assert [e["name"] for e in result["entries"]] == ["a.txt", "b.txt"]
        assert result["has_more"] is True

    def test_list_shows_reparse_as_type(self, server, wd_root) -> None:
        (wd_root / "real").mkdir()
        (wd_root / "real").joinpath("inner.txt").write_bytes(b"i\n")
        if not make_junction(wd_root / "jlink", wd_root / "real"):
            pytest.fail("junction creation with mklink /J failed on this host")
        result = call_success(server, "list_directory", {"workdir": "test", "path": ""})
        types = {e["name"]: e["type"] for e in result["entries"]}
        assert types.get("jlink") == "reparse_point"

    def test_list_subdirectory_path_syntax(self, server, wd_root) -> None:
        (wd_root / "sub").mkdir()
        (wd_root / "sub" / "c.txt").write_bytes(b"c\n")
        result = call_success(server, "list_directory", {"workdir": "test", "path": "sub"})
        assert result["entries"][0]["path"] == "sub/c.txt"

    def test_validate_channel_errors_on_list(self, server, wd_root) -> None:
        assert (
            error_code(call_error(server, "list_directory", {"workdir": "test", "path": "gone"}))
            == "PATH_NOT_FOUND"
        )
        (wd_root / "f.txt").write_bytes(b"f\n")
        assert (
            error_code(call_error(server, "list_directory", {"workdir": "test", "path": "f.txt"}))
            == "NOT_A_DIRECTORY"
        )


class TestReadE2E:
    def test_read_text_roundtrip_and_revision(self, server, wd_root) -> None:
        (wd_root / "a.txt").write_bytes(b"l1\nl2\nl3\n")
        result = call_success(server, "read_text_file", {"workdir": "test", "path": "a.txt"})
        assert result["content"] == "l1\nl2\nl3\n"
        assert result["revision"].startswith("v1:")
        assert result["end_line"] == 3

    def test_read_pagination(self, server, wd_root) -> None:
        (wd_root / "many.txt").write_bytes(b"".join(f"{i}\n".encode() for i in range(1, 31)))
        page = call_success(
            server, "read_text_file", {"workdir": "test", "path": "many.txt", "max_lines": 5}
        )
        assert page["content"] == "1\n2\n3\n4\n5\n"
        assert page["has_more"] is True
        page2 = call_success(
            server,
            "read_text_file",
            {"workdir": "test", "path": "many.txt", "start_line": 6, "max_lines": 5},
        )
        assert page2["content"] == "6\n7\n8\n9\n10\n"

    def test_read_bom_and_binary_detection(self, server, wd_root) -> None:
        (wd_root / "bom.txt").write_bytes(b"\xef\xbb\xbfhead\n")
        result = call_success(server, "read_text_file", {"workdir": "test", "path": "bom.txt"})
        assert result["content"] == "head\n"
        (wd_root / "bin.dat").write_bytes(b"a\x00b\n")
        assert (
            error_code(call_error(server, "read_text_file", {"workdir": "test", "path": "bin.dat"}))
            == "BINARY_FILE"
        )

    def test_read_line_too_large(self, server, wd_root) -> None:
        (wd_root / "wide.txt").write_bytes(b"x" * (Settings().max_read_bytes + 10) + b"\n")
        assert (
            error_code(
                call_error(server, "read_text_file", {"workdir": "test", "path": "wide.txt"})
            )
            == "LINE_TOO_LARGE"
        )

    def test_read_directory_refused(self, server, wd_root) -> None:
        (wd_root / "d").mkdir()
        assert (
            error_code(call_error(server, "read_text_file", {"workdir": "test", "path": "d"}))
            == "NOT_A_FILE"
        )

    def test_resource_read_through_session(self, server, wd_root) -> None:
        (wd_root / "res.txt").write_bytes(b"resource body\n")

        async def _read():
            contents = await server.read_resource("serverfs://test/res.txt")
            return contents

        result = asyncio.run(_read())
        text = result[0].content if not isinstance(result, list) else result[0].content
        assert "resource body" in text


class TestBinaryDownloadE2E:
    def test_download_exact_bytes_and_sha(self, server, wd_root) -> None:
        payload = bytes(range(256)) * 40
        (wd_root / "blob.bin").write_bytes(payload)
        import hashlib

        result = call_success(
            server, "download_binary_file", {"workdir": "test", "path": "blob.bin"}
        )
        assert result["size"] == len(payload)
        assert result["sha256"] == hashlib.sha256(payload).hexdigest()
        assert result["revision"].startswith("v1:")

    def test_download_over_limit(self, server, wd_root) -> None:
        (wd_root / "big.bin").write_bytes(b"z" * (Settings().max_binary_transfer_bytes + 1))
        assert (
            error_code(
                call_error(server, "download_binary_file", {"workdir": "test", "path": "big.bin"})
            )
            == "BINARY_FILE_TOO_LARGE"
        )

    def test_download_empty_file(self, server, wd_root) -> None:
        (wd_root / "empty.bin").write_bytes(b"")
        result = call_success(
            server, "download_binary_file", {"workdir": "test", "path": "empty.bin"}
        )
        assert result["size"] == 0


class TestRootRenameRetention:
    def test_channels_keep_working_after_root_renamed(self, wd_root) -> None:
        # the acceptance the review demanded beyond object_token: after the
        # configured root path is renamed by the host, every channel still
        # serves the SAME retained root handle, not a reopened path
        (wd_root / "a.txt").write_bytes(b"keep\n")
        server = create_server(Settings(), WorkdirRegistry([Workdir("test", wd_root, None)]))
        before = call_success(server, "stat_file", {"workdir": "test", "path": "a.txt"})
        moved = wd_root.parent / (wd_root.name + "-moved")
        wd_root.rename(moved)
        try:
            listed = call_success(server, "list_directory", {"workdir": "test", "path": ""})
            assert [e["name"] for e in listed["entries"]] == ["a.txt"]
            read = call_success(server, "read_text_file", {"workdir": "test", "path": "a.txt"})
            assert read["content"] == "keep\n"
            after = call_success(server, "stat_file", {"workdir": "test", "path": "a.txt"})
            assert after["revision"] == before["revision"]
        finally:
            moved.rename(wd_root)


@pytest.fixture(autouse=True)
def _isolate_backend_singleton():
    # the WindowsBackend singleton caches sessions by (alias, root, mode);
    # tests must not inherit each other's retained roots
    from serverfs_mcp import windows_backend

    windows_backend.WindowsBackend._shared = None
    yield
    windows_backend.WindowsBackend._shared = None
