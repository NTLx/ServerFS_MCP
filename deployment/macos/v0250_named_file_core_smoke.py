"""Real macOS bundle and private-state acceptance without LaunchAgent registration.

Creates only a disposable instance with synthetic auth/token and a file-only
Core, checks actual codesign and ServiceManager status, stages a synthetic
Cloudflare Connector, then reverses both staging steps. No production state.
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
    unstage_cloudflare_connector,
    unstage_file_only_instance,
)
from serverfs_mcp.darwin_instances import APP_NAME, instance_app_path, instance_service_home
from serverfs_mcp.instance_identity import InstanceIdentity


def _run(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, check=True, capture_output=True, text=True, timeout=180)


def main() -> None:
    repo = Path(__file__).resolve().parents[2]
    source = repo / "native/macos/service_manager"
    identity = InstanceIdentity(instance_id="v25-file-smoke", bridge_mode="disabled")
    with tempfile.TemporaryDirectory(prefix="serverfs-v25-named-smoke-") as tmp:
        root = Path(tmp).resolve(strict=True)
        home = root / "user"
        base = home / "Library/Application Support/ServerFS"
        base.mkdir(parents=True, mode=0o700)
        base.chmod(0o700)
        workdir = root / "workdir"
        workdir.mkdir(mode=0o700)
        (workdir / "hello.txt").write_text("isolated")
        config = root / "file-only.toml"
        config.write_text(
            "[[workdirs]]\nalias = 'isolated'\n" + f"path = '{workdir}'\n" + "read_only = false\n"
        )
        credentials = root / "operator"
        credentials.mkdir(mode=0o700)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        env = credentials / "core.env"
        env.write_text(
            f"SERVERFS_HTTP_API_KEY=synthetic-disposable-key\n"
            f"SERVERFS_HTTP_ALLOWED_HOSTS=smoke.invalid,127.0.0.1:{port}\n"
        )
        env.chmod(0o600)
        built = root / "build"
        _run("bash", str(source / "build.sh"), str(built))
        result = stage_file_only_instance(
            identity, config, env, port, "smoke.invalid", built / APP_NAME, home=home
        )
        assert result["state"] == "staged-not-registered"

        app = instance_app_path(identity, home=home)
        _run("codesign", "--verify", "--deep", "--strict", str(app))
        info = json.loads(_run(str(app / "Contents/MacOS/serverfs-service-manager"), "info").stdout)
        assert info["ok"] is True
        assert info["instance_id"] == "v25-file-smoke"
        assert info["bundle_identifier"] == ("com.ntlx.serverfs.service-manager.v25-file-smoke")
        status = instance_status(identity, home=home)
        assert status["components"]["core"] in {"not-registered", "not-found"}
        assert status["bridge_mode"] == "disabled"
        private = instance_service_home(identity, home=home)
        data = private / "data"
        assert data.is_dir()
        snapshot = json.loads((private / "http-origin-environment.json").read_text())
        assert snapshot["SERVERFS_DATA_HOME"] == str(data)
        assert snapshot["SERVERFS_FILE_INGRESS_ENABLED"] == "false"
        assert snapshot["SERVERFS_HTTP_API_KEY"] == "synthetic-disposable-key"
        assert "synthetic-disposable-key" not in (private / "services.json").read_text()
        assert (private / "services.json").stat().st_mode & 0o777 == 0o600

        fake_binary = credentials / "synthetic-cloudflared"
        fake_binary.write_text("not an executable payload")
        fake_binary.chmod(0o700)
        fake_token = credentials / "cloudflare-token"
        fake_token.write_text("synthetic-token")
        fake_token.chmod(0o600)
        staged = stage_cloudflare_connector(identity, fake_binary, fake_token, home=home)
        assert staged["state"] == "staged-not-registered"
        status = instance_status(identity, home=home)
        assert set(status["components"]) == {"core", "cloudflare"}
        assert status["components"]["cloudflare"] in {"not-registered", "not-found"}
        unstage_cloudflare_connector(identity, home=home)
        unstage_file_only_instance(identity, home=home)
        assert not private.exists()
        assert config.exists() and env.exists() and fake_token.exists()
        print(
            json.dumps(
                {
                    "result": "PASS",
                    "instance": identity.instance_id,
                    "bundle_signed": True,
                    "file_only_core": True,
                    "cloudflare_stage_roundtrip": True,
                    "private_state_removed": True,
                    "registered": False,
                    "production_services_touched": False,
                },
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
