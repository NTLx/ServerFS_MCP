import Foundation
import Darwin

private let allowedServices: Set<String> = ["tunnel", "agent-bridge"]
private let environmentKeyPattern = try! NSRegularExpression(pattern: "^[A-Za-z_][A-Za-z0-9_]*$")

private struct Manifest: Decodable {
    let schemaVersion: Int
    let services: [String: ServiceConfig]

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case services
    }
}

private struct ServiceConfig: Decodable {
    let executable: String
    let arguments: [String]
    let environmentFile: String?
    let workingDirectory: String?
    let stdoutLog: String
    let stderrLog: String

    enum CodingKeys: String, CodingKey {
        case executable
        case arguments
        case environmentFile = "environment_file"
        case workingDirectory = "working_directory"
        case stdoutLog = "stdout_log"
        case stderrLog = "stderr_log"
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
            return "expected exactly one service id: tunnel or agent-bridge"
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

private func dataHome() -> URL {
    if let override = ProcessInfo.processInfo.environment["SERVERFS_DATA_HOME"]?.trimmingCharacters(in: .whitespacesAndNewlines),
       !override.isEmpty
    {
        return URL(fileURLWithPath: override, isDirectory: true)
    }
    return FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/ServerFS", isDirectory: true)
}

private func manifestURL() -> URL {
    dataHome().appendingPathComponent("service-manager/services.json", isDirectory: false)
}

private func requireAbsolute(_ raw: String, name: String) throws -> URL {
    guard raw.hasPrefix("/") else { throw LauncherError.invalidPath("\(name) must be absolute") }
    let url = URL(fileURLWithPath: raw).standardizedFileURL
    guard url.path == raw || URL(fileURLWithPath: raw).resolvingSymlinksInPath().path == url.resolvingSymlinksInPath().path else {
        throw LauncherError.invalidPath("\(name) is not canonical")
    }
    return url
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

private func loadManifest() throws -> Manifest {
    let url = manifestURL()
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
    guard Set(manifest.services.keys).isSubset(of: allowedServices) else {
        throw LauncherError.invalidManifest("unknown service id")
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
    guard CommandLine.arguments.count == 2 else { throw LauncherError.invalidArguments }
    let serviceID = CommandLine.arguments[1]
    guard allowedServices.contains(serviceID) else { throw LauncherError.invalidArguments }

    let manifest = try loadManifest()
    guard let config = manifest.services[serviceID] else {
        throw LauncherError.invalidManifest("service is not configured")
    }
    let executable = try requireAbsolute(config.executable, name: "executable")
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
