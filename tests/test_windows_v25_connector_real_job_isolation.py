"""Native Windows: a v25 Connector Job never owns or kills its Core.

The tunnel executable is deliberately replaced by a fresh temporary Python
child. No cloudflared, OpenAI tunnel, credentials, DNS, public network, Task
Scheduler installation or installed production Core/Bridge is touched.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from serverfs_mcp.windows_v25 import runtime, storage
from serverfs_mcp.windows_v25.spec import Manifest

if sys.platform != "win32":
    pytest.skip("real Windows Job lifecycle requires Windows", allow_module_level=True)

_SID = "S-1-5-21-111-222-333-1001"


def _manifest(tmp_path: Path, component: str):
    root = tmp_path / "isolated-connector"
    core = Manifest.parse(
        {
            "schema": 2,
            "instance_id": "isolated-connector",
            "component": "core",
            "sid": _SID,
            "pythonw": str(root / "envs" / "core" / "Scripts" / "pythonw.exe"),
            "settings": {
                "config": str(tmp_path / "serverfs.toml"),
                "auth_file": str(tmp_path / "http.env"),
                "port": 18879,
                "hostname": "isolated.example.test",
            },
        }
    )
    connector = Manifest.parse(
        {
            "schema": 2,
            "instance_id": "isolated-connector",
            "component": component,
            "sid": _SID,
            "pythonw": str(root / "envs" / component / "Scripts" / "pythonw.exe"),
            "settings": (
                {
                    "cloudflared": str(tmp_path / "cloudflared.exe"),
                    "token_file": str(tmp_path / "never-used-token.txt"),
                    "port": 18879,
                    "hostname": "isolated.example.test",
                }
                if component == "cloudflare"
                else {
                    "tunnel_client": str(tmp_path / "never-used-tunnel.exe"),
                    "tunnel_id": "tunnel_" + "a" * 32,
                    "control_plane_key_file": str(tmp_path / "never-used-control.txt"),
                    "core_auth_header_file": str(tmp_path / "never-used-header.txt"),
                    "port": 18879,
                    "hostname": "isolated.example.test",
                }
            ),
        }
    )
    return core, connector


@pytest.mark.parametrize("component", ["cloudflare", "openai"])
def test_connector_owns_only_its_child_and_preserves_independent_core_process(
    tmp_path: Path, monkeypatch, component: str
) -> None:
    from serverfs_mcp.windows_v25 import readiness

    core, manifest = _manifest(tmp_path, component)
    config = Path(str(core.settings["config"]))
    config.write_text("[server]\n", encoding="utf-8")

    # A separate native process represents the already-running Core. It is
    # explicitly NOT assigned to the connector's anonymous Windows Job.
    sibling_core = subprocess.Popen(
        [sys.executable, "-I", "-B", "-c", "import time;time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    child = []
    verified_calls = []
    published = []
    original_popen = subprocess.Popen

    def track_child(*args, **kwargs):
        proc = original_popen(*args, **kwargs)
        child.append(proc)
        return proc

    try:
        monkeypatch.setattr(storage, "component_inventory", lambda _component: ())
        monkeypatch.setattr(storage, "load", lambda name, instance: core)
        monkeypatch.setattr(runtime, "_roots", lambda path: (path, []))
        monkeypatch.setattr(
            runtime,
            "_connector_command",
            lambda _manifest, _roots: (
                Path(sys.executable),
                ["-I", "-B", "-c", "import time;time.sleep(30)"],
            ),
        )
        monkeypatch.setattr(runtime, "_minimal_env", lambda **_kwargs: dict(os.environ))
        monkeypatch.setattr(readiness, "publish", lambda profile: published.append(profile))

        def verify_owner(profile):
            assert profile is manifest
            verified_calls.append(True)
            return len(verified_calls) == 1

        monkeypatch.setattr(runtime, "_core_is_verified", verify_owner)
        monkeypatch.setattr(runtime.subprocess, "Popen", track_child)

        with pytest.raises(runtime.RuntimeErrorV25, match="Core changed"):
            runtime.run_connector(manifest)

        assert len(verified_calls) >= 2
        assert published == [manifest]
        assert len(child) == 1
        assert child[0].poll() is not None
        assert sibling_core.poll() is None
    finally:
        # Preserve test containment if an assertion fails at any point.
        for proc in child:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
        if sibling_core.poll() is None:
            sibling_core.kill()
        sibling_core.wait(timeout=5)
