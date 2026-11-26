import Foundation
import Darwin
import CryptoKit

private let allowedServices: Set<String> = ["tunnel", "agent-bridge", "http-origin", "cloudflare"]
private let connectorServicePattern = try! NSRegularExpression(
    pattern: "^(cloudflare|tunnel)\\.[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$"
)
private let tunnelIDPattern = try! NSRegularExpression(pattern: "^tunnel_[a-z0-9]{32}$")
private let environmentKeyPattern = try! NSRegularExpression(pattern: "^[A-Za-z_][A-Za-z0-9_]*$")

private struct Manifest: Decodable {
    let schemaVersion: Int
    let services: [String: ServiceConfig]
    let owner: String?
    let instanceID: String?
    let bridgeMode: String?
    let bridgeID: String?

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case services
        case owner
        case instanceID = "instance_id"
        case bridgeMode = "bridge_mode"
        case bridgeID = "bridge_id"
    }
}

private struct ServiceConfig: Decodable {
    let executable: String
    let arguments: [String]
    let environmentFile: String?
    let workingDirectory: String?
    let stdoutLog: String
    let stderrLog: String
    let bridgePython: String?
    let bridgeConfig: String?
    let protocolMode: String?
    let originOwner: String?
    let connectorOwner: String?
    let corePort: Int?
    let controlKeyFile: String?
    let coreBearerFile: String?
    let tunnelID: String?

    enum CodingKeys: String, CodingKey {
        case executable
        case arguments
        case environmentFile = "environment_file"
        case workingDirectory = "working_directory"
        case stdoutLog = "stdout_log"
        case stderrLog = "stderr_log"
        case bridgePython = "bridge_python"
        case bridgeConfig = "bridge_config"
        case protocolMode = "protocol_mode"
        case originOwner = "origin_owner"
        case connectorOwner = "connector_owner"
        case corePort = "core_port"
        case controlKeyFile = "control_key_file"
        case coreBearerFile = "core_bearer_file"
        case tunnelID = "tunnel_id"
    }
}

private enum LauncherError: Error, CustomStringConvertible {
    case invalidArguments
    case invalidManifest(String)
    case invalidPath(String)
    case insecureFile(String)
    case io(String)

    var description: String {
        switch self {
        case .invalidArguments:
            return "expected exactly one service id: tunnel, agent-bridge, http-origin, or cloudflare"
        case let .invalidManifest(reason):
            return "invalid service manifest: \(reason)"
        case let .invalidPath(reason):
            return "invalid service path: \(reason)"
        case let .insecureFile(reason):
            return "insecure private file: \(reason)"
        case let .io(reason):
            return "service launcher I/O error: \(reason)"
        }
    }
}

// LaunchAgent argv[0] is not a trusted bundle locator. Resolve the actual
// executable image and bind its signed app metadata to the private manifest path.
private func bundledAppContents() throws -> URL {
    var length: UInt32 = 0
    _ = _NSGetExecutablePath(nil, &length)
    guard length > 0 && length < 4096 else {
        throw LauncherError.invalidPath("launcher executable location is invalid")
    }
    var buffer = [CChar](repeating: 0, count: Int(length))
    guard _NSGetExecutablePath(&buffer, &length) == 0 else {
        throw LauncherError.invalidPath("launcher executable location is unavailable")
    }
    let executable = URL(fileURLWithPath: String(cString: buffer)).resolvingSymlinksInPath()
    let contents = executable.deletingLastPathComponent().deletingLastPathComponent()
    guard executable.lastPathComponent == "serverfs-service-launcher",
          contents.lastPathComponent == "Contents",
          contents.deletingLastPathComponent().lastPathComponent == "ServerFSServiceManager.app"
    else {
        throw LauncherError.invalidPath("launcher must be inside its managed app bundle")
    }
    return contents
}

private func bundledInstanceID() throws -> String {
    let contents = try bundledAppContents()
    let infoURL = contents.appendingPathComponent("Info.plist", isDirectory: false)
    guard let data = try? Data(contentsOf: infoURL),
          let info = (try? PropertyListSerialization.propertyList(from: data, format: nil)) as? [String: Any]
    else {
        throw LauncherError.invalidManifest("missing app identity")
    }
    let id = info["ServerFSInstanceID"] as? String ?? "default"
    let pattern = try! NSRegularExpression(pattern: "^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$")
    let range = NSRange(id.startIndex..<id.endIndex, in: id)
    guard pattern.firstMatch(in: id, range: range)?.range == range else {
        throw LauncherError.invalidManifest("invalid bundled instance identity")
    }
    let expectedBundleID = id == "default"
        ? "com.ntlx.serverfs.service-manager"
        : "com.ntlx.serverfs.service-manager.\(id)"
    guard info["CFBundleIdentifier"] as? String == expectedBundleID else {
        throw LauncherError.invalidManifest("bundled app identity mismatch")
    }
    return id
}

