import Foundation

struct SharedManifestGateService {
    let executable: String
    let arguments: [String]
    let workingDirectory: String
    let environmentFile: String
    let stdoutLog: String
    let stderrLog: String
}

struct SharedManifestGateCore {
    let config: String
    let sha256: String
    let hostname: String
    let port: Int
    let service: SharedManifestGateService
}

enum SharedManifestGateError: Error {
    case invalidManifest
}

private struct SharedJSONScanner {
    private let bytes: [UInt8]
    private var index = 0

    init(_ data: Data) { bytes = Array(data) }

    mutating func validate() throws {
        try value(depth: 0)
        whitespace()
        guard index == bytes.count else { throw SharedManifestGateError.invalidManifest }
    }

    private mutating func value(depth: Int) throws {
        guard depth <= 32 else { throw SharedManifestGateError.invalidManifest }
        whitespace()
        guard index < bytes.count else { throw SharedManifestGateError.invalidManifest }
        switch bytes[index] {
        case 0x7B: try object(depth: depth)
        case 0x5B: try array(depth: depth)
        case 0x22: _ = try string()
        case 0x74: try literal(Array("true".utf8))
        case 0x66: try literal(Array("false".utf8))
        case 0x6E: try literal(Array("null".utf8))
        case 0x2D, 0x30...0x39: try number()
        default: throw SharedManifestGateError.invalidManifest
        }
    }

    private mutating func object(depth: Int) throws {
        index += 1
        whitespace()
        if consume(0x7D) { return }
        var keys = Set<String>()
        while true {
            whitespace()
            guard index < bytes.count, bytes[index] == 0x22 else {
                throw SharedManifestGateError.invalidManifest
            }
            let key = try string()
            guard keys.insert(key).inserted else { throw SharedManifestGateError.invalidManifest }
            whitespace()
            guard consume(0x3A) else { throw SharedManifestGateError.invalidManifest }
            try value(depth: depth + 1)
            whitespace()
            if consume(0x7D) { return }
            guard consume(0x2C) else { throw SharedManifestGateError.invalidManifest }
        }
    }

    private mutating func array(depth: Int) throws {
        index += 1
        whitespace()
        if consume(0x5D) { return }
        while true {
            try value(depth: depth + 1)
            whitespace()
            if consume(0x5D) { return }
            guard consume(0x2C) else { throw SharedManifestGateError.invalidManifest }
        }
    }

    private mutating func string() throws -> String {
        let start = index
        index += 1
        while index < bytes.count {
            let byte = bytes[index]
            if byte == 0x22 {
                index += 1
                let token = Data(bytes[start..<index])
                guard let decoded = try JSONSerialization.jsonObject(
                    with: token, options: [.fragmentsAllowed]
                ) as? String else { throw SharedManifestGateError.invalidManifest }
                return decoded
            }
            if byte < 0x20 { throw SharedManifestGateError.invalidManifest }
            if byte == 0x5C {
                index += 1
                guard index < bytes.count else { throw SharedManifestGateError.invalidManifest }
                if bytes[index] == 0x75 {
                    guard index + 4 < bytes.count,
                          bytes[(index + 1)...(index + 4)].allSatisfy(isHex) else {
                        throw SharedManifestGateError.invalidManifest
                    }
                    index += 5
                    continue
                }
                guard [0x22, 0x5C, 0x2F, 0x62, 0x66, 0x6E, 0x72, 0x74].contains(bytes[index]) else {
                    throw SharedManifestGateError.invalidManifest
                }
            }
            index += 1
        }
        throw SharedManifestGateError.invalidManifest
    }

    private mutating func number() throws {
        let start = index
        _ = consume(0x2D)
        if !consume(0x30) {
            guard consumeDigit(nonzero: true) else { throw SharedManifestGateError.invalidManifest }
            while consumeDigit() {}
        }
        if consume(0x2E) {
            guard consumeDigit() else { throw SharedManifestGateError.invalidManifest }
            while consumeDigit() {}
        }
        if consume(0x65) || consume(0x45) {
            if !consume(0x2B) { _ = consume(0x2D) }
            guard consumeDigit() else { throw SharedManifestGateError.invalidManifest }
            while consumeDigit() {}
        }
        guard index > start else { throw SharedManifestGateError.invalidManifest }
    }

