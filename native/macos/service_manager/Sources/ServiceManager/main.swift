import Foundation
import ServiceManagement
import Darwin

private struct ServiceSpec {
    let id: String
    let plistName: String
}

private let legacyServiceSpecs = [
    ServiceSpec(id: "tunnel", plistName: "com.ntlx.serverfs.tunnel.plist"),
    ServiceSpec(id: "agent-bridge", plistName: "com.ntlx.serverfs.bridge.plist"),
]
private let serviceSpecs = legacyServiceSpecs + [
    ServiceSpec(id: "http-origin", plistName: "com.ntlx.serverfs.http-origin.plist"),
    ServiceSpec(id: "cloudflare", plistName: "com.ntlx.serverfs.cloudflare.plist"),
]

private func statusName(_ status: SMAppService.Status) -> String {
    switch status {
    case .notRegistered:
        return "not-registered"
    case .enabled:
        return "enabled"
    case .requiresApproval:
        return "requires-approval"
    case .notFound:
        return "not-found"
    @unknown default:
        return "unknown-\(status.rawValue)"
    }
}

private func service(_ spec: ServiceSpec) -> SMAppService {
    SMAppService.agent(plistName: spec.plistName)
}

private func selectedSpecs(_ raw: String?, instance: String) -> [ServiceSpec]? {
    guard let raw, raw != "all" else { return legacyServiceSpecs }
    if let spec = serviceSpecs.first(where: { $0.id == raw }) { return [spec] }
    guard instance != "default",
          let separator = raw.firstIndex(of: ".")
    else { return nil }
    let provider = String(raw[..<separator])
    let connectorID = String(raw[raw.index(after: separator)...])
    guard ["cloudflare", "tunnel"].contains(provider),
          validConnectorID(connectorID)
    else { return nil }
    let component = provider == "cloudflare" ? "cloudflare" : "openai"
    let label = "com.ntlx.serverfs.\(instance).\(component).\(connectorID)"
    let plistName = "\(label).plist"
    let plistURL = Bundle.main.bundleURL
        .appendingPathComponent("Contents/Library/LaunchAgents", isDirectory: true)
        .appendingPathComponent(plistName, isDirectory: false)
    guard FileManager.default.fileExists(atPath: plistURL.path),
          let data = try? Data(contentsOf: plistURL),
          let plist = try? PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any],
          plist["Label"] as? String == label,
          plist["ProgramArguments"] as? [String] == ["serverfs-service-launcher", instance, raw]
    else { return nil }
    return [ServiceSpec(id: raw, plistName: plistName)]
}

private func validConnectorID(_ value: String) -> Bool {
    let pattern = try! NSRegularExpression(pattern: "^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$")
    let range = NSRange(value.startIndex..<value.endIndex, in: value)
    return pattern.firstMatch(in: value, range: range)?.range == range
}

// Instance identity is embedded in the signed app bundle, never taken from a CLI flag.
private func instanceID() -> String {
    let id = Bundle.main.object(forInfoDictionaryKey: "ServerFSInstanceID") as? String ?? "default"
    let pattern = try! NSRegularExpression(pattern: "^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$")
    let range = NSRange(id.startIndex..<id.endIndex, in: id)
    guard pattern.firstMatch(in: id, range: range)?.range == range else {
        emit(["ok": false, "error": "invalid bundled instance identity"], exitCode: 64)
    }
    return id
}

private func scopedSpecs(_ specs: [ServiceSpec], instance: String) -> [ServiceSpec] {
    guard instance != "default" else { return specs }
    return specs.map { spec in
        if spec.id.contains(".") { return spec }
        let component: String
        switch spec.id {
        case "tunnel": component = "openai"
        case "agent-bridge": component = "bridge"
        case "http-origin": component = "core"
        case "cloudflare": component = "cloudflare"
        default: fatalError("unknown bundled component")
        }
        return ServiceSpec(
            id: spec.id,
            plistName: "com.ntlx.serverfs.\(instance).\(component).plist"
        )
    }
}

