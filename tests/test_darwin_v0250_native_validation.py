"""Source-static checks for the macOS Swift Launcher validation boundary."""

from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def launcher() -> str:
    path = (
        Path(__file__).resolve().parents[1]
        / "native/macos/service_manager/Sources/ServiceLauncher/main.swift"
    )
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "case,source_fragments",
    [
        ("invalid key", ["unknownServiceKeys.isEmpty", "connectorServicePattern"]),
        (
            "missing or cross-instance signed plist",
            [
                "bundledAppContents()",
                'appendingPathComponent("Library/LaunchAgents", isDirectory: true)',
                'plist["Label"] as? String == label',
                'plist["ProgramArguments"] as? [String] == '
                '["serverfs-service-launcher", instanceID, key]',
            ],
        ),
        (
            "per-key logs and Core binding",
            [
                "logs/\\(logStem).stdout.log",
                "service.corePort == port",
                "service.workingDirectory == workingDirectory",
            ],
        ),
        (
            "provider owner and exact argv",
            [
                "independent-connector",
                "independent-openai-http",
                "owned Cloudflare argv mismatch",
                "owned OpenAI argv mismatch",
            ],
        ),
        ("duplicate Tunnel IDs", ["tunnelIDs.insert(tunnelID).inserted", "tunnelIDPattern"]),
        (
            "secure files and credential aliases",
            [
                "mode == 0o600",
                "owner mismatch",
                "resolvingSymlinksInPath().path",
                "sameFile(credentialFiles[index], credentialFiles[other])",
                "sameFile(cloudflareTokenFiles[index], cloudflareTokenFiles[other])",
            ],
        ),
        (
            "OpenAI bearer/control metadata",
            ["controlValue != coreBearer", 'bearerValue == "Bearer \\(coreBearer)"'],
        ),
    ],
)
def test_native_connector_validation_rejects_invalid_manifest_cases(
    launcher: str, case: str, source_fragments: list[str]
) -> None:
    for fragment in source_fragments:
        assert fragment in launcher, f"missing native guard for {case}: {fragment}"


@pytest.mark.parametrize(
    "manifest_owner,bridge_mode,bridge_id,required_services",
    [
        (
            "v0.25-named-owned-core",
            "owned",
            "fixture",
            {"http-origin", "agent-bridge"},
        ),
        ("v0.25-named-file-core", "disabled", None, {"http-origin"}),
    ],
)
def test_synthetic_owned_and_file_only_shapes_share_connector_validation(
    launcher: str,
    manifest_owner: str,
    bridge_mode: str,
    bridge_id: str | None,
    required_services: set[str],
) -> None:
    # Source-static fixtures exercise both accepted manifest owner shapes.
    services = {
        "http-origin": {},
        "cloudflare.blue": {},
        "tunnel.green": {},
    }
    if bridge_mode == "owned":
        services["agent-bridge"] = {}
    manifest = {
        "schema_version": 1,
        "owner": manifest_owner,
        "instance_id": "fixture",
        "bridge_mode": bridge_mode,
        "bridge_id": bridge_id,
        "services": services,
    }
    assert manifest_owner in launcher
    assert bridge_mode in launcher
    assert (
        manifest["bridge_id"] is None if bridge_id is None else manifest["bridge_id"] == "fixture"
    )
    assert required_services.issubset(manifest["services"])
    assert {"cloudflare.blue", "tunnel.green"}.issubset(manifest["services"])
    assert "manifest.bridgeID == nil" in launcher
    assert "manifest.bridgeID == instanceID" in launcher
    owner_gate = launcher.index(
        "guard manifest.instanceID == instanceID, fileOnly || owned || shared"
    )
    connector_loop = launcher.index("for key in declaredConnectorKeys")
    assert owner_gate < connector_loop
    assert "let connectorKeys = manifest.services.keys.filter" not in launcher


def test_native_dynamic_activation_is_gated_before_execution_side_effects(launcher: str) -> None:
    manager_path = (
        Path(__file__).resolve().parents[1]
        / "native/macos/service_manager/Sources/ServiceManager/main.swift"
    )
    manager = manager_path.read_text(encoding="utf-8")
    assert 'args[1] == "register", selected.contains(where: { $0.id.contains(".") })' in manager
    manifest_load = launcher.index("let manifest = try loadManifest(instanceID: instance)")
    config_lookup = launcher.index("guard let config = manifest.services[serviceID]")
    execution_guard = launcher.index("manifest.services[serviceID] != nil", config_lookup)
    side_effects = [
        launcher.index("try loadEnvironment(config.environmentFile)"),
        launcher.index("try prepareLog(config.stdoutLog"),
        launcher.index("chdir(workingDirectory.path)"),
        launcher.index("execv(executable.path, cArguments)"),
    ]
    assert manifest_load < config_lookup < execution_guard < min(side_effects)
    assert "Bundle.main.bundleURL" not in launcher
    assert "let contents = try bundledAppContents()" in launcher
    assert "args[1] == instance" in launcher
    assert 'instance != "default"' in launcher
    assert 'info["CFBundleIdentifier"] as? String == expectedBundleID' in launcher


