"""Executable shape checks for the source-only native Shared Core gate."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "native/macos/service_manager/Sources/ServiceLauncher/SharedManifestGate.swift"
LAUNCHER = ROOT / "native/macos/service_manager/Sources/ServiceLauncher/main.swift"


@pytest.fixture(scope="module")
def gate_binary(tmp_path_factory: pytest.TempPathFactory) -> Path:
    swiftc = shutil.which("swiftc")
    if swiftc is None:
        pytest.skip("swiftc is unavailable; executable Swift behavior is not verified")
    scratch = tmp_path_factory.mktemp("shared-swift-gate")
    harness = scratch / "main.swift"
    harness.write_text(
        """
import Foundation
let data = FileHandle.standardInput.readDataToEndOfFile()
do {
    _ = try validateSharedManifestGate(
        rawManifest: data,
        instanceID: "core-one",
        expectedServiceHome: URL(fileURLWithPath: "/tmp/service-home")
    )
    print("accepted")
} catch {
    print("rejected")
}
""",
        encoding="utf-8",
    )
    binary = scratch / "gate-check"
    result = subprocess.run(
        [swiftc, str(SOURCE), str(harness), "-o", str(binary)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return binary


def _manifest() -> dict:
    config = "/tmp/core/serverfs.toml"
    home = "/tmp/service-home"
    return {
        "schema_version": 1,
        "owner": "v0.25-named-shared-core",
        "instance_id": "core-one",
        "bridge_mode": "shared",
        "bridge_id": "bridge-one",
        "core": {
            "config": config,
            "sha256": "a" * 64,
            "port": 19283,
            "hostname": "core.example",
        },
        "services": {
            "http-origin": {
                "executable": "/usr/bin/python3",
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
                    "19283",
                    "--macos-instance",
                    "core-one",
                    "--macos-shared-bridge",
                    "bridge-one",
                ],
                "working_directory": "/tmp/core",
                "environment_file": f"{home}/http-origin-environment.json",
                "stdout_log": f"{home}/logs/core.stdout.log",
                "stderr_log": f"{home}/logs/core.stderr.log",
                "origin_owner": "independent-core",
            }
        },
    }


def _run(binary: Path, raw: bytes) -> str:
    result = subprocess.run([str(binary)], input=raw, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return result.stdout.decode().strip()


def test_accepts_exact_shared_shape(gate_binary: Path) -> None:
    assert _run(gate_binary, json.dumps(_manifest()).encode()) == "accepted"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda doc: doc.update(owner="v0.25-named-owned-core"),
        lambda doc: doc.update(bridge_mode="owned"),
        lambda doc: doc.update(bridge_id="bad id"),
        lambda doc: doc["services"].update({"agent-bridge": {}}),
        lambda doc: doc.update(unexpected=True),
        lambda doc: doc["core"].pop("hostname"),
        lambda doc: doc["services"]["http-origin"].update(
            stdout_log="/tmp/service-home/logs/http-origin.stdout.log"
        ),
        lambda doc: doc["services"]["http-origin"]["arguments"].__setitem__(-1, "other"),
        lambda doc: doc["services"]["http-origin"].update(
            environment_file="/tmp/other/http-origin-environment.json"
        ),
    ],
)
def test_rejects_shape_and_identity_drift(gate_binary: Path, mutate) -> None:
    doc = _manifest()
    mutate(doc)
    assert _run(gate_binary, json.dumps(doc).encode()) == "rejected"


@pytest.mark.parametrize(
    "raw",
    [
        b'{"schema_version":1,"schema_version":1}',
        b'{"schema_version":1,"\\u0073chema_version":1}',
        b" " * (16 * 1024 + 1),
        b'{"schema_version":01}',
    ],
)
def test_rejects_duplicate_keys_invalid_json_and_oversize(gate_binary: Path, raw: bytes) -> None:
    assert _run(gate_binary, raw) == "rejected"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 1.5),
        ("schema_version", 1e0),
        ("port", 19283.5),
        ("port", 19283.0),
        ("port", 4294967297),
    ],
)
def test_rejects_noninteger_schema_and_port(
    gate_binary: Path, field: str, value: float | int
) -> None:
    doc = _manifest()
    if field == "schema_version":
        doc[field] = value
    else:
        doc["core"][field] = value
    assert _run(gate_binary, json.dumps(doc).encode()) == "rejected"


def test_build_includes_shared_gate_in_launcher_compilation() -> None:
    build = (ROOT / "native/macos/service_manager/build.sh").read_text(encoding="utf-8")
    gate = '"$ROOT/Sources/ServiceLauncher/SharedManifestGate.swift"'
    launcher = '"$ROOT/Sources/ServiceLauncher/main.swift"'
    output = '-o "$RESOURCES/serverfs-service-launcher"'
    assert gate in build
    assert build.index(gate) < build.index(launcher) < build.index(output)


def test_rejects_excessive_json_nesting(gate_binary: Path) -> None:
    doc = _manifest()
    doc["extra"] = [[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[0]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]
    assert _run(gate_binary, json.dumps(doc).encode()) == "rejected"


def test_launcher_runs_shared_validation_before_execution_side_effects() -> None:
    source = LAUNCHER.read_text(encoding="utf-8")
    manifest_gate = source.index("validateSharedManifestGate(")
    config_hash = source.index("SHA256.hash(data: configData)")
    environment_gate = source.index("validateSharedEnvironmentGate(")
    load_environment = source.index("try loadEnvironment(config.environmentFile)")
    prepare_log = source.index("try prepareLog(config.stdoutLog")
    execute = source.index("execv(executable.path, cArguments)")
    assert manifest_gate < config_hash < environment_gate < load_environment
    assert environment_gate < prepare_log < execute
    assert 'serviceID != "http-origin"' in source
    assert "mode & 0o022 == 0" in source
