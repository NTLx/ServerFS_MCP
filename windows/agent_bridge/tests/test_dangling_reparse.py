"""Deterministic regression for the dangling-reparse defect (§25, §28; maintainer review A).

The defect: ``_is_reparse`` was written as ``path.exists() and is_reparse_point(path)``. On Windows
``Path.exists()`` *follows* the link, so a dangling symlink — one whose target does not exist —
reports False while ``lstat`` still reports the reparse tag. The conjunction therefore classified
the cheapest object an attacker can plant, and the one that leaves no visible trace, as "nothing
there", and publication proceeded to write through it.

A real symlink fixture is the honest test but is not always available: creating one needs Developer
Mode or elevation, and this host does not have it. So the property is pinned two ways. A
**simulated** case drives the classification directly, proving the renderer does not depend on
``Path.exists()`` returning True; it runs everywhere and carries the coverage that would otherwise
skip. A **real** dangling symlink runs only where the host permits it.

The simulated case asserts the whole chain: classification says reparse, and publication refuses
without ever reaching ``os.replace``. Asserting only the classification would leave the refusal
itself untested.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from serverfs_agent_bridge.errors import BridgeError
from serverfs_agent_bridge.render_config import _is_reparse, render_native_bridge_config

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows private-state semantics")


def _request(workdir: Path):
    return {
        "workdirs": [
            {
                "alias": "repo",
                "host_path": str(workdir),
                "read_only": False,
                "agent_mode": "workspace-write",
                "agent_runtimes": ["codex"],
            }
        ],
        "runtimes": ["codex"],
    }


@pytest.fixture()
def workdir(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    return root


def _can_symlink() -> bool:
    tmp = Path(os.environ.get("TEMP", ".")) / f"serverfs-symlink-probe-{os.getpid()}"
    target = tmp / "absent-target"
    link = tmp / "link"
    try:
        link.symlink_to(target)
        return True
    except (OSError, NotImplementedError):
        return False
    finally:
        link.unlink(missing_ok=True)
        target.unlink(missing_ok=True)


class TestSimulatedDanglingReparse:
    """The deterministic regression: exists() False, is_reparse_point() True."""

    def test_a_dangling_reparse_is_still_classified_as_reparse(self, tmp_path: Path, monkeypatch):
        """The exact shape the old conjunction missed."""
        import serverfs_agent_bridge.windows_security as ws

        dangling = tmp_path / "bridge.json"
        real_lstat = Path.lstat

        def _fake_lstat(self: Path):
            if self == dangling:
                # A dangling symlink lstats successfully and carries the reparse tag, while
                # exists() — which follows the link — reports False.
                raise AssertionError("the fixture path must not be probed by exists()")
            return real_lstat(self)

        # exists() False and is_reparse_point() True is the whole point of the fixture.
        monkeypatch.setattr(Path, "exists", lambda self: False if self == dangling else True)
        monkeypatch.setattr(
            ws,
            "is_reparse_point",
            lambda path: True if path == dangling else False,
        )
        monkeypatch.setattr(Path, "lstat", _fake_lstat, raising=False)

        assert dangling.exists() is False, "the fixture must model a dangling link"
        assert _is_reparse(dangling) is True, (
            "a dangling reparse point must be classified as reparse; the old exists() guard made "
            "this False"
        )

    def test_publication_refuses_a_dangling_reparse_without_reaching_replace(
        self, tmp_path: Path, workdir: Path, monkeypatch
    ):
        """The refusal must happen, and must happen before os.replace is called."""
        import serverfs_agent_bridge.render_config as rc

        home = tmp_path / "home"
        config_path = home / "bridge.json"
        replaced: list[tuple] = []
        real_replace = os.replace

        def _tracking_replace(src, dst, *args, **kwargs):
            replaced.append((str(src), str(dst)))
            return real_replace(src, dst, *args, **kwargs)

        monkeypatch.setattr(rc.os, "replace", _tracking_replace)

        # First render creates the legitimate private file.
        rc.render_native_bridge_config(_request(workdir), home=home)
        assert config_path.is_file()
        replaced.clear()

        # Now model a dangling symlink at that path: exists() False, reparse True.
        monkeypatch.setattr(
            Path, "exists", lambda self: False if self == config_path else _real_exists(self)
        )
        monkeypatch.setattr(
            "serverfs_agent_bridge.windows_security.is_reparse_point",
            lambda path: True if path == config_path else False,
        )

        with pytest.raises(BridgeError, match="reparse"):
            rc.render_native_bridge_config(_request(workdir), home=home)

        assert replaced == [], "publication reached os.replace despite the reparse classification"
        # And the legitimate file the first render produced is untouched.
        assert config_path.is_file()


def _real_exists(self: Path) -> bool:
    """The unpatched ``Path.exists``, captured before any monkeypatching."""
    return _REAL_EXISTS(self)


_REAL_EXISTS = Path.exists


class TestRealDanglingSymlink:
    """Runs only where the host permits creating a symlink."""

    def test_real_dangling_symlink_is_refused_unchanged(self, tmp_path: Path, workdir: Path):
        if not _can_symlink():
            pytest.skip("symlink creation needs Developer Mode or elevation on this host")
        home = tmp_path / "home"
        config_path = home / "bridge.json"
        target = tmp_path / "absent-target.txt"
        config_path.symlink_to(target)
        assert config_path.exists() is False, "the fixture must be a dangling link"

        with pytest.raises((BridgeError, ValueError)):
            render_native_bridge_config(_request(workdir), home=home)
        # The link is still a link: nothing was written through it.
        assert config_path.is_symlink()
        assert not target.exists()


class TestOrdinaryPathsAreUnaffected:
    """The fix must not change the three ordinary cases."""

    def test_absent_path_is_not_a_reparse(self, tmp_path: Path):
        assert _is_reparse(tmp_path / "never-created.json") is False

    def test_absent_bridge_json_publishes_normally(self, tmp_path: Path, workdir: Path):
        home = tmp_path / "home"
        rendered = render_native_bridge_config(_request(workdir), home=home)
        assert rendered.config_path.is_file()

    def test_ordinary_private_file_is_not_a_reparse_and_rerenders(
        self, tmp_path: Path, workdir: Path
    ):
        home = tmp_path / "home"
        rendered = render_native_bridge_config(_request(workdir), home=home)
        assert _is_reparse(rendered.config_path) is False
        again = render_native_bridge_config(_request(workdir), home=home)
        assert again.config_path == rendered.config_path
        assert again.config_path.is_file()

    def test_directory_is_not_mistaken_for_a_reparse(self, tmp_path: Path, workdir: Path):
        """A directory is refused as a directory, which is a different and still-correct refusal."""
        home = tmp_path / "home"
        rendered = render_native_bridge_config(_request(workdir), home=home)
        rendered.config_path.unlink()
        rendered.config_path.mkdir()
        assert _is_reparse(rendered.config_path) is False
        with pytest.raises(BridgeError, match="directory"):
            render_native_bridge_config(_request(workdir), home=home)