private func dataHome() -> URL {
    if let override = ProcessInfo.processInfo.environment["SERVERFS_DATA_HOME"]?.trimmingCharacters(in: .whitespacesAndNewlines),
       !override.isEmpty
    {
        return URL(fileURLWithPath: override, isDirectory: true)
    }
    return FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/ServerFS", isDirectory: true)
}

private func manifestURL(instanceID: String) -> URL {
    if instanceID == "default" {
        return dataHome().appendingPathComponent("service-manager/services.json", isDirectory: false)
    }
    // Named instances never inherit a SERVERFS_DATA_HOME override from a child.
    return FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/ServerFS", isDirectory: true)
        .appendingPathComponent("instances/\(instanceID)/service-manager/services.json", isDirectory: false)
}

private func requireAbsolute(_ raw: String, name: String) throws -> URL {
    guard raw.hasPrefix("/") else { throw LauncherError.invalidPath("\(name) must be absolute") }
    let url = URL(fileURLWithPath: raw).standardizedFileURL
    guard url.path == raw || URL(fileURLWithPath: raw).resolvingSymlinksInPath().path == url.resolvingSymlinksInPath().path else {
        throw LauncherError.invalidPath("\(name) is not canonical")
    }
    return url
}

// Shared v2 Core inputs are pinned by exact physical paths from Python's
// Path.resolve(strict: true). Foundation standardizedFileURL can rewrite a
// physical /private/tmp path as /tmp, which is a different string even when
// it points at the same file. Validate raw paths with POSIX realpath instead;
// reject symlinks and lexical dot/slash aliases without weakening provenance.
private func requirePhysicalSharedFile(_ raw: String, name: String) throws -> URL {
    let components = raw.split(separator: "/", omittingEmptySubsequences: false)
    guard raw.hasPrefix("/"), !raw.hasSuffix("/"), !raw.contains("//"),
          !components.contains(".") && !components.contains(".."),
          let resolved = realpath(raw, nil)
    else { throw LauncherError.invalidPath("\(name) is not canonical") }
    defer { free(resolved) }
    guard String(cString: resolved) == raw else {
        throw LauncherError.insecureFile("\(name) path contains a symlink or alias")
    }
    return URL(fileURLWithPath: raw, isDirectory: false)
}

private func requirePrivateRegularFile(_ url: URL, name: String) throws {
    let attrs: [FileAttributeKey: Any]
    do {
        attrs = try FileManager.default.attributesOfItem(atPath: url.path)
    } catch {
        throw LauncherError.io("cannot stat \(name)")
    }
    guard attrs[.type] as? FileAttributeType == .typeRegular else {
        throw LauncherError.invalidPath("\(name) must be a regular file")
    }
    let owner = (attrs[.ownerAccountID] as? NSNumber)?.uint32Value
    guard owner == getuid() else { throw LauncherError.insecureFile("\(name) owner mismatch") }
    let mode = (attrs[.posixPermissions] as? NSNumber)?.intValue ?? -1
    guard mode == 0o600 else { throw LauncherError.insecureFile("\(name) mode must be 0600") }
}

