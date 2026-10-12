import Foundation
import ServiceManagement

private struct ServiceSpec {
    let id: String
    let plistName: String
}

private let serviceSpecs = [
    ServiceSpec(id: "tunnel", plistName: "com.ntlx.serverfs.tunnel.plist"),
    ServiceSpec(id: "agent-bridge", plistName: "com.ntlx.serverfs.bridge.plist"),
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

private func selectedSpecs(_ raw: String?) -> [ServiceSpec]? {
    guard let raw, raw != "all" else { return serviceSpecs }
    guard let spec = serviceSpecs.first(where: { $0.id == raw }) else { return nil }
    return [spec]
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
        "usage: serverfs-service-manager {register|unregister|status} [all|tunnel|agent-bridge]\n" +
            "       serverfs-service-manager open-settings\n" +
            "       serverfs-service-manager legacy-status <plist>\n" +
            "       serverfs-service-manager info\n",
        stderr
    )
    exit(64)
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

let args = CommandLine.arguments
guard args.count >= 2 else { usage() }

switch args[1] {
case "register", "unregister", "status":
    guard args.count <= 3, let specs = selectedSpecs(args.count == 3 ? args[2] : nil) else { usage() }
    if args[1] == "register" { register(specs) }
    if args[1] == "unregister" { unregister(specs) }
    emit(["ok": true, "services": states(specs)])
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
    ])
default:
    usage()
}
