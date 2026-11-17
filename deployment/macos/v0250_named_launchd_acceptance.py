"""One-time isolated macOS SMAppService Core acceptance with scoped rollback.

This deliberately registers *only a new unique* named file-only Core. It never
registers/stops/changes the legacy default OpenAI Tunnel, Agent Bridge, Cloudflare
Connector or HTTP Origin. An explicit --execute flag is mandatory.

A macOS 'requires-approval' state is a truthful acceptance blocker. Never
bypass Login Items approval; always attempt exact named-service cleanup.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import shutil
import socket
import stat
import subprocess
import tempfile
from pathlib import Path

from v0250_file_only_http_e2e import _mcp_roundtrip

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

_PROTECTED = (
    "com.ntlx.serverfs.tunnel",
    "com.ntlx.serverfs.bridge",
    "com.ntlx.serverfs.cloudflare",
    "com.ntlx.serverfs.http-origin",
)


def _production_pids() -> dict[str, int | None]:
    return {label: darwin_service._launchd_job_pid(label) for label in _PROTECTED}


def _file_owner(path: Path) -> bool:
    st = path.stat(follow_symlinks=False)
    return stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid() and not path.is_symlink()


def _clean_exact_test_logs(root: Path) -> None:
    logs = root / "logs"
    if not logs.is_dir() or logs.is_symlink():
        raise RuntimeError("named instance log directory cannot be verified")
    allowed = {"core.stdout.log", "core.stderr.log"}
    for item in logs.iterdir():
        if item.name not in allowed or not _file_owner(item):
            raise RuntimeError("unexpected named instance log entry; preserving state")
    for item in logs.iterdir():
        item.unlink()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        print("NOT_EXECUTED: use --execute for one disposable named SMAppService registration")
        return 2

    before = _production_pids()
    if any(pid is None for pid in before.values()):
        raise RuntimeError("production PID baseline incomplete; no staging or registration")
    instance_id = "v25-e2e-" + secrets.token_hex(4)
    identity = InstanceIdentity(instance_id=instance_id, bridge_mode="disabled")
    app_home = Path.home()
    root = instance_service_home(identity, home=app_home)
    if root.exists() or root.is_symlink():
        raise RuntimeError("new instance identity collision")
    temporary = Path(tempfile.mkdtemp(prefix="serverfs-v25-managed-core-")).resolve()
    staged = False
    confirmed_stopped = True
    accepted = False
    cleanup_error: str | None = None
    test_result: dict[str, object] = {}
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
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        key = "synthetic-" + secrets.token_hex(12)
        env = operator / "core.env"
        env.write_text(
            f"SERVERFS_HTTP_API_KEY={key}\n"
            f"SERVERFS_HTTP_ALLOWED_HOSTS=smoke.invalid,127.0.0.1:{port}\n",
            encoding="utf-8",
        )
        env.chmod(0o600)
        build = temporary / "build"
        result = subprocess.run(
            [
                "bash",
                str(Path(__file__).resolve().parents[2] / "native/macos/service_manager/build.sh"),
                str(build),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
        )
        if result.returncode != 0:
            raise RuntimeError("disposable native ServiceManager build failed")
        stage_file_only_instance(identity, config, env, port, "smoke.invalid", build / APP_NAME)
        staged = True
        snapshot = instance_status(identity)
        if snapshot["components"].get("core") not in {"not-registered", "not-found"}:
            raise RuntimeError("unexpected pre-registration Core state")
        if _production_pids() != before:
            raise RuntimeError("protected production PID changed before registration")

        # This is the only permitted ServiceManagement mutation: a unique
        # synthetic file-only Core, never any default or Bridge/Connector.
        confirmed_stopped = False
        start_result = start_component(identity, "core")
        if start_result["registration"] != "enabled":
            raise RuntimeError("named Core not enabled")
        test_result = asyncio.run(_mcp_roundtrip(f"http://127.0.0.1:{port}/mcp", key))
        accepted = True
    except Exception as exc:
        cleanup_error = f"{type(exc).__name__}: {exc}"
    finally:
        if staged:
            try:
                state = _manager(identity, "status", "http-origin")
                if state not in {"not-registered", "not-found"}:
                    stop_component(identity, "core")
                if darwin_service._launchd_job_pid(identity.macos_label("core")) is not None:
                    raise RuntimeError("named Core process did not stop")
                # A just-closed TCP listener may leave the port temporarily
                # unavailable for bind(). A refused TCP connection confirms
                # no active IPv4 loopback listener without relying on lsof's
                # host-wide warnings or the kernel's port reuse interval.
                try:
                    probe = socket.create_connection(("127.0.0.1", port), timeout=1)
                except ConnectionRefusedError:
                    pass
                else:
                    probe.close()
                    raise RuntimeError("named Core loopback listener remains active")
                confirmed_stopped = True
                _clean_exact_test_logs(root)
                unstage_file_only_instance(identity)
            except Exception as exc:
                cleanup_error = (cleanup_error + "; " if cleanup_error else "") + (
                    f"cleanup: {type(exc).__name__}: {exc}"
                )
        after = _production_pids()
        preserved = after == before
        removable = confirmed_stopped and not root.exists()
        if removable:
            shutil.rmtree(temporary)
        result = {
            "result": "PASS"
            if accepted and preserved and cleanup_error is None
            else "NOT_ACCEPTED",
            "instance_id": instance_id,
            "bridge_mode": "disabled",
            "isolated_mcp_accepted": accepted,
            "original_production_pids_unchanged": preserved,
            "named_service_stopped": confirmed_stopped,
            "named_state_removed": not root.exists(),
            "retained_diagnostic_dir": None if removable else str(temporary),
            "detail": cleanup_error,
            "evidence": test_result,
        }
        print(json.dumps(result, sort_keys=True))
    return 0 if accepted and preserved and cleanup_error is None and not root.exists() else 1


if __name__ == "__main__":
    raise SystemExit(main())
