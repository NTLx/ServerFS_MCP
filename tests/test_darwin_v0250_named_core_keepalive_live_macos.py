"""Opt-in real SMAppService KeepAlive recovery for one disposable HTTP Core."""

from __future__ import annotations

import asyncio
import json
import os
import plistlib
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from serverfs_mcp import darwin_service
from serverfs_mcp.darwin_instance_runtime import (
    _manager,
    instance_status,
    stage_file_only_instance,
    start_component,
    stop_component,
    unstage_file_only_instance,
)
from serverfs_mcp.darwin_instances import APP_NAME, instance_service_home
from serverfs_mcp.instance_identity import InstanceIdentity

_DEPLOYMENT_MACOS = Path(__file__).resolve().parents[1] / "deployment/macos"
if str(_DEPLOYMENT_MACOS) not in sys.path:
    sys.path.insert(0, str(_DEPLOYMENT_MACOS))

from v0250_file_only_http_e2e import _mcp_roundtrip  # noqa: E402
from v0250_named_launchd_acceptance import (  # noqa: E402
    _clean_exact_test_logs,
    _production_pids,
)

_PROTECTED = {
    "com.ntlx.serverfs.tunnel",
    "com.ntlx.serverfs.bridge",
    "com.ntlx.serverfs.cloudflare",
    "com.ntlx.serverfs.http-origin",
}
_RECOVERY_SECONDS = 120


def _active_serverfs_labels() -> set[str]:
    result = subprocess.run(
        ["launchctl", "list"], capture_output=True, text=True, timeout=10, check=False
    )
    if result.returncode:
        raise RuntimeError("launchd inventory unavailable")
    return {
        fields[-1]
        for row in result.stdout.splitlines()
        if len(fields := row.split()) >= 3 and fields[-1].startswith("com.ntlx.serverfs.")
    }


def _verified_pid(pid: int, config: Path, port: int, executable: str) -> bool:
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "pid=,uid=,command="],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    fields = result.stdout.strip().split(None, 2)
    if (
        result.returncode != 0
        or len(fields) != 3
        or fields[0] != str(pid)
        or fields[1] != str(os.getuid())
    ):
        return False

    # launchd/ps can report /tmp where the manifest pins /private/tmp.
    # Compare physical executable identity, but require the entire expected
    # file-only Core argv, rather than accepting path/port substrings.
    suffix = (
        f" -m serverfs_mcp.cli serve --transport streamable-http"
        f" --config {config} --http-port {port}"
    )
    if not fields[2].endswith(suffix):
        return False
    observed_executable = Path(fields[2][: -len(suffix)])
    try:
        return observed_executable.is_absolute() and observed_executable.samefile(executable)
    except (OSError, ValueError):
        return False


def _no_test_process(config: Path) -> bool:
    result = subprocess.run(
        ["ps", "-axo", "pid=,command="],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    marker = str(config)
    return result.returncode == 0 and not any(marker in row for row in result.stdout.splitlines())


def _mcp(port: int, key: str) -> dict[str, object]:
    return asyncio.run(_mcp_roundtrip(f"http://127.0.0.1:{port}/mcp", key))


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("same-path", True),
        ("same-file-alias", True),
        ("different-file", False),
        ("prefix-lookalike", False),
        ("missing-file", False),
        ("wrong-pid", False),
        ("wrong-uid", False),
        ("wrong-config", False),
        ("wrong-port", False),
        ("extra-argument", False),
    ],
)
def test_verified_pid_requires_same_executable_and_exact_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str, expected: bool
) -> None:
    executable = tmp_path / "python"
    executable.touch()
    other = tmp_path / "python-other"
    other.touch()
    alias = tmp_path / "alias" / "python"
    config = tmp_path / "core.toml"
    port = 54321
    observed = {
        "same-file-alias": alias,
        "different-file": other,
        "prefix-lookalike": Path(str(executable) + "-other"),
        "missing-file": tmp_path / "missing",
    }.get(case, executable)

    # Mock physical equivalence for the alias; the real macOS live gate
    # exercises the actual filesystem alias and launchd process identity.
    samefile = Path.samefile

    def physical_match(path: Path, target: str) -> bool:
        if path == alias:
            return Path(target) == executable
        return samefile(path, target)

    monkeypatch.setattr(Path, "samefile", physical_match)
    monkeypatch.setattr(os, "getuid", lambda: 501, raising=False)
    process_pid = 124 if case == "wrong-pid" else 123
    process_uid = 502 if case == "wrong-uid" else 501
    actual_config = str(config) + ".other" if case == "wrong-config" else config
    actual_port = 543210 if case == "wrong-port" else port
    command = (
        f"{observed} -m serverfs_mcp.cli serve --transport streamable-http"
        f" --config {actual_config} --http-port {actual_port}"
    )
    if case == "extra-argument":
        command += " --extra"
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, f"{process_pid} {process_uid} {command}\n"
        ),
    )
    assert _verified_pid(123, config, port, str(executable)) is expected