private func validateSharedCore(_ raw: Data, instanceID: String, serviceHome: URL) throws {
    let gate: SharedManifestGateCore
    do {
        gate = try validateSharedManifestGate(
            rawManifest: raw, instanceID: instanceID, expectedServiceHome: serviceHome
        )
    } catch {
        throw LauncherError.invalidManifest("shared Core manifest validation failed")
    }
    let config = try requirePhysicalSharedFile(gate.config, name: "shared Core config")
    guard let attrs = try? FileManager.default.attributesOfItem(atPath: config.path),
          attrs[.type] as? FileAttributeType == .typeRegular,
          let configData = try? Data(contentsOf: config),
          SHA256.hash(data: configData).map({ String(format: "%02x", $0) }).joined() == gate.sha256
    else { throw LauncherError.invalidManifest("shared Core config is untrusted") }

    let environmentURL = try requirePhysicalSharedFile(
        gate.service.environmentFile, name: "shared Core environment"
    )
    try requirePrivateRegularFile(environmentURL, name: "shared Core environment")
    guard let environment = try? Data(contentsOf: environmentURL) else {
        throw LauncherError.invalidManifest("shared Core environment is unavailable")
    }
    do {
        try validateSharedEnvironmentGate(
            rawEnvironment: environment, instanceID: instanceID,
            expectedDataHome: serviceHome.appendingPathComponent("data", isDirectory: true),
            hostname: gate.hostname, port: gate.port
        )
    } catch {
        throw LauncherError.invalidManifest("shared Core environment validation failed")
    }

    let contents = try bundledAppContents()
    let infoURL = contents.appendingPathComponent("Info.plist", isDirectory: false)
    guard let infoData = try? Data(contentsOf: infoURL),
          let info = (try? PropertyListSerialization.propertyList(from: infoData, format: nil)) as? [String: Any],
          let pinnedPython = info["ServerFSSharedCorePythonPath"] as? String,
          !pinnedPython.isEmpty,
          pinnedPython == gate.service.executable
    else { throw LauncherError.invalidManifest("shared Core Python pin is missing or mismatched") }
    let executable = try requirePhysicalSharedFile(
        gate.service.executable, name: "shared Core executable"
    )
    guard let executableAttrs = try? FileManager.default.attributesOfItem(atPath: executable.path),
          executableAttrs[.type] as? FileAttributeType == .typeRegular,
          (executableAttrs[.ownerAccountID] as? NSNumber)?.uint32Value == getuid(),
          let mode = (executableAttrs[.posixPermissions] as? NSNumber)?.intValue,
          mode & 0o022 == 0, FileManager.default.isExecutableFile(atPath: executable.path)
    else { throw LauncherError.insecureFile("shared Core executable is untrusted") }
}

private func sameFile(_ lhs: URL, _ rhs: URL) -> Bool {
    guard let left = try? FileManager.default.attributesOfItem(atPath: lhs.path),
          let right = try? FileManager.default.attributesOfItem(atPath: rhs.path),
          let leftDevice = (left[.systemNumber] as? NSNumber)?.uint64Value,
          let rightDevice = (right[.systemNumber] as? NSNumber)?.uint64Value,
          let leftInode = (left[.systemFileNumber] as? NSNumber)?.uint64Value,
          let rightInode = (right[.systemFileNumber] as? NSNumber)?.uint64Value
    else { return false }
    return leftDevice == rightDevice && leftInode == rightInode
}

private func requirePrivateDirectory(_ url: URL, name: String) throws {
    let attrs: [FileAttributeKey: Any]
    do {
        attrs = try FileManager.default.attributesOfItem(atPath: url.path)
    } catch {
        throw LauncherError.io("cannot inspect \(name)")
    }
    guard attrs[.type] as? FileAttributeType == .typeDirectory,
          (attrs[.ownerAccountID] as? NSNumber)?.uint32Value == getuid(),
          let mode = (attrs[.posixPermissions] as? NSNumber)?.intValue,
          mode & 0o077 == 0
    else {
        throw LauncherError.insecureFile("\(name) must be a private current-user directory")
    }
}


