"""Native Windows Job lifetime isolation for two same-provider Cloudflare Connectors.

The Core and tunnel executables are disposable Python processes. No real
cloudflared, credential, public network, Task Scheduler or production service.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from serverfs_mcp.windows_v25 import readiness, runtime, storage
from serverfs_mcp.windows_v25.isolation import assert_connector_isolation
from serverfs_mcp.windows_v25.spec import Manifest

if sys.platform != "win32":
    pytest.skip("requires native Windows Job objects", allow_module_level=True)

_SID = "S-1-5-21-111-222-333-1001"
_INSTANCE = "two-cloudflare-fixture"


def _manifest(root: Path, connector_id: str) -> Manifest:
    return Manifest.parse(
        {
            "schema": 2,
            "instance_id": _INSTANCE,
            "connector_id": connector_id,
            "component": "cloudflare",
            "sid": _SID,
            "pythonw": str(root / "envs/cloudflare/Scripts/pythonw.exe"),
            "settings": {
                "cloudflared": str(root / "bin/cloudflared.exe"),
                "token_file": str(root / "private" / (connector_id + ".token")),
                "port": 18879,
                "hostname": "core.example.test",
            },
        }
    )


def test_two_cloudflare_connector_jobs_are_independently_owned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "instances" / _INSTANCE
    config = tmp_path / "serverfs.toml"
    config.write_text("[server]\n", encoding="utf-8")
    core = Manifest.parse(
        {
            "schema": 2,
            "instance_id": _INSTANCE,
            "component": "core",
            "sid": _SID,
            "pythonw": str(base / "envs/core/Scripts/pythonw.exe"),
            "settings": {
                "config": str(config),
                "auth_file": str(base / "private/core.env"),
                "port": 18879,
                "hostname": "core.example.test",
            },
        }
    )
    profiles = {name: _manifest(base, name) for name in ("edge-a", "edge-b")}
    assert_connector_isolation(profiles["edge-a"], profiles.values())
    assert_connector_isolation(profiles["edge-b"], profiles.values())
    assert len({profile.task_name() for profile in profiles.values()}) == 2
    assert len({profile.settings["token_file"] for profile in profiles.values()}) == 2

    # Start the independent Core first: its PID must never belong to either
    # anonymously owned Cloudflare process Job.
    core_proc = subprocess.Popen(
        [sys.executable, "-I", "-B", "-c", "import time;time.sleep(50)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    original_popen = subprocess.Popen
    children: dict[str, subprocess.Popen] = {}
    ready = {name: threading.Event() for name in profiles}
    results: dict[str, object] = {}
    threads: dict[str, threading.Thread] = {}
    lock = threading.Lock()

    def capture_child(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        with lock:
            children[threading.current_thread().name] = process
        return process

    def run(name: str) -> None:
        try:
            results[name] = runtime.run_connector(profiles[name])
        except BaseException as exc:
            results[name] = exc

    try:
        monkeypatch.setattr(
            storage,
            "component_inventory",
            lambda component: tuple(profiles.values()) if component == "cloudflare" else (),
        )
        monkeypatch.setattr(storage, "load", lambda component, instance_id: core)
        monkeypatch.setattr(runtime, "_roots", lambda path: (path, []))
        monkeypatch.setattr(runtime, "_core_is_verified", lambda profile: True)
        monkeypatch.setattr(
            runtime,
            "_connector_command",
            lambda profile, roots: (
                Path(sys.executable),
                ["-I", "-B", "-c", "import time;time.sleep(50)"],
            ),
        )
        allowed_env = {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH"}
        monkeypatch.setattr(
            runtime,
            "_minimal_env",
            lambda **kwargs: {
                name: value for name, value in os.environ.items() if name.upper() in allowed_env
            },
        )
        monkeypatch.setattr(
            readiness,
            "publish",
            lambda manifest: ready[manifest.connector_id].set(),
        )
        monkeypatch.setattr(runtime.subprocess, "Popen", capture_child)

        for name in profiles:
            thread = threading.Thread(target=run, args=(name,), name=name, daemon=True)
            threads[name] = thread
            thread.start()
        assert all(signal.wait(timeout=12) for signal in ready.values())
        assert set(children) == set(profiles)
        assert all(child.poll() is None for child in children.values())
        assert children["edge-a"].pid != children["edge-b"].pid
        assert core_proc.poll() is None

        peer_pid = children["edge-b"].pid
        children["edge-a"].terminate()
        threads["edge-a"].join(timeout=12)
        assert not threads["edge-a"].is_alive()
        assert isinstance(results["edge-a"], int)
        assert children["edge-a"].poll() is not None
        assert children["edge-b"].poll() is None
        assert children["edge-b"].pid == peer_pid
        assert core_proc.poll() is None

        children["edge-b"].terminate()
        threads["edge-b"].join(timeout=12)
        assert not threads["edge-b"].is_alive()
        assert isinstance(results["edge-b"], int)
        assert children["edge-b"].poll() is not None
        assert core_proc.poll() is None
    finally:
        for child in tuple(children.values()):
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
        for thread in threads.values():
            thread.join(timeout=8)
        if core_proc.poll() is None:
            core_proc.kill()
        core_proc.wait(timeout=5)
