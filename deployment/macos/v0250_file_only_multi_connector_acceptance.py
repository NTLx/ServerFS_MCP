"""Isolated native macOS file-only Core with three named fake Connectors.

Explicit --execute required. All SMAppService calls target one new random
instance; synthetic Connector executables only run /bin/sleep, never tunnels.
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
import sys
import tempfile
from pathlib import Path

from v0250_file_only_http_e2e import _mcp_roundtrip
from v0250_multi_connector_native_acceptance import (
    BUILD,
    CONNECTORS,
    _baseline,
    _port,
    _tracked_exited,
)

from serverfs_mcp import darwin_instance_runtime as runtime
from serverfs_mcp.darwin_instances import APP_NAME, instance_service_home
from serverfs_mcp.instance_identity import InstanceIdentity

SERVICE_KEYS = ("http-origin", "cloudflare.blue", "cloudflare.green", "tunnel.blue")


def _label(identity: InstanceIdentity, key: str) -> str:
    if key == "http-origin":
        return identity.macos_label("core")
    provider, connector_id = key.split(".", 1)
    return identity.macos_label("openai" if provider == "tunnel" else provider, connector_id)


def _state(identity: InstanceIdentity, key: str) -> tuple[str, int | None]:
    return (
        runtime._manager(identity, "status", key),
        runtime.darwin_service._launchd_job_pid(_label(identity, key)),
    )


def _verify(identity: InstanceIdentity, expected: dict[str, int]) -> None:
    for key, pid in expected.items():
        status, actual_pid = _state(identity, key)
        if status != "enabled" or actual_pid != pid:
            raise RuntimeError("named file-only service registration or PID drift")


def _registrations_clear(identity: InstanceIdentity) -> bool:
    try:
        for key in SERVICE_KEYS:
            registration, pid = _state(identity, key)
            if registration not in {"not-registered", "not-found"} or pid is not None:
                return False
        return True
    except Exception:
        return False


def _no_core_children(config: Path) -> bool:
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and all(
        str(config) not in line for line in result.stdout.splitlines()
    )


def _no_listener(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return False
    except ConnectionRefusedError:
        return True
    except OSError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    if not parser.parse_args().execute:
        print(json.dumps({"result": "NOT_EXECUTED", "phase": "explicit-opt-in"}))
        return 2

    phase = "preflight"
    errors: list[str] = []
    identity: InstanceIdentity | None = None
    owner: Path | None = None
    temp: Path | None = None
    config: Path | None = None
    port: int | None = None
    baseline: tuple[int | None, ...] = ()
    tracked: set[int] = set()
    attempted: list[tuple[str, str, str]] = []
    core_attempted = False
    staged = False
    tested = False
    unique_initial_pids = False
    removed = False
    temp_removed = False
    no_jobs_children = False
    core_stable = False
    stop_refused = False
    no_bridge = False
    production_same = False
    mcp_checks = 0

    try:
        if sys.platform != "darwin" or os.uname().machine != "arm64":
            raise RuntimeError("native Apple Silicon macOS required")
        baseline = _baseline()
        if any(pid is None for pid in baseline):
            raise RuntimeError("production launchd PID baseline incomplete")

        instance_id = "v25-file-multi-" + secrets.token_hex(6)
        identity = InstanceIdentity(instance_id=instance_id, bridge_mode="disabled")
        root = instance_service_home(identity)
        owner = root.parent
        if owner.exists() or owner.is_symlink():
            raise RuntimeError("random file-only instance already exists")
        if (
            any(
                runtime.darwin_service._launchd_job_pid(_label(identity, key)) is not None
                for key in SERVICE_KEYS
            )
            or runtime.darwin_service._launchd_job_pid(identity.macos_label("bridge")) is not None
        ):
            raise RuntimeError("random instance launchd namespace occupied")

        temp = Path(tempfile.mkdtemp(prefix="serverfs-v0250-file-multi-")).resolve(strict=True)
        workdir = temp / "workdir"
        private = temp / "private"
        workdir.mkdir(mode=0o700)
        private.mkdir(mode=0o700)
        (workdir / "hello.txt").write_text("synthetic-file-only", encoding="utf-8")
        config = temp / "serverfs.toml"
        config.write_text(f"[[workdirs]]\nalias='isolated'\npath='{workdir}'\nread_only=false\n")
        port = _port()
        api_key = "synthetic-api-" + secrets.token_hex(24)
        env = private / "core.env"
        env.write_text(
            f"SERVERFS_HTTP_API_KEY={api_key}\n"
            f"SERVERFS_HTTP_ALLOWED_HOSTS=smoke.invalid,127.0.0.1:{port}\n"
        )
        cf_blue = private / "cloudflare-blue.token"
        cf_green = private / "cloudflare-green.token"
        control = private / "openai-control.key"
        bearer = private / "openai-bearer.key"
        cf_blue.write_text("synthetic-cf-blue-" + secrets.token_hex(24))
        cf_green.write_text("synthetic-cf-green-" + secrets.token_hex(24))
        control.write_text("synthetic-openai-control-" + secrets.token_hex(24))
        bearer.write_text("Bearer " + api_key)
        for file in (env, cf_blue, cf_green, control, bearer):
            file.chmod(0o600)
            stat_info = file.stat(follow_symlinks=False)
            if (
                file.is_symlink()
                or stat_info.st_uid != os.getuid()
                or stat.S_IMODE(stat_info.st_mode) != 0o600
            ):
                raise RuntimeError("private synthetic credential safety check failed")
        fake = private / "fake-connector"
        fake.write_text("#!/bin/sh\nexec /bin/sleep 300\n")
        fake.chmod(0o700)

        phase = "signed-app-build"
        build = temp / "build"
        subprocess.run(
            ["bash", str(BUILD), str(build)], check=True, capture_output=True, timeout=120
        )
        phase = "stage-file-only"
        runtime.stage_file_only_instance(
            identity,
            config,
            env,
            port,
            "smoke.invalid",
            build / APP_NAME,
            connector_ids={"cloudflare": ["blue", "green"], "openai": ["blue"]},
        )
        staged = True
        runtime.stage_cloudflare_connector(identity, fake, cf_blue, connector_id="blue")
        runtime.stage_cloudflare_connector(identity, fake, cf_green, connector_id="green")
        runtime.stage_openai_http_connector(
            identity,
            fake,
            "tunnel_" + secrets.token_hex(16),
            control,
            bearer,
            connector_id="blue",
        )
        if any(
            _state(identity, key)[0] not in {"not-found", "not-registered"} for key in SERVICE_KEYS
        ):
            raise RuntimeError("test instance already registered after staging")

        phase = "core-start"
        core_attempted = True
        core_result = runtime.start_component(identity, "core")
        core_pid = core_result["pid"]
        if type(core_pid) is not int or core_pid <= 0:
            raise RuntimeError("file-only Core PID invalid")
        tracked.add(core_pid)
        expected = {"http-origin": core_pid}
        _verify(identity, expected)
        no_bridge = runtime.darwin_service._launchd_job_pid(identity.macos_label("bridge")) is None
        if not no_bridge:
            raise RuntimeError("file-only instance unexpectedly started Bridge")
        url = f"http://127.0.0.1:{port}/mcp"
        asyncio.run(_mcp_roundtrip(url, api_key))
        mcp_checks += 1

        phase = "dynamic-connectors-start"
        for provider, connector_id, key in CONNECTORS:
            attempted.append((provider, connector_id, key))
            result = runtime.start_component(identity, provider, connector_id=connector_id)
            if result.get("component") != key or result.get("registration") != "enabled":
                raise RuntimeError("dynamic Connector returned unexpected identity")
            pid = result.get("pid")
            if type(pid) is not int or pid <= 0:
                raise RuntimeError("dynamic Connector PID invalid")
            tracked.add(pid)
            expected[key] = pid
        if len(set(expected.values())) != 4:
            raise RuntimeError("four file-only components do not own independent PIDs")
        unique_initial_pids = True
        _verify(identity, expected)

        phase = "core-stop-refusal"
        before = {key: _state(identity, key) for key in SERVICE_KEYS}
        try:
            runtime.stop_component(identity, "core")
        except runtime.DarwinInstanceRuntimeError as exc:
            if "Connector" not in str(exc):
                raise RuntimeError("Core stopped for unexpected reason") from exc
        else:
            raise RuntimeError("Core stopped while Connectors were active")
        stop_refused = before == {key: _state(identity, key) for key in SERVICE_KEYS}
        _verify(identity, expected)
        if not stop_refused:
            raise RuntimeError("Core stop refusal mutated service state")

        phase = "missing-id-refusal"
        try:
            runtime.start_component(identity, "cloudflare", connector_id="missing")
        except runtime.DarwinInstanceRuntimeError as exc:
            if str(exc) != "requested component is not staged":
                raise RuntimeError("missing signed ID failed for unrelated reason") from exc
        else:
            raise RuntimeError("missing signed Connector unexpectedly started")
        missing_label = identity.macos_label("cloudflare", "missing")
        lookup = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{missing_label}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if (
            lookup.returncode == 0
            or "Could not find service" not in lookup.stderr
            or runtime.darwin_service._launchd_job_pid(missing_label) is not None
        ):
            raise RuntimeError("missing signed ID may have a registered job")

        phase = "cloudflare-blue-cycle"
        runtime.stop_component(identity, "cloudflare", connector_id="blue")
        del expected["cloudflare.blue"]
        _verify(identity, expected)
        asyncio.run(_mcp_roundtrip(url, api_key))
        mcp_checks += 1
        restart = runtime.start_component(identity, "cloudflare", connector_id="blue")
        if restart.get("registration") != "enabled":
            raise RuntimeError("Cloudflare blue restart failed")
        tracked.add(restart["pid"])
        expected["cloudflare.blue"] = restart["pid"]
        _verify(identity, expected)

        phase = "openai-blue-cycle"
        runtime.stop_component(identity, "openai", connector_id="blue")
        del expected["tunnel.blue"]
        _verify(identity, expected)
        asyncio.run(_mcp_roundtrip(url, api_key))
        mcp_checks += 1
        restart = runtime.start_component(identity, "openai", connector_id="blue")
        if restart.get("registration") != "enabled":
            raise RuntimeError("OpenAI blue restart failed")
        tracked.add(restart["pid"])
        expected["tunnel.blue"] = restart["pid"]
        _verify(identity, expected)
        asyncio.run(_mcp_roundtrip(url, api_key))
        mcp_checks += 1
        core_stable = _state(identity, "http-origin") == ("enabled", core_pid)
        no_bridge = (
            no_bridge
            and runtime.darwin_service._launchd_job_pid(identity.macos_label("bridge")) is None
        )
        tested = core_stable and no_bridge and stop_refused and mcp_checks == 4
    except Exception as exc:
        errors.append(type(exc).__name__ + " during " + phase)
    finally:
        if (
            staged
            and identity is not None
            and owner is not None
            and owner.is_dir()
            and not owner.is_symlink()
        ):
            for provider, connector_id, _key in reversed(attempted):
                try:
                    runtime.stop_component(identity, provider, connector_id=connector_id)
                except Exception as exc:
                    errors.append("scoped Connector teardown: " + type(exc).__name__)
            if core_attempted:
                try:
                    runtime.stop_component(identity, "core")
                except Exception as exc:
                    errors.append("scoped Core teardown: " + type(exc).__name__)

        if identity is not None:
            no_bridge = (
                no_bridge
                and runtime.darwin_service._launchd_job_pid(identity.macos_label("bridge")) is None
            )
            if staged and owner is not None and owner.is_dir() and not owner.is_symlink():
                registrations_clear = _registrations_clear(identity)
            else:
                registrations_clear = all(
                    runtime.darwin_service._launchd_job_pid(_label(identity, key)) is None
                    for key in SERVICE_KEYS
                )
        else:
            registrations_clear = False
        no_jobs_children = (
            registrations_clear
            and _tracked_exited(tracked)
            and (config is None or _no_core_children(config))
            and port is not None
            and _no_listener(port)
            and no_bridge
        )
        production_same = bool(baseline) and _baseline() == baseline
        safe_remove = (
            tested
            and not errors
            and no_jobs_children
            and production_same
            and identity is not None
            and owner is not None
            and owner.is_dir()
            and not owner.is_symlink()
            and owner.parent.name == "instances"
            and owner.name == identity.instance_id
        )
        if safe_remove:
            try:
                shutil.rmtree(owner)
                removed = not owner.exists()
                if removed and temp is not None:
                    shutil.rmtree(temp)
                    temp_removed = not temp.exists()
            except OSError as exc:
                errors.append("scoped artifact cleanup: " + type(exc).__name__)

    outcome = {
        "result": "PASS"
        if tested
        and removed
        and temp_removed
        and no_jobs_children
        and production_same
        and not errors
        else "NOT_ACCEPTED",
        "phase": phase,
        "real_smappservice": tested,
        "four_distinct_components": unique_initial_pids,
        "core_pid_stable": core_stable,
        "mcp_roundtrips": mcp_checks,
        "stop_refusal": stop_refused,
        "no_bridge": no_bridge,
        "no_jobs_or_children_at_end": no_jobs_children,
        "production_pids_unchanged": production_same,
        "test_instance_removed": removed,
        "retained_instance": None if removed else (identity.instance_id if identity else None),
        "retained_dir": None if temp_removed else (str(temp) if temp else None),
        "errors": errors,
    }
    print(json.dumps(outcome, sort_keys=True))
    return 0 if outcome["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