private func verifyOwnedConnector(
    _ service: ServiceConfig,
    provider: String,
    connectorID: String?,
    serviceHome: URL,
    port: Int,
    workingDirectory: String?,
    coreBearer: String?
) throws {
    let logName = provider == "tunnel" ? "openai" : "cloudflare"
    let logStem = connectorID.map { "\(logName).\($0)" } ?? logName
    guard service.corePort == port,
          service.environmentFile == nil,
          service.workingDirectory == workingDirectory,
          service.stdoutLog == serviceHome.appendingPathComponent("logs/\(logStem).stdout.log").path,
          service.stderrLog == serviceHome.appendingPathComponent("logs/\(logStem).stderr.log").path
    else { throw LauncherError.invalidManifest("owned Connector origin does not match Core") }
    let binary = try requireAbsolute(service.executable, name: "Connector executable")
    guard binary.path == binary.resolvingSymlinksInPath().path,
          let attributes = try? FileManager.default.attributesOfItem(atPath: binary.path),
          attributes[.type] as? FileAttributeType == .typeRegular,
          (attributes[.ownerAccountID] as? NSNumber)?.uint32Value == getuid(),
          let mode = (attributes[.posixPermissions] as? NSNumber)?.intValue,
          mode & 0o022 == 0,
          FileManager.default.isExecutableFile(atPath: binary.path)
    else { throw LauncherError.insecureFile("owned Connector executable is untrusted") }

    if provider == "cloudflare" {
        guard service.connectorOwner == "independent-connector",
              service.arguments.count == 5,
              Array(service.arguments.prefix(4)) == [
                  "tunnel", "--no-autoupdate", "run", "--token-file"
              ]
        else { throw LauncherError.invalidManifest("owned Cloudflare argv mismatch") }
        let token = try requireAbsolute(service.arguments[4], name: "Cloudflare token")
        guard token.path == token.resolvingSymlinksInPath().path else {
            throw LauncherError.insecureFile("owned Cloudflare token symlink")
        }
        try requirePrivateRegularFile(token, name: "owned Cloudflare token")
    } else if provider == "tunnel" {
        guard service.connectorOwner == "independent-openai-http",
              let tunnelID = service.tunnelID,
              tunnelIDPattern.firstMatch(
                  in: tunnelID, range: NSRange(tunnelID.startIndex..<tunnelID.endIndex, in: tunnelID)
              )?.range == NSRange(tunnelID.startIndex..<tunnelID.endIndex, in: tunnelID),
              let controlRaw = service.controlKeyFile,
              let bearerRaw = service.coreBearerFile,
              controlRaw != bearerRaw
        else { throw LauncherError.invalidManifest("owned OpenAI identity mismatch") }
        let control = try requireAbsolute(controlRaw, name: "OpenAI control key")
        let bearer = try requireAbsolute(bearerRaw, name: "OpenAI Core bearer")
        guard control.path == control.resolvingSymlinksInPath().path,
              bearer.path == bearer.resolvingSymlinksInPath().path
        else { throw LauncherError.insecureFile("owned OpenAI credential symlink") }
        try requirePrivateRegularFile(control, name: "owned OpenAI control key")
        try requirePrivateRegularFile(bearer, name: "owned OpenAI Core bearer")
        guard let coreBearer,
              let controlValue = try? String(contentsOf: control, encoding: .utf8),
              let bearerValue = try? String(contentsOf: bearer, encoding: .utf8),
              !controlValue.isEmpty,
              controlValue == controlValue.trimmingCharacters(in: .whitespacesAndNewlines),
              controlValue != coreBearer,
              bearerValue == "Bearer \(coreBearer)"
        else { throw LauncherError.invalidManifest("OpenAI credential metadata mismatch") }
        // Foundation can normalize /private/var to /var. Match the original
        // manifest argument spelling after validating its resolved file.
        let expectedArguments = [
            "run",
            "--control-plane.tunnel-id", tunnelID,
            "--control-plane.api-key", "file:\(controlRaw)",
            "--mcp.server-url", "url=http://127.0.0.1:\(port)/mcp",
            "--mcp.extra-headers", "Authorization: file:\(bearerRaw)",
            "--mcp.discovery-extra-headers", "Authorization: file:\(bearerRaw)",
            "--health.listen-addr", "127.0.0.1:0"
        ]
        if service.arguments != expectedArguments {
            let firstMismatch = zip(service.arguments, expectedArguments)
                .enumerated()
                .first(where: { $0.element.0 != $0.element.1 })?.offset
                ?? Swift.min(service.arguments.count, expectedArguments.count)
            // Do not log argument values: they may contain private credential file paths.
            throw LauncherError.invalidManifest(
                "owned OpenAI argv mismatch at index \(firstMismatch)"
            )
        }
    } else {
        throw LauncherError.invalidManifest("unknown owned Connector")
    }
}

private func verifyDynamicConnectorLaunchAgent(_ key: String, instanceID: String) throws {
    let parts = key.split(separator: ".", omittingEmptySubsequences: false)
    guard parts.count == 2,
          let provider = parts.first.map(String.init),
          let connectorID = parts.last.map(String.init),
          ["cloudflare", "tunnel"].contains(provider)
    else { throw LauncherError.invalidManifest("invalid dynamic Connector key") }
    let component = provider == "cloudflare" ? "cloudflare" : "openai"
    let label = "com.ntlx.serverfs.\(instanceID).\(component).\(connectorID)"
    let plistURL = try bundledAppContents()
        .appendingPathComponent("Library/LaunchAgents", isDirectory: true)
        .appendingPathComponent("\(label).plist", isDirectory: false)
    guard plistURL.standardizedFileURL.path == plistURL.resolvingSymlinksInPath().path,
          let data = try? Data(contentsOf: plistURL),
          let plist = try? PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any],
          plist["Label"] as? String == label,
          plist["ProgramArguments"] as? [String] == ["serverfs-service-launcher", instanceID, key]
    else { throw LauncherError.invalidManifest("signed dynamic Connector LaunchAgent mismatch") }
}

