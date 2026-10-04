"""Tests for the backend session seam (v0.10 Phase A closure).

These tests prove the product layer can operate through the
``FilesystemBackend``/``WorkdirSession`` contract WITHOUT importing fdio:

- a fake backend implementing the session contract drives the full MCP
  tool surface (read/stat/list/find/search/mutations), showing tools.py
  has no hidden Linux dependency;
- a module-import scan asserts the product layer (tools.py, main.py) no
  longer imports fdio or platform primitives directly;
- revision ownership: the product layer receives and compares opaque
  tokens produced by the backend, never stat objects.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from platform_contract import LINUX
from serverfs_mcp.backends import BackendError, WorkdirSession, get_backend
from serverfs_mcp.config import Settings
from serverfs_mcp.main import create_server
from serverfs_mcp.models import (
    CreateDirectoryResult,
    CreateTextFileResult,
    DeleteDirectoryResult,
    DeleteFileResult,
    EntryInfo,
    StatFileResult,
    TextMatch,
)
from serverfs_mcp.workdirs import EffectiveWorkdirPolicy, Workdir, WorkdirRegistry

if TYPE_CHECKING:
    from serverfs_mcp.paths import ResolvedPath


class FakeSession:
    """In-memory session implementing the WorkdirSession contract.

    Records every call so the tests can assert what the product layer
    actually asked for. Emits BackendError for paths starting with "boom".
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.stat_results: dict[str, StatFileResult] = {}

    # ---- helpers for the test ----

    def seed_stat(self, path: str, result: StatFileResult) -> None:
        self.stat_results[path] = result

    def _maybe_boom(self, resolved: ResolvedPath) -> None:
        if resolved.rel_path.startswith("boom"):
            raise BackendError("PATH_NOT_FOUND", "boom path is absent by contract")

    # ---- read channels ----

    def stat(self, resolved: ResolvedPath) -> StatFileResult:
        self.calls.append(f"stat:{resolved.rel_path}")
        self._maybe_boom(resolved)
        if resolved.rel_path in self.stat_results:
            return self.stat_results[resolved.rel_path]
        return StatFileResult(
            workdir=resolved.workdir.alias,
            path=resolved.rel_path,
            type="file",
            size=1,
            modified_at="2026-10-01T00:00:00Z",
            mime_type="text/plain",
            revision="v1:fake000000000000",
        )

    def list(self, resolved: ResolvedPath, *, offset: int, limit: int):
        self.calls.append(f"list:{resolved.rel_path}:{offset}:{limit}")
        self._maybe_boom(resolved)
        entries = [
            EntryInfo(name="a.txt", path="a.txt", type="file", size=1),
        ]
        return entries[offset : offset + limit], False

    def validate_directory(self, resolved: ResolvedPath) -> None:
        self.calls.append(f"validate:{resolved.rel_path}")
        self._maybe_boom(resolved)

    def find(self, resolved: ResolvedPath, *, pattern: str, limit: int, max_walk_entries: int):
        self.calls.append(f"find:{resolved.rel_path}:{pattern}")
        self._maybe_boom(resolved)
        return ["a.txt"], False

    def search(
        self,
        resolved: ResolvedPath,
        *,
        query: str,
        glob,
        case_sensitive: bool,
        limit: int,
        timeout_seconds: float,
        max_file_bytes: int,
    ):
        self.calls.append(f"search:{resolved.rel_path}:{query}")
        self._maybe_boom(resolved)
        return [TextMatch(path="a.txt", line=1, text="NEEDLE")], False

    def read_text_page(
        self,
        resolved: ResolvedPath,
        *,
        start_line: int,
        max_lines: int,
        max_read_bytes: int,
        binary_sample: int,
    ):
        self.calls.append(f"read:{resolved.rel_path}:{start_line}:{max_lines}")
        self._maybe_boom(resolved)

        class _Page:
            revision = "v1:fake000000000000"
            lines = [b"content\n"]
            bytes_returned = 8
            end_line = start_line
            has_more = False
            has_nul = False
            has_bom = False

        return _Page()

    def read_binary(self, resolved: ResolvedPath, *, max_bytes: int):
        self.calls.append(f"read_binary:{resolved.rel_path}")
        self._maybe_boom(resolved)

        class _Binary:
            data = b"x"
            size = 1
            mime_type = "application/octet-stream"
            sha256 = "0" * 64
            revision = "v1:fake000000000000"

        return _Binary()

    # ---- mutation channels ----

    def create_file(
        self, resolved: ResolvedPath, content: str, *, max_write_bytes: int
    ) -> CreateTextFileResult:
        self.calls.append(f"create_file:{resolved.rel_path}")
        self._maybe_boom(resolved)
        return CreateTextFileResult(
            workdir=resolved.workdir.alias,
            path=resolved.rel_path,
            created=True,
            bytes_written=len(content),
            revision="v1:fake000000000000",
        )

    def create_binary_file(self, resolved: ResolvedPath, data: bytes, *, max_binary_bytes: int):
        from serverfs_mcp.models import UploadBinaryFileResult

        self.calls.append(f"create_binary_file:{resolved.rel_path}")
        self._maybe_boom(resolved)
        return UploadBinaryFileResult(
            workdir=resolved.workdir.alias,
            path=resolved.rel_path,
            created=True,
            bytes_written=len(data),
            sha256="0" * 64,
            revision="v1:fake000000000000",
        )

    def replace_binary_file(
        self,
        resolved: ResolvedPath,
        data: bytes,
        expected_revision: str,
        *,
        max_binary_bytes: int,
    ):
        from serverfs_mcp.models import UploadBinaryFileResult

        self.calls.append(f"replace_binary_file:{resolved.rel_path}")
        self._maybe_boom(resolved)
        return UploadBinaryFileResult(
            workdir=resolved.workdir.alias,
            path=resolved.rel_path,
            created=False,
            replaced=True,
            bytes_written=len(data),
            sha256="0" * 64,
            revision_before=expected_revision,
            revision="v1:fake000000000000",
        )

    def replace_file(
        self,
        resolved: ResolvedPath,
        expected_revision: str,
        edits,
        *,
        max_write_bytes: int,
        max_edits_per_call: int,
    ):
        from serverfs_mcp.models import EditTextFileResult

        self.calls.append(f"replace_file:{resolved.rel_path}")
        self._maybe_boom(resolved)
        return EditTextFileResult(
            workdir=resolved.workdir.alias,
            path=resolved.rel_path,
            edited=True,
            edits_applied=1,
            bytes_before=1,
            bytes_after=1,
            revision_before=expected_revision,
            revision="v1:fake000000000001",
        )

    def delete_file(self, resolved: ResolvedPath, expected_revision: str) -> DeleteFileResult:
        self.calls.append(f"delete_file:{resolved.rel_path}")
        self._maybe_boom(resolved)
        return DeleteFileResult(
            workdir=resolved.workdir.alias,
            path=resolved.rel_path,
            deleted=True,
            bytes_deleted=1,
            revision_deleted="v1:fake000000000000",
        )

    def create_directory(self, resolved: ResolvedPath) -> CreateDirectoryResult:
        self.calls.append(f"create_directory:{resolved.rel_path}")
        self._maybe_boom(resolved)
        return CreateDirectoryResult(
            workdir=resolved.workdir.alias,
            path=resolved.rel_path,
            created=True,
            revision="v1:fake000000000000",
        )

    def delete_directory(
        self, resolved: ResolvedPath, expected_revision: str
    ) -> DeleteDirectoryResult:
        self.calls.append(f"delete_directory:{resolved.rel_path}")
        self._maybe_boom(resolved)
        return DeleteDirectoryResult(
            workdir=resolved.workdir.alias,
            path=resolved.rel_path,
            deleted=True,
            revision_deleted="v1:fake000000000000",
        )