private func emit(_ fields: [String: Any], exitCode: Int32 = 0) -> Never {
    do {
        let data = try JSONSerialization.data(withJSONObject: fields, options: [.sortedKeys])
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data([0x0A]))
    } catch {
        fputs("unable to encode response: \(error)\n", stderr)
        exit(70)
    }
    exit(exitCode)
}

private func usage() -> Never {
    fputs(
        "usage: serverfs-service-manager {register|unregister|status} [all|tunnel|agent-bridge|http-origin|cloudflare]\n" +
            "       serverfs-service-manager open-settings\n" +
            "       serverfs-service-manager legacy-status <plist>\n" +
            "       serverfs-service-manager register-preflight agent-bridge\n" +
            "       serverfs-service-manager register-preflight http-origin|cloudflare.<id>|tunnel.<id>\n" +
            "       serverfs-service-manager info\n",
        stderr
    )
    exit(64)
}

private func connectorRegistrationPreflight(_ serviceKey: String, instance: String) -> Bool {
    guard instance != "default",
          Bundle.main.bundleIdentifier == "com.ntlx.serverfs.service-manager.\(instance)",
          let selected = selectedSpecs(serviceKey, instance: instance),
          selected.count == 1,
          selected[0].id == serviceKey,
          serviceKey.hasPrefix("cloudflare.") || serviceKey.hasPrefix("tunnel.")
              || serviceKey == "http-origin"
    else {
        return false
    }

    let bundleURL = Bundle.main.bundleURL
    let launcherURL = bundleURL
        .appendingPathComponent("Contents/Resources/serverfs-service-launcher", isDirectory: false)
    guard FileManager.default.isExecutableFile(atPath: launcherURL.path) else {
        return false
    }

    let process = Process()
    let output = Pipe()
    process.executableURL = launcherURL
    process.arguments = ["--validate-only", instance, serviceKey]
    process.standardOutput = output
    process.standardError = FileHandle.nullDevice

    let exited = DispatchSemaphore(value: 0)
    process.terminationHandler = { _ in exited.signal() }
    do {
        try process.run()
    } catch {
        return false
    }

    if exited.wait(timeout: .now() + 10) == .timedOut {
        process.terminate()
        if exited.wait(timeout: .now() + .milliseconds(250)) == .timedOut {
            _ = kill(process.processIdentifier, SIGKILL)
            _ = exited.wait(timeout: .now() + 1)
        }
        return false
    }

    let data = output.fileHandleForReading.readDataToEndOfFile()
    guard process.terminationStatus == 0,
          data == Data("{\"ok\":true}\n".utf8)
    else {
        return false
    }
    return true
}

private func states(_ specs: [ServiceSpec]) -> [String: String] {
    Dictionary(uniqueKeysWithValues: specs.map { ($0.id, statusName(service($0).status)) })
}

private func register(_ specs: [ServiceSpec]) -> Never {
    var errors: [String: String] = [:]
    for spec in specs {
        let item = service(spec)
        let current = statusName(item.status)
        if current == "enabled" || current == "requires-approval" {
            continue
        }
        do {
            try item.register()
        } catch {
            let after = statusName(item.status)
            if after != "enabled" && after != "requires-approval" {
                errors[spec.id] = String(describing: error)
            }
        }
    }
    let result = states(specs)
    let ok = errors.isEmpty && result.values.allSatisfy { $0 == "enabled" || $0 == "requires-approval" }
    var payload: [String: Any] = ["ok": ok, "services": result]
    if !errors.isEmpty { payload["errors"] = errors }
    emit(payload, exitCode: ok ? 0 : 1)
}

private func unregister(_ specs: [ServiceSpec]) -> Never {
    var errors: [String: String] = [:]
    for spec in specs {
        let item = service(spec)
        let current = statusName(item.status)
        if current == "not-registered" || current == "not-found" {
            continue
        }
        do {
            try item.unregister()
        } catch {
            let after = statusName(item.status)
            if after != "not-registered" && after != "not-found" {
                errors[spec.id] = String(describing: error)
            }
        }
    }
    let result = states(specs)
    let ok = errors.isEmpty && result.values.allSatisfy { $0 == "not-registered" || $0 == "not-found" }
    var payload: [String: Any] = ["ok": ok, "services": result]
    if !errors.isEmpty { payload["errors"] = errors }
    emit(payload, exitCode: ok ? 0 : 1)
}


