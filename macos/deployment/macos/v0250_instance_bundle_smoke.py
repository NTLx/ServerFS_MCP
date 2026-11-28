"""Build and inspect an isolated named macOS ServiceManager bundle.

No SMAppService register/unregister, launchctl bootstrap, live manifest or
production credentials are touched. Run explicitly on a supported Mac host.
"""

from __future__ import annotations

import json
import plistlib
import subprocess
import tempfile
from pathlib import Path

from serverfs_mcp.darwin_instances import APP_NAME, stage_instance_app
from serverfs_mcp.instance_identity import InstanceIdentity

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE = REPO_ROOT / "native" / "macos" / "service_manager"


def _run(*command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=True, timeout=180)


def main() -> None:
    instance = InstanceIdentity(instance_id="v25-smoke", bridge_mode="owned", bridge_id="v25-smoke")
    with tempfile.TemporaryDirectory(prefix="serverfs-v25-macos-") as temporary:
        root = Path(temporary).resolve()
        build = root / "build"
        _run("bash", str(SOURCE / "build.sh"), str(build))
        source_app = build / APP_NAME
        stage_root = root / "staged"
        stage_root.mkdir(mode=0o700)
        staged_app = stage_instance_app(instance, source_app, stage_root / APP_NAME)

        agents = staged_app / "Contents" / "Library" / "LaunchAgents"
        for component in ("core", "bridge", "cloudflare", "openai"):
            label = instance.macos_label(component)
            plist = agents / f"{label}.plist"
            _run("plutil", "-lint", str(plist))
            spec = plistlib.loads(plist.read_bytes())
            assert spec["Label"] == label
            assert spec["ProgramArguments"][1] == instance.instance_id
        _run("codesign", "--verify", "--deep", "--strict", str(staged_app))
        result = _run(str(staged_app / "Contents" / "MacOS" / "serverfs-service-manager"), "info")
        info = json.loads(result.stdout)
        assert info["ok"] is True
        assert info["instance_id"] == instance.instance_id
        assert info["bundle_identifier"] == ("com.ntlx.serverfs.service-manager.v25-smoke")
        assert (
            not (source_app / "Contents" / "Info.plist").read_bytes().find(b"ServerFSInstanceID")
            >= 0
        )
        print(
            json.dumps(
                {
                    "result": "PASS",
                    "identity": instance.instance_id,
                    "component_labels": 4,
                    "signed_bundle": True,
                    "installed": False,
                    "registered": False,
                },
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
