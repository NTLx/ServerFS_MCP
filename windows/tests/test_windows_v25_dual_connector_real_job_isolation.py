"""Native Windows: two ingress Jobs targeting one Core remain independent.

Disposable Python children stand in for tunnel binaries. No Cloudflare/OpenAI
network, credentials, Task Scheduler, production service, or installed fixture.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from serverfs_mcp.windows_v25 import runtime, storage
from serverfs_mcp.windows_v25.spec import Manifest

if sys.platform != "win32":
    pytest.skip("requires native Windows Job objects", allow_module_level=True)

_SID = "S-1-5-21-111-222-333-1001"
_INSTANCE = "dual-ingress-fixture"


def _manifest(root: Path, component: str) -> Manifest:
    if component == "cloudflare":
        inputs = {
            "cloudflared": str(root / "cloudflared.exe"),
            "token_file": str(root / "unused-cloudflare.token"),
        }
    else:
        inputs = {
            "tunnel_client": str(root / "unused-tunnel.exe"),
            "tunnel_id": "tunnel_" + "a" * 32,
            "control_plane_key_file": str(root / "unused-control.key"),
            "core_auth_header_file": str(root / "unused-http.header"),
        }
    return Manifest.parse(
        {
            "schema": 2,
            "instance_id": _INSTANCE,
            "component": component,
            "sid": _SID,
            "pythonw": str(root / "envs" / component / "Scripts" / "pythonw.exe"),
            "settings": {**inputs, "port": 18879, "hostname": "dual.example.test"},
        }
    )


def test_two_connector_jobs_preserve_peer_and_core_when_one_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from serverfs_mcp.windows_v25 import readiness

    config = tmp_path / "serverfs.toml"
    config.write_text("[server]\n", encoding="utf-8")
    core = Manifest.parse(
        {
            "schema": 2,
            "instance_id": _INSTANCE,
            "component": "core",
            "sid": _SID,
            "pythonw": str(tmp_path / "envs" / "core" / "Scripts" / "pythonw.exe"),
            "settings": {
                "config": str(config),
                "auth_file": str(tmp_path / "unused-http.env"),
                "port": 18879,
                "hostname": "dual.example.test",
            },
        }
    )
    profiles = {kind: _manifest(tmp_path, kind) for kind in ("cloudflare", "openai")}
    # Start Core before monkeypatching Popen so its PID belongs to no ingress Job.
    core_proc = subprocess.Popen(
        [sys.executable, "-I", "-B", "-c", "import time;time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    real_popen = subprocess.Popen
    children: dict[str, subprocess.Popen] = {}
    ready = {name: threading.Event() for name in profiles}
    finished: dict[str, object] = {}
    lock = threading.Lock()
    threads: dict[str, threading.Thread] = {}

    def capture_child(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        with lock:
            children[threading.current_thread().name] = process
        return process

    def publish(profile: Manifest) -> None:
        ready[profile.component].set()

    def run(name: str) -> None:
        try:
            finished[name] = runtime.run_connector(profiles[name])
        except BaseException as exc:
            finished[name] = exc

    try:
        monkeypatch.setattr(storage, "component_inventory", lambda component: ())
        monkeypatch.setattr(storage, "load", lambda component, instance_id: core)
        monkeypatch.setattr(runtime, "_roots", lambda path: (path, []))
        monkeypatch.setattr(runtime, "_core_is_verified", lambda profile: True)
        monkeypatch.setattr(
            runtime,
            "_connector_command",
            lambda profile, roots: (
                Path(sys.executable),
                ["-I", "-B", "-c", "import time;time.sleep(60)"],
            ),
        )
        allowed_env = {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH"}
        monkeypatch.setattr(
            runtime,
            "_minimal_env",
            lambda **kwargs: {
                key: value for key, value in os.environ.items() if key.upper() in allowed_env
            },
        )
        monkeypatch.setattr(readiness, "publish", publish)
        monkeypatch.setattr(runtime.subprocess, "Popen", capture_child)

        for name in profiles:
            thread = threading.Thread(target=run, args=(name,), name=name, daemon=True)
            threads[name] = thread
            thread.start()
        assert all(event.wait(timeout=12) for event in ready.values())
        assert set(children) == set(profiles)
        assert all(child.poll() is None for child in children.values())
        assert core_proc.poll() is None

        other_pid = children["openai"].pid
        children["cloudflare"].terminate()
        threads["cloudflare"].join(timeout=12)
        assert not threads["cloudflare"].is_alive()
        assert isinstance(finished["cloudflare"], int)
        assert children["cloudflare"].poll() is not None
        assert children["openai"].poll() is None
        assert children["openai"].pid == other_pid
        assert core_proc.poll() is None

        children["openai"].terminate()
        threads["openai"].join(timeout=12)
        assert not threads["openai"].is_alive()
        assert isinstance(finished["openai"], int)
        assert children["openai"].poll() is not None
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
