"""Executed evidence for the wheel-release helper (tag-only workflow logic).

The workflow itself can only run on a release tag, so its two non-trivial
gates -- METADATA-authoritative version matching and the paired-marker
release-notes rewrite -- are unit-tested here for every push:
version normalization against real wheel filenames (the pitfall that
filename-position parsing misread ``cp312``/``py3`` as versions), and the
full notes matrix: human text before/after the block, empty body, re-run
idempotence, digest replacement and fail-closed orphan markers.
"""

from __future__ import annotations

import importlib.util
import io
import sys
import tempfile
import zipfile
from contextlib import redirect_stderr
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "wheel_release",
    Path(__file__).resolve().parents[1] / "deployment" / "native" / "wheel_release.py",
)
assert _SPEC and _SPEC.loader
wheel_release = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = wheel_release  # dataclass resolution needs a registered module
_SPEC.loader.exec_module(wheel_release)

START = wheel_release.MARKER_START
END = wheel_release.MARKER_END

BLOCK_ARGS = {
    "product_name": "serverfs_mcp-0.10.0-py3-none-any.whl",
    "product_url": "https://github.com/o/r/releases/download/v0.10.0/serverfs_mcp-0.10.0-py3-none-any.whl",
    "product_sha": "a" * 64,
    "native_name": "serverfs_windows_native-0.10.0-cp312-abi3-win_amd64.whl",
    "native_url": "https://github.com/o/r/releases/download/v0.10.0/serverfs_windows_native-0.10.0-cp312-abi3-win_amd64.whl",
    "native_sha": "b" * 64,
}


def make_wheel(directory: Path, filename: str, *, name: str, version: str) -> Path:
    wheel = directory / filename
    with zipfile.ZipFile(wheel, "w") as bundle:
        metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
        bundle.writestr(f"{name.replace('-', '_')}-{version}.dist-info/METADATA", metadata)
    return wheel


def gate_main(tag: str, *wheels: Path) -> int:
    argv = ["version-gate", "--tag", tag]
    for wheel in wheels:
        argv += ["--wheel", str(wheel)]
    with redirect_stderr(io.StringIO()):
        return wheel_release.main(argv)


def notes_main(body: str, **overrides: str) -> tuple[int, str]:
    """Run notes-update with full block args (overridable) in a scratch dir."""
    args = {**BLOCK_ARGS, **overrides}
    with tempfile.TemporaryDirectory() as scratch:
        body_file = Path(scratch) / "body.md"
        out_file = Path(scratch) / "out.md"
        body_file.write_text(body, encoding="utf-8")
        argv = ["notes-update", "--body-file", str(body_file), "--out-file", str(out_file)]
        flags = {
            "product_name": "--product-name",
            "product_url": "--product-url",
            "product_sha": "--product-sha",
            "native_name": "--native-name",
            "native_url": "--native-url",
            "native_sha": "--native-sha",
        }
        for key, flag in flags.items():
            argv += [flag, args[key]]
        with redirect_stderr(io.StringIO()):
            code = wheel_release.main(argv)
        result = out_file.read_text(encoding="utf-8") if out_file.exists() else ""
    return code, result


class TestVersionGate:
    def test_real_abi3_filename_is_read_by_metadata_not_position(self, tmp_path: Path) -> None:
        # The regression this replaces: splitting the file name on '-' yields
        # 'cp312'/'py3' at index 2, not the version.
        native = make_wheel(
            tmp_path,
            "serverfs_windows_native-0.10.0-cp312-abi3-win_amd64.whl",
            name="serverfs-windows-native",
            version="0.10.0",
        )
        product = make_wheel(
            tmp_path, "serverfs_mcp-0.10.0-py3-none-any.whl", name="serverfs-mcp", version="0.10.0"
        )
        assert gate_main("v0.10.0", native, product) == 0

    @pytest.mark.parametrize(
        ("tag", "metadata_version"),
        [
            ("v0.10.0", "0.10.0"),
            ("v0.10.0b1", "0.10.0b1"),
            ("v0.10.0-rc1", "0.10.0rc1"),
            ("v0.10.0-dev", "0.10.0.dev0"),
            ("V0.10.0+a390c16", "0.10.0"),
        ],
    )
    def test_accepted_tag_shapes(self, tmp_path: Path, tag: str, metadata_version: str) -> None:
        wheel = make_wheel(tmp_path, "x-1.whl", name="serverfs-mcp", version=metadata_version)
        assert gate_main(tag, wheel) == 0

    def test_mismatched_versions_fail(self, tmp_path: Path) -> None:
        good = make_wheel(tmp_path, "a.whl", name="serverfs-mcp", version="0.10.0")
        stale = make_wheel(tmp_path, "b.whl", name="serverfs-windows-native", version="0.9.0")
        assert gate_main("v0.10.0", good, stale) == wheel_release.EXIT_VERSION_MISMATCH

    def test_missing_metadata_fails_closed(self, tmp_path: Path) -> None:
        hollow = tmp_path / "empty.whl"
        with zipfile.ZipFile(hollow, "w") as bundle:
            bundle.writestr("README.txt", "nothing")
        assert gate_main("v0.10.0", hollow) == wheel_release.EXIT_VERSION_MISMATCH


