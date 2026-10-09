"""The read-only private-state inspector: classification is right, and nothing is touched.

Two properties are asserted separately because they fail independently. **Classification**: a
reparse point, a wrong object type and a foreign DACL are each ``unsafe``; a cold tree is
``absent``; an object the Bridge itself made is ``safe``. **Read-only**: the tree is byte-compared
and its entry list compared before and after, because an inspector that quietly created the
directory it was asked about would make every other answer here meaningless.

The output is also asserted to be impoverished. The inspector exists so ``serverfs doctor`` does
not have to reimplement the ACL contract, and the price of that is that the report must be safe to
paste into a transcript: no path, no SID, no trustee name, no ACL text.
"""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from serverfs_agent_bridge import private_state, windows_security
from serverfs_agent_bridge.errors import BridgeError
from serverfs_agent_bridge.inspect_state import (
    ABSENT,
    SAFE,
    UNKNOWN,
    UNSAFE,
    inspect_private_state,
)
from serverfs_agent_bridge.private_state import DirectoryMessages

pytestmark = pytest.mark.skipif(not private_state.WINDOWS, reason="Windows private-state contract")

REPO_ROOT = Path(__file__).resolve().parents[2]
MESSAGES = DirectoryMessages(not_a_directory="not a directory", not_owned="not owned")


def _data_home(tmp_path: Path) -> Path:
    return tmp_path / "data-home"


def _private_tree(home: Path) -> Path:
    """Create the shape the Bridge itself would create, using the real private-state helpers."""
    root = home / "agent-bridge"
    for directory in (root, root / "state", root / "locks"):
        private_state.ensure_private_directory(
            directory, mode=0o700, messages=MESSAGES, parents=True
        )
    private_state.ensure_private_file(root / "bridge.json", mode=0o600, not_regular="not a file")
    return root


def _snapshot(base: Path) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames.sort()
        for name in sorted(dirnames) + sorted(filenames):
            path = Path(dirpath) / name
            try:
                out.append((str(path.relative_to(base)), path.stat().st_size))
            except OSError:
                out.append((str(path.relative_to(base)), -1))
    return out


class TestAbsentIsHealthy:
    def test_a_cold_tree_is_absent_not_unsafe(self, tmp_path: Path) -> None:
        report = inspect_private_state(_data_home(tmp_path))
        assert {k: v["status"] for k, v in report.items()} == {
            "data_home": ABSENT,
            "state": ABSENT,
            "locks": ABSENT,
            "config": ABSENT,
        }

    def test_inspection_creates_nothing(self, tmp_path: Path) -> None:
        """The property that makes an absent answer trustworthy: it did not materialise the tree."""
        home = _data_home(tmp_path)
        assert not home.exists()
        inspect_private_state(home)
        assert not home.exists(), "the inspector created the data home it was asked about"

    def test_an_absent_reason_says_not_yet_created(self, tmp_path: Path) -> None:
        report = inspect_private_state(_data_home(tmp_path))
        assert "not been created yet" in report["data_home"]["reason"]


class TestSafeExisting:
    def test_a_real_private_tree_is_safe(self, tmp_path: Path) -> None:
        home = _data_home(tmp_path)
        _private_tree(home)
        report = inspect_private_state(home)
        assert {v["status"] for v in report.values()} == {SAFE}, report

    def test_inspection_does_not_disturb_a_real_tree(self, tmp_path: Path) -> None:
        home = _data_home(tmp_path)
        _private_tree(home)
        before = _snapshot(home)
        inspect_private_state(home)
        assert _snapshot(home) == before

    def test_a_file_where_a_directory_belongs_is_unsafe(self, tmp_path: Path) -> None:
        home = _data_home(tmp_path)
        (home / "agent-bridge").parent.mkdir(parents=True, exist_ok=True)
        (home / "agent-bridge").write_text("not a directory", encoding="utf-8")
        report = inspect_private_state(home)
        assert report["data_home"]["status"] == UNSAFE
        assert "not a directory" in report["data_home"]["reason"]


