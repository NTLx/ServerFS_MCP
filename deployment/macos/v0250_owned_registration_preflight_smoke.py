"""One real-home macOS named-owned registration preflight, never registration.

Opt-in. Stages a signed temporary named owner under user ServerFS/instances.
Calls only native status and register-preflight, never register or unregister.
Cleanup uses the conservative owned-unstage API.
"""

from __future__ import annotations

import argparse
import json
import secrets
import socket
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from serverfs_mcp.darwin_instance_owned_runtime import stage_owned_core_bridge
from serverfs_mcp.darwin_instance_owned_unstage import unstage_owned_candidate
from serverfs_mcp.darwin_instance_runtime import _manager, darwin_service
from serverfs_mcp.darwin_instances import APP_NAME, instance_app_path, instance_service_home
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


def _port() -> int:
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    if not parser.parse_args().execute:
        print("NOT_EXECUTED: pass --execute for native private preflight")
        return 2
    if sys.platform != "darwin":
        raise RuntimeError("native preflight requires macOS")
    baseline = tuple(darwin_service._launchd_job_pid(label) for label in PROTECTED)
    if any(pid is None for pid in baseline):
        raise RuntimeError("production service PID baseline missing")
    name = "v25-smoke-" + secrets.token_hex(4)
    identity = InstanceIdentity(instance_id=name, bridge_mode="owned", bridge_id=name)
    owner = instance_service_home(identity).parent
    if owner.exists() or owner.is_symlink():
        raise RuntimeError("random test instance already exists")
    staged = False
    eligible = False
    clean = False
    error = ""
    with tempfile.TemporaryDirectory(prefix="serverfs-v25-reg-preflight-") as scratch:
        root = Path(scratch).resolve(strict=True)
        try:
            workdir = root / "workdir"
            workdir.mkdir(mode=0o700)
            secrets_dir = root / "secrets"
            secrets_dir.mkdir(mode=0o700)
            config = root / "serverfs.toml"
            config.write_text(
                "[agent]\nenabled=true\n[agent.codex]\nenabled=true\n"
                "[[workdirs]]\nalias='synthetic'\n"
                f"path='{workdir}'\nread_only=false\n"
                "agent_mode='workspace-write'\nagent_runtimes=['codex']\n"
            )
            port = _port()
            env_file = secrets_dir / "http.env"
            env_file.write_text(
                "SERVERFS_HTTP_API_KEY=synthetic-only-no-real-key\n"
                f"SERVERFS_HTTP_ALLOWED_HOSTS=synthetic.test,127.0.0.1:{port}\n"
            )
            env_file.chmod(0o600)
            template = root / "build"
            subprocess.run(
                ["bash", str(BUILD), str(template)],
                capture_output=True,
                check=True,
                timeout=120,
            )
            result = stage_owned_core_bridge(
                identity,
                config,
                env_file,
                port,
                "synthetic.test",
                template / APP_NAME,
                BRIDGE_PYTHON,
            )
            staged = result["state"] == "staged-not-registered"
            app = instance_app_path(identity)
            manager = app / "Contents/MacOS/serverfs-service-manager"
            verified = subprocess.run(
                ["codesign", "--verify", "--deep", "--strict", str(app)],
                capture_output=True,
                check=False,
                timeout=30,
            )
            if verified.returncode != 0:
                raise RuntimeError("signed test App verification failed")
            report = subprocess.run(
                [str(manager), "register-preflight", "agent-bridge"],
                capture_output=True,
                text=True,
                check=False,
                timeout=20,
            )
            document = json.loads(report.stdout)
            eligible = (
                report.returncode == 0
                and document.get("ok") is True
                and document.get("eligible") is True
                and document.get("instance_id") == identity.instance_id
            )
            if not eligible:
                raise RuntimeError("native owned registration preflight refused")
            for service in ("agent-bridge", "http-origin"):
                if _manager(identity, "status", service) not in {"not-registered", "not-found"}:
                    raise RuntimeError("test service unexpectedly registered")
            manifest = instance_service_home(identity) / "services.json"
            if stat.S_IMODE(manifest.stat().st_mode) != 0o600:
                raise RuntimeError("named preflight manifest is not private")
        except Exception as exc:
            error = type(exc).__name__ + ": " + str(exc)
        finally:
            if owner.is_dir() and not owner.is_symlink():
                try:
                    unstage_owned_candidate(identity)
                except Exception as exc:
                    error = (error + "; " if error else "") + (
                        "conservative unstage refused: " + type(exc).__name__
                    )
            clean = not owner.exists()
    unchanged = tuple(darwin_service._launchd_job_pid(label) for label in PROTECTED) == baseline
    payload = {
        "result": "PASS"
        if staged and eligible and clean and unchanged and not error
        else "NOT_ACCEPTED",
        "signed_owned_app_staged": staged,
        "native_registration_eligible": eligible,
        "register_called": False,
        "instance_removed": clean,
        "production_pids_unchanged": unchanged,
        "error": error,
    }
    print(json.dumps(payload, sort_keys=True))
    return 0 if payload["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
