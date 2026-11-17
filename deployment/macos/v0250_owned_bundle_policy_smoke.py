"""Disposable macOS v0.25 owned-Bridge private policy + signed app smoke.

No SMAppService register/unregister, no native Bridge execution, no Core,
Tunnel, credential mutation or public network. All test data is synthetic.
"""

from __future__ import annotations

import json
import os
import plistlib
import socket
import stat
import subprocess
import tempfile
from pathlib import Path

from serverfs_mcp.darwin_instance_owned_runtime import stage_owned_core_bridge
from serverfs_mcp.darwin_instance_owned_staging import check_owned_bridge_policy
from serverfs_mcp.darwin_instances import APP_NAME, instance_service_home
from serverfs_mcp.instance_identity import InstanceIdentity

REPO = Path(__file__).resolve().parents[2]
BUILD_SCRIPT = REPO / "native/macos/service_manager/build.sh"
BRIDGE_PYTHON = REPO / "agent_bridge/.venv/bin/python"


def _run(*args: str) -> str:
    run = subprocess.run(args, capture_output=True, text=True, check=True, timeout=180)
    return run.stdout


def _private(path: Path) -> Path:
    if not path.exists():
        path.mkdir(mode=0o700, parents=True)
    path.chmod(0o700)
    return path


def main() -> None:
    identity = InstanceIdentity(
        instance_id="v25-owned-smoke", bridge_mode="owned", bridge_id="v25-owned-smoke"
    )
    with tempfile.TemporaryDirectory(prefix="serverfs-v25-owned-bundle-") as temp:
        base = Path(temp).resolve()
        user_home = _private(base / "user")
        _private(user_home / "Library/Application Support/ServerFS")
        service_root = instance_service_home(identity, home=user_home)

        workdir = _private(base / "synthetic-workdir")
        default_dir = _private(base / "production-synthetic-workdir")
        default = base / "default-bridge.json"
        default.write_text(
            json.dumps(
                {
                    "workdirs": [
                        {"alias": "default", "host_path": str(default_dir), "read_only": False}
                    ]
                }
            )
        )
        default.chmod(0o600)
        operator = base / "serverfs.toml"
        operator.write_text(
            "[agent]\nenabled = true\n"
            "[agent.codex]\nenabled = true\n"
            "[[workdirs]]\nalias = 'staging'\n"
            f"path = '{workdir}'\n"
            "read_only = false\n"
            "agent_mode = 'workspace-write'\n"
            "agent_runtimes = ['codex']\n"
        )

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        secret_dir = _private(base / "synthetic-secrets")
        env_file = secret_dir / "http.env"
        env_file.write_text(
            "SERVERFS_HTTP_API_KEY=synthetic-only-test-key\n"
            f"SERVERFS_HTTP_ALLOWED_HOSTS=synthetic.test,127.0.0.1:{port}\n"
        )
        env_file.chmod(0o600)

        template_dir = base / "build"
        _run("bash", str(BUILD_SCRIPT), str(template_dir))
        staged = stage_owned_core_bridge(
            identity,
            operator,
            env_file,
            port,
            "synthetic.test",
            template_dir / APP_NAME,
            BRIDGE_PYTHON,
            home=user_home,
            default_bridge_config=default,
        )
        assert staged["state"] == "staged-not-registered"
        app = service_root / APP_NAME
        _run("codesign", "--verify", "--deep", "--strict", str(app))
        info = json.loads(_run(str(app / "Contents/MacOS/serverfs-service-manager"), "info"))
        assert info["instance_id"] == identity.instance_id
        assert info["bundle_identifier"].endswith("." + identity.instance_id)

        # These are app-associated plist resources, not launchd registrations.
        bridge_plist = (
            app / "Contents/Library/LaunchAgents" / (identity.macos_label("bridge") + ".plist")
        )
        assert bridge_plist.is_file()
        spec = plistlib.loads(bridge_plist.read_bytes())
        assert spec["Label"] == identity.macos_label("bridge")
        assert spec["ProgramArguments"] == [
            "serverfs-service-launcher",
            identity.instance_id,
            "agent-bridge",
        ]

        assert staged["bridge_mode"] == "owned"
        manifest_path = service_root / "services.json"
        assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600
        manifest = json.loads(manifest_path.read_text())
        assert manifest["owner"] == "v0.25-named-owned-core"
        assert set(manifest["services"]) == {"http-origin", "agent-bridge"}
        assert manifest["services"]["http-origin"]["arguments"][-2:] == [
            "--macos-instance",
            identity.instance_id,
        ]
        assert "synthetic-only-test-key" not in manifest_path.read_text()
        bridge_home = service_root / "data/agent-bridge"
        config = bridge_home / "bridge.json"
        assert stat.S_IMODE(config.stat().st_mode) == 0o600
        document = json.loads(config.read_text())
        assert document["socket_path"].endswith("/bridge.sock")
        assert document["allowed_peer_uid"] == os.getuid()
        assert "allowed_peer_gid" not in document
        assert document["state_dir"] == str(bridge_home / "state")
        assert document["lock_dir"] == str(bridge_home / "locks")
        before = config.read_bytes()
        checked = check_owned_bridge_policy(
            identity,
            operator,
            BRIDGE_PYTHON,
            home=user_home,
            default_bridge_config=default,
        )
        assert checked["result"] == "PASS_READ_ONLY"
        assert config.read_bytes() == before

        print(
            json.dumps(
                {
                    "result": "PASS",
                    "named_signed_app": True,
                    "bridge_config_validated_by_bridge_package": True,
                    "policy_drift_check": True,
                    "owned_core_manifest_validated": True,
                    "native_bridge_plist_staged": True,
                    "registered": False,
                    "agent_executed": False,
                    "production_modified": False,
                    "temporary_state_retained": False,
                },
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
