"""Validate named Connector manifests through the signed macOS Launcher only.

Requires an explicit --execute. It never registers or launches services and
uses only synthetic local files and credentials.
"""

from __future__ import annotations

import argparse
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

from serverfs_mcp import darwin_instance_runtime as runtime
from serverfs_mcp.darwin_instance_bridge_identity import named_owned_bridge_resources
from serverfs_mcp.darwin_instance_owned_runtime import stage_owned_core_bridge
from serverfs_mcp.darwin_instances import APP_NAME, instance_app_path, instance_service_home
from serverfs_mcp.darwin_service import _launchd_job_pid
from serverfs_mcp.instance_identity import InstanceIdentity

REPO = Path(__file__).resolve().parents[2]
BRIDGE_PYTHON = REPO / "agent_bridge/.venv/bin/python"
BUILD = REPO / "native/macos/service_manager/build.sh"
PROTECTED = (
    "com.ntlx.serverfs.tunnel",
    "com.ntlx.serverfs.bridge",
    "com.ntlx.serverfs.http-origin",
    "com.ntlx.serverfs.cloudflare",
)


def _run(binary: Path, instance: str, service: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(binary), "--validate-only", instance, service],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def _no_owned_children(instance: str, bridge_config: Path) -> bool:
    result = subprocess.run(
        ["ps", "-axo", "pid=,command="], capture_output=True, text=True, timeout=10
    )
    return result.returncode == 0 and not any(
        f"--macos-instance {instance}" in line
        or f"--instance {instance}" in line
        or str(bridge_config) in line
        for line in result.stdout.splitlines()
    )


def _validate(binary: Path, instance: str, service: str) -> bool:
    result = _run(binary, instance, service)
    try:
        return result.returncode == 0 and json.loads(result.stdout) == {"ok": True}
    except (json.JSONDecodeError, TypeError):
        return False