    private mutating func literal(_ expected: [UInt8]) throws {
        guard index + expected.count <= bytes.count,
              Array(bytes[index..<(index + expected.count)]) == expected else {
            throw SharedManifestGateError.invalidManifest
        }
        index += expected.count
    }

    private mutating func whitespace() {
        while index < bytes.count && [0x20, 0x09, 0x0A, 0x0D].contains(bytes[index]) { index += 1 }
    }

    @discardableResult private mutating func consume(_ byte: UInt8) -> Bool {
        guard index < bytes.count, bytes[index] == byte else { return false }
        index += 1
        return true
    }

    private mutating func consumeDigit(nonzero: Bool = false) -> Bool {
        guard index < bytes.count, bytes[index] >= (nonzero ? 0x31 : 0x30), bytes[index] <= 0x39 else {
            return false
        }
        index += 1
        return true
    }

    private func isHex(_ byte: UInt8) -> Bool {
        (0x30...0x39).contains(byte) || (0x41...0x46).contains(byte) || (0x61...0x66).contains(byte)
    }
}

private func sharedObject(_ value: Any?) throws -> [String: Any] {
    guard let value = value as? [String: Any] else { throw SharedManifestGateError.invalidManifest }
    return value
}

private func sharedString(_ value: Any?) throws -> String {
    guard let value = value as? String else { throw SharedManifestGateError.invalidManifest }
    return value
}

private func sharedExactKeys(_ object: [String: Any], _ keys: Set<String>) throws {
    guard Set(object.keys) == keys else { throw SharedManifestGateError.invalidManifest }
}

private func sharedInteger(_ value: Any?) throws -> Int {
    guard let number = value as? NSNumber,
          CFGetTypeID(number) != CFBooleanGetTypeID(),
          ["c", "s", "i", "l", "q", "C", "S", "I", "L", "Q"].contains(String(cString: number.objCType))
    else { throw SharedManifestGateError.invalidManifest }
    return number.intValue
}

func validateSharedManifestGate(
    rawManifest: Data,
    instanceID: String,
    expectedServiceHome: URL
) throws -> SharedManifestGateCore {
    guard !rawManifest.isEmpty, rawManifest.count <= 16 * 1024,
          !instanceID.isEmpty, instanceID != "default" else {
        throw SharedManifestGateError.invalidManifest
    }
    var scanner = SharedJSONScanner(rawManifest)
    try scanner.validate()
    guard let document = try JSONSerialization.jsonObject(with: rawManifest) as? [String: Any] else {
        throw SharedManifestGateError.invalidManifest
    }
    try sharedExactKeys(document, ["schema_version", "owner", "instance_id", "bridge_mode", "bridge_id", "core", "services"])
    let version = try sharedInteger(document["schema_version"])
    guard version == 1,
          try sharedString(document["owner"]) == "v0.25-named-shared-core",
          try sharedString(document["instance_id"]) == instanceID,
          try sharedString(document["bridge_mode"]) == "shared" else {
        throw SharedManifestGateError.invalidManifest
    }
    let bridgeID = try sharedString(document["bridge_id"])
    guard bridgeID.range(of: "^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$", options: .regularExpression) != nil else {
        throw SharedManifestGateError.invalidManifest
    }

    let core = try sharedObject(document["core"])
    try sharedExactKeys(core, ["config", "sha256", "port", "hostname"])
    let configPath = try sharedString(core["config"])
    let digest = try sharedString(core["sha256"])
    let hostname = try sharedString(core["hostname"])
    let port = try sharedInteger(core["port"])
    let components = configPath.split(separator: "/")
    guard configPath.hasPrefix("/"), !configPath.hasSuffix("/"), !configPath.contains("//"),
          !components.contains(".") && !components.contains(".."),
          digest.range(of: "^[0-9a-f]{64}$", options: .regularExpression) != nil,
          !hostname.isEmpty, (1...65535).contains(port) else {
        throw SharedManifestGateError.invalidManifest
    }

    let services = try sharedObject(document["services"])
    try sharedExactKeys(services, ["http-origin"])
    let service = try sharedObject(services["http-origin"])
    let serviceKeys: Set<String> = ["executable", "arguments", "working_directory", "environment_file", "stdout_log", "stderr_log", "origin_owner"]
    try sharedExactKeys(service, serviceKeys)
    let executable = try sharedString(service["executable"])
    let arguments = service["arguments"] as? [String]
    let workingDirectory = try sharedString(service["working_directory"])
    let environmentFile = try sharedString(service["environment_file"])
    let stdoutLog = try sharedString(service["stdout_log"])
    let stderrLog = try sharedString(service["stderr_log"])
    guard executable.hasPrefix("/"), let arguments,
          arguments == ["-B", "-m", "serverfs_mcp.cli", "serve", "--transport", "streamable-http", "--config", configPath,
                        "--http-port", String(port), "--macos-instance", instanceID,
                        "--macos-shared-bridge", bridgeID],
          try sharedString(service["origin_owner"]) == "independent-core" else {
        throw SharedManifestGateError.invalidManifest
    }
    // Foundation may rewrite a physical /private/tmp pathname to /tmp when
    // standardizedFileURL is used. Preserve the already validated lexical
    // absolute paths pinned by the Python staging/manifest contract instead.
    let home = expectedServiceHome.path
    let configDirectory = (configPath as NSString).deletingLastPathComponent
    guard home.hasPrefix("/"),
          workingDirectory == configDirectory,
          environmentFile == "\(home)/http-origin-environment.json",
          stdoutLog == "\(home)/logs/core.stdout.log",
          stderrLog == "\(home)/logs/core.stderr.log" else {
        throw SharedManifestGateError.invalidManifest
    }
    let validatedService = SharedManifestGateService(
        executable: executable, arguments: arguments, workingDirectory: workingDirectory,
        environmentFile: environmentFile, stdoutLog: stdoutLog, stderrLog: stderrLog
    )
    return SharedManifestGateCore(config: configPath, sha256: digest, hostname: hostname, port: port, service: validatedService)
}