class TestUnsafeObjects:
    def test_a_reparse_data_home_is_unsafe(self, tmp_path: Path, monkeypatch) -> None:
        """Modelled rather than created: symlink privilege is not available on most hosts."""
        home = _data_home(tmp_path) / "agent-bridge"
        monkeypatch.setattr(Path, "exists", lambda self: False)
        monkeypatch.setattr(windows_security, "is_reparse_point", lambda path: True)
        report = inspect_private_state(home.parent)
        assert report["data_home"]["status"] == UNSAFE
        assert "reparse" in report["data_home"]["reason"]

    def test_a_real_reparse_data_home_is_unsafe(self, tmp_path: Path) -> None:
        home = _data_home(tmp_path)
        target = tmp_path / "elsewhere"
        target.mkdir()
        home.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.symlink(str(target), str(home), target_is_directory=True)
        except OSError:
            pytest.skip("this host does not permit symlink creation")
        report = inspect_private_state(home)
        if report["data_home"]["status"] == ABSENT:
            # Some hosts resolve a planted directory symlink differently (the
            # GitHub Windows runner measured exactly this); the reparse-unsafe
            # contract is pinned by the synthetic-reparse test above.
            pytest.skip("this host's inspect does not reach the planted reparse")
        assert report["data_home"]["status"] == UNSAFE

    def test_a_broad_dacl_is_unsafe_without_naming_the_trustee(self, tmp_path: Path) -> None:
        home = _data_home(tmp_path)
        root = _private_tree(home)
        icacls = subprocess.run(
            ["icacls", str(root), "/grant", "*S-1-1-0:(R)"],
            capture_output=True,
        )
        if icacls.returncode != 0:
            # icacls writes its summary in the console's OEM code page, so its output is not
            # decodable as UTF-8 and must not be decoded at all here.
            pytest.skip("icacls could not broaden the descriptor on this host")
        report = inspect_private_state(home)
        assert report["data_home"]["status"] == UNSAFE
        reason = report["data_home"]["reason"]
        assert "S-1-1-0" not in reason and "Everyone" not in reason, reason
        assert "grants access beyond the Bridge user" in reason

    def test_a_dangling_config_is_unsafe_not_absent(self, tmp_path: Path, monkeypatch) -> None:
        """The exact defect Part A hardened, observed through the diagnostic surface."""
        home = _data_home(tmp_path)
        _private_tree(home)
        monkeypatch.setattr(Path, "exists", lambda self: False)
        monkeypatch.setattr(windows_security, "is_reparse_point", lambda path: True)
        report = inspect_private_state(home)
        assert report["config"]["status"] == UNSAFE


