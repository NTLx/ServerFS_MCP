"""Isolated no-network Connector-only SMAppService test for macOS v0.25.

Run only with --execute. Registers a brand-new named file-only Core and a
synthetic Connector which execs /bin/sleep, never the real cloudflared.
Checks Connector stop/restart leaves Core PID and MCP CRUD unaffected.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import plistlib
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import tempfile
import time
from pathlib import Path

from v0250_file_only_http_e2e import _mcp_roundtrip
from v0250_named_launchd_acceptance import _production_pids

from serverfs_mcp import darwin_service
from serverfs_mcp.darwin_instance_runtime import (
    _manager,
    stage_cloudflare_connector,
    stage_file_only_instance,
    start_component,
    stop_component,
    unstage_cloudflare_connector,
    unstage_file_only_instance,
)
from serverfs_mcp.darwin_instances import APP_NAME, instance_service_home
from serverfs_mcp.instance_identity import InstanceIdentity

_RECOVERY_SECONDS = 60


def _verified_sleep_pid(pid: int, dummy: Path) -> bool:
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "pid=,uid=,command="],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    fields = result.stdout.strip().split(None, 2)
    return (
        result.returncode == 0
        and len(fields) == 3
        and fields[0] == str(pid)
        and fields[1] == str(os.getuid())
        and fields[2] == "/bin/sleep 300"
        and dummy.is_file()
        and dummy.read_text(encoding="utf-8") == "#!/bin/sh\nexec /bin/sleep 300\n"
    )


def _await_verified_sleep_pid(label: str, pid: int, dummy: Path, timeout: float) -> bool:
    """Wait for the registered PID to finish exec'ing the fixture.

    launchd reports the job PID before its launcher image has been replaced
    by the fixture, so `ps` briefly shows the launcher. Only the *same* PID
    converging on the exact fixture command line counts as verified; a PID
    change means the job we intended to observe is gone.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if darwin_service._launchd_job_pid(label) != pid:
            return False
        if _verified_sleep_pid(pid, dummy):
            return True
        time.sleep(0.1)
    return False