@pytest.mark.skipif(
    sys.platform != "darwin" or os.environ.get("SERVERFS_ENABLE_NAMED_CORE_KEEPALIVE_LIVE") != "1",
    reason="requires explicit disposable macOS SMAppService KeepAlive acceptance opt-in",
)
def test_named_file_core_keepalive_recovers_after_one_sigkill() -> None:
    before = _production_pids()
    if any(pid is None for pid in before.values()):
        pytest.fail("protected production PID baseline incomplete")
    if _active_serverfs_labels() != _PROTECTED:
        pytest.fail("unknown active ServerFS Login Item; refusing test registration")

    identity = InstanceIdentity("v25-kac-" + secrets.token_hex(5), "disabled")
    root = instance_service_home(identity)
    if root.exists() or root.is_symlink():
        pytest.fail("random test identity collision")
    temporary = Path(tempfile.mkdtemp(prefix="serverfs-v25-kac-")).resolve(strict=True)
    temporary.chmod(0o700)
    staged = False
    killed = False
    first_pid: int | None = None
    recovered_pid: int | None = None
    accepted = False
    cleanup_error: str | None = None
    port: int | None = None
    recovery_seconds: float | None = None
    try:
        workdir = temporary / "workdir"
        workdir.mkdir(mode=0o700)
        (workdir / "hello.txt").write_text("synthetic-file-only\n", encoding="utf-8")
        config = temporary / "core.toml"
        config.write_text(
            f"[[workdirs]]\nalias = 'isolated'\npath = '{workdir}'\nread_only = false\n",
            encoding="utf-8",
        )
        operator = temporary / "operator"
        operator.mkdir(mode=0o700)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = int(probe.getsockname()[1])
        key = secrets.token_urlsafe(32)
        env = operator / "core.env"
        env.write_text(
            f"SERVERFS_HTTP_API_KEY={key}\n"
            f"SERVERFS_HTTP_ALLOWED_HOSTS=keepalive.invalid,127.0.0.1:{port}\n",
            encoding="utf-8",
        )
        env.chmod(0o600)
        build = temporary / "build"
        result = subprocess.run(
            [
                "bash",
                str(Path(__file__).resolve().parents[1] / "native/macos/service_manager/build.sh"),
                str(build),
            ],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if result.returncode:
            raise RuntimeError("isolated signed ServiceManager build failed")
        stage_file_only_instance(identity, config, env, port, "keepalive.invalid", build / APP_NAME)
        staged = True
        state = instance_status(identity)
        if state["bridge_mode"] != "disabled" or state["components"].get("core") not in {
            "not-registered",
            "not-found",
        }:
            raise RuntimeError("unexpected staged Core or Bridge state")
        manifest = json.loads((root / "services.json").read_text(encoding="utf-8"))
        core_record = manifest.get("core", {})
        service_record = manifest.get("services", {}).get("http-origin", {})
        config_path = Path(core_record.get("config", ""))
        executable = service_record.get("executable", "")
        if (
            manifest.get("owner") != "v0.25-named-file-core"
            or manifest.get("instance_id") != identity.instance_id
            or config_path != config.resolve(strict=True)
            or core_record.get("port") != port
            or executable != str(Path(sys.executable).absolute())
            or service_record.get("arguments", [])[-2:] != ["--http-port", str(port)]
        ):
            raise RuntimeError("test manifest does not pin the disposable Core argv")
        app = root / APP_NAME
        verify = subprocess.run(
            ["codesign", "--verify", "--deep", "--strict", str(app)],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if verify.returncode:
            raise RuntimeError("test Core signed artifact verification failed")
        with (app / "Contents/Library/LaunchAgents" / f"{identity.macos_label('core')}.plist").open(
            "rb"
        ) as stream:
            spec = plistlib.load(stream)
        if spec.get("Label") != identity.macos_label("core") or spec.get("KeepAlive") is not True:
            raise RuntimeError("test LaunchAgent does not carry the expected KeepAlive contract")
        preflight = subprocess.run(
            [
                str(app / "Contents/MacOS/serverfs-service-manager"),
                "register-preflight",
                "http-origin",
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if preflight.returncode:
            raise RuntimeError("native signed registration preflight failed")
        if _production_pids() != before or _active_serverfs_labels() != _PROTECTED:
            raise RuntimeError("protected or unknown launchd state changed before registration")

        start_component(identity, "core")
        first_pid = darwin_service._launchd_job_pid(identity.macos_label("core"))
        if first_pid is None or not _verified_pid(first_pid, config_path, port, executable):
            raise RuntimeError("registered Core PID failed test-owner verification")
        print(
            json.dumps(
                {
                    "phase": "registered-before-crash-test",
                    "instance_id": identity.instance_id,
                    "launchd_label": identity.macos_label("core"),
                    "pid": first_pid,
                    "port": port,
                    "config": str(config_path),
                    "protected_pids": before,
                },
                sort_keys=True,
            )
        )
        _mcp(port, key)
        if darwin_service._launchd_job_pid(
            identity.macos_label("core")
        ) != first_pid or not _verified_pid(first_pid, config_path, port, executable):
            raise RuntimeError("test Core PID changed before the single authorized SIGKILL")
        if _production_pids() != before:
            raise RuntimeError("protected production PID changed before SIGKILL")
        os.kill(first_pid, signal.SIGKILL)
        killed = True

        deadline = time.monotonic() + _RECOVERY_SECONDS
        recovery_started = time.monotonic()
        while time.monotonic() < deadline:
            candidate = darwin_service._launchd_job_pid(identity.macos_label("core"))
            if (
                candidate is not None
                and candidate != first_pid
                and _verified_pid(candidate, config_path, port, executable)
            ):
                try:
                    _mcp(port, key)
                    recovered_pid = candidate
                    recovery_seconds = time.monotonic() - recovery_started
                    break
                except Exception:
                    pass
            if _production_pids() != before:
                raise RuntimeError("protected production PID changed during recovery")
            time.sleep(0.5)
        if recovered_pid is None:
            raise AssertionError("authenticated Core did not recover within 120 seconds")
        if _manager(identity, "status", "http-origin") != "enabled":
            raise RuntimeError("recovered Core registration identity changed")
        accepted = True
    except Exception as exc:
        cleanup_error = f"{type(exc).__name__}: {exc}"
    finally:
        if staged:
            try:
                status = _manager(identity, "status", "http-origin")
                if status in {"enabled", "requires-approval"}:
                    stop_component(identity, "core")
                elif status not in {"not-registered", "not-found"}:
                    raise RuntimeError("test registration state is unknown")
                if darwin_service._launchd_job_pid(identity.macos_label("core")) is not None:
                    raise RuntimeError("test Core PID remains after unregister")
                if not _no_test_process(config_path):
                    raise RuntimeError("test-owned Core process remains")
                manifest = json.loads((root / "services.json").read_text())
                if (
                    manifest.get("owner") != "v0.25-named-file-core"
                    or manifest.get("instance_id") != identity.instance_id
                ):
                    raise RuntimeError("test manifest ownership cannot be verified")
                _clean_exact_test_logs(root)
                unstage_file_only_instance(identity)
            except Exception as exc:
                cleanup_error = (
                    cleanup_error + "; " if cleanup_error else ""
                ) + f"cleanup: {type(exc).__name__}"
        if _production_pids() != before or _active_serverfs_labels() != _PROTECTED:
            cleanup_error = "protected/unknown launchd state changed"
        if root.exists():
            cleanup_error = cleanup_error or "test-owned Core state remains"
        if cleanup_error is None:
            shutil.rmtree(temporary)
        else:
            pytest.fail(f"test-owned state retained because cleanup proof failed: {cleanup_error}")

    assert accepted and killed and first_pid is not None and recovered_pid != first_pid
    print(
        json.dumps(
            {
                "result": "PASS",
                "instance_id": identity.instance_id,
                "launchd_label": identity.macos_label("core"),
                "first_pid_sigkilled_once": first_pid,
                "recovered_pid": recovered_pid,
                "authenticated_http_mcp": True,
                "recovery_seconds": recovery_seconds,
                "protected_pids_unchanged": before,
                "test_service_removed": not root.exists(),
                "test_scratch_removed": not temporary.exists(),
            },
            sort_keys=True,
        )
    )