private func loadManifest(instanceID: String) throws -> Manifest {
    let url = manifestURL(instanceID: instanceID)
    if instanceID != "default" {
        let serviceHome = url.deletingLastPathComponent()
        let instanceHome = serviceHome.deletingLastPathComponent()
        let instancesHome = instanceHome.deletingLastPathComponent()
        try requirePrivateDirectory(instancesHome, name: "instances")
        try requirePrivateDirectory(instanceHome, name: "instance")
        try requirePrivateDirectory(serviceHome, name: "service-manager")
        guard url.standardizedFileURL.path == url.resolvingSymlinksInPath().path else {
            throw LauncherError.insecureFile("named manifest must not be a symlink")
        }
    }
    try requirePrivateRegularFile(url, name: "services.json")
    let data: Data
    do {
        data = try Data(contentsOf: url, options: [.mappedIfSafe])
    } catch {
        throw LauncherError.io("cannot read services.json")
    }
    let manifest: Manifest
    do {
        manifest = try JSONDecoder().decode(Manifest.self, from: data)
    } catch {
        throw LauncherError.invalidManifest("JSON schema mismatch")
    }
    guard manifest.schemaVersion == 1 else {
        throw LauncherError.invalidManifest("unsupported schema_version")
    }
    let unknownServiceKeys = Set(manifest.services.keys).filter { key in
        if allowedServices.contains(key) { return false }
        let range = NSRange(key.startIndex..<key.endIndex, in: key)
        return connectorServicePattern.firstMatch(in: key, range: range)?.range != range
    }
    guard unknownServiceKeys.isEmpty else {
        throw LauncherError.invalidManifest("unknown service id")
    }
    if instanceID != "default" {
        let serviceHome = url.deletingLastPathComponent()
        if manifest.owner == "v0.25-named-shared-core" {
            let instanceHome = serviceHome.deletingLastPathComponent()
            let instancesHome = instanceHome.deletingLastPathComponent()
            for (directory, name) in [(instancesHome, "instances"), (instanceHome, "instance"), (serviceHome, "service-manager")] {
                let mode = (try? FileManager.default.attributesOfItem(atPath: directory.path)[.posixPermissions] as? NSNumber)?.intValue
                guard mode == 0o700 else { throw LauncherError.insecureFile("named \(name) mode must be 0700") }
            }
            guard manifest.bridgeMode == "shared", Set(manifest.services.keys) == ["http-origin"] else {
                throw LauncherError.invalidManifest("named shared service policy mismatch")
            }
            try validateSharedCore(data, instanceID: instanceID, serviceHome: serviceHome)
            guard manifest.instanceID == instanceID else {
                throw LauncherError.invalidManifest("named shared instance identity mismatch")
            }
            return manifest
        }
        let fileOnly = manifest.owner == "v0.25-named-file-core"
            && manifest.bridgeMode == "disabled"
            && manifest.bridgeID == nil
            && manifest.services["agent-bridge"] == nil
            && manifest.services["http-origin"] != nil
        let owned = manifest.owner == "v0.25-named-owned-core"
            && manifest.bridgeMode == "owned"
            && manifest.bridgeID == instanceID
            && Set(["http-origin", "agent-bridge"]).isSubset(of: Set(manifest.services.keys))
            && Set(manifest.services.keys).subtracting(
                ["http-origin", "agent-bridge", "cloudflare", "tunnel"]
            ).allSatisfy { key in
                let range = NSRange(key.startIndex..<key.endIndex, in: key)
                return connectorServicePattern.firstMatch(in: key, range: range)?.range == range
            }
        let shared = manifest.owner == "v0.25-named-shared-core"
            && manifest.bridgeMode == "shared"
            && manifest.bridgeID != nil
            && Set(manifest.services.keys) == ["http-origin"]
        guard manifest.instanceID == instanceID, fileOnly || owned || shared else {
            throw LauncherError.invalidManifest("named instance owner or bridge policy mismatch")
        }
        var corePort: Int?
        var coreWorkingDirectory: String?
        if owned {
            // The Bridge's registered launchd job execs ONLY the bounded
            // macOS supervisor, not the Agent runtime directly. That owner
            // retains its bootstrap pipe; prior execution containment is
            // never inferred from a launched process alone.
            guard let core = manifest.services["http-origin"],
                  let bridge = manifest.services["agent-bridge"]
            else {
                throw LauncherError.invalidManifest("named owned services are incomplete")
            }
            let data = serviceHome.appendingPathComponent("data", isDirectory: true)
            let bridgeHome = data.appendingPathComponent("agent-bridge", isDirectory: true)
            let config = bridgeHome.appendingPathComponent("bridge.json")
            let v2 = bridge.protocolMode == "v2"
            let expectedSuffix = ["--macos-instance", instanceID]
                + (v2 ? ["--macos-agent-protocol", "v2"] : [])
            guard bridge.protocolMode == nil || v2,
                  core.originOwner == "independent-core",
                  Array(core.arguments.suffix(expectedSuffix.count)) == expectedSuffix,
                  core.environmentFile == serviceHome.appendingPathComponent("http-origin-environment.json").path,
                  bridge.originOwner == "independent-bridge",
                  bridge.executable == core.executable,
                  bridge.arguments == [
                      "-B", "-m", "serverfs_mcp.darwin_owned_bridge_supervisor",
                      "--instance", instanceID
                  ],
                  bridge.bridgeConfig == config.path,
                  bridge.environmentFile == nil,
                  bridge.workingDirectory == data.path,
                  bridge.stdoutLog == serviceHome.appendingPathComponent("logs/bridge.stdout.log").path,
                  bridge.stderrLog == serviceHome.appendingPathComponent("logs/bridge.stderr.log").path,
                  core.stdoutLog == serviceHome.appendingPathComponent("logs/core.stdout.log").path,
                  core.stderrLog == serviceHome.appendingPathComponent("logs/core.stderr.log").path,
                  let bridgePython = bridge.bridgePython,
                  bridgePython.hasPrefix("/"),
                  FileManager.default.isExecutableFile(atPath: bridgePython)
            else {
                throw LauncherError.invalidManifest("named owned launch configuration mismatch")
            }
            try requirePrivateDirectory(data, name: "owned data")
            try requirePrivateDirectory(bridgeHome, name: "owned Bridge")
            try requirePrivateDirectory(bridgeHome.appendingPathComponent("state"), name: "Bridge state")
            try requirePrivateDirectory(bridgeHome.appendingPathComponent("locks"), name: "Bridge locks")
            guard config.standardizedFileURL.path == config.resolvingSymlinksInPath().path else {
                throw LauncherError.insecureFile("named Bridge config has symlink components")
            }
            try requirePrivateRegularFile(config, name: "named Bridge config")
            if v2 {
                let binding = serviceHome.appendingPathComponent("owned-bridge-v2-binding.json")
                guard binding.standardizedFileURL.path == binding.resolvingSymlinksInPath().path else {
                    throw LauncherError.insecureFile("owned v2 binding has symlink components")
                }
                try requirePrivateRegularFile(binding, name: "owned v2 binding")
            }
            guard let portPosition = core.arguments.firstIndex(of: "--http-port"),
                  core.arguments.indices.contains(portPosition + 1),
                  let port = Int(core.arguments[portPosition + 1]),
                  (1...65535).contains(port)
            else {
                throw LauncherError.invalidManifest("named owned Core port is untrusted")
            }
            corePort = port
            coreWorkingDirectory = core.workingDirectory
        } else if fileOnly, let core = manifest.services["http-origin"],
                  let position = core.arguments.firstIndex(of: "--http-port"),
                  core.arguments.indices.contains(position + 1),
                  let port = Int(core.arguments[position + 1]), (1...65535).contains(port) {
            corePort = port
            coreWorkingDirectory = core.workingDirectory
        }
        let declaredConnectorKeys = manifest.services.keys.filter { key in
            key == "cloudflare" || key == "tunnel" || connectorServicePattern.firstMatch(
                in: key, range: NSRange(key.startIndex..<key.endIndex, in: key)
            )?.range == NSRange(key.startIndex..<key.endIndex, in: key)
        }.sorted()
        if !declaredConnectorKeys.isEmpty, corePort == nil {
            throw LauncherError.invalidManifest("named Core port is untrusted")
        }
        if let port = corePort {
            var coreBearer: String?
            if declaredConnectorKeys.contains(where: { $0 == "tunnel" || $0.hasPrefix("tunnel.") }) {
                guard let environmentPath = manifest.services["http-origin"]?.environmentFile,
                      environmentPath == serviceHome.appendingPathComponent("http-origin-environment.json").path
                else { throw LauncherError.invalidManifest("OpenAI Core environment metadata mismatch") }
                let environmentURL = try requireAbsolute(environmentPath, name: "Core environment")
                guard environmentURL.path == environmentURL.resolvingSymlinksInPath().path else {
                    throw LauncherError.insecureFile("Core environment symlink")
                }
                try requirePrivateRegularFile(environmentURL, name: "Core environment")
                if let environmentData = try? Data(contentsOf: environmentURL),
                   let environment = try? JSONDecoder().decode([String: String].self, from: environmentData) {
                    coreBearer = environment["SERVERFS_HTTP_API_KEY"]
                }
            }
            var tunnelIDs = Set<String>()
            var credentialFiles: [URL] = []
            var cloudflareTokenFiles: [URL] = []
            for key in declaredConnectorKeys {
                let provider = key.hasPrefix("cloudflare") ? "cloudflare" : "tunnel"
                let connectorID = key == provider ? nil : String(key.dropFirst(provider.count + 1))
                guard let connector = manifest.services[key] else { continue }
                try verifyOwnedConnector(
                    connector, provider: provider, connectorID: connectorID,
                    serviceHome: serviceHome, port: port, workingDirectory: coreWorkingDirectory,
                    coreBearer: coreBearer
                )
                if connectorID != nil { try verifyDynamicConnectorLaunchAgent(key, instanceID: instanceID) }
                if provider == "cloudflare" {
                    guard connector.arguments.count == 5 else {
                        throw LauncherError.invalidManifest("owned Cloudflare argv mismatch")
                    }
                    cloudflareTokenFiles.append(URL(fileURLWithPath: connector.arguments[4]))
                } else {
                    guard let tunnelID = connector.tunnelID, tunnelIDs.insert(tunnelID).inserted,
                          let control = connector.controlKeyFile, let bearer = connector.coreBearerFile
                    else { throw LauncherError.invalidManifest("duplicate Tunnel ID or missing OpenAI metadata") }
                    credentialFiles.append(URL(fileURLWithPath: control))
                    credentialFiles.append(URL(fileURLWithPath: bearer))
                }
            }
            for index in cloudflareTokenFiles.indices {
                for other in cloudflareTokenFiles.indices where other > index {
                    if sameFile(cloudflareTokenFiles[index], cloudflareTokenFiles[other]) {
                        throw LauncherError.invalidManifest("Cloudflare token files alias")
                    }
                }
            }
            for index in credentialFiles.indices {
                for other in credentialFiles.indices where other > index {
                    if sameFile(credentialFiles[index], credentialFiles[other]) {
                        throw LauncherError.invalidManifest("OpenAI credential files alias")
                    }
                }
            }
        }
    }
    return manifest
}