// Only the signed, correctly staged owned app may register a *named* Bridge.
// Source-policy and writable-lease checks are performed by the native Core
// launcher and separate Bridge supervisor before the service can run. This
// structural check must not touch default/other instance registrations.
private func namedOwnedRegistrationReady(instance: String) -> Bool {
    let fm = FileManager.default
    let root = fm.homeDirectoryForCurrentUser
        .appendingPathComponent(
            "Library/Application Support/ServerFS/instances/\(instance)/service-manager",
            isDirectory: true
        )
    let app = root.appendingPathComponent("ServerFSServiceManager.app", isDirectory: true)
    guard Bundle.main.bundleURL.resolvingSymlinksInPath().path == app.path else { return false }
    let data = root.appendingPathComponent("data", isDirectory: true)
    let bridgeHome = data.appendingPathComponent("agent-bridge", isDirectory: true)
    let config = bridgeHome.appendingPathComponent("bridge.json", isDirectory: false)
    let manifest = root.appendingPathComponent("services.json", isDirectory: false)

    func privatePath(_ url: URL, directory: Bool) -> Bool {
        guard url.standardizedFileURL.path == url.resolvingSymlinksInPath().path,
              let attrs = try? fm.attributesOfItem(atPath: url.path),
              let uid = attrs[.ownerAccountID] as? NSNumber,
              uid.uint32Value == getuid(),
              let perms = attrs[.posixPermissions] as? NSNumber
        else { return false }
        return directory
            ? (attrs[.type] as? FileAttributeType == .typeDirectory
                && perms.intValue & 0o077 == 0)
            : (attrs[.type] as? FileAttributeType == .typeRegular
                && perms.intValue == 0o600)
    }
    guard privatePath(root.deletingLastPathComponent().deletingLastPathComponent(), directory: true),
          privatePath(root.deletingLastPathComponent(), directory: true),
          privatePath(root, directory: true),
          privatePath(data, directory: true),
          privatePath(bridgeHome, directory: true),
          privatePath(bridgeHome.appendingPathComponent("locks"), directory: true),
          privatePath(bridgeHome.appendingPathComponent("state"), directory: true),
          privatePath(config, directory: false),
          privatePath(manifest, directory: false),
          let contents = try? Data(contentsOf: manifest),
          let document = try? JSONSerialization.jsonObject(with: contents) as? [String: Any],
          document["schema_version"] as? Int == 1,
          document["owner"] as? String == "v0.25-named-owned-core",
          document["instance_id"] as? String == instance,
          document["bridge_mode"] as? String == "owned",
          document["bridge_id"] as? String == instance,
          let services = document["services"] as? [String: [String: Any]],
          Set(["http-origin", "agent-bridge"]).isSubset(of: Set(services.keys)),
          services.keys.allSatisfy({ key in
              if ["http-origin", "agent-bridge", "cloudflare", "tunnel"].contains(key) {
                  return true
              }
              return (key.hasPrefix("cloudflare.") || key.hasPrefix("tunnel."))
                  && selectedSpecs(key, instance: instance)?.count == 1
          }),
          let core = services["http-origin"],
          let bridge = services["agent-bridge"],
          core["origin_owner"] as? String == "independent-core",
          bridge["origin_owner"] as? String == "independent-bridge",
          let coreExecutable = core["executable"] as? String,
          let bridgeExecutable = bridge["executable"] as? String,
          bridgeExecutable == coreExecutable,
          coreExecutable.hasPrefix("/"),
          fm.isExecutableFile(atPath: coreExecutable),
          bridge["arguments"] as? [String] == [
              "-B", "-m", "serverfs_mcp.darwin_owned_bridge_supervisor",
              "--instance", instance
          ],
          (core["arguments"] as? [String])?.suffix(2).elementsEqual(
              ["--macos-instance", instance]
          ) == true,
          bridge["bridge_config"] as? String == config.path,
          bridge["environment_file"] == nil || bridge["environment_file"] is NSNull,
          let bridgePython = bridge["bridge_python"] as? String,
          bridgePython.hasPrefix("/"),
          fm.isExecutableFile(atPath: bridgePython)
    else { return false }
    return true
}