class TestNotesMatrix:
    def test_human_text_before_block_survives_first_append(self) -> None:
        body = "release v0.10.0\n\nnotes written by a human\n"
        code, result = notes_main(body)
        assert code == 0
        assert result.startswith("release v0.10.0\n\nnotes written by a human\n\n" + START)
        assert result.count(END) == 1

    def test_empty_body_gets_exactly_one_block(self) -> None:
        code, result = notes_main("")
        assert code == 0
        assert result.startswith(START)
        assert result.count(END) == 1

    def test_human_text_after_block_survives_replacement(self) -> None:
        after = "human epilogue line one\nline two\n"
        _code, seeded = notes_main("human prologue\n")
        code2, replaced = notes_main(seeded + "\n" + after)
        assert code2 == 0
        assert replaced.startswith("human prologue")
        assert replaced.endswith("\n" + after)
        assert replaced.count(START) == 1 and replaced.count(END) == 1

    def test_rerun_is_byte_idempotent(self) -> None:
        _code, once = notes_main("prose\n")
        _code2, twice = notes_main(once)
        assert once == twice

    def test_old_digests_are_replaced(self) -> None:
        _code, old = notes_main("prose\n", product_sha="1" * 64, native_sha="2" * 64)
        code, new = notes_main(old, product_sha="3" * 64, native_sha="4" * 64)
        assert code == 0
        assert "1" * 64 not in new and "2" * 64 not in new
        assert "3" * 64 in new and "4" * 64 in new

    @pytest.mark.parametrize(
        "broken",
        [
            START + "\nblock never closed\n",
            "orphan end " + END + "\n",
            START + "\nx\n" + END + "\n" + START + "\ny\n" + END,
        ],
        ids=["unclosed", "stray-end", "duplicate-pair"],
    )
    def test_marker_conflicts_fail_closed_without_output(self, broken: str) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            body_file = Path(scratch) / "body.md"
            out_file = Path(scratch) / "out.md"
            body_file.write_text(broken, encoding="utf-8")
            argv = [
                "notes-update",
                "--body-file",
                str(body_file),
                "--out-file",
                str(out_file),
                "--product-name",
                BLOCK_ARGS["product_name"],
                "--product-url",
                BLOCK_ARGS["product_url"],
                "--product-sha",
                BLOCK_ARGS["product_sha"],
                "--native-name",
                BLOCK_ARGS["native_name"],
                "--native-url",
                BLOCK_ARGS["native_url"],
                "--native-sha",
                BLOCK_ARGS["native_sha"],
            ]
            with redirect_stderr(io.StringIO()):
                code = wheel_release.main(argv)
            assert code == wheel_release.EXIT_MARKER_CONFLICT
            assert not out_file.exists()

    def test_block_content_names_both_assets(self) -> None:
        _code, result = notes_main("")
        assert BLOCK_ARGS["product_url"] in result
        assert BLOCK_ARGS["native_url"] in result
        assert "serverfs bootstrap native-wheel" in result


def test_normalize_release_version_table() -> None:
    normalize = wheel_release.normalize_release_version
    assert normalize("v0.10.0") == "0.10.0"
    assert normalize("V0.10.0+sha") == "0.10.0"
    assert normalize("0.10.0-rc1") == "0.10.0rc1"
    assert normalize("0.10.0.dev0") == "0.10.0dev0"
    assert normalize("0.10.0-dev") == "0.10.0dev0"
    assert normalize(" v0.10.0 ") == "0.10.0"