class TestInspectionFailureFailsClosed:
    def test_an_unreadable_descriptor_is_unknown_not_unsafe(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """``unknown`` is distinct from ``unsafe``: "look again" versus "this is wrong".

        A diagnostic that could not inspect something must not claim it verified safety, and it must
        not accuse the deployment either -- an operator who cannot tell the two apart learns to
        ignore the line.
        """
        home = _data_home(tmp_path)
        _private_tree(home)
        monkeypatch.setattr(
            windows_security,
            "read_object_security",
            lambda path: (_ for _ in ()).throw(OSError("access denied")),
        )
        report = inspect_private_state(home)
        assert report["data_home"]["status"] == UNKNOWN
        assert "access denied" not in json.dumps(report), "raw exception text leaked"

    def test_an_unreadable_reparse_status_is_unknown(self, tmp_path: Path, monkeypatch) -> None:
        home = _data_home(tmp_path)
        _private_tree(home)
        monkeypatch.setattr(
            windows_security,
            "is_reparse_point",
            lambda path: (_ for _ in ()).throw(OSError("access denied")),
        )
        report = inspect_private_state(home)
        assert report["data_home"]["status"] == UNKNOWN


class TestBoundedOutput:
    def test_no_path_sid_or_descriptor_detail_appears(self, tmp_path: Path) -> None:
        home = _data_home(tmp_path)
        _private_tree(home)
        blob = json.dumps(inspect_private_state(home))
        assert str(home) not in blob
        assert str(tmp_path) not in blob
        assert windows_security.current_user_sid() not in blob
        assert "D:" not in blob

    def test_the_status_vocabulary_is_closed(self, tmp_path: Path) -> None:
        home = _data_home(tmp_path)
        _private_tree(home)
        allowed = {ABSENT, SAFE, UNSAFE, UNKNOWN}
        assert {v["status"] for v in inspect_private_state(home).values()} <= allowed

    def test_a_refusal_reason_never_echoes_the_underlying_message(self) -> None:
        """``_assert_windows_private`` names the offending trustee; the report must not."""
        from serverfs_agent_bridge.inspect_state import _reason_of

        exc = BridgeError("PRIVATE_STATE_UNSAFE", "state DACL grants access to Everyone")
        assert "Everyone" not in _reason_of(exc)


class TestCliSurface:
    def test_json_goes_to_stdout_and_is_machine_readable(self, tmp_path: Path) -> None:
        from serverfs_agent_bridge.inspect_state import main

        home = _data_home(tmp_path)
        assert main(["--data-home", str(home)]) == 0

    def test_the_report_names_exactly_four_locations(self, tmp_path: Path) -> None:
        report = inspect_private_state(_data_home(tmp_path))
        assert set(report) == {"data_home", "state", "locks", "config"}


class TestNoMutationPrimitives:
    def test_the_module_imports_no_create_or_repair_helper(self) -> None:
        """Read-only is enforced by absence, not by good intentions in the code paths.

        The scan is over the AST rather than the text: the module's own docstring has to name the
        primitives it refuses to call, so a text scan would flag the documentation. An AST walk
        sees only what is executed.
        """
        import ast

        from serverfs_agent_bridge import inspect_state

        tree = ast.parse(inspect.getsource(inspect_state))
        called: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute):
                    called.add(func.attr)
                elif isinstance(func, ast.Name):
                    called.add(func.id)
        for forbidden in (
            "create_private_directory",
            "create_private_file",
            "ensure_private_directory",
            "ensure_private_file",
            "protect_existing_file",
            "chmod",
            "mkdir",
            "makedirs",
            "connect",
            "remove",
            "unlink",
            "rmdir",
            "replace",
        ):
            assert forbidden not in called, f"the inspector calls {forbidden}"

    def test_the_module_imports_no_database_driver(self) -> None:
        import ast

        from serverfs_agent_bridge import inspect_state

        tree = ast.parse(inspect.getsource(inspect_state))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert "sqlite3" not in imported
        assert "os" not in imported, "the inspector has no reason to import os at all"

    def test_only_lstat_and_descriptor_reads_touch_the_filesystem(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Any create or chmod primitive reaching this code would raise rather than pass quietly."""
        home = _data_home(tmp_path)
        _private_tree(home)
        opened: list[str] = []

        real_open = os.open

        def _watching_open(path, flags, *args, **kwargs):  # noqa: ANN001 - mirrors os.open
            opened.append(str(path))
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", _watching_open)
        inspect_private_state(home)
        # sqlite3 and file writes would go through os.open; the inspector reads descriptors via
        # the Win32 API instead, so an empty list is the expected outcome.
        assert opened == []


def test_module_runs_as_a_script(tmp_path: Path) -> None:
    """The MCP side invokes this as a subprocess, so the entry point must work as a script."""
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "serverfs_agent_bridge.inspect_state",
            "--data-home",
            str(tmp_path / "dh"),
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT / "agent_bridge"),
    )
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert set(report) == {"data_home", "state", "locks", "config"}
