r"""Phase F acceptance: long/deep/Unicode paths through the real MCP surface.

The kernel opens the workdir root in the ``\\?\`` literal namespace and every
request component HANDLE-relatively, so Win32 MAX_PATH parsing never applies
inside a workdir -- this suite is the executed evidence for that claim:
components up to 255 UTF-16 units, whole trees whose absolute paths exceed
260 characters, and the Unicode shapes that expose the documented
casefolding axis (over-deny allowed, under-deny forbidden; NTFS performs no
Unicode normalization, so NFC and NFD names are distinct files).
"""

from __future__ import annotations

import sys

import pytest

from helpers import call_error, call_success, error_code
from serverfs_mcp.config import Settings
from serverfs_mcp.workdirs import EffectiveWorkdirPolicy, Workdir, WorkdirRegistry

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows native kernel")

pytest.importorskip(
    "serverfs_windows_native", reason="serverfs-windows-native wheel/pyd not installed"
)

from serverfs_mcp.main import create_server  # noqa: E402


def _make_server(root, *, read_only: bool):
    wd = Workdir(
        "test",
        root,
        None,
        read_only=read_only,
        policy=EffectiveWorkdirPolicy(binary_transfer_enabled=True),
    )
    return create_server(Settings(binary_transfer_enabled=True), WorkdirRegistry([wd]))


@pytest.fixture()
def ro_root(tmp_path):
    root = tmp_path / "ro"
    root.mkdir()
    return root


@pytest.fixture()
def rw_root(tmp_path):
    root = tmp_path / "rw"
    root.mkdir()
    return root


@pytest.fixture()
def ro_server(ro_root):
    return _make_server(ro_root, read_only=True)


@pytest.fixture()
def rw_server(rw_root):
    return _make_server(rw_root, read_only=False)


def build_deep_tree(root, levels: int, component_len: int) -> tuple[str, int]:
    """Host-side creation (the environment cannot be pre-seeded through MCP
    because create_directory is deliberately non-recursive). Returns the
    slash-joined relative path and its absolute total length."""
    import os

    parts = [f"d{index:02d}-{'x' * component_len}" for index in range(levels)]
    os.makedirs(os.path.join(root, *parts), exist_ok=True)
    relative = "/".join(parts)
    return relative, len(str(root)) + 1 + len(relative) + len("/leaf.txt")


class TestDeepPaths:
    def test_stat_read_edit_roundtrip_beyond_max_path(self, rw_root) -> None:
        server = _make_server(rw_root, read_only=False)
        relative, absolute_len = build_deep_tree(rw_root, levels=24, component_len=120)
        assert absolute_len > 260, absolute_len
        leaf = f"{relative}/leaf.txt"
        created = call_success(
            server,
            "create_text_file",
            {"workdir": "test", "path": leaf, "content": "first line\n"},
        )
        assert created["revision"].startswith("v1:")
        page = call_success(
            server,
            "read_text_file",
            {"workdir": "test", "path": leaf, "start_line": 1},
        )
        assert page["content"] == "first line\n"
        edited = call_success(
            server,
            "edit_text_file",
            {
                "workdir": "test",
                "path": leaf,
                "expected_revision": created["revision"],
                "edits": [{"old_text": "first", "new_text": "deep"}],
            },
        )
        assert edited["edits_applied"] == 1
        assert edited["bytes_before"] == page["bytes_returned"]
        assert edited["bytes_after"] == 10  # "deep line\n"
        stat = call_success(server, "stat_file", {"workdir": "test", "path": leaf})
        assert stat["type"] == "file"
        deleted = call_success(
            server,
            "delete_file",
            {"workdir": "test", "path": leaf, "expected_revision": edited["revision"]},
        )
        assert deleted["deleted"] is True
        # the atomic-publication temp file lived in the deep directory too:
        # nothing of it remains
        listing = call_success(server, "list_directory", {"workdir": "test", "path": relative})
        assert [entry["name"] for entry in listing["entries"]] == []

    def test_find_and_search_reach_deep_leaves(self, ro_root) -> None:
        server = _make_server(ro_root, read_only=True)
        relative, absolute_len = build_deep_tree(ro_root, levels=12, component_len=40)
        assert absolute_len > 260, absolute_len
        import os

        deep_leaf = os.path.join(ro_root, *relative.split("/"), "needle.txt")
        with open(deep_leaf, "w", encoding="utf-8") as fh:
            fh.write("PHASEF-NEEDLE deep\n")
        found = call_success(
            server, "find_files", {"workdir": "test", "path": "", "pattern": "needle.txt"}
        )
        assert any(m["path"].endswith("needle.txt") for m in found["matches"]), found
        searched = call_success(
            server, "search_text", {"workdir": "test", "path": "", "query": "PHASEF-NEEDLE"}
        )
        hits = searched["matches"]
        assert len(hits) == 1, hits
        assert hits[0]["path"].replace("\\", "/").endswith(f"{relative}/needle.txt")