class FakeBackend:
    """FilesystemBackend implementation returning one shared FakeSession."""

    def __init__(self) -> None:
        self.session = FakeSession()

    def open_session(self, workdir: Workdir) -> FakeSession:
        return self.session


@pytest.fixture()
def fake_seam(monkeypatch, tmp_path: Path):
    """A server whose product layer runs entirely against the fake backend."""
    from helpers import call_error, call_success, error_code

    backend = FakeBackend()
    monkeypatch.setattr("serverfs_mcp.tools.get_backend", lambda: backend)

    wd = Workdir(
        "test",
        tmp_path / "root",
        None,
        read_only=False,
        policy=EffectiveWorkdirPolicy(binary_transfer_enabled=True),
    )
    server = create_server(Settings(), WorkdirRegistry([wd]))
    return backend.session, server, (call_success, call_error, error_code)


class TestFakeBackendDrivesTools:
    """The MCP surface works with a non-Linux backend and no fdio."""

    def test_read_text_file_through_fake_session(self, fake_seam) -> None:
        session, server, (call_success, _, _) = fake_seam
        result = call_success(server, "read_text_file", {"workdir": "test", "path": "a.txt"})
        assert result["content"] == "content\n"
        assert result["revision"].startswith("v1:")
        assert "read:a.txt:1:200" in session.calls

    def test_stat_through_fake_session(self, fake_seam) -> None:
        session, server, (call_success, _, _) = fake_seam
        result = call_success(server, "stat_file", {"workdir": "test", "path": "a.txt"})
        assert result["type"] == "file"
        assert "stat:a.txt" in session.calls

    def test_list_and_find_through_fake_session(self, fake_seam) -> None:
        session, server, (call_success, _, _) = fake_seam
        result = call_success(
            server, "list_directory", {"workdir": "test", "path": "", "limit": 10}
        )
        assert result["entries"][0]["name"] == "a.txt"
        found = call_success(server, "find_files", {"workdir": "test", "pattern": "*.txt"})
        assert found["matches"][0]["path"] == "a.txt"
        assert "find::*.txt" in session.calls

    def test_search_through_fake_session(self, fake_seam) -> None:
        session, server, (call_success, _, _) = fake_seam
        result = call_success(server, "search_text", {"workdir": "test", "query": "NEEDLE"})
        assert result["matches"][0]["text"] == "NEEDLE"
        assert "search::NEEDLE" in session.calls

    def test_mutation_channels_through_fake_session(self, fake_seam) -> None:
        session, server, (call_success, _, _) = fake_seam
        created = call_success(
            server,
            "create_text_file",
            {"workdir": "test", "path": "new.txt", "content": "hi\n"},
        )
        assert created["created"] is True
        edited = call_success(
            server,
            "edit_text_file",
            {
                "workdir": "test",
                "path": "new.txt",
                "expected_revision": "v1:fake000000000000",
                "edits": [{"old_text": "hi", "new_text": "hello", "expected_count": 1}],
            },
        )
        assert edited["edited"] is True
        deleted = call_success(
            server,
            "delete_file",
            {"workdir": "test", "path": "new.txt", "expected_revision": "v1:fake000000000001"},
        )
        assert deleted["deleted"] is True
        mkdir = call_success(server, "create_directory", {"workdir": "test", "path": "sub"})
        assert mkdir["created"] is True
        rmdir = call_success(
            server,
            "delete_directory",
            {"workdir": "test", "path": "sub", "expected_revision": "v1:fake000000000000"},
        )
        assert rmdir["deleted"] is True
        # every mutation channel went through the session
        assert "create_file:new.txt" in session.calls
        assert "replace_file:new.txt" in session.calls
        assert "delete_file:new.txt" in session.calls
        assert "create_directory:sub" in session.calls
        assert "delete_directory:sub" in session.calls

    def test_binary_upload_download_through_fake_session(self, fake_seam) -> None:
        import base64

        session, server, (call_success, _, _) = fake_seam
        uploaded = call_success(
            server,
            "upload_binary_file",
            {
                "workdir": "test",
                "path": "blob.bin",
                "data_base64": base64.b64encode(b"x").decode(),
            },
        )
        assert uploaded["created"] is True
        assert "create_binary_file:blob.bin" in session.calls
        downloaded = call_success(
            server, "download_binary_file", {"workdir": "test", "path": "blob.bin"}
        )
        assert downloaded["size"] == 1
        assert "read_binary:blob.bin" in session.calls

    def test_backend_error_maps_to_coded_tool_error(self, fake_seam) -> None:
        _, server, (_, call_error, error_code) = fake_seam
        msg = call_error(server, "stat_file", {"workdir": "test", "path": "boom.txt"})
        assert error_code(msg) == "PATH_NOT_FOUND"


