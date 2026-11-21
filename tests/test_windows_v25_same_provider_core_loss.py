"""Native Windows Core-loss containment across same-provider Connector Jobs.

Two independent Cloudflare-like Connector Jobs target one disposable Core.
A third Connector targets an independent disposable Core. The first Core
exiting must tear down BOTH related Connector children and their Jobs without
interrupting the second Core/Connector. No real cloudflared, DNS, credentials,
external network, Task Scheduler, installed fixtures or production services.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from serverfs_mcp.windows_v25 import readiness, runtime, storage
from serverfs_mcp.windows_v25.spec import Manifest

if sys.platform != "win32":
    pytest.skip("requires native Windows Jobs", allow_module_level=True)

_SID = "S-1-5-21-111-222-333-1001"
_PAIR = ("isolated-a", "isolated-b")


def _core(root: Path, instance: str, port: int) -> Manifest:
    config = root / instance / "settings.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("[server]\n", encoding="utf-8")
    return Manifest.parse(
        {
            "schema": 2,
            "instance_id": instance,
            "component": "core",
            "sid": _SID,
            "pythonw": str(root / instance / "envs/core/Scripts/pythonw.exe"),
            "settings": {
                "config": str(config),
                "auth_file": str(root / instance / "private/http.env"),
                "port": port,
                "hostname": instance + ".example.test",
            },
        }
    )


def _connector(root: Path, core: Manifest, ident: str) -> Manifest:
    return Manifest.parse(
        {
            "schema": 2,
            "instance_id": core.instance_id,
            "connector_id": ident,
            "component": "cloudflare",
            "sid": _SID,
            "pythonw": str(root / core.instance_id / "envs/cloudflare/Scripts/pythonw.exe"),
            "settings": {
                "cloudflared": str(root / core.instance_id / "bin/cloudflared.exe"),
                "token_file": str(root / core.instance_id / "private" / (ident + ".token")),
                "port": core.settings["port"],
                "hostname": core.settings["hostname"],
            },
        }
    )


def test_native_two_connectors_stop_after_core_loss_other_instance_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cores = {
        name: _core(tmp_path, name, port) for name, port in zip(_PAIR, (18961, 18962), strict=True)
    }
    profiles = {
        "edge-a": _connector(tmp_path, cores["isolated-a"], "edge-a"),
        "edge-b": _connector(tmp_path, cores["isolated-a"], "edge-b"),
        "edge-c": _connector(tmp_path, cores["isolated-b"], "edge-c"),
    }
    real_popen = subprocess.Popen
    real_sleep = time.sleep
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    core_processes: dict[str, subprocess.Popen] = {}
    children: dict[str, subprocess.Popen] = {}
    ready = {name: threading.Event() for name in profiles}
    threads: dict[str, threading.Thread] = {}
    results: dict[str, object] = {}
    lock = threading.Lock()

    def spawn(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        with lock:
            children[threading.current_thread().name] = process
        return process

    def run(name: str) -> None:
        try:
            results[name] = runtime.run_connector(profiles[name])
        except BaseException as exc:
            results[name] = exc

    try:
        for name in _PAIR:
            core_processes[name] = real_popen(
                [sys.executable, "-I", "-B", "-c", "import time;time.sleep(40)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
        monkeypatch.setattr(
            storage,
            "component_inventory",
            lambda component: tuple(profiles.values()) if component == "cloudflare" else (),
        )
        monkeypatch.setattr(storage, "load", lambda component, instance_id: cores[instance_id])
        monkeypatch.setattr(runtime, "_roots", lambda path: (path, []))
        monkeypatch.setattr(
            runtime,
            "_core_is_verified",
            lambda profile: core_processes[profile.instance_id].poll() is None,
        )
        monkeypatch.setattr(
            runtime,
            "_connector_command",
            lambda manifest, roots: (
                Path(sys.executable),
                ["-I", "-B", "-c", "import time;time.sleep(40)"],
            ),
        )
        allowed = {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH"}
        monkeypatch.setattr(
            runtime,
            "_minimal_env",
            lambda **kwargs: {
                key: value for key, value in os.environ.items() if key.upper() in allowed
            },
        )
        # Do not monkeypatch the global time module; accelerate only
        # runtime's connector-health polling.
        monkeypatch.setattr(runtime, "time", SimpleNamespace(sleep=lambda _: real_sleep(0.04)))
        monkeypatch.setattr(readiness, "publish", lambda profile: ready[profile.connector_id].set())
        monkeypatch.setattr(runtime.subprocess, "Popen", spawn)
        for ident in profiles:
            thread = threading.Thread(target=run, args=(ident,), name=ident, daemon=True)
            threads[ident] = thread
            thread.start()

        assert all(signal.wait(timeout=12) for signal in ready.values())
        assert set(children) == set(profiles)
        assert all(process.poll() is None for process in children.values())
        assert all(process.poll() is None for process in core_processes.values())
        unaffected_pid = children["edge-c"].pid
        unaffected_core_pid = core_processes["isolated-b"].pid

        # Simulate one authentic Core process dying. BOTH of its ingress
        # components must cease, but unrelated Core B remains untouched.
        core_processes["isolated-a"].kill()
        core_processes["isolated-a"].wait(timeout=8)
        for ident in ("edge-a", "edge-b"):
            threads[ident].join(timeout=12)
            assert not threads[ident].is_alive(), ident
            assert isinstance(results.get(ident), runtime.RuntimeErrorV25)
            assert children[ident].poll() is not None

        assert threads["edge-c"].is_alive()
        assert children["edge-c"].poll() is None
        assert children["edge-c"].pid == unaffected_pid
        assert core_processes["isolated-b"].poll() is None
        assert core_processes["isolated-b"].pid == unaffected_core_pid

        children["edge-c"].terminate()
        threads["edge-c"].join(timeout=12)
        assert not threads["edge-c"].is_alive()
        assert isinstance(results.get("edge-c"), int)
        assert children["edge-c"].poll() is not None
        assert core_processes["isolated-b"].poll() is None
    finally:
        for child in tuple(children.values()):
            if child.poll() is None:
                child.kill()
            child.wait(timeout=8)
        for thread in tuple(threads.values()):
            thread.join(timeout=8)
        for core in core_processes.values():
            if core.poll() is None:
                core.kill()
            core.wait(timeout=8)