class TestComponentLimits:
    def test_255_unit_component_is_accepted(self, ro_root) -> None:
        name = "x" * 255
        (ro_root / name).write_bytes(b"ok\n")
        server = _make_server(ro_root, read_only=True)
        result = call_success(server, "stat_file", {"workdir": "test", "path": name})
        assert result["type"] == "file"

    def test_256_unit_component_is_refused_by_the_kernel(self, ro_root) -> None:
        server = _make_server(ro_root, read_only=True)
        error = call_error(server, "stat_file", {"workdir": "test", "path": "x" * 256})
        assert error_code(error) == "INVALID_NAME"

    def test_emoji_component_counts_utf16_units(self, ro_root) -> None:
        name = "📁" * 128  # 256 UTF-16 code units, 128 characters
        error = call_error(
            _make_server(ro_root, read_only=True), "stat_file", {"workdir": "test", "path": name}
        )
        assert error_code(error) == "INVALID_NAME"
        ok_name = "📁" * 100  # 200 units
        (ro_root / ok_name).write_bytes(b"ok\n")
        stat = call_success(
            _make_server(ro_root, read_only=True), "stat_file", {"workdir": "test", "path": ok_name}
        )
        assert stat["type"] == "file"

    @pytest.mark.parametrize("name", ["trail.", "trail ", "sp .ace.", "a\\b", "a:b"])
    def test_ambiguous_or_structured_names_are_refused(self, rw_root, name: str) -> None:
        server = _make_server(rw_root, read_only=False)
        error = call_error(
            server,
            "create_text_file",
            {"workdir": "test", "path": name, "content": "x\n"},
        )
        assert error_code(error) == "INVALID_NAME"
        assert not any(child.name.startswith("trail") for child in rw_root.iterdir())


class TestUnicodePolicyAxes:
    def test_nfc_and_nfd_names_are_distinct_files(self, ro_root) -> None:
        nfc = "café-NFC.txt"  # é single code point
        nfd = "café-NFD.txt"  # e + combining acute
        assert nfc != nfd
        (ro_root / nfc).write_bytes(b"nfc\n")
        (ro_root / nfd).write_bytes(b"nfd\n")
        server = _make_server(ro_root, read_only=True)
        for name, expected in ((nfc, "nfc\n"), (nfd, "nfd\n")):
            page = call_success(
                server, "read_text_file", {"workdir": "test", "path": name, "start_line": 1}
            )
            assert page["content"] == expected

    def test_casefold_widened_denies_never_under_deny(self, ro_root) -> None:
        server = _make_server(ro_root, read_only=True)
        for name in (
            "ID_RSA",
            "SeCrEt.PEM",
            "ＡＰＩ.key",  # fullwidth ASCII letters keep the .key basename
            "id_rsa",
        ):
            (ro_root / name).write_bytes(b"x\n")
            error = call_error(server, "stat_file", {"workdir": "test", "path": name})
            code = error_code(error)
            assert code == "DENIED_PATH", (name, code)

    def test_reserved_names_are_refused_case_insensitively(self, rw_root) -> None:
        server = _make_server(rw_root, read_only=False)
        error = call_error(
            server,
            "create_text_file",
            {"workdir": "test", "path": ".SERVERFS-TMP-evil", "content": "x\n"},
        )
        # with hidden paths denied the hidden axis answers first; the object
        # is refused either way
        assert error_code(error) == "HIDDEN_PATH_NOT_ALLOWED"
        hidden_wd = Workdir(
            "test",
            rw_root,
            None,
            read_only=False,
            policy=EffectiveWorkdirPolicy(allow_hidden=True),
        )
        open_server = create_server(
            Settings(binary_transfer_enabled=True), WorkdirRegistry([hidden_wd])
        )
        error = call_error(
            open_server,
            "create_text_file",
            {"workdir": "test", "path": ".SERVERFS-TMP-evil", "content": "x\n"},
        )
        assert error_code(error) in {"RESERVED_PATH", "DENIED_PATH"}

    def test_turkish_dotted_i_file_reads_back_verbatim(self, ro_root) -> None:
        name = "İstanbul-report.txt"  # precomposed dotted capital I
        (ro_root / name).write_bytes(b"tr\n")
        server = _make_server(ro_root, read_only=True)
        page = call_success(
            server, "read_text_file", {"workdir": "test", "path": name, "start_line": 1}
        )
        assert page["content"] == "tr\n"

    def test_device_shaped_names_are_literal_on_ntfs_namespace(self, rw_root) -> None:
        # The root opens in the \\?\ literal namespace and every component is
        # HANDLE-relative, so DOS device names are plain files -- matching the
        # Linux-visible behavior, and NOT the Win32 path-parsing surprise.
        server = _make_server(rw_root, read_only=False)
        created = call_success(
            server,
            "create_text_file",
            {"workdir": "test", "path": "nul.txt", "content": "literal\n"},
        )
        assert created["revision"].startswith("v1:")
        assert (rw_root / "nul.txt").read_bytes() == b"literal\n"