def test_validate_only_is_exact_argv_side_effect_free_and_reuses_manifest_validation(
    launcher: str,
) -> None:
    validate = launcher.index('args[1] == "--validate-only"')
    normal_dispatch = launcher.index('if instance == "default" {', validate)
    branch = launcher[validate:normal_dispatch]
    assert "args.count == 4" in branch
    assert 'instance != "default"' in branch
    assert "args[2] == instance" in branch
    assert "connectorServicePattern.firstMatch" in branch
    assert 'guard isDynamicConnector || serviceID == "http-origin"' in branch
    assert 'if serviceID == "http-origin", ![' in branch
    for owner in ("file", "owned", "shared"):
        assert f'"v0.25-named-{owner}-core"' in branch
    assert "loadManifest(instanceID: instance)" in branch
    assert "manifest.services[serviceID] != nil" in branch
    assert '["ok": true]' in branch
    assert "exit(0)" in branch
    for forbidden in (
        "loadEnvironment(",
        "prepareLog(",
        "chdir(",
        "execv(",
        "SMAppService",
        "register(",
        "unregister(",
    ):
        assert forbidden not in branch


def test_service_manager_dynamic_register_and_diagnostic_share_strict_preflight() -> None:
    manager_path = (
        Path(__file__).resolve().parents[1]
        / "native/macos/service_manager/Sources/ServiceManager/main.swift"
    )
    manager = manager_path.read_text(encoding="utf-8")
    preflight = manager[
        manager.index("private func connectorRegistrationPreflight(") : manager.index(
            "private func states("
        )
    ]
    assert 'appendingPathComponent("Contents/Resources/serverfs-service-launcher"' in preflight
    assert 'process.arguments = ["--validate-only", instance, serviceKey]' in preflight
    assert 'serviceKey.hasPrefix("cloudflare.") || serviceKey.hasPrefix("tunnel.")' in preflight
    assert 'serviceKey == "http-origin"' in preflight
    assert "selectedSpecs(serviceKey, instance: instance)" in preflight
    assert 'guard instance != "default"' in preflight
    assert "validConnectorID(connectorID)" in manager
    assert 'plist["Label"] as? String == label' in manager
    assert (
        'plist["ProgramArguments"] as? [String] == ["serverfs-service-launcher", instance, raw]'
        in manager
    )
    assert "guard FileManager.default.fileExists(atPath: plistURL.path)" in manager
    assert 'data == Data("{\\"ok\\":true}\\n".utf8)' in preflight
    assert "process.terminationStatus == 0" in preflight
    assert ".now() + 10" in preflight
    assert "process.terminate()" in preflight
    assert "kill(process.processIdentifier, SIGKILL)" in preflight
    assert "FileHandle.nullDevice" in preflight
    assert "SMAppService" not in preflight
    assert "register(" not in preflight
    helper = manager[
        manager.index("private func connectorRegistrationPreflight(") : manager.index(
            "private func states("
        )
    ]
    assert (
        'Bundle.main.bundleIdentifier == "com.ntlx.serverfs.service-manager.\\(instance)"' in helper
    )
    assert (
        "private func connectorRegistrationPreflight("
        "_ serviceKey: String, instance: String) -> Bool" in helper
    )
    assert "return true" in helper and "return false" in helper
    register_path = manager[
        manager.index('case "register", "unregister", "status":') : manager.index(
            'case "register-preflight":'
        )
    ]
    assert "connectorRegistrationPreflight(spec.id, instance: instance)" in register_path
    assert 'connectorRegistrationPreflight("http-origin", instance: instance)' in register_path
    assert 'selected.contains(where: { $0.id == "http-origin" })' in register_path
    assert register_path.index(
        'connectorRegistrationPreflight("http-origin"'
    ) < register_path.index("register(specs)")
    assert register_path.index("connectorRegistrationPreflight") < register_path.index(
        "register(specs)"
    )
    diagnostic = manager[
        manager.index('case "register-preflight":') : manager.index('case "open-settings":')
    ]
    assert 'guard instance != "default"' in diagnostic
    assert "connectorRegistrationPreflight(args[2], instance: instance)" in diagnostic
    assert 'args[2] == "http-origin"' in diagnostic
    assert "dynamic Connector registration is not implemented" not in manager
    assert 'guard args[2] == "agent-bridge" else { usage() }' in manager


def test_named_owned_bridge_gate_accepts_only_signed_dynamic_service_keys() -> None:
    manager_path = (
        Path(__file__).resolve().parents[1]
        / "native/macos/service_manager/Sources/ServiceManager/main.swift"
    )
    manager = manager_path.read_text(encoding="utf-8")
    gate = manager[
        manager.index("private func namedOwnedRegistrationReady(") : manager.index(
            "let args = CommandLine.arguments"
        )
    ]
    assert "services.keys.allSatisfy({ key in" in gate
    assert 'Set(["http-origin", "agent-bridge"]).isSubset(of: Set(services.keys))' in gate
    assert 'if ["http-origin", "agent-bridge", "cloudflare", "tunnel"].contains(key)' in gate
    assert 'key.hasPrefix("cloudflare.") || key.hasPrefix("tunnel.")' in gate
    assert "selectedSpecs(key, instance: instance)?.count == 1" in gate
    assert 'guard ["cloudflare", "tunnel"].contains(provider)' in manager
    assert "validConnectorID(connectorID)" in manager
    assert 'plist["Label"] as? String == label' in manager
    assert (
        'plist["ProgramArguments"] as? [String] == ["serverfs-service-launcher", instance, raw]'
    ) in manager
    assert 'Set(["http-origin", "agent-bridge", "cloudflare", "tunnel"]).isSubset' not in gate

    diagnostic = manager[
        manager.index('case "register-preflight":') : manager.index('case "open-settings":')
    ]
    assert 'guard args[2] == "agent-bridge" else { usage() }' in diagnostic
    assert 'guard instance != "default"' in diagnostic
    assert "let eligible = namedOwnedRegistrationReady(instance: instance)" in diagnostic
