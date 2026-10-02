r"""Windows MCP E2E over the native read kernel (Phase B acceptance).

Every case exercises the published MCP surface (server.call_tool /
read_resource) against the real Rust kernel on real NTFS: the product
layer, policy layer, pagination, limits and audit mapping are the same
code Linux runs — only the backend session differs.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import subprocess
import sys
import threading

import pytest

from helpers import call_concurrently, call_error, call_success, error_code
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


@pytest.fixture()
def rw_server(wd_root):
    from serverfs_mcp.workdirs import EffectiveWorkdirPolicy

    wd = Workdir(
        "test",
        wd_root,
        None,
        read_only=False,
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


class TestCreateOnlyMutationsE2E:
    def test_text_empty_unicode_directory_and_revision(self, rw_server, wd_root) -> None:
        text = call_success(
            rw_server,
            "create_text_file",
            {"workdir": "test", "path": "文档.txt", "content": "hello 🐈\n"},
        )
        assert text["created"] is True
        assert text["bytes_written"] == len("hello 🐈\n".encode())
        assert (wd_root / "文档.txt").read_bytes() == "hello 🐈\n".encode()
        assert (
            call_success(
                rw_server,
                "stat_file",
                {"workdir": "test", "path": "文档.txt"},
            )["revision"]
            == text["revision"]
        )

        empty = call_success(
            rw_server,
            "create_text_file",
            {"workdir": "test", "path": "empty.txt", "content": ""},
        )
        assert empty["bytes_written"] == 0
        directory = call_success(
            rw_server, "create_directory", {"workdir": "test", "path": "new-dir"}
        )
        assert directory["created"] is True
        assert (
            directory["revision"]
            == call_success(rw_server, "stat_file", {"workdir": "test", "path": "new-dir"})[
                "revision"
            ]
        )

    def test_binary_create_exact_bytes_and_sha(self, rw_server, wd_root) -> None:
        payload = b"\x00\xffexact\x00bytes"
        result = call_success(
            rw_server,
            "upload_binary_file",
            {
                "workdir": "test",
                "path": "payload.bin",
                "data_base64": base64.b64encode(payload).decode("ascii"),
            },
        )
        assert (wd_root / "payload.bin").read_bytes() == payload
        assert result["created"] is True and result["replaced"] is False
        assert result["revision_before"] is None
        assert result["sha256"] == hashlib.sha256(payload).hexdigest()
        assert (
            result["revision"]
            == call_success(rw_server, "stat_file", {"workdir": "test", "path": "payload.bin"})[
                "revision"
            ]
        )

    def test_create_collision_and_missing_parent(self, rw_server, wd_root) -> None:
        (wd_root / "occupied").write_bytes(b"original")
        assert (
            error_code(
                call_error(
                    rw_server,
                    "create_text_file",
                    {"workdir": "test", "path": "occupied", "content": "changed"},
                )
            )
            == "PATH_ALREADY_EXISTS"
        )
        assert (wd_root / "occupied").read_bytes() == b"original"
        assert (
            error_code(
                call_error(
                    rw_server,
                    "create_text_file",
                    {"workdir": "test", "path": "absent/file", "content": "x"},
                )
            )
            == "PARENT_NOT_FOUND"
        )
        assert not list(wd_root.glob(".serverfs-tmp-*"))

    def test_directory_and_reparse_collisions_and_junction_parent(self, rw_server, wd_root) -> None:
        (wd_root / "occupied-dir").mkdir()
        assert (
            error_code(
                call_error(
                    rw_server,
                    "create_directory",
                    {"workdir": "test", "path": "occupied-dir"},
                )
            )
            == "PATH_ALREADY_EXISTS"
        )
        outside = wd_root.parent / "outside"
        outside.mkdir()
        if not make_junction(wd_root / "leaf-link", outside):
            pytest.fail("junction creation with mklink /J failed on this host")
        assert (
            error_code(
                call_error(
                    rw_server,
                    "create_text_file",
                    {"workdir": "test", "path": "leaf-link", "content": "must-not-write"},
                )
            )
            == "PATH_ALREADY_EXISTS"
        )
        if not make_junction(wd_root / "parent-link", outside):
            pytest.fail("junction creation with mklink /J failed on this host")
        assert (
            error_code(
                call_error(
                    rw_server,
                    "create_text_file",
                    {"workdir": "test", "path": "parent-link/escaped.txt", "content": "x"},
                )
            )
            == "REPARSE_POINT_NOT_ALLOWED"
        )
        assert not (outside / "escaped.txt").exists()
        assert not list(wd_root.glob(".serverfs-tmp-*"))

    def test_retained_root_handle_survives_external_rename(self, rw_server, wd_root) -> None:
        call_success(rw_server, "stat_file", {"workdir": "test", "path": ""})
        moved = wd_root.with_name(wd_root.name + "-moved")
        wd_root.rename(moved)
        try:
            result = call_success(
                rw_server,
                "create_text_file",
                {"workdir": "test", "path": "after-rename.txt", "content": "ok"},
            )
            assert (moved / "after-rename.txt").read_bytes() == b"ok"
            assert (
                result["revision"]
                == call_success(
                    rw_server,
                    "stat_file",
                    {"workdir": "test", "path": "after-rename.txt"},
                )["revision"]
            )
        finally:
            moved.rename(wd_root)

    def test_read_only_tool_layer_and_pending_mutations(self, server, rw_server, wd_root) -> None:
        assert (
            error_code(
                call_error(
                    server,
                    "create_text_file",
                    {"workdir": "test", "path": "blocked.txt", "content": "x"},
                )
            )
            == "WORKDIR_READ_ONLY"
        )
        assert not (wd_root / "blocked.txt").exists()
        for tool, args in (
            (
                "edit_text_file",
                {
                    "expected_revision": "v1:0000000000000000",
                    "edits": [{"old_text": "x", "new_text": "y"}],
                },
            ),
            ("delete_file", {"expected_revision": "v1:0000000000000000"}),
            ("delete_directory", {"expected_revision": "v1:0000000000000000"}),
        ):
            result = call_error(rw_server, tool, {"workdir": "test", "path": "missing", **args})
            assert error_code(result) == "WINDOWS_KERNEL_PENDING"

    def test_concurrent_creators_publish_one_complete_payload(self, rw_server, wd_root) -> None:
        payloads = ["A" * 300_000, "B" * 300_000]

        outcomes = call_concurrently(
            rw_server,
            [
                (
                    "create_text_file",
                    {"workdir": "test", "path": "race.txt", "content": content},
                )
                for content in payloads
            ],
        )
        successes = [payload for kind, payload in outcomes if kind == "ok"]
        failures = [payload for kind, payload in outcomes if kind == "err"]
        assert len(successes) == 1
        assert len(failures) == 1 and error_code(failures[0]) == "PATH_ALREADY_EXISTS"
        final = (wd_root / "race.txt").read_text(encoding="utf-8")
        assert final in payloads
        assert not list(wd_root.glob(".serverfs-tmp-*"))

    def test_readers_never_observe_partial_final_content(self, rw_server, wd_root) -> None:
        payload = "complete-payload\n" * 40_000
        finished = threading.Event()
        observations: list[tuple[str, bool, int, str, str]] = []

        def observe() -> tuple[str, bool, int, str, str]:
            page = call_success(
                rw_server,
                "read_text_file",
                {"workdir": "test", "path": "atomic.txt"},
            )
            stat = call_success(rw_server, "stat_file", {"workdir": "test", "path": "atomic.txt"})
            return (
                page["content"],
                page["has_more"],
                stat["size"],
                page["revision"],
                stat["revision"],
            )

        def read_until_done() -> None:
            while not finished.is_set():
                try:
                    observations.append(observe())
                except Exception as exc:
                    # Absence before the atomic rename is allowed.
                    assert error_code(str(exc)) == "PATH_NOT_FOUND"

        reader = threading.Thread(target=read_until_done)
        reader.start()
        try:
            call_success(
                rw_server,
                "create_text_file",
                {"workdir": "test", "path": "atomic.txt", "content": payload},
            )
        finally:
            finished.set()
            reader.join(timeout=10)
        assert not reader.is_alive()
        # read_text_file is paginated; pair its first page with stat so a
        # visible prefix of the final object cannot pass as a complete file.
        observations.append(observe())
        expected_page = "complete-payload\n" * 200
        assert observations
        assert all(
            content == expected_page
            and has_more
            and size == len(payload.encode("utf-8"))
            and page_revision == stat_revision
            for content, has_more, size, page_revision, stat_revision in observations
        )
        listing = call_success(rw_server, "list_directory", {"workdir": "test", "path": ""})
        assert all(not entry["name"].startswith(".serverfs-tmp-") for entry in listing["entries"])


class TestAcceptanceClosure:
    def test_reparse_error_leaks_neither_status_nor_path(self, server, wd_root) -> None:
        (wd_root / "real").mkdir()
        (wd_root / "real" / "inner.txt").write_bytes(b"i\n")
        if not make_junction(wd_root / "jlink", wd_root / "real"):
            pytest.fail("junction creation with mklink /J failed on this host")
        msg = call_error(server, "read_text_file", {"workdir": "test", "path": "jlink/inner.txt"})
        assert error_code(msg) == "REPARSE_POINT_NOT_ALLOWED"
        assert "0x" not in msg
        assert str(wd_root) not in msg

    def test_same_name_replacement_never_mixes_objects(self, server, wd_root) -> None:
        import threading

        from mcp.server.mcpserver.exceptions import ToolError

        target = wd_root / "swap.txt"
        target.write_bytes(b"short\n")
        stop = threading.Event()

        def mutator():
            while not stop.is_set():
                try:
                    # grow then shrink: every observable size (0, 7, 26)
                    # is a legitimate single-snapshot state
                    target.write_bytes(b"much-longer-content-line\n")
                    target.write_bytes(b"short\n")
                except OSError:
                    pass  # transient sharing violation with a reader; retry

        thread = threading.Thread(target=mutator, daemon=True)
        thread.start()
        unexpected: list[str] = []
        try:
            for _ in range(120):
                try:
                    read = call_success(
                        server, "read_text_file", {"workdir": "test", "path": "swap.txt"}
                    )
                except (AssertionError, ToolError) as exc:
                    # both designed race outcomes are acceptable: the name
                    # can vanish mid-round, and a read straddling an
                    # external rewrite must abort with FILE_CHANGED (the
                    # consistency guarantee itself — CI proved it fires).
                    # call_success surfaces is_error as AssertionError and
                    # a raised tool failure as ToolError; accept both.
                    text = str(exc)
                    if "PATH_NOT_FOUND:" not in text and "FILE_CHANGED_DURING_READ:" not in text:
                        unexpected.append(text)
                    continue
                # every successful read must describe ONE object snapshot.
                # A concurrent write can be observed at any prefix length
                # (0..full), so the invariant is: content is a prefix of
                # one canonical state, and end_line agrees with content —
                # never bytes from one object plus metadata from another
                content = read["content"]
                legal = ("short\n", "much-longer-content-line\n")
                if not any(cand.startswith(content) for cand in legal):
                    unexpected.append(f"mixed read: {read!r}")
                expected_end = 0 if content == "" else 1
                if read["end_line"] != expected_end:
                    unexpected.append(f"inconsistent page: {read!r}")
                stat = call_success(server, "stat_file", {"workdir": "test", "path": "swap.txt"})
                if stat["size"] not in range(0, 27):
                    unexpected.append(f"mixed stat: {stat!r}")
        finally:
            stop.set()
            thread.join(timeout=5)
        assert not unexpected, unexpected[:3]

    def test_enumeration_races_never_escape_or_traceback(self, server, wd_root) -> None:
        import threading

        stop = threading.Event()
        names = [f"racer{i}.txt" for i in range(24)]
        for name in names[:8]:
            (wd_root / name).write_bytes(b"seed\n")

        def churn():
            i = 0
            while not stop.is_set():
                name = names[i % len(names)]
                path = wd_root / name
                moved = wd_root / (name + ".moved")
                try:
                    if path.exists():
                        path.rename(moved)
                    else:
                        moved.write_bytes(b"moved\n")
                        moved.rename(path)
                except OSError:
                    pass  # raced with the reader side; the next round retries
                i += 1

        thread = threading.Thread(target=churn, daemon=True)
        thread.start()
        try:
            for _ in range(60):
                result = call_success(
                    server, "list_directory", {"workdir": "test", "path": "", "limit": 50}
                )
                for entry in result["entries"]:
                    # bounded skip/retry/error but never: separators in
                    # names, reserved leaks, path escape or wrong types
                    assert "\\" not in entry["name"] and "/" not in entry["name"]
                    assert not entry["name"].startswith(".serverfs-tmp")
                    assert entry["type"] in ("file", "directory", "reparse_point")
                    assert not entry["path"].startswith(("\\", "/", ".."))
        finally:
            stop.set()
            thread.join(timeout=5)

    def test_mcp_stress_keeps_handle_count_bounded(self, server, wd_root) -> None:
        import ctypes

        (wd_root / "f.txt").write_bytes(b"payload\n")
        (wd_root / "blob.bin").write_bytes(b"b" * 100)

        def handle_count() -> int:
            kernel32 = ctypes.windll.kernel32
            count = ctypes.c_uint32(0)
            ok = kernel32.GetProcessHandleCount(ctypes.c_void_p(-1), ctypes.byref(count))
            assert ok, ctypes.WinError()
            return count.value

        handle_count()  # warm
        baseline = handle_count()
        for _ in range(300):
            call_success(server, "stat_file", {"workdir": "test", "path": "f.txt"})
            call_success(server, "list_directory", {"workdir": "test", "path": ""})
            call_success(server, "read_text_file", {"workdir": "test", "path": "f.txt"})
            call_success(server, "download_binary_file", {"workdir": "test", "path": "blob.bin"})
            call_error(server, "stat_file", {"workdir": "test", "path": "absent.txt"})
        after = handle_count()
        # per-operation leaf handles must all close; only retained roots
        # persist. margin absorbs runtime/allocator noise on this host
        assert after - baseline <= 64, f"handle drift {baseline} -> {after}"


class TestFindSearchE2E:
    def _seed_tree(self, wd_root) -> None:
        (wd_root / "docs").mkdir()
        (wd_root / "docs" / "a.txt").write_bytes(b"first NEEDLE line\nsecond\nthird NEEDLE too\n")
        (wd_root / "docs" / "b.md").write_bytes(b"NEEDLE in markdown\n")
        (wd_root / "notes.txt").write_bytes(b"needle lowercase\n")
        (wd_root / "skip.py").write_bytes(b"NEEDLE in python\n")
        (wd_root / ".hidden.txt").write_bytes(b"NEEDLE hidden\n")
        (wd_root / ".env").write_bytes(b"NEEDLE=secret\n")
        big = "x" * 200 + ".txt"
        (wd_root / big).write_bytes(b"NEEDLE unicode-name\n")

    def test_find_matches_and_truncation(self, server, wd_root) -> None:
        self._seed_tree(wd_root)
        result = call_success(server, "find_files", {"workdir": "test", "pattern": "*.txt"})
        paths = {m["path"] for m in result["matches"]}
        assert "docs/a.txt" in paths and "notes.txt" in paths
        assert "skip.py" not in paths
        # hidden and denied namespaces never surface
        assert ".hidden.txt" not in paths
        assert all(".env" not in p for p in paths)
        assert result["truncated"] is False

    def test_find_limit_truncated_flag(self, server, wd_root) -> None:
        self._seed_tree(wd_root)
        result = call_success(
            server, "find_files", {"workdir": "test", "pattern": "*.txt", "limit": 1}
        )
        assert result["returned"] == 1
        assert result["truncated"] is True

    def test_find_skips_reparse_dirs(self, server, wd_root) -> None:
        (wd_root / "real").mkdir()
        (wd_root / "real" / "in.txt").write_bytes(b"x\n")
        if not make_junction(wd_root / "jdir", wd_root / "real"):
            pytest.fail("junction creation failed on this host")
        result = call_success(server, "find_files", {"workdir": "test", "pattern": "*"})
        paths = {m["path"] for m in result["matches"]}
        assert "real/in.txt" in paths
        assert not any(p.startswith("jdir/") for p in paths)

    def test_find_root_missing_and_file(self, server, wd_root) -> None:
        assert (
            error_code(
                call_error(
                    server, "find_files", {"workdir": "test", "path": "gone", "pattern": "*"}
                )
            )
            == "PATH_NOT_FOUND"
        )

    def test_search_literal_case_and_glob(self, server, wd_root) -> None:
        self._seed_tree(wd_root)
        result = call_success(server, "search_text", {"workdir": "test", "query": "NEEDLE"})
        matches = result["matches"]
        big = "x" * 200 + ".txt"
        assert {(m["path"], m["line"]) for m in matches} == {
            ("docs/a.txt", 1),
            ("docs/a.txt", 3),
            ("docs/b.md", 1),
            ("skip.py", 1),
            (big, 1),
        }
        globbed = call_success(
            server, "search_text", {"workdir": "test", "query": "NEEDLE", "glob": "*.md"}
        )
        assert [m["path"] for m in globbed["matches"]] == ["docs/b.md"]
        ci = call_success(
            server,
            "search_text",
            {"workdir": "test", "query": "needle", "case_sensitive": False},
        )
        cpaths = {(m["path"], m["line"]) for m in ci["matches"]}
        assert ("notes.txt", 1) in cpaths and ("docs/a.txt", 1) in cpaths

    def test_search_denied_hidden_and_binary_skipped(self, server, wd_root) -> None:
        self._seed_tree(wd_root)
        (wd_root / "bin.dat").write_bytes(b"NEEDLE\x00after\n")
        result = call_success(server, "search_text", {"workdir": "test", "query": "NEEDLE"})
        paths = {m["path"] for m in result["matches"]}
        assert ".env" not in paths and ".hidden.txt" not in paths and "bin.dat" not in paths

    def test_search_limit_early_stop(self, server, wd_root) -> None:
        self._seed_tree(wd_root)
        result = call_success(
            server, "search_text", {"workdir": "test", "query": "NEEDLE", "limit": 2}
        )
        assert result["returned"] == 2
        assert result["truncated"] is True

    def test_search_text_content_preserves_line(self, server, wd_root) -> None:
        (wd_root / "l.txt").write_bytes(b"  spaced NEEDLE  \n")
        result = call_success(server, "search_text", {"workdir": "test", "query": "NEEDLE"})
        assert result["matches"][0]["text"] == "  spaced NEEDLE  "

    def test_search_timeout_code_parity(self, wd_root) -> None:
        # session-level: a strictly past deadline must surface exactly the
        # BackendError code the Linux searcher uses
        from serverfs_mcp.backends import BackendError
        from serverfs_mcp.paths import DenyPolicy, resolve_workdir_path
        from serverfs_mcp.windows_backend import WindowsBackend

        wd = Workdir("test", wd_root, None)
        (wd_root / "a.txt").write_bytes(b"NEEDLE\n")
        session = WindowsBackend().open_session(wd)
        resolved = resolve_workdir_path(wd, "", allow_hidden=False, deny_policy=DenyPolicy())
        with pytest.raises(BackendError) as excinfo:
            session.search(
                resolved,
                query="NEEDLE",
                glob=None,
                case_sensitive=True,
                limit=10,
                # strictly past deadline: Windows time.monotonic() can have
                # ~16ms granularity, so timeout_seconds=0 is only a coin
                # flip; a negative budget makes the first check certain
                timeout_seconds=-1.0,
                max_file_bytes=1 << 20,
            )
        assert excinfo.value.code == "SEARCH_TIMEOUT"

    def test_wide_tree_timeout_terminates_and_recovers(self, wd_root) -> None:
        import time
        from concurrent.futures import ThreadPoolExecutor

        from serverfs_mcp.workdirs import EffectiveWorkdirPolicy

        names = [f"w{i:04d}.txt" for i in range(2500)]
        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(lambda n: (wd_root / n).write_bytes(b"no match here\n"), names))
        # 60 matching files scattered at the end of the walk order
        for i in range(60):
            (wd_root / names[2440 + i]).write_bytes(b"NEEDLE\n")

        server = create_server(
            Settings(search_timeout_seconds=0.05),
            WorkdirRegistry([Workdir("test", wd_root, None, policy=EffectiveWorkdirPolicy())]),
        )
        started = time.monotonic()
        msg = call_error(server, "search_text", {"workdir": "test", "query": "NEEDLE"})
        elapsed = time.monotonic() - started
        assert error_code(msg) == "SEARCH_TIMEOUT"
        # must abort on the deadline, not after finishing the whole tree
        assert elapsed < 5.0, f"deadline overshoot: {elapsed:.2f}s"

        # the session keeps working after a timed-out search
        ok = create_server(
            Settings(),
            WorkdirRegistry([Workdir("test", wd_root, None, policy=EffectiveWorkdirPolicy())]),
        )
        result = call_success(ok, "search_text", {"workdir": "test", "query": "NEEDLE"})
        assert result["returned"] > 0

    def test_find_max_walk_truncated(self, server, wd_root) -> None:
        self._seed_tree(wd_root)
        small_walk = create_server(
            Settings(max_walk_entries=2),
            WorkdirRegistry([Workdir("test", wd_root, None)]),
        )
        result = call_success(small_walk, "find_files", {"workdir": "test", "pattern": "*"})
        assert result["truncated"] is True


class TestRootRenameRetention:
    def test_channels_keep_working_after_root_renamed(self, wd_root) -> None:
        # the acceptance the review demanded beyond object_token: after the
        # configured root path is renamed by the host, every channel still
        # serves the SAME retained root handle, not a reopened path
        from serverfs_mcp.workdirs import EffectiveWorkdirPolicy

        (wd_root / "a.txt").write_bytes(b"keep\n")
        plain = Workdir("test", wd_root, None)
        server = create_server(Settings(), WorkdirRegistry([plain]))
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
            # download + resource through the same cached session: same
            # (alias, root, read_only) key, binary policy from this registry
            binary_server = create_server(
                Settings(binary_transfer_enabled=True),
                WorkdirRegistry(
                    [
                        Workdir(
                            "test",
                            wd_root,
                            None,
                            policy=EffectiveWorkdirPolicy(binary_transfer_enabled=True),
                        )
                    ]
                ),
            )
            downloaded = call_success(
                binary_server, "download_binary_file", {"workdir": "test", "path": "a.txt"}
            )
            assert downloaded["size"] == 5
            assert downloaded["revision"] == before["revision"]

            async def _read_resource():
                return await binary_server.read_resource("serverfs://test/a.txt")

            contents = asyncio.run(_read_resource())
            assert "keep" in contents[0].content
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
