"""Isolated macOS v0.25 two named file-only Core lifecycle acceptance.

Explicit --execute only. Two new signed SMAppService bundles with synthetic
credentials and disjoint workdirs are registered independently. No default
OpenAI Tunnel / Bridge / Cloudflare / Core operation is performed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

from v0250_file_only_http_e2e import _mcp_roundtrip
from v0250_named_launchd_acceptance import _clean_exact_test_logs, _production_pids

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


def _random_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    if not parser.parse_args().execute:
        print("NOT_EXECUTED: explicit --execute required for two disposable SMAppService jobs")
        return 2

    before = _production_pids()
    if any(pid is None for pid in before.values()):
        raise RuntimeError("cannot establish all four production PIDs; no service operation")
    ident = secrets.token_hex(4)
    identities = [
        InstanceIdentity(instance_id=f"v25a-{ident}", bridge_mode="disabled"),
        InstanceIdentity(instance_id=f"v25b-{ident}", bridge_mode="disabled"),
    ]
    temporary = Path(tempfile.mkdtemp(prefix="serverfs-v25-dual-core-")).resolve()
    stage_done: list[InstanceIdentity] = []
    errors: list[str] = []
    port_by_id: dict[str, int] = {}
    key_by_id: dict[str, str] = {}
    accepted = False
    tests: list[dict] = []
    try:
        build = temporary / "build"
        script = Path(__file__).resolve().parents[2] / "native/macos/service_manager/build.sh"
        built = subprocess.run(
            ["bash", str(script), str(build)],
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
        )
        if built.returncode != 0:
            raise RuntimeError("isolated native template build failed")

        for identity in identities:
            suffix = identity.instance_id
            instance_dir = temporary / suffix
            instance_dir.mkdir(mode=0o700)
            workdir = instance_dir / "workdir"
            workdir.mkdir(mode=0o700)
            (workdir / "hello.txt").write_text(f"synthetic-file-only {suffix}", encoding="utf-8")
            config = instance_dir / "core.toml"
            config.write_text(
                f"[[workdirs]]\nalias = 'isolated'\npath = '{workdir}'\nread_only = false\n",
                encoding="utf-8",
            )
            port = _random_port()
            while port in port_by_id.values():
                port = _random_port()
            port_by_id[suffix] = port
            key = "synthetic-" + secrets.token_hex(12)
            key_by_id[suffix] = key
            operator = instance_dir / "operator"
            operator.mkdir(mode=0o700)
            env = operator / "core.env"
            env.write_text(
                f"SERVERFS_HTTP_API_KEY={key}\n"
                f"SERVERFS_HTTP_ALLOWED_HOSTS=smoke.invalid,127.0.0.1:{port}\n",
                encoding="utf-8",
            )
            env.chmod(0o600)
            stage_file_only_instance(
                identity,
                config,
                env,
                port,
                "smoke.invalid",
                build / APP_NAME,
            )
            stage_done.append(identity)
            status = instance_status(identity)
            if status["components"].get("core") not in {"not-registered", "not-found"}:
                raise RuntimeError("named Core already registered before test")
        if _production_pids() != before:
            raise RuntimeError("protected production PID baseline changed before starts")

        for identity in identities:
            result = start_component(identity, "core")
            if result["registration"] != "enabled" or not isinstance(result["pid"], int):
                raise RuntimeError("newly registered named Core did not become ready")
            port = port_by_id[identity.instance_id]
            tests.append(
                asyncio.run(
                    _mcp_roundtrip(f"http://127.0.0.1:{port}/mcp", key_by_id[identity.instance_id])
                )
            )

        first, second = identities
        second_pid = darwin_service._launchd_job_pid(second.macos_label("core"))
        assert isinstance(second_pid, int) and second_pid > 0
        stop_component(first, "core")
        if darwin_service._launchd_job_pid(second.macos_label("core")) != second_pid:
            raise RuntimeError("stopping one named Core interrupted the other")
        tests.append(
            asyncio.run(
                _mcp_roundtrip(
                    f"http://127.0.0.1:{port_by_id[second.instance_id]}/mcp",
                    key_by_id[second.instance_id],
                )
            )
        )
        start_component(first, "core")
        if darwin_service._launchd_job_pid(second.macos_label("core")) != second_pid:
            raise RuntimeError("restarting one named Core interrupted the other")
        tests.append(
            asyncio.run(
                _mcp_roundtrip(
                    f"http://127.0.0.1:{port_by_id[first.instance_id]}/mcp",
                    key_by_id[first.instance_id],
                )
            )
        )
        accepted = True
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
    finally:
        for identity in reversed(stage_done):
            try:
                state = _manager(identity, "status", "http-origin")
                if state not in {"not-registered", "not-found"}:
                    stop_component(identity, "core")
                if darwin_service._launchd_job_pid(identity.macos_label("core")) is not None:
                    raise RuntimeError("named Core still owns a launchd process")
                root = instance_service_home(identity)
                _clean_exact_test_logs(root)
                unstage_file_only_instance(identity)
            except Exception as exc:
                errors.append(f"cleanup {identity.instance_id}: {type(exc).__name__}: {exc}")
        preserved = _production_pids() == before
        all_removed = all(not instance_service_home(i).exists() for i in identities)
        if all_removed:
            shutil.rmtree(temporary)
        result = {
            "result": "PASS"
            if accepted and preserved and all_removed and not errors
            else "NOT_ACCEPTED",
            "instances": [item.instance_id for item in identities],
            "bridge_mode": "disabled",
            "distinct_ports": len(set(port_by_id.values())) == 2,
            "independent_lifecycle": accepted,
            "http_roundtrips": len(tests),
            "production_pids_unchanged": preserved,
            "named_state_removed": all_removed,
            "retained_diagnostic_dir": None if all_removed else str(temporary),
            "errors": errors,
        }
        print(json.dumps(result, sort_keys=True))
    return 0 if accepted and preserved and all_removed and not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
