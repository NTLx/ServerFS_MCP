# v0.13.2 macOS ServiceManagement feasibility probe

**Date:** 2026-10-12
**Host:** supported ServerFS macOS development host (Apple M-series, native arm64, macOS 27.x)
**Result:** PASS for the implementation-bearing ServiceManagement assumptions

## Objective

Before replacing the v0.13.0/v0.13.1 hand-installed LaunchAgent lifecycle, prove the current Apple-native `SMAppService` path on the actual supported Mac.

## Probe

A disposable headless Swift app bundle embedded one test LaunchAgent under `Contents/Library/LaunchAgents` and registered it with `SMAppService.agent(plistName:)`.

Measured sequence:

1. Build the app and helper with the installed Apple toolchain.
2. Ad-hoc sign the bundle and validate it with `codesign --verify --deep --strict`.
3. Install it under the private per-user ServerFS Application Support directory.
4. Query status before registration: `not-found`.
5. Register through `SMAppService`: `enabled`.
6. Confirm launchd runs the helper from the installed app bundle.
7. Send `TERM` only to the test helper; `KeepAlive` restarts it with a new PID and status remains `enabled`.
8. Unregister through `SMAppService`: `not-registered`; the launchd job disappears and the process exits.

## Conclusions carried into production

- `SMAppService` works for ServerFS's per-user, headless LaunchAgent topology on the supported Mac.
- A stable app-associated helper plus `BundleProgram` is viable; ServerFS no longer needs to install normal-operation plists in `~/Library/LaunchAgents`.
- Ad-hoc signing is sufficient for local development/acceptance on this host.
- User-visible authorization state is available through `SMAppService.Status`, including `requiresApproval`.
- launchd crash recovery remains available through the embedded LaunchAgent policy.

The disposable probe sources were removed after the measurement. Production code contains only the Service Manager, bundle-owned launcher, and the two ServerFS LaunchAgent plists.

## Deferred release-environment checks

The following are acceptance/packaging evidence rather than implementation prerequisites:

- reboot + user login recovery of the real Tunnel and Agent Bridge;
- disabling and re-enabling the ServerFS Login Items entries in System Settings;
- Developer ID signing/notarization requirements for any future externally distributed prebuilt `.app` artifact.

v0.13.2 assembles the small companion locally from source with the Apple command-line toolchain and validates the resulting app before installation.