let args = CommandLine.arguments
guard args.count >= 2 else { usage() }

switch args[1] {
case "register", "unregister", "status":
    let instance = instanceID()
    guard args.count <= 3,
          let selected = selectedSpecs(args.count == 3 ? args[2] : nil, instance: instance)
    else { usage() }
    guard instance == "default" || (args.count == 3 && args[2] != "all") else {
        emit(["ok": false, "error": "non-default instances require explicit component"], exitCode: 64)
    }
    // Every named Core must pass the signed launcher preflight before
    // SMAppService can register it. This does not launch or stop a Bridge.
    if args[1] == "register", instance != "default",
       selected.contains(where: { $0.id == "http-origin" }),
       !connectorRegistrationPreflight("http-origin", instance: instance) {
        emit(["ok": false, "error": "named Core registration preflight failed"], exitCode: 1)
    }
    if args[1] == "register", selected.contains(where: { $0.id.contains(".") }) {
        let spec = selected[0]
        guard connectorRegistrationPreflight(spec.id, instance: instance) else {
            emit(["ok": false, "error": "dynamic Connector registration preflight failed"], exitCode: 1)
        }
    }
    if instance != "default"
        && selected.contains(where: { $0.id == "agent-bridge" })
        && args[1] == "register"
        && !namedOwnedRegistrationReady(instance: instance)
    {
        emit(["ok": false, "error": "named Bridge is not an authorized owned instance"], exitCode: 64)
    }
    // Named Bridge unregister remains available even if the manifest has
    // become invalid, so a failed staged instance can always be rolled back.
    let specs = scopedSpecs(selected, instance: instance)
    if args[1] == "register" { register(specs) }
    if args[1] == "unregister" { unregister(specs) }
    emit(["ok": true, "services": states(specs)])
case "register-preflight":
    guard args.count == 3 else { usage() }
    let instance = instanceID()
    if args[2] == "cloudflare." || args[2] == "tunnel." {
        usage()
    }
    if args[2] == "http-origin" || args[2].hasPrefix("cloudflare.") || args[2].hasPrefix("tunnel.") {
        guard instance != "default" else {
            emit(["ok": false, "eligible": false, "error": "invalid or unavailable connector service"], exitCode: 64)
        }
        let eligible = connectorRegistrationPreflight(args[2], instance: instance)
        emit([
            "ok": eligible,
            "eligible": eligible,
            "instance_id": instance,
            "operation": "register-preflight",
        ], exitCode: eligible ? 0 : 1)
    }
    guard args[2] == "agent-bridge" else { usage() }
    guard instance != "default" else {
        emit(["ok": false, "eligible": false, "error": "default Bridge is not a named owner"], exitCode: 64)
    }
    let eligible = namedOwnedRegistrationReady(instance: instance)
    emit([
        "ok": eligible,
        "eligible": eligible,
        "instance_id": instance,
        "operation": "register-preflight",
    ], exitCode: eligible ? 0 : 1)
case "open-settings":
    guard args.count == 2 else { usage() }
    SMAppService.openSystemSettingsLoginItems()
    emit(["ok": true])
case "legacy-status":
    guard args.count == 3 else { usage() }
    let url = URL(fileURLWithPath: args[2])
    emit(["ok": true, "status": statusName(SMAppService.statusForLegacyPlist(at: url))])
case "info":
    guard args.count == 2 else { usage() }
    let bundle = Bundle.main
    emit([
        "ok": true,
        "bundle_path": bundle.bundlePath,
        "bundle_identifier": bundle.bundleIdentifier ?? "",
        "version": bundle.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "",
        "instance_id": instanceID(),
    ])
default:
    usage()
}