class TestProductLayerPurity:
    """The product layer must not import platform primitives directly."""

    @pytest.mark.parametrize(
        ("module", "forbidden"),
        [
            ("serverfs_mcp.tools", ("serverfs_mcp.fdio",)),
            ("serverfs_mcp.main", ("serverfs_mcp.fdio",)),
        ],
    )
    def test_no_fdio_import_in_product_layer(self, module: str, forbidden: tuple) -> None:
        import importlib

        mod = importlib.import_module(module)
        source_file = Path(mod.__file__)
        text = source_file.read_text(encoding="utf-8")
        for name in forbidden:
            assert f"from {name} import" not in text, f"{module} imports {name}"
            assert f"import {name}" not in text, f"{module} imports {name}"

    def test_tools_has_no_os_fstat_or_openat(self) -> None:
        """No raw platform syscalls in the product layer source."""
        from serverfs_mcp import tools

        text = Path(tools.__file__).read_text(encoding="utf-8")
        for banned in ("os.fstat", "os.open", "dir_fd", "/proc/self/fd", "fcntl"):
            assert banned not in text, f"tools.py uses platform primitive: {banned}"


@pytest.mark.skipif(not LINUX, reason="Linux kernel contract: the POSIX backend implementation")
class TestLinuxSessionContract:
    """The real Linux backend satisfies the same contract shape."""

    def test_open_session_returns_session(self, tmp_path: Path) -> None:
        from serverfs_mcp.linux_backend import LinuxBackend

        backend = get_backend()
        assert isinstance(backend, LinuxBackend)
        wd = Workdir("test", tmp_path, None)
        session = backend.open_session(wd)
        for method in (
            "stat",
            "list",
            "find",
            "search",
            "read_text_page",
            "read_binary",
            "validate_directory",
            "create_file",
            "create_binary_file",
            "replace_binary_file",
            "replace_file",
            "delete_file",
            "create_directory",
            "delete_directory",
        ):
            assert hasattr(session, method), f"Linux session lacks {method}"


