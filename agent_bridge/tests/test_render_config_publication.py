"""Windows regressions for private bridge.json publication (§25, §15 D2, maintainer review P0).

The earlier implementation replaced an existing ``bridge.json`` and verified it afterwards. That
inverts the §25 rule in the one direction that matters: a pre-planted object — a reparse point, a
foreign or broad DACL, a non-regular file — was overwritten before anything inspected it, so the
"fail closed, never repair" boundary was only enforced against objects the function itself had
produced.

Each case here plants the hostile object first, then renders, then asserts two things: the render
was refused **and the target is byte-for-byte unchanged**. The second half distinguishes a real
refusal from one that still managed to damage the target on its way out.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from serverfs_agent_bridge.errors import BridgeError
from serverfs_agent_bridge.render_config import render_native_bridge_config

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows private-state semantics")

PLANTED = b'{"planted": "attacker controlled"}'


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


def _first_render(tmp_path: Path, workdir: Path) -> Path:
    """Create a legitimate config once, so later cases start from a real private object."""
    rendered = render_native_bridge_config(_request(workdir), home=tmp_path / "home")
    return rendered.config_path


class TestSafeExistingConfig:
    """The idempotent re-render must keep working."""

    def test_rerender_over_a_private_file_succeeds(self, tmp_path: Path, workdir: Path):
        config_path = _first_render(tmp_path, workdir)
        original = json.loads(config_path.read_text(encoding="utf-8"))

        rendered = render_native_bridge_config(_request(workdir), home=tmp_path / "home")
        assert rendered.config_path == config_path
        # Same content, and the file is still the private object we published.
        assert json.loads(config_path.read_text(encoding="utf-8")) == original

    def test_rerender_replaces_changed_policy(self, tmp_path: Path, workdir: Path):
        config_path = _first_render(tmp_path, workdir)
        request = _request(workdir)
        request["runtimes"] = []
        request["workdirs"][0]["agent_runtimes"] = []
        request["workdirs"][0]["agent_mode"] = "disabled"
        render_native_bridge_config(request, home=tmp_path / "home")
        document = json.loads(config_path.read_text(encoding="utf-8"))
        assert document["workdirs"][0]["agent_mode"] == "disabled"


class TestPlantedBroadDacl:
    """A world-readable target must be refused and left untouched."""

    @pytest.mark.skipif(sys.platform != "win32", reason="icacls is a Windows tool")
    def test_broad_dacl_target_is_refused_unchanged(self, tmp_path: Path, workdir: Path):
        config_path = _first_render(tmp_path, workdir)
        config_path.write_bytes(PLANTED)

        # Replace the protected DACL with one granting Everyone, so the object is no longer private
        # and §25 requires a refusal rather than a repair. icacls output is not UTF-8 on this
        # locale, so the pipes are read as bytes rather than decoded text.
        icacls = subprocess.run(
            ["icacls", str(config_path), "/grant", "*S-1-1-0:(R)"],
            capture_output=True,
        )
        if icacls.returncode != 0:
            pytest.skip("icacls could not broaden the descriptor here")

        with pytest.raises((BridgeError, ValueError)):
            render_native_bridge_config(_request(workdir), home=tmp_path / "home")
        assert config_path.read_bytes() == PLANTED, "the planted target was modified"


class TestPlantedReparse:
    """A junction or symlink at the config path must be refused and left in place."""

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows reparse semantics")
    def test_reparse_target_is_refused_unchanged(self, tmp_path: Path, workdir: Path):
        config_path = _first_render(tmp_path, workdir)
        config_path.unlink()

        elsewhere = tmp_path / "elsewhere.txt"
        elsewhere.write_bytes(PLANTED)
        # A symbolic link needs Developer Mode or elevation; fall back cleanly when unavailable.
        link = subprocess.run(
            ["cmd", "/c", "mklink", str(config_path), str(elsewhere)],
            capture_output=True,
            text=True,
        )
        if link.returncode != 0:
            pytest.skip("symlink creation is unavailable on this host")

        with pytest.raises((BridgeError, ValueError)):
            render_native_bridge_config(_request(workdir), home=tmp_path / "home")
        # The link is still a link and its target is untouched: no replacement happened through it.
        assert config_path.is_symlink() or os.path.islink(config_path)
        assert elsewhere.read_bytes() == PLANTED


class TestPlantedUnsafeType:
    """A directory where the config belongs must be refused, not replaced."""

    def test_directory_target_is_refused(self, tmp_path: Path, workdir: Path):
        home = tmp_path / "home"
        rendered = render_native_bridge_config(_request(workdir), home=home)
        config_path = rendered.config_path
        config_path.unlink()
        config_path.mkdir()

        with pytest.raises((BridgeError, ValueError)):
            render_native_bridge_config(_request(workdir), home=home)
        assert config_path.is_dir(), "the refusal replaced a directory with a file"


class TestNoTempLeftBehind:
    """A refused publication must not leave a half-written temp file."""

    def test_refusal_leaves_no_temp_file(self, tmp_path: Path, workdir: Path):
        config_path = _first_render(tmp_path, workdir)
        config_path.unlink()
        config_path.mkdir()  # force the refusal path

        with pytest.raises((BridgeError, ValueError)):
            render_native_bridge_config(_request(workdir), home=tmp_path / "home")
        leftovers = [p.name for p in config_path.parent.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == []


class TestStrictReadOnly:
    """read_only follows the Bridge loader's strict boolean rule."""

    def test_string_read_only_is_refused(self, tmp_path: Path, workdir: Path):
        request = _request(workdir)
        request["workdirs"][0]["read_only"] = "false"
        with pytest.raises(BridgeError, match="read_only"):
            render_native_bridge_config(request, home=tmp_path / "home")

    def test_numeric_read_only_is_refused(self, tmp_path: Path, workdir: Path):
        request = _request(workdir)
        request["workdirs"][0]["read_only"] = 0
        with pytest.raises(BridgeError, match="read_only"):
            render_native_bridge_config(request, home=tmp_path / "home")

    def test_boolean_read_only_is_accepted(self, tmp_path: Path, workdir: Path):
        request = _request(workdir)
        request["workdirs"][0]["read_only"] = True
        # True with workspace-write is a contradiction, so the refusal must name that, not the type.
        with pytest.raises(BridgeError, match="writable workdir"):
            render_native_bridge_config(request, home=tmp_path / "home")