def _manager_preflight(manager: Path, instance: str, service: str) -> bool:
    result = subprocess.run(
        [str(manager), "register-preflight", service],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return False
    return result.returncode == 0 and payload == {
        "ok": True,
        "eligible": True,
        "instance_id": instance,
        "operation": "register-preflight",
    }


def _reject(binary: Path, instance: str, service: str, secrets_to_hide: list[str]) -> bool:
    result = _run(binary, instance, service)
    combined = result.stdout + result.stderr
    return result.returncode != 0 and not any(
        value and value in combined for value in secrets_to_hide
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    if not parser.parse_args().execute:
        print(json.dumps({"result": "NOT_EXECUTED", "reason": "explicit --execute required"}))
        return 2
    if sys.platform != "darwin" or os.uname().machine != "arm64":
        print(
            json.dumps({"result": "NOT_ACCEPTED", "reason": "requires native Apple Silicon macOS"})
        )
        return 1

    baseline = tuple(_launchd_job_pid(label) for label in PROTECTED)
    if any(pid is None for pid in baseline):
        print(
            json.dumps({"result": "NOT_ACCEPTED", "reason": "production PID baseline unavailable"})
        )
        return 1

    instance = "v25-cid-" + secrets.token_hex(6)
    identity = InstanceIdentity(instance_id=instance, bridge_mode="owned", bridge_id=instance)
    connector_labels = (
        identity.macos_label("cloudflare", "blue"),
        identity.macos_label("cloudflare", "green"),
        identity.macos_label("openai", "blue"),
    )
    root = instance_service_home(identity)
    owner = root.parent
    resources = named_owned_bridge_resources(identity)
    if owner.exists() or owner.is_symlink() or resources.socket_path.parent.exists():
        print(
            json.dumps({"result": "NOT_ACCEPTED", "reason": "random instance namespace occupied"})
        )
        return 1

    temp = Path(tempfile.mkdtemp(prefix="serverfs-v0250-validate-")).resolve(strict=True)
    secrets_to_hide: list[str] = []
    errors: list[str] = []
    positive: dict[str, bool] = {}
    manager_preflight: dict[str, bool] = {}
    negative: dict[str, bool] = {}
    staged = False
    phase = "prepare"
    removed = False
    production_same = False
    no_children = False
    no_logs = False
    no_connector_jobs = False
    try:
        workdir = temp / "workdir"
        workdir.mkdir(mode=0o700)
        private = temp / "private"
        private.mkdir(mode=0o700)
        config = temp / "serverfs.toml"
        config.write_text(
            "[agent]\nenabled=true\n[agent.codex]\nenabled=true\n"
            "[[workdirs]]\nalias='synthetic'\n"
            f"path='{workdir}'\nread_only=false\n"
            "agent_mode='workspace-write'\nagent_runtimes=['codex']\n"
        )
        with socket.socket() as candidate:
            candidate.bind(("127.0.0.1", 0))
            port = int(candidate.getsockname()[1])
        api_key = "synthetic-api-" + secrets.token_hex(24)
        env = private / "http.env"
        env.write_text(
            f"SERVERFS_HTTP_API_KEY={api_key}\n"
            f"SERVERFS_HTTP_ALLOWED_HOSTS=synthetic.test,127.0.0.1:{port}\n"
        )
        env.chmod(0o600)
        token = private / "cloudflare.token"
        token.write_text("synthetic-cf-" + secrets.token_hex(24))
        green_token = private / "cloudflare-green.token"
        green_token.write_text("synthetic-cf-green-" + secrets.token_hex(24))
        control = private / "openai.control"
        control.write_text("synthetic-control-" + secrets.token_hex(24))
        bearer = private / "openai.bearer"
        bearer.write_text("Bearer " + api_key)
        for path in (token, green_token, control, bearer):
            path.chmod(0o600)
            if stat.S_IMODE(path.stat().st_mode) != 0o600:
                raise RuntimeError("private credential permissions are not 0600")
        secrets_to_hide.extend(
            [
                api_key,
                token.read_text(),
                green_token.read_text(),
                control.read_text(),
                bearer.read_text(),
            ]
        )
        fake = private / "fake-connector"
        fake.write_text("#!/bin/sh\nexec /bin/sleep 300\n")
        fake.chmod(0o700)

        template = temp / "build"
        subprocess.run(
            ["bash", str(BUILD), str(template)], capture_output=True, timeout=120, check=True
        )
        phase = "stage-owned"
        stage_owned_core_bridge(
            identity,
            config,
            env,
            port,
            "synthetic.test",
            template / APP_NAME,
            BRIDGE_PYTHON,
            connector_ids={"cloudflare": ["blue", "green"], "openai": ["blue"]},
        )
        phase = "stage-cloudflare-blue"
        runtime.stage_cloudflare_connector(identity, fake, token, connector_id="blue")
        phase = "stage-cloudflare-green"
        runtime.stage_cloudflare_connector(identity, fake, green_token, connector_id="green")
        phase = "stage-openai-blue"
        runtime.stage_openai_http_connector(
            identity,
            fake,
            "tunnel_" + secrets.token_hex(16),
            control,
            bearer,
            connector_id="blue",
        )
        staged = True
        phase = "signed-launcher-validation"
        binary = instance_app_path(identity) / "Contents/Resources/serverfs-service-launcher"
        positive = {
            key: _validate(binary, instance, key)
            for key in ("cloudflare.blue", "cloudflare.green", "tunnel.blue")
        }
        phase = "signed-manager-preflight"
        manager = instance_app_path(identity) / "Contents/MacOS/serverfs-service-manager"
        manager_preflight = {
            key: _manager_preflight(manager, instance, key)
            for key in ("cloudflare.blue", "cloudflare.green", "tunnel.blue")
        }
        phase = "signed-launcher-negative-tests"
        negative = {
            "manager_missing": not _manager_preflight(manager, instance, "cloudflare.missing"),
            "missing": _reject(binary, instance, "cloudflare.missing", secrets_to_hide),
            "invalid": _reject(binary, instance, "cloudflare.Bad", secrets_to_hide),
            "other_instance": _reject(
                binary, instance + "-foreign", "cloudflare.blue", secrets_to_hide
            ),
        }
        manifest_path = root / "services.json"
        original = json.loads(manifest_path.read_text())
        bad_log = json.loads(json.dumps(original))
        bad_log["services"]["cloudflare.blue"]["stderr_log"] = str(temp / "outside.log")
        manifest_path.write_text(json.dumps(bad_log, sort_keys=True, separators=(",", ":")))
        negative["mutated_bad_log"] = _reject(binary, instance, "cloudflare.blue", secrets_to_hide)
        duplicate = json.loads(json.dumps(original))
        args = duplicate["services"]["tunnel.blue"]["arguments"]
        bearer_arg = args.index("--mcp.extra-headers") + 1
        args[bearer_arg] = args[bearer_arg].replace(str(bearer), str(control))
        manifest_path.write_text(json.dumps(duplicate, sort_keys=True, separators=(",", ":")))
        negative["mutated_duplicate_credential"] = _reject(
            binary, instance, "tunnel.blue", secrets_to_hide
        )
        manifest_path.write_text(json.dumps(original, sort_keys=True, separators=(",", ":")))
        phase = "postconditions"
        no_logs = not (root / "logs").exists() or not any((root / "logs").iterdir())
        no_connector_jobs = all(_launchd_job_pid(label) is None for label in connector_labels)
        no_children = _no_owned_children(instance, resources.config_path)
        if not all(positive.values()):
            errors.append("one or more signed positive validations failed")
        if not all(manager_preflight.values()):
            errors.append("one or more signed manager preflights failed")
        if not all(negative.values()):
            errors.append("one or more negative validations were accepted or exposed a secret")
        if not no_logs or not no_connector_jobs or not no_children:
            errors.append("side-effect or Connector process/job check failed")
    except Exception as exc:
        errors.append(type(exc).__name__)
    finally:
        no_children = _no_owned_children(instance, resources.config_path)
        try:
            production_same = tuple(_launchd_job_pid(label) for label in PROTECTED) == baseline
            no_connector_jobs = all(_launchd_job_pid(label) is None for label in connector_labels)
        except Exception as exc:
            errors.append(type(exc).__name__)
            production_same = False
            no_connector_jobs = False
        safe_to_clean = (
            staged
            and not errors
            and no_logs
            and all(positive.values())
            and all(manager_preflight.values())
            and all(negative.values())
            and no_children
            and production_same
            and no_connector_jobs
            and not resources.socket_path.parent.exists()
        )
        if safe_to_clean and (
            owner.is_dir()
            and not owner.is_symlink()
            and owner.name == instance
            and owner.parent.name == "instances"
        ):
            try:
                shutil.rmtree(owner)
                removed = not owner.exists()
                if removed:
                    shutil.rmtree(temp)
            except OSError as exc:
                errors.append(type(exc).__name__)
        if not removed:
            errors.append("cleanup ownership or process state uncertain; evidence retained")

    result = (
        "PASS"
        if (
            staged
            and removed
            and production_same
            and no_children
            and no_logs
            and no_connector_jobs
            and all(positive.values())
            and all(manager_preflight.values())
            and all(negative.values())
            and not errors
        )
        else "NOT_ACCEPTED"
    )
    print(
        json.dumps(
            {
                "result": result,
                "positive_signed_launcher_validation": positive,
                "manager_registration_preflight": manager_preflight,
                "negative_validation": negative,
                "connectors_staged": staged,
                "phase": phase,
                "no_logs_created": no_logs,
                "no_connector_launchd_jobs": no_connector_jobs,
                "no_connector_or_bridge_children": no_children,
                "production_pids_unchanged": production_same,
                "test_instance_removed": removed,
                "retained_instance": None if removed else instance,
                "retained_private_directory": None if removed else str(temp),
                "errors": errors,
            },
            sort_keys=True,
        )
    )
    return 0 if result == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
