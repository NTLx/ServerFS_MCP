"""Exercise Swift Shared Core manifest gate with real-world canonical path shapes."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SWIFT_GATE = (
    Path(__file__).resolve().parents[1]
    / "native/macos/service_manager/Sources/ServiceLauncher/SharedManifestGate.swift"
)


@pytest.fixture(scope="module")
def executable_gate(tmp_path_factory: pytest.TempPathFactory) -> Path:
    swiftc = shutil.which("swiftc")
    if swiftc is None:
        pytest.skip("Swift compiler unavailable")
    root = tmp_path_factory.mktemp("gate-paths")
    harness = root / "main.swift"
    harness.write_text(
        """
import Foundation
let raw = FileHandle.standardInput.readDataToEndOfFile()
let home = CommandLine.arguments[1]
let ident = CommandLine.arguments[2]
do {
    _ = try validateSharedManifestGate(
        rawManifest: raw, instanceID: ident,
        expectedServiceHome: URL(fileURLWithPath: home, isDirectory: true)
    )
    print("accepted")
} catch {
    print("rejected")
}
""",
        encoding="utf-8",
    )
    binary = root / "gate"
    compiled = subprocess.run(
        [swiftc, str(SWIFT_GATE), str(harness), "-o", str(binary)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert compiled.returncode == 0, compiled.stderr
    return binary


@pytest.mark.parametrize(
    ("home", "config", "instance", "port"),
    [
        (
            "/Users/example/Library/Application Support/ServerFS/"
            "instances/shgateabcdef/service-manager",
            "/private/var/folders/demo/pytest-2/test/config.toml",
            "shgateabcdef",
            41001,
        ),
        (
            "/Users/example/Library/Application Support/ServerFS/"
            "instances/shgateabcdef/service-manager",
            "/private/var/folders/demo with spaces/pytest-2/test/config.toml",
            "shgateabcdef",
            42111,
        ),
        (
            "/Users/example/Library/Application Support/ServerFS/instances/core-a/service-manager",
            "/private/tmp/testdir/core.toml",
            "core-a",
            19001,
        ),
    ],
)
def test_gate_accepts_signed_staging_path_shapes(
    executable_gate: Path, home: str, config: str, instance: str, port: int
) -> None:
    service = {
        "executable": (
            "/Users/example/.local/share/uv/python/"
            "cpython-3.12.12-macos-aarch64-none/bin/python3.12"
        ),
        "arguments": [
            "-B",
            "-m",
            "serverfs_mcp.cli",
            "serve",
            "--transport",
            "streamable-http",
            "--config",
            config,
            "--http-port",
            str(port),
            "--macos-instance",
            instance,
            "--macos-shared-bridge",
            "test-owner",
        ],
        "working_directory": str(Path(config).parent),
        "environment_file": f"{home}/http-origin-environment.json",
        "stdout_log": f"{home}/logs/core.stdout.log",
        "stderr_log": f"{home}/logs/core.stderr.log",
        "origin_owner": "independent-core",
    }
    doc = {
        "schema_version": 1,
        "owner": "v0.25-named-shared-core",
        "instance_id": instance,
        "bridge_mode": "shared",
        "bridge_id": "test-owner",
        "core": {
            "config": config,
            "sha256": "b" * 64,
            "port": port,
            "hostname": "gate.example.test",
        },
        "services": {"http-origin": service},
    }
    result = subprocess.run(
        [str(executable_gate), home, instance],
        input=json.dumps(doc, sort_keys=True, separators=(",", ":")).encode(),
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert result.stdout.decode().strip() == "accepted"


def test_gate_preserves_physical_private_tmp_paths(executable_gate: Path, tmp_path: Path) -> None:
    """A real macOS /private/tmp path must not be rewritten to /tmp."""
    base = tmp_path.resolve()
    config = base / "core.toml"
    config.write_text("synthetic", encoding="utf-8")
    home = base / "home with spaces" / "instances" / "shgateabcdef" / "service-manager"
    home.mkdir(parents=True, mode=0o700)
    test_gate_accepts_signed_staging_path_shapes(
        executable_gate, str(home), str(config), "shgateabcdef", 39127
    )