def _remove_only_test_logs(root: Path) -> None:
    logs = root / "logs"
    if not logs.is_dir() or logs.is_symlink():
        raise RuntimeError("named log directory is untrusted")
    allowed = {
        "core.stdout.log",
        "core.stderr.log",
        "cloudflare.stdout.log",
        "cloudflare.stderr.log",
    }
    for path in logs.iterdir():
        st = path.stat(follow_symlinks=False)
        if (
            path.name not in allowed
            or not stat.S_ISREG(st.st_mode)
            or st.st_uid != os.getuid()
            or path.is_symlink()
        ):
            raise RuntimeError("unexpected named log artifact")
    for path in logs.iterdir():
        path.unlink()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--verify-keepalive", action="store_true")
    args = parser.parse_args()
    if args.verify_keepalive and not args.execute:
        parser.error("--verify-keepalive requires --execute")
    if not args.execute:
        print("NOT_EXECUTED: explicit --execute required")
        return 2
    before = _production_pids()
    if any(pid is None for pid in before.values()):
        raise RuntimeError("incomplete production baseline; no service registration")
    if not Path("/bin/sleep").is_file():
        raise RuntimeError("no-network executable is unavailable")
    identity = InstanceIdentity(instance_id="v25c-" + secrets.token_hex(4), bridge_mode="disabled")
    root = instance_service_home(identity)
    if root.exists() or root.is_symlink():
        raise RuntimeError("new instance ID collision")
    temporary = Path(tempfile.mkdtemp(prefix="serverfs-v25-named-connector-")).resolve()
    core_staged = False
    connector_staged = False
    accepted = False
    crash_pid: int | None = None
    recovery_pid: int | None = None
    recovery_seconds: float | None = None
    cleanup_result = "not-started"
    errors: list[str] = []
    try:
        workdir = temporary / "workdir"
        workdir.mkdir(mode=0o700)
        (workdir / "hello.txt").write_text("synthetic-file-only", encoding="utf-8")
        config = temporary / "core.toml"
        config.write_text(
            f"[[workdirs]]\nalias = 'isolated'\npath = '{workdir}'\nread_only = false\n",
            encoding="utf-8",
        )
        operator = temporary / "operator"
        operator.mkdir(mode=0o700)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        key = "synthetic-" + secrets.token_hex(12)
        env = operator / "core.env"
        env.write_text(
            f"SERVERFS_HTTP_API_KEY={key}\n"
            f"SERVERFS_HTTP_ALLOWED_HOSTS=smoke.invalid,127.0.0.1:{port}\n",
            encoding="utf-8",
        )
        env.chmod(0o600)
        token = operator / "synthetic-cloudflare-token"
        token.write_text("synthetic-not-cloudflare", encoding="utf-8")
        token.chmod(0o600)
        dummy = operator / "no-network-cloudflared"
        dummy.write_text("#!/bin/sh\nexec /bin/sleep 300\n", encoding="utf-8")
        dummy.chmod(0o700)
        build = temporary / "build"
        build_script = Path(__file__).resolve().parents[2] / "native/macos/service_manager/build.sh"
        build_result = subprocess.run(
            ["bash", str(build_script), str(build)],
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
        )
        if build_result.returncode != 0:
            raise RuntimeError("disposable native bundle failed to build")
        stage_file_only_instance(identity, config, env, port, "smoke.invalid", build / APP_NAME)
        core_staged = True
        stage_cloudflare_connector(identity, dummy, token)
        connector_staged = True
        if _production_pids() != before:
            raise RuntimeError("production PID changed before registration")
        core_pid = start_component(identity, "core")["pid"]
        if not isinstance(core_pid, int):
            raise RuntimeError("named Core PID unavailable")
        url = f"http://127.0.0.1:{port}/mcp"
        start_component(identity, "cloudflare")
        if darwin_service._launchd_job_pid(identity.macos_label("core")) != core_pid:
            raise RuntimeError("Connector start changed Core PID")
        asyncio.run(_mcp_roundtrip(url, key))
        stop_component(identity, "cloudflare")
        if darwin_service._launchd_job_pid(identity.macos_label("core")) != core_pid:
            raise RuntimeError("Connector stop changed Core PID")
        start_component(identity, "cloudflare")
        label = identity.macos_label("cloudflare")
        if darwin_service._launchd_job_pid(identity.macos_label("core")) != core_pid:
            raise RuntimeError("Connector restart changed Core PID")
        asyncio.run(_mcp_roundtrip(url, key))
        if args.verify_keepalive:
            connector_pid = darwin_service._launchd_job_pid(label)
            connector_ready = connector_pid is not None and _await_verified_sleep_pid(
                label, connector_pid, dummy, timeout=15.0
            )
            signed_app = root / APP_NAME
            verify = subprocess.run(
                ["codesign", "--verify", "--deep", "--strict", str(signed_app)],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            if verify.returncode:
                raise RuntimeError("test Connector signed artifact verification failed")
            plist = signed_app / "Contents/Library/LaunchAgents" / f"{label}.plist"
            with plist.open("rb") as stream:
                spec = plistlib.load(stream)
            if (
                not connector_ready
                or spec.get("Label") != label
                or spec.get("KeepAlive") is not True
                or _production_pids() != before
                or darwin_service._launchd_job_pid(identity.macos_label("core")) != core_pid
            ):
                raise RuntimeError("Connector crash preflight ownership proof failed")
            asyncio.run(_mcp_roundtrip(url, key))
            if (
                darwin_service._launchd_job_pid(label) != connector_pid
                or not _verified_sleep_pid(connector_pid, dummy)
                or _production_pids() != before
                or darwin_service._launchd_job_pid(identity.macos_label("core")) != core_pid
            ):
                raise RuntimeError("Connector ownership changed before authorized SIGKILL")
            crash_pid = connector_pid
            os.kill(crash_pid, signal.SIGKILL)
            recovery_started = time.monotonic()
            deadline = recovery_started + _RECOVERY_SECONDS
            while time.monotonic() < deadline:
                candidate = darwin_service._launchd_job_pid(label)
                if (
                    candidate is not None
                    and candidate != crash_pid
                    and _verified_sleep_pid(candidate, dummy)
                ):
                    if (
                        _production_pids() != before
                        or darwin_service._launchd_job_pid(identity.macos_label("core")) != core_pid
                    ):
                        raise RuntimeError("protected PID changed during Connector recovery")
                    try:
                        asyncio.run(_mcp_roundtrip(url, key))
                    except Exception:
                        candidate = None
                    if candidate is not None:
                        if (
                            darwin_service._launchd_job_pid(label) != candidate
                            or not _verified_sleep_pid(candidate, dummy)
                            or _production_pids() != before
                            or darwin_service._launchd_job_pid(identity.macos_label("core"))
                            != core_pid
                        ):
                            raise RuntimeError("recovered Connector ownership changed")
                        recovery_pid = candidate
                        recovery_seconds = time.monotonic() - recovery_started
                        break
                if (
                    _production_pids() != before
                    or darwin_service._launchd_job_pid(identity.macos_label("core")) != core_pid
                ):
                    raise RuntimeError("protected PID changed during Connector recovery")
                time.sleep(0.5)
            if recovery_pid is None:
                raise RuntimeError("synthetic Connector did not recover within 60 seconds")
            if not _verified_sleep_pid(recovery_pid, dummy):
                raise RuntimeError("recovered Connector PID failed ownership verification")
            if _manager(identity, "status", "cloudflare") != "enabled":
                raise RuntimeError("recovered Connector registration is not enabled")
        accepted = True
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
    finally:
        if core_staged:
            try:
                if connector_staged:
                    state = _manager(identity, "status", "cloudflare")
                    if state not in {"not-registered", "not-found"}:
                        stop_component(identity, "cloudflare")
                    unstage_cloudflare_connector(identity)
                state = _manager(identity, "status", "http-origin")
                if state not in {"not-registered", "not-found"}:
                    stop_component(identity, "core")
                for component in ("core", "cloudflare"):
                    if darwin_service._launchd_job_pid(identity.macos_label(component)) is not None:
                        raise RuntimeError("test-owned named job remains active")
                _remove_only_test_logs(root)
                unstage_file_only_instance(identity)
                cleanup_result = "test-owned jobs stopped and staged state removed"
            except Exception as exc:
                cleanup_result = "failed; test state retained"
                errors.append(f"cleanup: {type(exc).__name__}: {exc}")
        preserved = _production_pids() == before
        cleaned = not root.exists()
        if cleaned:
            shutil.rmtree(temporary)
        print(
            json.dumps(
                {
                    "result": "PASS"
                    if accepted and cleaned and preserved and not errors
                    else "NOT_ACCEPTED",
                    "instance": identity.instance_id,
                    "fixture": "no-network exec /bin/sleep",
                    "connector_only_stop_restart": accepted,
                    "core_pid_unchanged": accepted,
                    "mcp_roundtrips": (4 if args.verify_keepalive else 2) if accepted else None,
                    "keepalive_verified": bool(args.verify_keepalive and recovery_pid),
                    "crash_pid": crash_pid,
                    "recovery_pid": recovery_pid,
                    "recovery_seconds": recovery_seconds,
                    "cleanup": cleanup_result,
                    "original_production_pids_unchanged": preserved,
                    "named_state_removed": cleaned,
                    "retained_diagnostic_dir": None if cleaned else str(temporary),
                    "errors": errors,
                },
                sort_keys=True,
            )
        )
    return 0 if accepted and cleaned and preserved and not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
