"""Opt-in signed native macOS Shared Core gate in a disposable named instance.

Foundation ignores process HOME overrides; the unique instance is placed in
the CURRENT user's normal ServerFS instances tree and deleted in finally.
It is NEVER registered, started, or connected to the public Internet.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import sysconfig
import venv
from pathlib import Path

import pytest

from serverfs_mcp.darwin_instance_shared_lifecycle import _signed_shared_core_python
from serverfs_mcp.darwin_instance_shared_manifest import (
    DarwinSharedManifestError,
    resolve_shared_core_manifest,
)
from serverfs_mcp.darwin_instance_shared_staging import stage_shared_core
from serverfs_mcp.darwin_instance_shared_unstage import unstage_shared_core
from serverfs_mcp.darwin_instances import instance_service_home
from serverfs_mcp.darwin_shared_runtime import create_shared_core_runtime
from serverfs_mcp.instance_identity import InstanceIdentity

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or os.environ.get("SERVERFS_ENABLE_EPHEMERAL_SIGNED_CORE_TEST") != "1",
    reason="requires explicit isolated signed-bundle test opt-in on Darwin",
)


def _run(argv: list[str], *, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)


def _create_test_runtime_python(test_dir: Path, source: Path) -> Path:
    # A resolved uv base Python loses the project's virtualenv dependencies.
    # Build an isolated copy-based venv in test scratch; a .pth file borrows
    # only the already installed pytest environment packages, read-only.
    # Production staging must instead receive a separately provisioned venv.
    runtime_home = test_dir / "runtime"
    venv.EnvBuilder(with_pip=False, symlinks=False).create(runtime_home)
    # uv-managed CPython on macOS links libpython relative to its binary.
    # A non-symlink executable copy needs the matching dylib as well.
    dylib_name = f"libpython{sys.version_info.major}.{sys.version_info.minor}.dylib"
    source_dylib = Path(sys.executable).resolve(strict=True).parent.parent / "lib" / dylib_name
    assert source_dylib.is_file(), "test-only uv Python library is unavailable"
    shutil.copy2(source_dylib, runtime_home / "lib" / dylib_name)
    packages = Path(sysconfig.get_path("purelib")).resolve(strict=True)
    runtime_sites = (
        runtime_home
        / "lib"
        / (f"python{sys.version_info.major}.{sys.version_info.minor}")
        / "site-packages"
    )
    runtime_sites.mkdir(parents=True, exist_ok=True)
    (runtime_sites / "test-dependencies.pth").write_text(
        str(packages) + "\n" + str(source / "src") + "\n", encoding="utf-8"
    )
    interpreter = runtime_home / "bin" / f"python{sys.version_info.major}.{sys.version_info.minor}"
    if interpreter.is_symlink():
        python = runtime_home / "bin" / "core-python"
        shutil.copy2(interpreter.resolve(strict=True), python)
    else:
        python = interpreter
    python.chmod(0o700)
    assert python.is_file() and not python.is_symlink() and os.access(python, os.X_OK)
    assert python.stat().st_uid == os.getuid()
    # Exercise the same isolated import contract that staging now enforces.
    runnable = _run([str(python), "-I", "-B", "-c", "import serverfs_mcp.cli, mcp, httpx2"])
    assert runnable.returncode == 0, "test-only copied venv lacks ServerFS dependencies"
    return python


def test_private_copy_venv_imports_runtime(tmp_path: Path) -> None:
    _create_test_runtime_python(tmp_path.resolve(), Path(__file__).resolve().parents[1])


def test_real_signed_shared_core_validate_only_and_fail_closed(tmp_path: Path) -> None:
    test_dir = tmp_path.resolve()
    instance = "shgate" + secrets.token_hex(6)
    identity = InstanceIdentity(instance, "shared", "test-owner")
    live_home = Path.home()
    base = live_home / "Library/Application Support/ServerFS"
    assert base.is_dir(), "existing ServerFS state root required"
    assert not base.is_symlink()
    instances = base / "instances"
    assert instances.is_dir() and not instances.is_symlink()
    owner_dir = instances / instance
    root = instance_service_home(identity)
    assert root.parent == owner_dir
    assert not owner_dir.exists() and not owner_dir.is_symlink()

    source = Path(__file__).resolve().parents[1]
    build = test_dir / "native-build"
    built = _run(
        ["bash", str(source / "native/macos/service_manager/build.sh"), str(build)],
        timeout=180,
    )
    assert built.returncode == 0, "isolated native ServiceManager build failed"
    template = build / "ServerFSServiceManager.app"
    assert template.is_dir()
    signed = _run(["codesign", "--verify", "--deep", "--strict", str(template)])
    assert signed.returncode == 0

    workdir = test_dir / "workspace"
    workdir.mkdir(mode=0o700)
    core_config = test_dir / "core.toml"
    core_config.write_text(
        "[agent]\nenabled = true\n[agent.codex]\nenabled = true\n"
        "[[workdirs]]\nalias = 'Workdir'\n"
        f"path = '{workdir}'\nread_only = false\n"
        "agent_mode = 'workspace-write'\nagent_runtimes = ['codex']\n",
        encoding="utf-8",
    )
    private = test_dir / "input"
    private.mkdir(mode=0o700)
    env_file = private / "http.env"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env_file.write_text(
        "SERVERFS_HTTP_API_KEY=synthetic-no-production-key\n"
        f"SERVERFS_HTTP_ALLOWED_HOSTS=gate.example.test,127.0.0.1:{port}\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    bridge_config = private / "bridge.json"
    bridge_socket = test_dir / "test-owner.sock"
    bridge_config.write_text(
        json.dumps(
            {
                "bridge_id": identity.bridge_id,
                "protocol_mode": "v2",
                "socket_path": str(bridge_socket),
            }
        )
    )
    bridge_config.chmod(0o600)
    binding_file = private / "owner-binding.json"
    binding_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "bridge_id": identity.bridge_id,
                "core_id": identity.instance_id,
                "key_id": "test-key",
                "secret_hex": "55" * 32,
                "workdir_alias": "Workdir",
                "workdir_path": str(workdir),
                "policy_revision": "e" * 64,
                "bridge_config_path": str(bridge_config),
                "socket_path": str(bridge_socket),
            }
        )
    )
    binding_file.chmod(0o600)
    # This native preflight uses an OFFLINE locked wheel runtime, not a
    # synthetic interpreter borrowing dependencies via a test-only .pth file.
    uv = shutil.which("uv")
    assert uv is not None, "explicit signed runtime test requires uv"
    installed = create_shared_core_runtime(
        test_dir / "installed-core-runtime",
        source,
        Path(sys.executable),
        Path(uv).resolve(strict=True),
    )
    python = Path(installed["python"])
    try:
        stage = stage_shared_core(
            identity,
            core_config,
            env_file,
            binding_file,
            port,
            "gate.example.test",
            template,
            python,
        )
        assert stage["state"] == "staged-not-registered"
        app = root / "ServerFSServiceManager.app"
        assert app.is_dir()
        assert _run(["codesign", "--verify", "--deep", "--strict", str(app)]).returncode == 0
        agent_dir = app / "Contents/Library/LaunchAgents"
        assert not (agent_dir / f"{identity.macos_label('bridge')}.plist").exists()

        launcher = app / "Contents/Resources/serverfs-service-launcher"
        manager = app / "Contents/MacOS/serverfs-service-manager"
        checked = _run([str(launcher), "--validate-only", instance, "http-origin"])
        assert checked.returncode == 0, (
            f"signed launcher refused candidate: exit={checked.returncode} "
            f"stderr={checked.stderr[:400]!r}"
        )
        assert checked.stdout == '{"ok":true}\n'
        # Management Python differs from the independently installed Core
        # interpreter pinned by this signed App. Validate the exact pin.
        assert Path(sys.executable).absolute() != python
        signed_pin = _signed_shared_core_python(identity, None)
        assert signed_pin == python
        alias, expected_socket = resolve_shared_core_manifest(
            identity, core_config, port, expected_executable=signed_pin
        )
        assert alias == "Workdir" and expected_socket == bridge_socket
        with pytest.raises(DarwinSharedManifestError):
            resolve_shared_core_manifest(identity, core_config, port)

        preflight = _run([str(manager), "register-preflight", "http-origin"])
        assert preflight.returncode == 0
        assert json.loads(preflight.stdout)["eligible"] is True
        state = _run([str(manager), "status", "http-origin"])
        assert state.returncode == 0
        assert json.loads(state.stdout)["services"]["http-origin"] in {
            "not-found",
            "not-registered",
        }
        assert not (root / "logs/core.stdout.log").exists()
        assert not (root / "logs/core.stderr.log").exists()
        assert not bridge_socket.exists()

        manifest = root / "services.json"
        raw = manifest.read_bytes()
        document = json.loads(raw)
        document["bridge_id"] = "other-owner"
        manifest.write_text(json.dumps(document))
        manifest.chmod(0o600)
        assert _run([str(launcher), "--validate-only", instance, "http-origin"]).returncode != 0
        manifest.write_bytes(raw)
        manifest.chmod(0o600)

        snap = root / "http-origin-environment.json"
        snap.chmod(0o644)
        assert _run([str(launcher), "--validate-only", instance, "http-origin"]).returncode != 0
        snap.chmod(0o600)
        assert _run([str(launcher), "--validate-only", instance, "http-origin"]).returncode == 0

        # The interpreter is outside the signed app but is pinned by physical
        # path. Even a user-owned symlink at that location must be rejected.
        moved_python = python.with_name(python.name + "-original")
        python.rename(moved_python)
        python.symlink_to(moved_python.name)
        try:
            assert _run([str(launcher), "--validate-only", instance, "http-origin"]).returncode != 0
        finally:
            python.unlink()
            moved_python.rename(python)
        assert _run([str(launcher), "--validate-only", instance, "http-origin"]).returncode == 0
        # Also verify real signed-instance removal does not require or alter the Bridge.
        assert unstage_shared_core(identity)["state"] == "unstaged"
    finally:
        # Never remove a preexisting instance or follow an unexpected symlink.
        assert owner_dir.name == instance and instance.startswith("shgate")
        assert owner_dir.parent == instances
        assert not owner_dir.is_symlink()
        if owner_dir.is_dir():
            shutil.rmtree(owner_dir)
    assert not owner_dir.exists()