private func loadEnvironment(_ rawPath: String?) throws {
    guard let rawPath else { return }
    let url = try requireAbsolute(rawPath, name: "environment_file")
    try requirePrivateRegularFile(url, name: "environment_file")
    let data: Data
    do {
        data = try Data(contentsOf: url, options: [.mappedIfSafe])
    } catch {
        throw LauncherError.io("cannot read environment_file")
    }
    let values: [String: String]
    do {
        values = try JSONDecoder().decode([String: String].self, from: data)
    } catch {
        throw LauncherError.invalidManifest("environment_file must be a JSON string map")
    }
    for (key, value) in values {
        let range = NSRange(location: 0, length: key.utf16.count)
        guard environmentKeyPattern.firstMatch(in: key, range: range)?.range == range,
              !value.utf8.contains(0)
        else {
            throw LauncherError.invalidManifest("invalid environment entry")
        }
        guard setenv(key, value, 1) == 0 else {
            throw LauncherError.io("setenv failed")
        }
    }
}

private func prepareLog(_ rawPath: String, targetFD: Int32, name: String) throws {
    let url = try requireAbsolute(rawPath, name: name)
    let parent = url.deletingLastPathComponent()
    do {
        try FileManager.default.createDirectory(
            at: parent,
            withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700]
        )
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: parent.path)
    } catch {
        throw LauncherError.io("cannot prepare log directory")
    }
    let fd = open(url.path, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0o600)
    guard fd >= 0 else { throw LauncherError.io("cannot open \(name)") }
    defer { close(fd) }
    guard fchmod(fd, 0o600) == 0, dup2(fd, targetFD) >= 0 else {
        throw LauncherError.io("cannot redirect \(name)")
    }
}