func validateSharedEnvironmentGate(
    rawEnvironment: Data, instanceID: String, expectedDataHome: URL, hostname: String, port: Int
) throws {
    guard !rawEnvironment.isEmpty, rawEnvironment.count <= 16 * 1024 else {
        throw SharedManifestGateError.invalidManifest
    }
    var scanner = SharedJSONScanner(rawEnvironment)
    try scanner.validate()
    guard let values = try JSONSerialization.jsonObject(with: rawEnvironment) as? [String: Any] else {
        throw SharedManifestGateError.invalidManifest
    }
    let permitted: Set<String> = [
        "SERVERFS_MACOS_INSTANCE_ID", "SERVERFS_DATA_HOME", "SERVERFS_FILE_INGRESS_ENABLED",
        "SERVERFS_HTTP_API_KEY", "SERVERFS_HTTP_ALLOWED_HOSTS", "SERVERFS_OAUTH_ENABLED",
        "SERVERFS_OAUTH_RESOURCE_URL", "SERVERFS_OAUTH_ISSUER_URL", "SERVERFS_OAUTH_REQUIRED_SCOPES",
        "SERVERFS_OAUTH_INTROSPECTION_URL", "SERVERFS_OAUTH_INTROSPECTION_SECRET",
        "SERVERFS_OAUTH_INTROSPECTION_TIMEOUT_SECONDS"
    ]
    let apiKey = values["SERVERFS_HTTP_API_KEY"] as? String
    let oauthEnabled = values["SERVERFS_OAUTH_ENABLED"] as? String == "true"
    guard Set(values.keys).isSubset(of: permitted),
          values.values.allSatisfy({ $0 is String }),
          ((apiKey?.isEmpty == false) != oauthEnabled),
          (!oauthEnabled || !(values["SERVERFS_OAUTH_INTROSPECTION_SECRET"] as? String ?? "").isEmpty),
          values["SERVERFS_MACOS_INSTANCE_ID"] as? String == instanceID,
          values["SERVERFS_DATA_HOME"] as? String == expectedDataHome.standardizedFileURL.path,
          values["SERVERFS_FILE_INGRESS_ENABLED"] as? String == "false",
          values["SERVERFS_HTTP_ALLOWED_HOSTS"] as? String == "\(hostname),127.0.0.1:\(port)" else {
        throw SharedManifestGateError.invalidManifest
    }
}

import CoreFoundation
