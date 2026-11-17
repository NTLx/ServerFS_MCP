"""macOS v0.25 named file-only instance: real signed-bundle staging smoke.

Uses a disposable home under a temporary directory. Compiles the Swift
ServiceManager, stages a separately signed instance app, verifies Core and
both Connector service identities through read-only ServiceManagement status,
then safely unstages all temporary state. Does not call register/unregister,
touch production home, launchd jobs, tunnels, Agent Bridge or real credentials.
"""

from __future__ import annotations

import json
import socket
import subprocess
import tempfile
from pathlib import Path

from serverfs_mcp.darwin_instance_runtime import (
    instance_status,
    stage_cloudflare_connector,
    stage_file_only_instance,
    stage_openai_http_connector,
    unstage_cloudflare_connector,
    unstage_file_only_instance,
    unstage_openai_http_connector,
)
from serverfs_mcp.darwin_instances import APP_NAME, instance_app_path
from serverfs_mcp.instance_identity import InstanceIdentity

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE = REPO_ROOT / "native" / "macos" / "service_manager"


def _port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _run(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=True, timeout=180)


def _write_private(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)
    return path


def main() -> None:
    identity = InstanceIdentity(instance_id="v25-runtime-smoke", bridge_mode="disabled")
    port = _port()
    hostname = "staging.example.test"
    api_key = "v0250-local-test-only-core-key"
    tunnel_id = "tunnel_0123456789abcdef0123456789abcdef"

    with tempfile.TemporaryDirectory(prefix="serverfs-v25-native-") as temp:
        home = Path(temp).resolve()
        user_data = home / "Library/Application Support/ServerFS"
        user_data.mkdir(parents=True, mode=0o700)
        private = home / "private"
        private.mkdir(mode=0o700)
        workdir = home / "workdir"
        workdir.mkdir(mode=0o700)
        config = home / "serverfs.toml"
        config.write_text(
            f'[[workdirs]]\nalias = "isolated"\npath = "{workdir}"\nread_only = false\n',
            encoding="utf-8",
        )
        env = _write_private(
            private / "core.env",
            f"SERVERFS_HTTP_API_KEY={api_key}\n"
            f"SERVERFS_HTTP_ALLOWED_HOSTS={hostname},127.0.0.1:{port}\n",
        )
        token = _write_private(private / "cf-token", "synthetic-test-only-token")
        control = _write_private(private / "control-key", "synthetic-control-secret")
        bearer = _write_private(private / "core-bearer", f"Bearer {api_key}")
        # These binaries are only validated as paths, not executed or registered.
        cloudflared = private / "cloudflared"
        openai = private / "tunnel-client"
        for binary in (cloudflared, openai):
            binary.write_bytes(b"synthetic-binary-never-executed")
            binary.chmod(0o700)

        build = home / "build"
        _run("bash", str(SOURCE / "build.sh"), str(build))
        stage = stage_file_only_instance(
            identity, config, env, port, hostname, build / APP_NAME, home=home
        )
        assert stage["state"] == "staged-not-registered"
        app = instance_app_path(identity, home=home)
        _run("codesign", "--verify", "--deep", "--strict", str(app))
        manager = str(app / "Contents/MacOS/serverfs-service-manager")
        info = json.loads(_run(manager, "info").stdout)
        assert info["instance_id"] == identity.instance_id
        assert info["bundle_identifier"].endswith("." + identity.instance_id)

        stage_cloudflare_connector(identity, cloudflared, token, home=home)
        stage_openai_http_connector(identity, openai, tunnel_id, control, bearer, home=home)
        report = instance_status(identity, home=home)
        assert report["instance_id"] == identity.instance_id
        assert report["core_port"] == port
        assert report["bridge_mode"] == "disabled"
        assert set(report["components"]) == {"core", "cloudflare", "openai"}
        assert all(
            state in {"not-registered", "not-found"} for state in report["components"].values()
        )
        assert all(pid is None for pid in report["pids"].values())
        assert not (
            app / "Contents/Library/LaunchAgents" / f"{identity.macos_label('bridge')}.plist"
        ).exists()

        unstage_openai_http_connector(identity, home=home)
        unstage_cloudflare_connector(identity, home=home)
        unstage_file_only_instance(identity, home=home)
        assert not (user_data / "instances" / identity.instance_id).exists()
        assert config.exists() and env.exists() and control.exists() and bearer.exists()

        print(
            json.dumps(
                {
                    "result": "PASS",
                    "instance_id": identity.instance_id,
                    "service_components": sorted(report["components"]),
                    "registered": False,
                    "production_service_changed": False,
                    "temporary_state_removed": True,
                },
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