class TestBackendErrorContract:
    """The coded-error contract holds on every platform."""

    def test_backend_error_is_coded(self) -> None:
        exc = BackendError("SOME_CODE", "plain words")
        assert exc.code == "SOME_CODE"
        assert exc.message == "plain words"
        # The code travels structurally (code attribute), not inside the
        # message: the tool layer formats CODE: message itself.
        assert str(exc) == "plain words"


def _protocol_methods() -> set[str]:
    """Public method names declared by the WorkdirSession Protocol."""
    return {
        name
        for name, member in vars(WorkdirSession).items()
        if callable(member) and not name.startswith("_")
    }


class TestProtocolCompleteness:
    """The declared Protocol is the full spec a new kernel implements."""

    def test_binary_mutations_are_declared(self) -> None:
        methods = _protocol_methods()
        assert "create_binary_file" in methods
        assert "replace_binary_file" in methods

    def test_every_protocol_method_has_a_return_annotation(self) -> None:
        # raw __annotations__ (strings under future-annotations): the check is
        # that the contract states the type, not that it resolves here
        missing = [
            name
            for name in _protocol_methods()
            if getattr(WorkdirSession, name).__annotations__.get("return") is None
        ]
        assert not missing, f"unannotated session methods: {missing}"

    @pytest.mark.skipif(not LINUX, reason="Linux kernel contract: the POSIX backend implementation")
    def test_linux_session_covers_the_protocol(self) -> None:
        from serverfs_mcp.linux_backend import LinuxWorkdirSession

        missing = [
            name
            for name in _protocol_methods()
            if not callable(getattr(LinuxWorkdirSession, name, None))
        ]
        assert not missing, f"LinuxWorkdirSession lacks protocol members: {missing}"

    def test_fake_session_covers_the_protocol(self) -> None:
        missing = [
            name for name in _protocol_methods() if not callable(getattr(FakeSession, name, None))
        ]
        assert not missing, f"FakeSession lacks protocol members: {missing}"
