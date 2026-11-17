"""Isolated v0.25.0 multi-dynamic-Connector native macOS acceptance.

Requires explicit --execute on native Apple Silicon macOS. Uses a fresh owned
instance, synthetic local credentials, and no provider tasks or network egress.
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
import time
from pathlib import Path

from v0250_owned_core_direct_runtime_acceptance import PROTECTED, probe_core
from v0250_owned_smappservice_live_acceptance import _no_owned_children

from serverfs_mcp import darwin_instance_runtime as runtime
from serverfs_mcp.darwin_instance_bridge_identity import named_owned_bridge_resources
from serverfs_mcp.darwin_instance_owned_lifecycle import (
    DarwinOwnedLifecycleError,
    owned_instance_status,
    start_owned_pair,
    stop_owned_pair,
)
from serverfs_mcp.darwin_instance_owned_runtime import stage_owned_core_bridge
from serverfs_mcp.darwin_instance_runtime import DarwinInstanceRuntimeError
from serverfs_mcp.darwin_instances import APP_NAME, instance_service_home
from serverfs_mcp.instance_identity import InstanceIdentity

REPO = Path(__file__).resolve().parents[2]
BRIDGE_PYTHON = REPO / "agent_bridge/.venv/bin/python"
BUILD = REPO / "native/macos/service_manager/build.sh"
CONNECTORS = (
    ("cloudflare", "blue", "cloudflare.blue"),
    ("cloudflare", "green", "cloudflare.green"),
    ("openai", "blue", "tunnel.blue"),
)


def _port() -> int:
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])


def _baseline() -> tuple[int | None, ...]:
    return tuple(runtime.darwin_service._launchd_job_pid(label) for label in PROTECTED)


def _connector_status(identity: InstanceIdentity, key: str) -> tuple[str, int | None]:
    service_provider, connector_id = key.split(".", 1)
    component = "openai" if service_provider == "tunnel" else service_provider
    return (
        runtime._manager(identity, "status", key),
        runtime.darwin_service._launchd_job_pid(identity.macos_label(component, connector_id)),
    )


def _registrations_clear(identity: InstanceIdentity) -> bool:
    try:
        status = owned_instance_status(identity)
        if any(
            status["registrations"].get(key) not in {"not-registered", "not-found"}
            for _, _, key in CONNECTORS
        ):
            return False
        if any(status["pids"].get(key) is not None for _, _, key in CONNECTORS):
            return False
        return all(
            runtime._manager(identity, "status", service) in {"not-registered", "not-found"}
            and runtime.darwin_service._launchd_job_pid(identity.macos_label(component)) is None
            for component, service in (("bridge", "agent-bridge"), ("core", "http-origin"))
        )
    except Exception:
        return False


def _verify(identity: InstanceIdentity, expected: dict[str, int]) -> None:
    status = owned_instance_status(identity)
    for key, pid in expected.items():
        if status["pids"].get(key) != pid:
            raise RuntimeError("expected owned PID changed")
        if status["registrations"].get(key) != "enabled":
            raise RuntimeError("expected exact service registration is not enabled")


def _tracked_exited(pids: set[int], timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        alive = False
        for pid in pids:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            except (PermissionError, OSError):
                return False
            else:
                alive = True
        if not alive:
            return True
        time.sleep(0.2)
    return False


async def _probe(port: int, key: str) -> None:
    await probe_core(port, key)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    if not parser.parse_args().execute:
        print(json.dumps({"result": "NOT_EXECUTED", "phase": "opt-in"}))
        return 2

    phase = "platform"
    instance: str | None = None
    temp: Path | None = None
    owner: Path | None = None
    identity: InstanceIdentity | None = None
    resource = None
    baseline: tuple[int | None, ...] = ()
    tracked: set[int] = set()
    started_keys: list[tuple[str, str, str]] = []
    errors: list[str] = []
    roundtrips = 0
    tested = False
    removed = False
    temp_removed = False
    core_stable = False
    failure_isolation = False
    no_jobs_children = False
    registrations_clear = False
    socket_clean = False
    default_same = False

    try:
        if sys.platform != "darwin" or os.uname().machine != "arm64":
            raise RuntimeError("requires native Apple Silicon macOS")
        phase = "production-pid-baseline"
        baseline = _baseline()
        if any(pid is None for pid in baseline):
            raise RuntimeError("all four production launchd PID baselines are required")

        phase = "collision-check"
        instance = "v25-mc4-" + secrets.token_hex(8)
        identity = InstanceIdentity(instance_id=instance, bridge_mode="owned", bridge_id=instance)
        root = instance_service_home(identity)
        owner = root.parent
        resource = named_owned_bridge_resources(identity)
        labels = [identity.macos_label("bridge"), identity.macos_label("core")]
        labels.extend(identity.macos_label(c, cid) for c, cid, _ in CONNECTORS)
        if owner.exists() or owner.is_symlink():
            raise RuntimeError("random instance namespace occupied")
        if resource.socket_path.parent.exists() or resource.socket_path.parent.is_symlink():
            raise RuntimeError("random socket namespace occupied")
        if any(runtime.darwin_service._launchd_job_pid(label) is not None for label in labels):
            raise RuntimeError("random launchd label already active")
        temp = Path(tempfile.mkdtemp(prefix="serverfs-v0250-mc4-")).resolve(strict=True)
        workdir = temp / "workdir"
        workdir.mkdir(mode=0o700)
        private = temp / "private"
        private.mkdir(mode=0o700)
        port = _port()
        api_key = "synthetic-api-" + secrets.token_hex(24)
        config = temp / "serverfs.toml"
        config.write_text(
            "[agent]\nenabled=true\n[agent.codex]\nenabled=true\n"
            "[[workdirs]]\nalias='synthetic'\n"
            f"path='{workdir}'\nread_only=false\n"
            "agent_mode='workspace-write'\nagent_runtimes=['codex']\n"
        )
        env = private / "core.env"
        env.write_text(
            f"SERVERFS_HTTP_API_KEY={api_key}\n"
            f"SERVERFS_HTTP_ALLOWED_HOSTS=synthetic.test,127.0.0.1:{port}\n"
        )
        token_blue = private / "cloudflare-blue.token"
        token_green = private / "cloudflare-green.token"
        control = private / "openai-control.key"
        bearer = private / "openai-bearer.key"
        token_blue.write_text("synthetic-cloudflare-blue-" + secrets.token_hex(24))
        token_green.write_text("synthetic-cloudflare-green-" + secrets.token_hex(24))
        control.write_text("synthetic-openai-control-" + secrets.token_hex(24))
        bearer.write_text("Bearer " + api_key)
        for path in (env, token_blue, token_green, control, bearer):
            path.chmod(0o600)
            if stat.S_IMODE(path.stat().st_mode) != 0o600:
                raise RuntimeError("synthetic private credential mode is not 0600")
        if len({p.stat().st_ino for p in (token_blue, token_green, control, bearer)}) != 4:
            raise RuntimeError("synthetic credentials are not independent files")
        fake = private / "fake-connector"
        fake.write_text("#!/bin/sh\nexec /bin/sleep 300\n")
        fake.chmod(0o700)

        phase = "signed-app-build"
        build = temp / "build"
        subprocess.run(
            ["bash", str(BUILD), str(build)], capture_output=True, timeout=120, check=True
        )
        phase = "stage-owned-instance"
        stage_owned_core_bridge(
            identity,
            config,
            env,
            port,
            "synthetic.test",
            build / APP_NAME,
            BRIDGE_PYTHON,
            connector_ids={"cloudflare": ["blue", "green"], "openai": ["blue"]},
        )
        runtime.stage_cloudflare_connector(identity, fake, token_blue, connector_id="blue")
        runtime.stage_cloudflare_connector(identity, fake, token_green, connector_id="green")
        runtime.stage_openai_http_connector(
            identity,
            fake,
            "tunnel_" + secrets.token_hex(16),
            control,
            bearer,
            connector_id="blue",
        )
        if any(
            _connector_status(identity, key)[0] not in {"not-registered", "not-found"}
            for _, _, key in CONNECTORS
        ):
            raise RuntimeError("staged dynamic Connector label is already registered")

        phase = "owned-core-bridge-start"
        pair = start_owned_pair(identity, config, port)
        core_pid, bridge_pid = pair["core_pid"], pair["bridge_pid"]
        tracked.update((core_pid, bridge_pid))
        expected = {"core": core_pid, "bridge": bridge_pid}
        _verify(identity, expected)
        await_probe = _probe(port, api_key)
        asyncio.run(await_probe)
        roundtrips += 1

        phase = "dynamic-connectors-start"
        for component, connector_id, key in CONNECTORS:
            started_keys.append((component, connector_id, key))
            result = runtime.start_component(identity, component, connector_id=connector_id)
            if result.get("component") != key or result.get("registration") != "enabled":
                raise RuntimeError("dynamic Connector returned unexpected identity/status")
            pid = result.get("pid")
            if type(pid) is not int or pid <= 0:
                raise RuntimeError("dynamic Connector PID missing")
            tracked.add(pid)
            expected[key] = pid
        if len(set(expected.values())) != 5:
            raise RuntimeError("five expected components do not have distinct PIDs")
        _verify(identity, expected)
        core_stable = True

        phase = "pair-stop-refusal"
        before = {key: _connector_status(identity, key) for _, _, key in CONNECTORS}
        try:
            stop_owned_pair(identity)
        except DarwinOwnedLifecycleError:
            pass
        else:
            raise RuntimeError("owned pair stop unexpectedly succeeded with active Connectors")
        after = {key: _connector_status(identity, key) for _, _, key in CONNECTORS}
        failure_isolation = before == after and all(
            state == "enabled" and pid for state, pid in after.values()
        )
        if not failure_isolation:
            raise RuntimeError("refused pair stop changed Connector registration or PID")
        _verify(identity, expected)

        phase = "missing-signed-id-refusal"
        try:
            runtime.start_component(identity, "cloudflare", connector_id="missing")
        except DarwinInstanceRuntimeError as exc:
            if str(exc) != "requested component is not staged":
                raise RuntimeError("missing signed ID failed for an unexpected reason") from exc
        else:
            raise RuntimeError("missing signed Connector ID unexpectedly started")
        # A missing key has no signed plist, so ServiceManager cannot select it.
        # Query launchd directly rather than asking that Manager to select it.
        missing_label = identity.macos_label("cloudflare", "missing")
        missing_job = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{missing_label}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if (
            missing_job.returncode == 0
            or "Could not find service" not in missing_job.stderr
            or runtime.darwin_service._launchd_job_pid(missing_label) is not None
        ):
            raise RuntimeError("missing signed ID left a registered job")

        phase = "cloudflare-blue-cycle"
        runtime.stop_component(identity, "cloudflare", connector_id="blue")
        del expected["cloudflare.blue"]
        _verify(identity, expected)
        asyncio.run(_probe(port, api_key))
        roundtrips += 1
        result = runtime.start_component(identity, "cloudflare", connector_id="blue")
        new_pid = result["pid"]
        tracked.add(new_pid)
        expected["cloudflare.blue"] = new_pid
        _verify(identity, expected)

        phase = "openai-blue-cycle"
        runtime.stop_component(identity, "openai", connector_id="blue")
        del expected["tunnel.blue"]
        _verify(identity, expected)
        asyncio.run(_probe(port, api_key))
        roundtrips += 1
        result = runtime.start_component(identity, "openai", connector_id="blue")
        new_pid = result["pid"]
        tracked.add(new_pid)
        expected["tunnel.blue"] = new_pid
        _verify(identity, expected)
        asyncio.run(_probe(port, api_key))
        roundtrips += 1
        tested = True
    except Exception as exc:
        errors.append(type(exc).__name__ + " during " + phase)
    finally:
        if identity is not None and owner is not None and owner.exists() and not owner.is_symlink():
            attempted: set[tuple[str, str]] = set()
            for component, connector_id, _key in reversed(started_keys):
                if (component, connector_id) in attempted:
                    continue
                attempted.add((component, connector_id))
                try:
                    runtime.stop_component(identity, component, connector_id=connector_id)
                except Exception as exc:
                    errors.append("scoped Connector teardown failed: " + type(exc).__name__)
            try:
                stop_owned_pair(identity)
            except Exception as exc:
                errors.append("owned pair teardown failed: " + type(exc).__name__)

        if identity is not None and owner is not None and owner.exists() and not owner.is_symlink():
            registrations_clear = _registrations_clear(identity)
        elif owner is not None:
            registrations_clear = True
        if identity is not None and resource is not None:
            if (
                resource.socket_path.parent.exists()
                and not resource.socket_path.parent.is_symlink()
            ):
                try:
                    resource.socket_path.parent.rmdir()
                except OSError:
                    pass
            socket_clean = not resource.socket_path.parent.exists()
        child_markers_clear = True
        if identity is not None and resource is not None and temp is not None:
            child_markers_clear = _no_owned_children(identity, resource.config_path)
        no_jobs_children = registrations_clear and _tracked_exited(tracked) and child_markers_clear
        default_same = bool(baseline) and _baseline() == baseline

        safe_remove = (
            tested
            and not errors
            and registrations_clear
            and no_jobs_children
            and socket_clean
            and default_same
        )
        if (
            owner is not None
            and owner.exists()
            and safe_remove
            and owner.parent.name == "instances"
            and owner.name == instance
        ):
            try:
                shutil.rmtree(owner)
                removed = not owner.exists()
            except OSError as exc:
                errors.append("test instance removal failed: " + type(exc).__name__)
        elif owner is not None:
            removed = not owner.exists()
        if temp is not None and removed and safe_remove:
            try:
                shutil.rmtree(temp)
                temp_removed = not temp.exists()
            except OSError as exc:
                errors.append("private temporary directory removal failed: " + type(exc).__name__)

    default_same = default_same and _baseline() == baseline
    pids_stable = core_stable
    if not default_same:
        errors.append("production PID baseline changed")
    outcome = {
        "result": "PASS"
        if (
            tested
            and removed
            and temp_removed
            and default_same
            and no_jobs_children
            and failure_isolation
            and core_stable
            and roundtrips == 4
            and not errors
        )
        else "NOT_ACCEPTED",
        "phase": phase,
        "real_smappservice": tested,
        "unique_component_pids": len(tracked) >= 5 and len(set(tracked)) >= 5,
        "dynamic_connector_lifecycle": tested,
        "core_bridge_pids_stable": pids_stable,
        "mcp_roundtrips": roundtrips,
        "failure_isolation": failure_isolation,
        "production_pids_unchanged": default_same,
        "no_jobs_or_children_at_end": no_jobs_children,
        "test_instance_removed": removed,
        "retained_instance": None if removed else instance,
        "retained_dir": None if temp_removed else (str(temp) if temp else None),
        "errors": errors,
    }
    print(json.dumps(outcome, sort_keys=True))
    return 0 if outcome["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