private func run() throws -> Never {
    let instance = try bundledInstanceID()
    let args = CommandLine.arguments
    if args.count > 1, args[1] == "--validate-only" {
        guard args.count == 4,
              instance != "default",
              args[2] == instance
        else { throw LauncherError.invalidArguments }
        let serviceID = args[3]
        let serviceRange = NSRange(serviceID.startIndex..<serviceID.endIndex, in: serviceID)
        let isDynamicConnector = connectorServicePattern.firstMatch(
            in: serviceID, range: serviceRange
        )?.range == serviceRange
        guard isDynamicConnector || serviceID == "http-origin" else {
            throw LauncherError.invalidArguments
        }
        let manifest = try loadManifest(instanceID: instance)
        // Named Core preflight is metadata-only: never load environment or exec.
        // loadManifest validates each of the three named Core owner shapes.
        if serviceID == "http-origin", ![
            "v0.25-named-file-core",
            "v0.25-named-owned-core",
            "v0.25-named-shared-core",
        ].contains(manifest.owner ?? "") {
            throw LauncherError.invalidArguments
        }
        guard manifest.services[serviceID] != nil else {
            throw LauncherError.invalidManifest("service is not configured")
        }
        let result = try JSONSerialization.data(withJSONObject: ["ok": true], options: [.sortedKeys])
        FileHandle.standardOutput.write(result)
        FileHandle.standardOutput.write(Data([0x0A]))
        exit(0)
    }
    if instance == "default" {
        guard args.count == 2 else { throw LauncherError.invalidArguments }
    } else {
        guard args.count == 3, args[1] == instance else {
            throw LauncherError.invalidArguments
        }
    }
    let serviceID = args[args.count - 1]
    let serviceRange = NSRange(serviceID.startIndex..<serviceID.endIndex, in: serviceID)
    let isConnectorService = connectorServicePattern.firstMatch(
        in: serviceID, range: serviceRange
    )?.range == serviceRange
    guard allowedServices.contains(serviceID) || (instance != "default" && isConnectorService)
    else { throw LauncherError.invalidArguments }
    let manifest = try loadManifest(instanceID: instance)
    if manifest.owner == "v0.25-named-shared-core", serviceID != "http-origin" {
        throw LauncherError.invalidManifest("named shared instances may launch only http-origin")
    }
    guard let config = manifest.services[serviceID] else {
        throw LauncherError.invalidManifest("service is not configured")
    }
    // loadManifest validates every declared Connector and its signed plist.
    // Keep dynamic identity/key checks before environment, logs, chdir or exec.
    if isConnectorService {
        guard instance != "default", args.count == 3, args[1] == instance,
              manifest.services[serviceID] != nil
        else { throw LauncherError.invalidManifest("dynamic Connector identity mismatch") }
    }
    let executable = try manifest.owner == "v0.25-named-shared-core"
        ? requirePhysicalSharedFile(config.executable, name: "shared Core executable")
        : requireAbsolute(config.executable, name: "executable")
    var isDirectory: ObjCBool = false
    guard FileManager.default.fileExists(atPath: executable.path, isDirectory: &isDirectory),
          !isDirectory.boolValue,
          FileManager.default.isExecutableFile(atPath: executable.path)
    else {
        throw LauncherError.invalidPath("executable is missing or not executable")
    }

    try loadEnvironment(config.environmentFile)
    try prepareLog(config.stdoutLog, targetFD: STDOUT_FILENO, name: "stdout_log")
    try prepareLog(config.stderrLog, targetFD: STDERR_FILENO, name: "stderr_log")

    if let rawWorkingDirectory = config.workingDirectory {
        let workingDirectory = try requireAbsolute(rawWorkingDirectory, name: "working_directory")
        var isDir: ObjCBool = false
        guard FileManager.default.fileExists(atPath: workingDirectory.path, isDirectory: &isDir), isDir.boolValue else {
            throw LauncherError.invalidPath("working_directory is missing")
        }
        guard chdir(workingDirectory.path) == 0 else {
            throw LauncherError.io("cannot enter working_directory")
        }
    }

    let argv = [executable.path] + config.arguments
    let cArguments = argv.map { strdup($0) } + [nil]
    defer { cArguments.dropLast().forEach { free($0) } }
    execv(executable.path, cArguments)
    throw LauncherError.io("execv failed with errno \(errno)")
}

do {
    try run()
} catch {
    fputs("serverfs-service-launcher: \(error)\n", stderr)
    exit(1)
}
