"""Opt-in native SMAppService acceptance for TWO named Shared Cores.

Only unique, randomly named test Core jobs may be registered. The Bridge owner
is a separate, disposable synthetic v2 subprocess (FakeAdapter), not a macOS
Login Item. Both Cores pin the independently built offline runtime. Every pre-existing
ServerFS launchd label must retain its baseline PID and exit status.

The test refuses manual approval bypass, makes a best effort to unregister
only its two Core jobs, and preserves unknown active state for diagnosis.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import plistlib
import re
import secrets
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import zipfile
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path

import pytest

from serverfs_mcp import darwin_service
from serverfs_mcp.core_bridge_client_v2 import (
    CoreBridgeClientV2,
    CoreBridgeRemoteError,
    CoreBridgeUnavailable,
)
from serverfs_mcp.darwin_instance_runtime import _manager
from serverfs_mcp.darwin_instance_shared_lifecycle import (
    start_shared_core,
    stop_shared_core,
)
from serverfs_mcp.darwin_instance_shared_manifest import resolve_shared_core_manifest
from serverfs_mcp.darwin_instances import APP_NAME, instance_service_home
from serverfs_mcp.darwin_shared_runtime import create_shared_core_runtime
from serverfs_mcp.instance_identity import InstanceIdentity
from test_darwin_v0250_shared_owner_process_macos import (
    _start_owner as _start_source_owner,
)
from test_darwin_v0250_shared_owner_process_macos import (
    _stop,
    _wait_owner,
)
from test_darwin_v0250_shared_two_core_http_macos import (
    _http_status,
    _mcp_call,
    _port,
    _prepare_core,
    _private_json,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or os.environ.get("SERVERFS_ENABLE_SHARED_SMAPP_LIVE_TEST") != "1",
    reason="requires explicit temporary macOS user Login Item SMAppService registration opt-in",
)

_PROTECTED = (
    "com.ntlx.serverfs.tunnel",
    "com.ntlx.serverfs.bridge",
    "com.ntlx.serverfs.cloudflare",
    "com.ntlx.serverfs.http-origin",
)


def _production_pids() -> tuple[int | None, ...]:
    return tuple(darwin_service._launchd_job_pid(label) for label in _PROTECTED)


def _protected_launchd_snapshot(
    excluded: frozenset[str] = frozenset(),
) -> dict[str, tuple[int | None, int]]:
    """Protect all current ServerFS jobs, not just the legacy default topology."""
    result = subprocess.run(
        ["launchctl", "list"], capture_output=True, text=True, timeout=10, check=False
    )
    if result.returncode:
        raise RuntimeError("protected launchd inventory unavailable")
    snapshot: dict[str, tuple[int | None, int]] = {}
    for row in result.stdout.splitlines():
        fields = row.split()
        if not fields or not fields[-1].startswith("com.ntlx.serverfs."):
            continue
        if len(fields) != 3:
            raise RuntimeError("malformed protected launchd row")
        raw_pid, raw_status, label = fields
        if label in excluded:
            continue
        if label in snapshot:
            raise RuntimeError("duplicate protected launchd label")
        try:
            pid = None if raw_pid == "-" else int(raw_pid)
            status = int(raw_status)
        except ValueError:
            raise RuntimeError("malformed protected launchd state") from None
        if pid is not None and pid <= 1:
            raise RuntimeError("invalid protected launchd PID")
        snapshot[label] = (pid, status)
    return snapshot


def _active_shsm_labels() -> set[str]:
    result = subprocess.run(
        ["launchctl", "list"], capture_output=True, text=True, timeout=10, check=False
    )
    if result.returncode:
        raise RuntimeError("launchd inventory unavailable")
    return {
        fields[-1]
        for row in result.stdout.splitlines()
        if len(fields := row.split()) >= 3 and "shsm-" in fields[-1]
    }


def _shsm_residuals() -> list[Path]:
    return sorted(Path("/tmp").glob("shsm-*"))


def _registration_state(identity: InstanceIdentity) -> str:
    """Classify only verified native status; never treat a query error as safe.

    Inconsistent ServiceManagement/launchd evidence stays UNKNOWN and cannot
    authorize either unregister or deletion.
    """
    try:
        status = _manager(identity, "status", "http-origin")
        pid = darwin_service._launchd_job_pid(identity.macos_label("core"))
    except Exception:
        return "unknown"
    if status in {"not-registered", "not-found"} and pid is None:
        return "inactive"
    if status in {"enabled", "requires-approval"}:
        return "registered"
    return "unknown"


def _nothing_registered(identity: InstanceIdentity) -> bool:
    return _registration_state(identity) == "inactive"


_INPUT_FILES = (
    "services.json",
    "shared-bridge-binding.json",
    "http-origin-environment.json",
)
_CandidateProof = tuple[Path, int, Path, dict[str, str]]


def _optional_versioned_core_source(default_source: Path) -> Path:
    """Accept only an explicit, private 0.25.0 development snapshot for this test."""
    raw = os.environ.get("SERVERFS_V0250_NATIVE_CORE_SOURCE")
    if raw is None:
        return default_source
    source = Path(raw)
    scratch_parent = default_source / "deployment/macos"
    if (
        not source.is_absolute()
        or source.parent != scratch_parent
        or re.fullmatch(r"\.serverfs-v0250-candidate-[a-f0-9]{12}", source.name) is None
        or source.is_symlink()
        or source.resolve(strict=True) != source
        or not source.is_dir()
    ):
        raise RuntimeError("native Core source must be a canonical test-owned candidate snapshot")
    info = source.stat(follow_symlinks=False)
    if (
        info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
        or (source / ".git").exists()
        or (source / ".git").is_symlink()
    ):
        raise RuntimeError("native Core candidate snapshot ownership is unsafe")
    try:
        metadata = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))
        version = metadata["project"]["version"]
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError("native Core candidate metadata unavailable") from exc
    if version != "0.25.0" or not (source / "uv.lock").is_file():
        raise RuntimeError("native Core candidate version or lock does not match 0.25.0")
    return source


def _verify_versioned_core_distribution(python: Path) -> None:
    """Prove the signed Core's pinned physical interpreter imports installed 0.25.0."""
    script = (
        "import importlib.metadata, json, pathlib, sys; "
        "import serverfs_mcp.cli, serverfs_mcp.darwin_shared_binding_private; "
        "root=pathlib.Path(sys.prefix).resolve(strict=True); "
        "modules=[serverfs_mcp.cli, serverfs_mcp.darwin_shared_binding_private]; "
        "assert importlib.metadata.version('serverfs-mcp') == '0.25.0'; "
        "assert all(pathlib.Path(m.__file__).resolve(strict=True).is_relative_to(root) "
        "for m in modules); "
        "print(json.dumps({'version':'0.25.0', 'installed': True}))"
    )
    result = subprocess.run(
        [str(python), "-I", "-B", "-c", script],
        cwd=python.parent.parent,
        env={"HOME": str(Path.home()), "PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    if result.returncode or result.stdout.strip() != '{"version": "0.25.0", "installed": true}':
        raise RuntimeError("signed native Core lacks installed 0.25.0 distribution provenance")


def _optional_versioned_bridge_python(project: Path) -> Path | None:
    """Allow only an isolated installed 0.25.0 Bridge for the native test owner."""
    raw = os.environ.get("SERVERFS_V0250_NATIVE_BRIDGE_PYTHON")
    if raw is None:
        return None
    python = Path(raw)
    venv = python.parent.parent
    if (
        not python.is_absolute()
        or python.name != "python"
        or python.parent.name != "bin"
        or venv.name != "bridge-venv"
        or venv.parent.parent != project / "deployment/macos"
        or re.fullmatch(r"\.serverfs-v0250-candidate-[a-f0-9]{12}", venv.parent.name) is None
        or venv.is_symlink()
        or not python.is_file()
    ):
        raise RuntimeError("native Bridge requires an isolated candidate wheelhouse venv")
    parent_info = venv.parent.stat(follow_symlinks=False)
    venv_info = venv.stat(follow_symlinks=False)
    if (
        parent_info.st_uid != os.getuid()
        or stat.S_IMODE(parent_info.st_mode) != 0o700
        or venv_info.st_uid != os.getuid()
        or bool(venv_info.st_mode & 0o022)
        or venv.parent.is_symlink()
    ):
        raise RuntimeError("native Bridge wheelhouse owner or mode is unsafe")
    script = (
        "import importlib.metadata, json, pathlib, sys; "
        "import serverfs_agent_bridge.main, serverfs_agent_bridge.core_grants_private; "
        "root=pathlib.Path(sys.prefix).resolve(strict=True); "
        "assert root == pathlib.Path(sys.argv[1]).resolve(strict=True); "
        "assert importlib.metadata.version('serverfs-agent-bridge') == '0.25.0'; "
        "assert all(pathlib.Path(m.__file__).resolve(strict=True).is_relative_to(root) "
        "for m in [serverfs_agent_bridge.main, serverfs_agent_bridge.core_grants_private]); "
        "print(json.dumps({'version':'0.25.0', 'installed': True}))"
    )
    result = subprocess.run(
        [str(python), "-I", "-B", "-c", script, str(venv)],
        cwd=venv.parent,
        env={"HOME": str(Path.home()), "PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    if result.returncode or result.stdout.strip() != '{"version": "0.25.0", "installed": true}':
        raise RuntimeError("native Bridge does not import the isolated 0.25.0 wheel")
    return python


def _require_single_versioned_source(
    checkout: Path, core_source: Path, bridge_python: Path | None
) -> None:
    """A joint versioned native test must not silently mix release inputs."""
    if bridge_python is not None and (
        core_source == checkout or bridge_python.parent.parent.parent != core_source
    ):
        raise RuntimeError("native Core and Bridge must share one versioned source snapshot")


def _start_test_owner(
    stack: ExitStack,
    source: Path,
    root: Path,
    home: Path,
    config: Path,
    serial: int,
    installed_python: Path | None,
) -> tuple[subprocess.Popen[bytes], Path]:
    if installed_python is None:
        return _start_source_owner(stack, source, root, home, config, serial)
    log = root / f"bridge-{serial}.stderr"
    stream = stack.enter_context(log.open("wb"))
    child = subprocess.Popen(
        [
            str(installed_python),
            "-I",
            "-B",
            "-m",
            "serverfs_agent_bridge.main",
            "--config",
            str(config),
        ],
        cwd=root,
        env={"HOME": str(home), "PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=stream,
        start_new_session=True,
    )
    stack.callback(_stop, child)
    return child, log


def _installed_historical_bridge_pair(source: Path, scratch: Path, uv: str) -> tuple[Path, Path]:
    """Build exact historical and candidate wheel inputs, never a mutable checkout."""
    baseline = "fd1019498b281cdeb8c5805242f1a71222f5eebd"
    candidate = os.environ["SERVERFS_V27_SMAPP_CANDIDATE_SHA"]
    assert re.fullmatch(r"[0-9a-f]{40}", candidate)
    repo = source.parent
    env = {
        "HOME": str(Path.home()),
        "PATH": os.defpath,
        "UV_OFFLINE": "1",
        "UV_LINK_MODE": "copy",
        "PYTHONDONTWRITEBYTECODE": "1",
    }

    def run(*args: str, cwd: Path) -> str:
        result = subprocess.run(
            list(args),
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        assert result.returncode == 0, "exact installed Bridge build/probe failed"
        return result.stdout.strip()

    assert run("git", "rev-parse", "HEAD", cwd=repo) == candidate
    outputs = []
    for name, tree in (
        ("historical", f"{baseline}:agent_bridge"),
        ("candidate", f"{candidate}:macos/agent_bridge"),
    ):
        target = scratch / name
        target.mkdir(mode=0o700)
        archive = target / "source.tar"
        run("git", "archive", "--output", str(archive), tree, cwd=repo)
        project = target / "source"
        project.mkdir(mode=0o700)
        with tarfile.open(archive) as stream:
            assert all(m.isfile() or m.isdir() for m in stream.getmembers())
            stream.extractall(project, filter="data")
        wheels = target / "wheels"
        requirements = target / "requirements.txt"
        venv = target / "venv"
        run(uv, "build", "--offline", "--wheel", "--out-dir", str(wheels), cwd=project)
        built = list(wheels.glob("serverfs_agent_bridge-*.whl"))
        assert len(built) == 1
        wheel = built[0]
        run(
            uv,
            "export",
            "--offline",
            "--locked",
            "--no-dev",
            "--no-emit-project",
            "--output-file",
            str(requirements),
            cwd=project,
        )
        run(
            uv,
            "venv",
            "--offline",
            "--no-project",
            "--python",
            str(Path(sys._base_executable).resolve(strict=True)),
            str(venv),
            cwd=project,
        )
        python = venv / "bin/python"
        run(uv, "pip", "sync", "--offline", "--python", str(python), str(requirements), cwd=project)
        run(
            uv,
            "pip",
            "install",
            "--offline",
            "--no-deps",
            "--python",
            str(python),
            str(wheel),
            cwd=project,
        )
        packages = Path(
            run(
                str(python),
                "-I",
                "-B",
                "-c",
                "import pathlib,sys,sysconfig,serverfs_agent_bridge as b;"
                "assert pathlib.Path(b.__file__).resolve().is_relative_to("
                "pathlib.Path(sys.prefix).resolve());"
                "print(sysconfig.get_path('purelib'))",
                cwd=target,
            )
        )
        assert packages.resolve(strict=True).is_relative_to(venv.resolve(strict=True))
        with zipfile.ZipFile(wheel) as bundle:
            assert bundle.testzip() is None
            members = [
                n
                for n in bundle.namelist()
                if n.startswith("serverfs_agent_bridge/") and not n.endswith("/")
            ]
            assert members
            for member in members:
                assert (packages / member).read_bytes() == bundle.read(member)
        print(f"SMAPP_BRIDGE_WHEEL {name} {hashlib.sha256(wheel.read_bytes()).hexdigest()}")
        outputs.append(python)
    return outputs[0], outputs[1]


def _pin_test_candidate(
    identity: InstanceIdentity, config: Path, port: int, python: Path
) -> _CandidateProof:
    """Pin complete test-owned state before any OS registration is attempted."""
    root = instance_service_home(identity)
    fingerprints: dict[str, str] = {}
    for name in _INPUT_FILES:
        file = root / name
        fingerprints[name] = hashlib.sha256(file.read_bytes()).hexdigest()
    proof = (config, port, python, fingerprints)
    if not _verified_test_manifest(identity, proof):
        raise RuntimeError("fresh Shared Core staging evidence is untrusted")
    return proof


def _verified_test_manifest(identity: InstanceIdentity, proof: _CandidateProof | None) -> bool:
    """Verify exact saved candidate bytes plus full Shared Core launch contract."""
    if proof is None:
        return False
    root = instance_service_home(identity)
    parent = root.parent
    if (
        not root.is_dir()
        or root.is_symlink()
        or parent.is_symlink()
        or parent.name != identity.instance_id
        or parent.parent.name != "instances"
    ):
        return False
    config, port, python, fingerprints = proof
    if set(fingerprints) != set(_INPUT_FILES):
        return False
    try:
        for name in _INPUT_FILES:
            file = root / name
            info = file.stat(follow_symlinks=False)
            if (
                file.is_symlink()
                or not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or hashlib.sha256(file.read_bytes()).hexdigest() != fingerprints[name]
            ):
                return False
        alias, _socket = resolve_shared_core_manifest(
            identity,
            config,
            port,
            expected_executable=python,
            environ={
                "SERVERFS_MACOS_INSTANCE_ID": identity.instance_id,
                "SERVERFS_DATA_HOME": str(root / "data"),
                "SERVERFS_FILE_INGRESS_ENABLED": "false",
            },
        )
        return bool(alias)
    except Exception:
        return False


def _verified_shared_core_pid(
    identity: InstanceIdentity, proof: _CandidateProof | None, pid: int
) -> bool:
    """Authorize a signal only for the exact active, signed random Core B job."""
    if (
        proof is None
        or re.fullmatch(r"shsm-b-[a-f0-9]{10}", identity.instance_id) is None
        or type(pid) is not int
        or pid <= 1
        or not _verified_test_manifest(identity, proof)
        or _registration_state(identity) != "registered"
    ):
        return False
    label = identity.macos_label("core")
    if not label.startswith("com.ntlx.serverfs.") or label != identity.macos_label("core"):
        return False
    try:
        launchd = subprocess.run(
            ["launchctl", "list"], capture_output=True, text=True, timeout=10, check=False
        )
        rows = [row.split() for row in launchd.stdout.splitlines() if row.split()[-1:] == [label]]
        if launchd.returncode or len(rows) != 1 or len(rows[0]) < 3 or rows[0][0] != str(pid):
            return False
        if darwin_service._launchd_job_pid(label) != pid:
            return False
        root = instance_service_home(identity)
        manifest = json.loads((root / "services.json").read_text(encoding="utf-8"))
        config, port, python, _fingerprints = proof
        service = manifest["services"]["http-origin"]
        expected_argv = [str(python), *service["arguments"]]
        if (
            manifest.get("owner") != "v0.25-named-shared-core"
            or manifest.get("instance_id") != identity.instance_id
            or manifest.get("bridge_mode") != "shared"
            or manifest.get("bridge_id") != identity.bridge_id
            or Path(manifest["core"]["config"]) != config.resolve(strict=True)
            or manifest["core"]["port"] != port
            or service["executable"] != str(python)
            or service["arguments"]
            != [
                "-B",
                "-m",
                "serverfs_mcp.cli",
                "serve",
                "--transport",
                "streamable-http",
                "--config",
                str(config.resolve(strict=True)),
                "--http-port",
                str(port),
                "--macos-instance",
                identity.instance_id,
                "--macos-shared-bridge",
                identity.bridge_id,
            ]
        ):
            return False
        app = root / APP_NAME
        plist_path = app / "Contents/Library/LaunchAgents" / f"{label}.plist"
        with plist_path.open("rb") as stream:
            launch_spec = plistlib.load(stream)
        info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
        signed = subprocess.run(
            ["codesign", "-dv", "--verbose=4", str(app)],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        identity_line = next(
            (
                line.split("=", 1)[1]
                for line in signed.stderr.splitlines()
                if line.startswith("Identifier=")
            ),
            None,
        )
        signature_check = subprocess.run(
            ["codesign", "--verify", "--deep", "--strict", str(app)],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if (
            launch_spec.get("Label") != label
            or launch_spec.get("KeepAlive") is not True
            or launch_spec.get("ProgramArguments")
            != ["serverfs-service-launcher", identity.instance_id, "http-origin"]
            or signed.returncode != 0
            or signature_check.returncode != 0
            or not identity_line
            or identity_line != info.get("CFBundleIdentifier")
        ):
            return False
        process = subprocess.run(
            ["ps", "-p", str(pid), "-o", "pid=,uid=,command="],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        fields = process.stdout.strip().split(None, 2)
        return (
            process.returncode == 0
            and len(fields) == 3
            and fields[0] == str(pid)
            and fields[1] == str(os.getuid())
            and shlex.split(fields[2]) == expected_argv
        )
    except (OSError, ValueError, KeyError, TypeError, IndexError, subprocess.SubprocessError):
        return False


async def _wait_authenticated_core_http_ready(
    identity: InstanceIdentity,
    pid: int,
    port: int,
    token: str,
    deadline: float,
    state_is_stable: Callable[[], bool],
) -> None:
    """Wait for authenticated local MCP readiness without extending recovery deadline."""
    while time.monotonic() < deadline:
        if darwin_service._launchd_job_pid(identity.macos_label("core")) != pid:
            raise RuntimeError("Core launchd PID changed before authenticated HTTP readiness")
        if not state_is_stable():
            raise RuntimeError("peer, owner, or protected process changed before HTTP readiness")
        try:
            response = await _mcp_call(port, token, "list_workdirs", {})
        except Exception:
            response = None
        if response is not None:
            if response.is_error:
                raise RuntimeError("authenticated Core MCP readiness returned an error")
            if response.structured_content is not None:
                return
        time.sleep(0.5)
    raise TimeoutError("authenticated Core MCP did not become ready within recovery deadline")


async def _wait_fixture_port_release(
    identity: InstanceIdentity, port: int, *, timeout: float = 120.0
) -> None:
    """Wait for the real conservative bind gate; never reuse an occupied port.

    Darwin can reject a plain bind after the previous TCP connection closed.
    SO_REUSEADDR is not a safe substitute: a wildcard listener can coexist
    with a more specific bind. Only the normal product preflight may pass.
    """
    started = time.monotonic()
    assert _nothing_registered(identity)
    while time.monotonic() - started < timeout:
        if darwin_service._loopback_port_available(port):
            assert _nothing_registered(identity) and _no_core_child(identity)
            print(
                f"SMAPP_CORE_PORT_RELEASE_PASS wait_ms={int((time.monotonic() - started) * 1000)}"
            )
            return
        await asyncio.sleep(0.5)
    pytest.fail("test Core port did not become available after unregister")


def _no_core_child(identity: InstanceIdentity) -> bool:
    result = subprocess.run(
        ["ps", "-axo", "pid=,command="],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return result.returncode == 0 and not any(
        f"--macos-instance {identity.instance_id}" in line for line in result.stdout.splitlines()
    )


def _drop_only_test_owner(
    identity: InstanceIdentity, expected_owner: Path, proof: _CandidateProof
) -> None:
    """Remove exactly a freshly-created test identity AFTER native deregistration.

    A real native Core has runtime logs and may write task-local data, so the
    ordinary shared-unstage must retain it. Only this explicitly opt-in test
    can dispose of its own fresh, proven-inactive random state.
    """
    root = instance_service_home(identity)
    if (
        root.parent != expected_owner
        or root.parent.name != identity.instance_id
        or root.parent.parent.name != "instances"
        or not root.is_dir()
        or root.is_symlink()
        or expected_owner.is_symlink()
        or not _verified_test_manifest(identity, proof)
        or not _nothing_registered(identity)
        or not _no_core_child(identity)
    ):
        raise RuntimeError("test Shared Core state not proven inactive; retain it")
    manifest = root / "services.json"
    doc = json.loads(manifest.read_text(encoding="utf-8"))
    if (
        doc.get("owner") != "v0.25-named-shared-core"
        or doc.get("instance_id") != identity.instance_id
        or doc.get("bridge_mode") != "shared"
        or doc.get("bridge_id") != identity.bridge_id
        or {p.name for p in expected_owner.iterdir()} != {"service-manager"}
    ):
        raise RuntimeError("test Core removal refused unknown owner")
    if stat.S_IMODE(expected_owner.stat().st_mode) != 0o700:
        raise RuntimeError("test Core removal refused untrusted directory mode")
    shutil.rmtree(expected_owner)


def _signed_preflight(identity: InstanceIdentity) -> None:
    app = instance_service_home(identity) / APP_NAME
    manager = app / "Contents/MacOS/serverfs-service-manager"
    launcher = app / "Contents/Resources/serverfs-service-launcher"
    for cmd in (
        ["codesign", "--verify", "--deep", "--strict", str(app)],
        [str(launcher), "--validate-only", identity.instance_id, "http-origin"],
        [str(manager), "register-preflight", "http-origin"],
    ):
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError("native signed Shared Core registration preflight failed")


def test_two_real_smappservice_shared_cores_keep_owner_alive() -> None:
    source = Path(__file__).resolve().parents[1]
    before = _protected_launchd_snapshot()
    if not before or not any(pid is not None for pid, _status in before.values()):
        pytest.fail("no active ServerFS launchd baseline for shared Core acceptance")
    if _active_shsm_labels() or _shsm_residuals():
        pytest.fail("pre-existing shsm test launchd jobs or scratch paths require investigation")

    suffix = secrets.token_hex(5)
    ids = (
        InstanceIdentity(f"shsm-a-{suffix}", "shared", "shsm-owner"),
        InstanceIdentity(f"shsm-b-{suffix}", "shared", "shsm-owner"),
    )
    test_labels = frozenset(item.macos_label("core") for item in ids)
    assert not test_labels.intersection(before)

    def protected_snapshot() -> dict[str, tuple[int | None, int]]:
        return _protected_launchd_snapshot(test_labels)

    owners = [instance_service_home(item).parent for item in ids]
    assert all(not path.exists() and not path.is_symlink() for path in owners)
    scratch = Path(tempfile.mkdtemp(prefix=f"shsm-{suffix}-", dir="/tmp")).resolve(strict=True)
    scratch.chmod(0o700)
    owner_config = scratch / "bridge.json"
    owner_socket = scratch / "bridge.sock"
    state = scratch / "bridge-state"
    (scratch / "home").mkdir(mode=0o700)
    project = scratch / "workspace"
    project.mkdir(mode=0o700)
    work = scratch / "run-times"
    work.mkdir(mode=0o700)
    accepted = False
    cleanup_errors: list[str] = []
    registration_attempted: set[str] = set()
    pinned_candidates: dict[str, _CandidateProof] = {}

    try:
        uv_binary = shutil.which("uv")
        if uv_binary is None:
            raise RuntimeError("offline runtime installer requires installed uv")
        core_source = _optional_versioned_core_source(source)
        bridge_python = _optional_versioned_bridge_python(source)
        _require_single_versioned_source(source, core_source, bridge_python)
        historical = os.environ.get("SERVERFS_V27_HISTORICAL_SMAPP_BRIDGE_ROLLBACK") == "1"
        first_bridge_python = bridge_python
        if historical:
            assert core_source == source and bridge_python is None
            first_bridge_python, bridge_python = _installed_historical_bridge_pair(
                source, scratch, str(Path(uv_binary).resolve(strict=True))
            )
        provisioned = create_shared_core_runtime(
            work / "shared-python",
            core_source,
            Path(sys.executable),
            Path(uv_binary).resolve(strict=True),
        )
        python = Path(provisioned["python"])
        if core_source != source:
            _verify_versioned_core_distribution(python)
        build = subprocess.run(
            [
                "bash",
                str(core_source / "native/macos/service_manager/build.sh"),
                str(scratch / "native-build"),
            ],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if build.returncode != 0:
            raise RuntimeError("isolated signed native app build failed")
        template = scratch / "native-build" / APP_NAME
        core_coexistence = os.environ.get("SERVERFS_V27_HISTORICAL_SMAPP_CORE_COEXISTENCE") == "1"
        core_runtimes = (python, python)
        core_templates = (template, template)
        if core_coexistence:
            # Staging is create-only: prove side-by-side old/new installed
            # Cores, not an unsupported in-place replacement.
            assert not historical and core_source == source and bridge_python is None
            expected = os.environ["SERVERFS_V27_SMAPP_CANDIDATE_SHA"]
            assert re.fullmatch(r"[0-9a-f]{40}", expected)
            assert (
                subprocess.check_output(
                    ["git", "-C", str(source.parent), "rev-parse", "HEAD"],
                    text=True,
                    timeout=20,
                ).strip()
                == expected
            )
            baseline = "fd1019498b281cdeb8c5805242f1a71222f5eebd"
            archive = scratch / "historical-core.tar"
            subprocess.run(
                ["git", "-C", str(source.parent), "archive", "--output", str(archive), baseline],
                check=True,
                timeout=90,
            )
            old_source = scratch / "historical-core-source"
            old_source.mkdir(mode=0o700)
            with tarfile.open(archive) as bundle:
                assert all(m.isfile() or m.isdir() for m in bundle.getmembers())
                bundle.extractall(old_source, filter="data")
            old_runtime = work / "historical-python"
            old = create_shared_core_runtime(
                old_runtime,
                old_source,
                Path(sys.executable),
                Path(uv_binary).resolve(strict=True),
            )
            from serverfs_mcp.darwin_shared_runtime import verify_shared_core_runtime

            for label, target, origin in (
                ("historical", old_runtime, old_source),
                ("candidate", work / "shared-python", source),
            ):
                verified = verify_shared_core_runtime(
                    target, project_root=origin, require_site_packages=True
                )
                assert verified["state"] == "verified"
                print(f"SMAPP_CORE_WHEEL {label} {verified['wheel_sha256']}", flush=True)
            old_build = scratch / "historical-native-build"
            subprocess.run(
                ["bash", str(old_source / "native/macos/service_manager/build.sh"), str(old_build)],
                capture_output=True,
                check=True,
                timeout=180,
            )
            core_runtimes = (Path(old["python"]), python)
            core_templates = (old_build / APP_NAME, template)
            assert core_runtimes[0] != core_runtimes[1]

        # Synthetic independent owner, not a production Bridge or native
        # ServiceManagement registration. No provider network calls.
        _private_json(
            owner_config,
            {
                "protocol_mode": "v2",
                "lease_key": "alias",
                "bridge_id": "shsm-owner",
                "socket_path": str(owner_socket),
                "state_dir": str(state),
                "lock_dir": str(scratch / "bridge-locks"),
                "allowed_peer_uid": os.getuid(),
                "enable_fake_runtime": True,
                "workdirs": [
                    {
                        "alias": "Workspace",
                        "host_path": str(project),
                        "read_only": False,
                        "agent_mode": "workspace-write",
                        "agent_runtimes": ["fake"],
                    }
                ],
            },
        )
        from serverfs_agent_bridge.config import BridgeConfig
        from serverfs_agent_bridge.core_grants import core_workdir_policy_digest
        from serverfs_agent_bridge.core_grants_private import provision_private_core_grants
        from serverfs_agent_bridge.store import TaskStore

        config = BridgeConfig.load(owner_config)
        from serverfs_mcp.lease_identity import alias_lease_id

        assert config.policies.get("Workspace").lease_id == alias_lease_id("Workspace")
        digest = core_workdir_policy_digest(config.policies.get("Workspace"))
        keys = (secrets.token_bytes(32), secrets.token_bytes(32))
        provision_private_core_grants(
            store=TaskStore(state),
            bridge_id="shsm-owner",
            policies=config.policies,
            document={
                "schema_version": 1,
                "bridge_id": "shsm-owner",
                "grants": [
                    {
                        "core_id": identity.instance_id,
                        "key_id": f"key-{identity.instance_id}",
                        "secret_hex": key.hex(),
                        "methods": ["bridge.ping", "task.submit", "task.get", "task.events"],
                        "workdirs": [{"alias": "Workspace", "policy_sha256": digest}],
                    }
                    for identity, key in zip(ids, keys, strict=True)
                ],
            },
        )
        ports = _port(), _port()
        while ports[0] == ports[1]:
            ports = _port(), _port()
        tokens = (secrets.token_hex(20), secrets.token_hex(20))
        direct = [
            CoreBridgeClientV2(
                owner_socket,
                bridge_id="shsm-owner",
                core_id=identity.instance_id,
                key_id=f"key-{identity.instance_id}",
                secret=key,
                workdir_alias="Workspace",
                policy_revision=digest,
                timeout_seconds=4,
            )
            for identity, key in zip(ids, keys, strict=True)
        ]

        async def exercise() -> None:
            with ExitStack() as stack:
                bridge, stderr = _start_test_owner(
                    stack, source, scratch, scratch / "home", owner_config, 1, first_bridge_python
                )
                await _wait_owner(bridge, stderr, direct[0])
                bridge_pid = bridge.pid

                for i, identity in enumerate(ids):
                    core_config, _ = _prepare_core(
                        root=scratch,
                        home=Path.home(),
                        identity=identity,
                        bridge_config=owner_config,
                        bridge_socket=owner_socket,
                        policy_digest=digest,
                        workspace=project,
                        template_app=core_templates[i],
                        python=core_runtimes[i],
                        port=ports[i],
                        token=tokens[i],
                        secret=keys[i],
                    )
                    pinned_candidates[identity.instance_id] = _pin_test_candidate(
                        identity, core_config, ports[i], core_runtimes[i]
                    )
                    _signed_preflight(identity)
                    assert protected_snapshot() == before
                    assert _nothing_registered(identity)
                    registration_attempted.add(identity.instance_id)
                    await asyncio.to_thread(start_shared_core, identity, core_config, ports[i])
                    assert protected_snapshot() == before
                    assert bridge.poll() is None and bridge.pid == bridge_pid

                for i in (0, 1):
                    assert await asyncio.to_thread(_http_status, ports[i]) == 401
                    assert (
                        await asyncio.to_thread(_http_status, ports[i], token=tokens[1 - i]) == 401
                    )
                    result = await _mcp_call(ports[i], tokens[i], "list_workdirs", {})
                    assert {wd["alias"] for wd in result.structured_content["workdirs"]} == {
                        "Workspace"
                    }

                if core_coexistence:
                    old_pid, new_pid = (
                        darwin_service._launchd_job_pid(identity.macos_label("core"))
                        for identity in ids
                    )
                    assert old_pid is not None and new_pid is not None and old_pid != new_pid
                    # The historical defect remains reproducible; never modify
                    # the old wheel to make the new writer test appear green.
                    refused = await _mcp_call(
                        ports[0],
                        tokens[0],
                        "create_text_file",
                        {
                            "workdir": "Workspace",
                            "path": "old-core.txt",
                            "content": "must-not-exist",
                        },
                    )
                    assert refused.is_error and "AGENT_LOCK_UNAVAILABLE" in str(refused.content)
                    assert not (project / "old-core.txt").exists()
                    import fcntl

                    from serverfs_mcp.lease_identity import guard_artifact_name, lock_artifact_name

                    lease_id = alias_lease_id("Workspace")
                    lock_dir = scratch / "bridge-locks"
                    artifact = lock_dir / lock_artifact_name(lease_id)
                    guard = lock_dir / "active" / guard_artifact_name(lease_id)
                    original_identity = (artifact.stat().st_dev, artifact.stat().st_ino)
                    request = {
                        "workdir": "Workspace",
                        "path": "candidate-core.txt",
                        "content": "candidate-core-persistent-data",
                    }
                    with artifact.open("rb") as held:
                        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        busy = await _mcp_call(ports[1], tokens[1], "create_text_file", request)
                        assert busy.is_error and "WORKDIR_BUSY" in str(busy.content)
                    assert not (project / "candidate-core.txt").exists()
                    assert not guard.exists()
                    _private_json(guard, {"fixture": "unresolved"})
                    try:
                        refused = await _mcp_call(ports[1], tokens[1], "create_text_file", request)
                        assert refused.is_error
                        assert "WORKDIR_RECOVERY_REQUIRED" in str(refused.content)
                        assert not (project / "candidate-core.txt").exists()
                    finally:
                        guard.unlink()
                    written = await _mcp_call(ports[1], tokens[1], "create_text_file", request)
                    assert not written.is_error, str(written.content)
                    assert (artifact.stat().st_dev, artifact.stat().st_ino) == original_identity
                    observed = await _mcp_call(
                        ports[0],
                        tokens[0],
                        "read_text_file",
                        {"workdir": "Workspace", "path": "candidate-core.txt"},
                    )
                    assert not observed.is_error
                    assert "candidate-core-persistent-data" in str(observed.content)
                    # The historical Core may restart without affecting the
                    # candidate Core or the shared owner. No app replacement.
                    await asyncio.to_thread(stop_shared_core, ids[0])
                    assert _nothing_registered(ids[0])
                    assert darwin_service._launchd_job_pid(ids[1].macos_label("core")) == new_pid
                    assert bridge.poll() is None and bridge.pid == bridge_pid
                    await _wait_fixture_port_release(ids[0], ports[0])
                    assert protected_snapshot() == before
                    old_config = scratch / f"input-{ids[0].instance_id}" / "core.toml"
                    await asyncio.to_thread(start_shared_core, ids[0], old_config, ports[0])
                    restarted = darwin_service._launchd_job_pid(ids[0].macos_label("core"))
                    assert restarted is not None and restarted not in {old_pid, new_pid}
                    await _wait_authenticated_core_http_ready(
                        ids[0],
                        restarted,
                        ports[0],
                        tokens[0],
                        time.monotonic() + 40,
                        lambda: (
                            protected_snapshot() == before
                            and bridge.poll() is None
                            and darwin_service._launchd_job_pid(ids[1].macos_label("core"))
                            == new_pid
                        ),
                    )
                    observed = await _mcp_call(
                        ports[0],
                        tokens[0],
                        "read_text_file",
                        {"workdir": "Workspace", "path": "candidate-core.txt"},
                    )
                    assert not observed.is_error
                    assert "candidate-core-persistent-data" in str(observed.content)
                    print("SMAPP_SHARED_WRITER_FIX_PASS", flush=True)
                    print("SMAPP_HISTORICAL_CORE_COEXISTENCE_PASS", flush=True)

                submitted = await direct[0].call(
                    "task.submit",
                    {
                        "runtime": "fake",
                        "path": ".",
                        "profile": "review",
                        "prompt": "synthetic native login item task",
                    },
                )
                task = submitted["task_id"]
                own = await _mcp_call(ports[0], tokens[0], "get_agent_task", {"task_id": task})
                assert not own.is_error
                foreign = await _mcp_call(ports[1], tokens[1], "get_agent_task", {"task_id": task})
                assert foreign.is_error

                async def retained_marker(client, marker):
                    result = await client.call(
                        "task.submit",
                        {
                            "runtime": "fake",
                            "path": ".",
                            "profile": "review",
                            "prompt": f"complete:{marker}",
                        },
                    )
                    task_id = result["task_id"]
                    for _ in range(100):
                        row = await client.call("task.get", {"task_id": task_id})
                        if row["status"] == "succeeded":
                            assert row["final_response"] == marker
                            return task_id
                        assert row["status"] in {"queued", "running"}
                        await asyncio.sleep(0.05)
                    pytest.fail("installed historical Bridge marker did not finish")

                if historical:
                    history_pids = tuple(
                        darwin_service._launchd_job_pid(identity.macos_label("core"))
                        for identity in ids
                    )
                    assert all(pid is not None for pid in history_pids)
                    state_identity = (state.stat().st_dev, state.stat().st_ino)
                    owner_bytes = owner_config.read_bytes()
                    grant_file = state / "core-grants.json"
                    grant_bytes = grant_file.read_bytes()
                    historical_task = await retained_marker(direct[0], "historical-bridge-result")

                # The synthetic Bridge owner can restart independently while
                # both real signed Shared Core Login Items remain registered.
                _stop(bridge)
                assert bridge.poll() is not None
                assert all(_registration_state(identity) == "registered" for identity in ids)
                for i in (0, 1):
                    still_live = await _mcp_call(ports[i], tokens[i], "list_workdirs", {})
                    assert still_live.structured_content is not None
                    with pytest.raises(CoreBridgeUnavailable):
                        await direct[i].call("bridge.ping", {})

                if core_coexistence:
                    request = {
                        "workdir": "Workspace",
                        "path": "after-owner-restart.txt",
                        "content": "fresh-live-proof",
                    }
                    offline = await _mcp_call(ports[1], tokens[1], "create_text_file", request)
                    assert offline.is_error and "AGENT_LOCK_UNAVAILABLE" in str(offline.content)
                    assert not (project / request["path"]).exists()

                bridge_next, stderr_next = _start_test_owner(
                    stack, source, scratch, scratch / "home", owner_config, 2, bridge_python
                )
                await _wait_owner(bridge_next, stderr_next, direct[0])
                assert bridge_next.poll() is None and bridge_next.pid != bridge_pid
                assert all(_registration_state(identity) == "registered" for identity in ids)
                if core_coexistence:
                    assert darwin_service._launchd_job_pid(ids[1].macos_label("core")) == new_pid
                    resumed = await _mcp_call(ports[1], tokens[1], "create_text_file", request)
                    assert not resumed.is_error
                    assert (project / request["path"]).read_text() == request["content"]
                    print("SMAPP_SHARED_WRITER_OWNER_RESTART_PASS", flush=True)
                restarted_tasks = []
                for i in (0, 1):
                    assert await direct[i].call("bridge.ping", {}) == {"status": "ok"}
                    submitted = await direct[i].call(
                        "task.submit",
                        {
                            "runtime": "fake",
                            "path": ".",
                            "profile": "review",
                            "prompt": f"synthetic-restarted-{i}",
                        },
                    )
                    restarted_tasks.append(submitted["task_id"])
                    restored = await _mcp_call(
                        ports[i],
                        tokens[i],
                        "get_agent_task",
                        {"task_id": restarted_tasks[i]},
                    )
                    assert not restored.is_error
                    assert restored.structured_content["task_id"] == restarted_tasks[i]
                for i in (0, 1):
                    foreign = await _mcp_call(
                        ports[1 - i],
                        tokens[1 - i],
                        "get_agent_task",
                        {"task_id": restarted_tasks[i]},
                    )
                    assert foreign.is_error

                if historical:
                    old = await direct[0].call("task.get", {"task_id": historical_task})
                    assert old["status"] == "succeeded"
                    assert old["final_response"] == "historical-bridge-result"
                    candidate_task = await retained_marker(direct[0], "candidate-bridge-result")
                    assert bridge_next.pid != bridge_pid
                    candidate_pid = bridge_next.pid
                    _stop(bridge_next)
                    assert bridge_next.poll() is not None
                    bridge_next, stderr_next = _start_test_owner(
                        stack,
                        source,
                        scratch,
                        scratch / "home",
                        owner_config,
                        20,
                        first_bridge_python,
                    )
                    await _wait_owner(bridge_next, stderr_next, direct[0])
                    assert bridge_next.pid not in {bridge_pid, candidate_pid}
                    for task_id, marker in (
                        (historical_task, "historical-bridge-result"),
                        (candidate_task, "candidate-bridge-result"),
                    ):
                        row = await direct[0].call("task.get", {"task_id": task_id})
                        assert row["status"] == "succeeded" and row["final_response"] == marker
                        owned = await _mcp_call(
                            ports[0], tokens[0], "get_agent_task", {"task_id": task_id}
                        )
                        assert not owned.is_error
                        assert owned.structured_content["final_response"] == marker
                        foreign = await _mcp_call(
                            ports[1], tokens[1], "get_agent_task", {"task_id": task_id}
                        )
                        assert foreign.is_error
                    assert (state.stat().st_dev, state.stat().st_ino) == state_identity
                    assert owner_config.read_bytes() == owner_bytes
                    assert grant_file.read_bytes() == grant_bytes
                    assert all(_registration_state(identity) == "registered" for identity in ids)
                    assert (
                        tuple(
                            darwin_service._launchd_job_pid(identity.macos_label("core"))
                            for identity in ids
                        )
                        == history_pids
                    )
                    assert protected_snapshot() == before
                    print("SMAPP_HISTORICAL_BRIDGE_ROLLBACK_PASS", flush=True)

                # 2026-11-16 diagnostic: revoke only Core B's owner grant and
                # prove both signed Core jobs and file HTTP remain live.
                core_pids = tuple(
                    darwin_service._launchd_job_pid(identity.macos_label("core"))
                    for identity in ids
                )
                assert all(pid is not None for pid in core_pids)
                _stop(bridge_next)
                assert bridge_next.poll() is not None
                provision_private_core_grants(
                    store=TaskStore(state),
                    bridge_id="shsm-owner",
                    policies=config.policies,
                    document={
                        "schema_version": 1,
                        "bridge_id": "shsm-owner",
                        "grants": [
                            {
                                "core_id": ids[0].instance_id,
                                "key_id": f"key-{ids[0].instance_id}",
                                "secret_hex": keys[0].hex(),
                                "methods": [
                                    "bridge.ping",
                                    "task.submit",
                                    "task.get",
                                    "task.events",
                                ],
                                "workdirs": [{"alias": "Workspace", "policy_sha256": digest}],
                            }
                        ],
                    },
                )
                bridge_revoked, stderr_revoked = _start_test_owner(
                    stack, source, scratch, scratch / "home", owner_config, 3, bridge_python
                )
                await _wait_owner(bridge_revoked, stderr_revoked, direct[0])
                assert bridge_revoked.poll() is None
                assert protected_snapshot() == before
                assert (
                    tuple(
                        darwin_service._launchd_job_pid(identity.macos_label("core"))
                        for identity in ids
                    )
                    == core_pids
                )
                assert await direct[0].call("bridge.ping", {}) == {"status": "ok"}
                with pytest.raises(CoreBridgeRemoteError) as revoked:
                    await direct[1].call("bridge.ping", {})
                assert revoked.value.code == "CORE_NOT_AUTHORIZED"
                revoked_http = await _mcp_call(
                    ports[1], tokens[1], "get_agent_task", {"task_id": restarted_tasks[1]}
                )
                assert revoked_http.is_error
                assert (
                    await _mcp_call(ports[1], tokens[1], "list_workdirs", {})
                ).structured_content is not None
                core_a_task = await _mcp_call(
                    ports[0], tokens[0], "get_agent_task", {"task_id": restarted_tasks[0]}
                )
                assert not core_a_task.is_error
                assert core_a_task.structured_content["task_id"] == restarted_tasks[0]
                assert (
                    tuple(
                        darwin_service._launchd_job_pid(identity.macos_label("core"))
                        for identity in ids
                    )
                    == core_pids
                )

                # Restore both original grants with their original generated
                # identities and keys; this is re-authorization, not key rotation.
                _stop(bridge_revoked)
                assert bridge_revoked.poll() is not None
                provision_private_core_grants(
                    store=TaskStore(state),
                    bridge_id="shsm-owner",
                    policies=config.policies,
                    document={
                        "schema_version": 1,
                        "bridge_id": "shsm-owner",
                        "grants": [
                            {
                                "core_id": identity.instance_id,
                                "key_id": f"key-{identity.instance_id}",
                                "secret_hex": key.hex(),
                                "methods": [
                                    "bridge.ping",
                                    "task.submit",
                                    "task.get",
                                    "task.events",
                                ],
                                "workdirs": [{"alias": "Workspace", "policy_sha256": digest}],
                            }
                            for identity, key in zip(ids, keys, strict=True)
                        ],
                    },
                )
                bridge_restored, stderr_restored = _start_test_owner(
                    stack, source, scratch, scratch / "home", owner_config, 4, bridge_python
                )
                await _wait_owner(bridge_restored, stderr_restored, direct[0])
                assert bridge_restored.poll() is None
                assert protected_snapshot() == before
                assert (
                    tuple(
                        darwin_service._launchd_job_pid(identity.macos_label("core"))
                        for identity in ids
                    )
                    == core_pids
                )
                for i in (0, 1):
                    assert await direct[i].call("bridge.ping", {}) == {"status": "ok"}
                    own_task = await _mcp_call(
                        ports[i],
                        tokens[i],
                        "get_agent_task",
                        {"task_id": restarted_tasks[i]},
                    )
                    assert not own_task.is_error
                    assert own_task.structured_content["task_id"] == restarted_tasks[i]
                    cross_task = await _mcp_call(
                        ports[1 - i],
                        tokens[1 - i],
                        "get_agent_task",
                        {"task_id": restarted_tasks[i]},
                    )
                    assert cross_task.is_error
                    assert (
                        await _mcp_call(ports[i], tokens[i], "list_workdirs", {})
                    ).structured_content is not None

                # Crash only the fully verified random Core B while its peer
                # and the independent Bridge owner remain live. Keep original
                # grant credentials in force for the entire recovery proof.
                core_a_pid = core_pids[0]
                core_b_pid = core_pids[1]
                bridge_pid_before_crash = bridge_restored.pid
                if core_a_pid is None or core_b_pid is None or bridge_pid_before_crash is None:
                    raise RuntimeError("native shared process PID baseline is incomplete")
                if not _verified_shared_core_pid(
                    ids[1], pinned_candidates[ids[1].instance_id], core_b_pid
                ):
                    raise RuntimeError("refusing SIGKILL: Core B ownership proof failed")
                if (
                    darwin_service._launchd_job_pid(ids[1].macos_label("core")) != core_b_pid
                    or protected_snapshot() != before
                    or bridge_restored.poll() is not None
                    or bridge_restored.pid != bridge_pid_before_crash
                    or darwin_service._launchd_job_pid(ids[0].macos_label("core")) != core_a_pid
                ):
                    raise RuntimeError("process state changed before Core B crash recovery")
                if not _verified_shared_core_pid(
                    ids[1], pinned_candidates[ids[1].instance_id], core_b_pid
                ):
                    raise RuntimeError("refusing SIGKILL: final Core B ownership proof failed")
                os.kill(core_b_pid, signal.SIGKILL)
                recovery_deadline = time.monotonic() + 75
                recovered_b_pid = None
                while time.monotonic() < recovery_deadline:
                    candidate = darwin_service._launchd_job_pid(ids[1].macos_label("core"))
                    if (
                        candidate is not None
                        and candidate != core_b_pid
                        and _verified_shared_core_pid(
                            ids[1], pinned_candidates[ids[1].instance_id], candidate
                        )
                    ):
                        try:
                            assert os.kill(core_b_pid, 0) is None
                        except ProcessLookupError:
                            recovered_b_pid = candidate
                            break
                    if (
                        protected_snapshot() != before
                        or bridge_restored.poll() is not None
                        or bridge_restored.pid != bridge_pid_before_crash
                        or darwin_service._launchd_job_pid(ids[0].macos_label("core")) != core_a_pid
                    ):
                        raise RuntimeError(
                            "peer or protected process changed during Core B recovery"
                        )
                    time.sleep(0.5)
                if recovered_b_pid is None:
                    raise AssertionError(
                        "launchd did not recover verified Core B within 75 seconds"
                    )

                def recovery_state_is_stable() -> bool:
                    return (
                        darwin_service._launchd_job_pid(ids[0].macos_label("core")) == core_a_pid
                        and bridge_restored.poll() is None
                        and bridge_restored.pid == bridge_pid_before_crash
                        and protected_snapshot() == before
                    )

                await _wait_authenticated_core_http_ready(
                    ids[1],
                    recovered_b_pid,
                    ports[1],
                    tokens[1],
                    recovery_deadline,
                    recovery_state_is_stable,
                )
                assert _verified_shared_core_pid(
                    ids[1], pinned_candidates[ids[1].instance_id], recovered_b_pid
                )
                recovered_tasks = await direct[1].call(
                    "task.submit",
                    {
                        "runtime": "fake",
                        "path": ".",
                        "profile": "review",
                        "prompt": "synthetic-after-core-b-sigkill-recovery",
                    },
                )
                recovered_task_id = recovered_tasks["task_id"]
                assert await direct[1].call("bridge.ping", {}) == {"status": "ok"}
                own_after_crash = await _mcp_call(
                    ports[1], tokens[1], "get_agent_task", {"task_id": recovered_task_id}
                )
                assert not own_after_crash.is_error
                assert own_after_crash.structured_content["task_id"] == recovered_task_id
                prior_b_task = await _mcp_call(
                    ports[1], tokens[1], "get_agent_task", {"task_id": restarted_tasks[1]}
                )
                assert not prior_b_task.is_error
                assert prior_b_task.structured_content["task_id"] == restarted_tasks[1]
                cross_after_crash = await _mcp_call(
                    ports[0], tokens[0], "get_agent_task", {"task_id": recovered_task_id}
                )
                assert cross_after_crash.is_error
                prior_b_cross_task = await _mcp_call(
                    ports[0], tokens[0], "get_agent_task", {"task_id": restarted_tasks[1]}
                )
                assert prior_b_cross_task.is_error
                assert await direct[0].call("bridge.ping", {}) == {"status": "ok"}
                assert _verified_shared_core_pid(
                    ids[1], pinned_candidates[ids[1].instance_id], recovered_b_pid
                )
                core_pids = (core_a_pid, recovered_b_pid)
                assert bridge_restored.poll() is None
                assert bridge_restored.pid == bridge_pid_before_crash
                assert protected_snapshot() == before

                # Rotate only Core B's OWNER grant while both signed Login
                # Items stay registered. The Core's private binding remains
                # pinned to its old key; this test must not rewrite it.
                rotated_key_id = f"rot-{secrets.token_hex(6)}"
                rotated_secret = secrets.token_bytes(32)
                _stop(bridge_restored)
                assert bridge_restored.poll() is not None
                provision_private_core_grants(
                    store=TaskStore(state),
                    bridge_id="shsm-owner",
                    policies=config.policies,
                    document={
                        "schema_version": 1,
                        "bridge_id": "shsm-owner",
                        "grants": [
                            {
                                "core_id": ids[0].instance_id,
                                "key_id": f"key-{ids[0].instance_id}",
                                "secret_hex": keys[0].hex(),
                                "methods": [
                                    "bridge.ping",
                                    "task.submit",
                                    "task.get",
                                    "task.events",
                                ],
                                "workdirs": [{"alias": "Workspace", "policy_sha256": digest}],
                            },
                            {
                                "core_id": ids[1].instance_id,
                                "key_id": rotated_key_id,
                                "secret_hex": rotated_secret.hex(),
                                "methods": [
                                    "bridge.ping",
                                    "task.submit",
                                    "task.get",
                                    "task.events",
                                ],
                                "workdirs": [{"alias": "Workspace", "policy_sha256": digest}],
                            },
                        ],
                    },
                )
                bridge_rotated, stderr_rotated = _start_test_owner(
                    stack, source, scratch, scratch / "home", owner_config, 5, bridge_python
                )
                await _wait_owner(bridge_rotated, stderr_rotated, direct[0])
                assert bridge_rotated.poll() is None
                assert protected_snapshot() == before
                assert all(_registration_state(identity) == "registered" for identity in ids)
                assert (
                    tuple(
                        darwin_service._launchd_job_pid(identity.macos_label("core"))
                        for identity in ids
                    )
                    == core_pids
                )
                assert await direct[0].call("bridge.ping", {}) == {"status": "ok"}

                # The native Core keeps using its old private identity, and
                # the original direct test signer does too; both fail closed.
                with pytest.raises(CoreBridgeRemoteError) as old_signer:
                    await direct[1].call("bridge.ping", {})
                assert old_signer.value.code == "CORE_NOT_AUTHORIZED"
                stale_private_task = await _mcp_call(
                    ports[1],
                    tokens[1],
                    "get_agent_task",
                    {"task_id": restarted_tasks[1]},
                )
                assert stale_private_task.is_error
                assert "CORE_NOT_AUTHORIZED" in str(stale_private_task)

                # Local filesystem MCP is independent of Bridge grant rotation.
                assert (
                    await _mcp_call(ports[1], tokens[1], "list_workdirs", {})
                ).structured_content is not None
                rotated_b = CoreBridgeClientV2(
                    owner_socket,
                    bridge_id="shsm-owner",
                    core_id=ids[1].instance_id,
                    key_id=rotated_key_id,
                    secret=rotated_secret,
                    workdir_alias="Workspace",
                    policy_revision=digest,
                    timeout_seconds=4,
                )
                assert await rotated_b.call("bridge.ping", {}) == {"status": "ok"}
                with pytest.raises(CoreBridgeRemoteError) as cross_core:
                    await rotated_b.call("task.get", {"task_id": restarted_tasks[0]})
                assert cross_core.value.code == "CORE_TASK_NOT_FOUND"
                assert (
                    tuple(
                        darwin_service._launchd_job_pid(identity.macos_label("core"))
                        for identity in ids
                    )
                    == core_pids
                )

                await asyncio.to_thread(stop_shared_core, ids[0])
                assert _nothing_registered(ids[0])
                assert bridge_rotated.poll() is None
                assert (
                    await _mcp_call(ports[1], tokens[1], "list_workdirs", {})
                ).structured_content is not None

                await asyncio.to_thread(stop_shared_core, ids[1])
                assert _nothing_registered(ids[1])
                assert bridge_rotated.poll() is None

        asyncio.run(exercise())
        accepted = True
    finally:
        # Cleanup never registers or mutates any default service. Stop ONLY
        # the two random labels, even after failed stage/start/acceptance.
        for identity in reversed(ids):
            root = instance_service_home(identity)
            if not root.exists() and not root.is_symlink():
                # A non-attempted identity cannot have been registered by
                # this test. An attempted one with missing app state is
                # ambiguous: preserve scratch rather than assume no job.
                if identity.instance_id in registration_attempted:
                    cleanup_errors.append(
                        f"{identity.instance_id}: attempted registration has missing state"
                    )
                elif darwin_service._launchd_job_pid(identity.macos_label("core")) is not None:
                    cleanup_errors.append(f"{identity.instance_id}: unexpected test-label process")
                continue
            proof = pinned_candidates.get(identity.instance_id)
            if not _verified_test_manifest(identity, proof):
                cleanup_errors.append(f"{identity.instance_id}: test ownership unverified")
                continue
            state_now = _registration_state(identity)
            if state_now == "unknown":
                cleanup_errors.append(
                    f"{identity.instance_id}: unknown native status; no unregister attempted"
                )
                continue
            try:
                if state_now == "registered":
                    stop_shared_core(identity)
                if not _nothing_registered(identity):
                    raise RuntimeError("test Core deregistration not proven")
                assert proof is not None
                _drop_only_test_owner(identity, root.parent, proof)
            except Exception as exc:
                cleanup_errors.append(f"{identity.instance_id}: {type(exc).__name__}")

        preserved = protected_snapshot() == before
        if not preserved:
            cleanup_errors.append("protected ServerFS launchd baseline changed")
        if not cleanup_errors and all(not path.exists() for path in owners):
            shutil.rmtree(scratch)
        else:
            # The test cannot prove native jobs are gone. Keep the runtime
            # and config at their original locations for safe manual repair.
            cleanup_errors.append("unverified state retained for diagnostic")
        if _active_shsm_labels() or _shsm_residuals():
            cleanup_errors.append("shsm launchd jobs or scratch remain after cleanup")
        if cleanup_errors:
            pytest.fail("; ".join(cleanup_errors))
    assert accepted
    assert all(not path.exists() for path in owners)
    assert protected_snapshot() == before
