# ServerFS v0.13.2 Development Plan — Native macOS Service Persistence

**Status:** RELEASED — v0.13.2
**Target:** v0.13.2
**Baseline:** released v0.13.1 on `main` (`98ce98fee38a9cea56b8eb2087553a2e7920b178`)
**Platform:** Apple M-series, native arm64, macOS 27.x Golden Gate

## 1. Mission

Replace the current macOS service-lifecycle glue with the current Apple-native Service Management path so the native ServerFS stack survives terminal/agent-session exit and returns automatically after reboot + user login without root, manual `nohup`, or hand-installed `~/Library/LaunchAgents` files.

The implementation must stay small: ServerFS, the Agent Bridge, tunnel supervisor, file ingress, configuration model, and runtime adapters remain Python. Native code exists only at the macOS lifecycle boundary.

## 2. Current problem

v0.13.1 has two different lifecycle shapes:

1. **Agent Bridge** — persistent, but installed by ServerFS itself writing
   `~/Library/LaunchAgents/com.ntlx.serverfs.agent-bridge.plist` and invoking
   `launchctl bootstrap/kickstart/bootout/print` from Python.
2. **Tunnel stack** — `serverfs tunnel -> tunnel-client + supervisor -> serve -> optional
   file-ingress`; normally started from `run-serverfs.sh tunnel`. A process started from an
   interactive agent/session can be reclaimed with that session, so operators currently need
   an out-of-band `nohup`/detach mechanism for persistence.

This is functional but not a coherent macOS product lifecycle.

## 3. Apple mechanism selected

As of 2026-10-12, Apple's current documented Service Management API for app-associated
LoginItems, LaunchAgents and LaunchDaemons is `SMAppService` (macOS 13+). Apple explicitly
positions `SMAppService.register()` / `unregister()` as the replacement for installing
LaunchAgent property lists in `~/Library/LaunchAgents` or `/Library/LaunchAgents`.

For an app-associated LaunchAgent:

- the plist lives inside the calling app at `Contents/Library/LaunchAgents`;
- `SMAppService.agent(plistName:)` registers and controls it;
- the plist uses `BundleProgram` for the executable inside the app bundle;
- `SMAppService.status` exposes `notRegistered`, `enabled`, `requiresApproval`, and `notFound`;
- a registered LaunchAgent is bootstrapped immediately when allowed and again on subsequent
  user logins;
- System Settings > General > Login Items becomes the user-visible authorization/control plane.

Primary Apple references:

- https://developer.apple.com/documentation/servicemanagement/smappservice
- https://developer.apple.com/documentation/servicemanagement/updating-helper-executables-from-earlier-versions-of-macos
- https://developer.apple.com/documentation/servicemanagement/smappservice/agent(plistname:)
- https://developer.apple.com/documentation/servicemanagement/smappservice/register()

No newer Apple API was found that supersedes `SMAppService` for this use case.

## 4. Product decision

### 4.1 Use per-user LaunchAgents, not a LaunchDaemon

v0.13.2 keeps ServerFS as a current-user service:

- no root;
- no privileged helper;
- no `/Library/LaunchDaemons`;
- no pre-login service requirement;
- automatic start means **after reboot when the user logs in**.

This matches ServerFS's user-scoped workdirs, credentials, Application Support state, Agent
runtimes, and desktop ChatGPT tunnel. A true pre-login boot daemon would complicate permissions,
Keychain/TCC, user-home access, runtime ownership and security without a product requirement.

### 4.2 Add one minimal native lifecycle companion

Introduce a small headless macOS app bundle, tentatively:

`ServerFSServiceManager.app`

Its responsibility is deliberately narrow:

- call `SMAppService.agent(plistName:)`;
- register/unregister the ServerFS LaunchAgents;
- expose native status including `requiresApproval`;
- optionally open the Login Items pane when operator action is required;
- provide the immutable bundle-owned launcher used by launchd.

It must not implement filesystem operations, MCP, Agent Bridge protocol, tunnel logic, proxy
logic, Jev, config parsing beyond its own minimal launcher manifest, or runtime adapters.

### 4.3 Preserve the existing two-service topology

Keep two independently supervised LaunchAgents:

1. `com.ntlx.serverfs.tunnel`
   - launchd owns the persistent top-level tunnel service;
   - that process preserves the existing ownership tree:
     `serverfs tunnel -> tunnel-client + supervisor -> serve -> optional file-ingress`.

2. `com.ntlx.serverfs.bridge`
   - launchd continues to own the long-lived Agent Bridge independently of tunnel reconnects;
   - provider/runtime recovery semantics remain unchanged.

Do **not** merge the Agent Bridge into the tunnel process merely to reduce the LaunchAgent count;
the current separation is useful and already validated.

## 5. Bundle and launcher contract

The Service Management bundle contains:

```text
ServerFSServiceManager.app/
  Contents/
    Info.plist
    MacOS/
      serverfs-service-manager
    Resources/
      serverfs-service-launcher
    Library/
      LaunchAgents/
        com.ntlx.serverfs.tunnel.plist
        com.ntlx.serverfs.bridge.plist
```

The exact helper placement may move between `MacOS` and `Resources` during Phase 0, but the
LaunchAgent plist must use Apple's app-bundle contract and `BundleProgram`, not a machine-local
Python/venv path.

The bundle-owned launcher is allowed to `exec` the real ServerFS/Bridge executable recorded in a
private runtime manifest. This keeps the signed/bundled launchd entry point stable while allowing
the Python environment and repository location to remain external and replaceable.

### Private manifest rules

Use a small, versioned manifest under the existing private ServerFS data home, outside exposed
workdirs, for example:

`~/Library/Application Support/ServerFS/service-manager/services.json`

It may contain absolute paths and non-secret lifecycle metadata. Secrets must never be copied
into the LaunchAgent plists or command line. Secret-bearing environment/config remains in private
0600 files and is injected/read only by the launched ServerFS process through an explicit path.

Requirements:

- parent directories 0700;
- manifest 0600;
- atomic replacement;
- no shell-profile dependency;
- no PATH lookup for Python, ServerFS, tunnel-client or Agent runtimes;
- no machine-specific path committed to Git;
- launcher errors are structured and written to a stable private log location.

## 6. CLI contract

Preserve the existing operator vocabulary as far as practical. The preferred end state is one
macOS service surface rather than exposing raw launchd operations.

Proposed commands:

```text
serverfs service install --config serverfs.toml --env-file .env
serverfs service status
serverfs service start
serverfs service restart
serverfs service stop
serverfs service uninstall
serverfs service open-settings
```

`install` on macOS must:

1. validate platform/runtime/config;
2. materialize/update the private service manifest;
3. install/validate the native app bundle;
4. migrate any v0.13.0/v0.13.1 legacy Agent Bridge LaunchAgent safely;
5. register both LaunchAgents through `SMAppService`;
6. report native authorization state accurately.

Existing `serverfs agent-bridge configure/sync/restart` policy operations may remain internally,
but raw `launchctl` lifecycle management must cease being the normal v0.13.2 path.

`status` must distinguish at least:

- `enabled` — registered and eligible to run;
- `requires-approval` — registered but waiting for the user in Login Items;
- `not-registered`;
- `not-found` / bundle mismatch;
- service process health, separately from registration state.

Never report “installed/running” merely because a plist exists.

## 7. Legacy migration

The migration from v0.13.1 is safety-critical because the existing Bridge uses the same logical
service identity.

Required sequence:

1. Detect the legacy plist at
   `~/Library/LaunchAgents/com.ntlx.serverfs.agent-bridge.plist`.
2. Inspect legacy authorization using `SMAppService.statusForLegacyPlist(at:)` where useful.
3. Validate the new app bundle, helper, manifest and native registrar before touching the old job.
4. Stop/boot out the legacy Bridge only at the hand-over point; do not delete its plist yet.
5. Register the new app-associated LaunchAgent.
6. If status is `requiresApproval`, surface the exact state and offer/open System Settings. Do not
   start a duplicate legacy/new Bridge pair.
7. After the new service reaches `enabled` and passes a live Bridge health probe, remove the stale
   legacy plist.
8. On failed registration, preserve enough legacy state for an explicit rollback path.

The tunnel side also needs duplicate-instance protection when migrating from a manually detached
`serverfs tunnel` process. Installation must detect an already-running unmanaged tunnel and refuse
or perform an explicit controlled hand-over; it must never silently start a second control-plane
client.

## 8. Phase plan

### Phase 0 — Native feasibility probe (must pass before production code)

On the actual supported Mac:

- build a minimal headless `.app` using Swift + ServiceManagement;
- embed one disposable LaunchAgent plist;
- prove `SMAppService.agent(...).register()`;
- prove `status == enabled` or correctly surfaced `requiresApproval`;
- prove unregister;
- prove the LaunchAgent relaunches after a crash/non-zero exit according to its launchd policy;
- prove reboot + login causes relaunch;
- determine whether local/ad-hoc signing is sufficient for development and what signing/notarization
  is required for a distributable release artifact;
- prove the chosen app install location remains discoverable and stable across login/reboot.

If the app-bundle/signing assumptions fail, stop and revise the design before changing Python
lifecycle code.

### Phase A — Native companion skeleton

- add the smallest maintainable Swift source tree under `native/macos/service_manager/`;
- implement register/unregister/status/open-settings only;
- add deterministic bundle assembly/build tooling;
- add the two LaunchAgent plists using `BundleProgram`;
- keep the app headless (no menu bar/UI unless later evidence proves it necessary).

### Phase B — Stable bundle-owned launcher

- add the tiny launcher that reads the private service manifest;
- strict allowlist of service IDs (`tunnel`, `agent-bridge`);
- absolute executable/config paths only;
- environment/private-file handling without secrets in plist/argv;
- `exec` the real process so launchd observes the service lifecycle directly;
- add stable stdout/stderr/log behavior.

### Phase C — Python lifecycle adapter

Replace the direct `launchctl` implementation in `darwin_lifecycle.py` with a narrow adapter to the
native Service Manager.

- keep Python responsible for validation and product-level errors;
- native companion owns ServiceManagement calls;
- map native statuses to stable CLI statuses;
- remove direct plist installation from the normal path;
- retain a deliberately narrow legacy migration helper only until v0.13.1 upgrades are safe.

### Phase D — Tunnel persistence

Add first-class lifecycle management for the tunnel top-level process.

- launchd/SMAppService owns `serverfs tunnel`;
- preserve current tunnel-client/supervisor/serve/file-ingress ownership and shutdown semantics;
- prevent duplicate unmanaged + managed tunnel instances;
- ensure graceful SIGTERM reaches the existing shutdown path;
- verify network/proxy failures cause expected recovery rather than permanent disappearance.

### Phase E — v0.13.1 migration

Implement and test the legacy Bridge hand-over and unmanaged tunnel hand-over described in §7.
Migration must be idempotent and recoverable.

### Phase F — Doctor, observability and UX

`serverfs doctor` / `serverfs service status` must report:

- native companion bundle presence/version;
- code-signing/bundle validity needed by the chosen deployment mode;
- SMAppService registration/approval state for both agents;
- actual process health/PID separately;
- manifest/config drift;
- stale legacy plist/job;
- duplicate unmanaged tunnel process;
- stable log paths.

When approval is required, give one exact action: open Login Items via the native API. Do not ask
users to run raw `launchctl` commands in normal operation.

### Phase G — Tests and live acceptance

Automated tests:

- status mapping;
- manifest validation/permissions/atomicity;
- launcher service allowlist and path validation;
- CLI idempotency;
- migration state machine;
- duplicate prevention;
- no secrets in plist or argv;
- legacy lifecycle tests rewritten around the new adapter;
- Linux/Windows regression suites unchanged.

macOS live tests:

- clean install -> both services healthy;
- terminal closes -> services remain alive;
- forced Bridge crash -> launchd recovery;
- forced tunnel failure -> policy-defined recovery;
- logout/login -> **deferred beyond v0.13.2 closeout**;
- reboot/login -> **deferred beyond v0.13.2 closeout**;
- disable Background/Login Item -> **deferred beyond v0.13.2 closeout**;
- re-enable -> **deferred beyond v0.13.2 closeout**;
- v0.13.1 legacy Bridge -> v0.13.2 migration with no duplicate process;
- unmanaged tunnel -> controlled hand-over;
- restart/update of Python environment -> manifest refresh + restart works;
- uninstall -> registrations and v0.13.2-owned artifacts removed; user config/data preserved.

## 9. Acceptance contract

v0.13.2 is complete only when all of the following are proven on the supported Mac:

1. `main` service lifecycle no longer depends on `nohup`, `disown`, terminal lifetime, WorkBuddy,
   Codex, Claude, or ChatGPT session lifetime.
2. Both persistent macOS services are registered through `SMAppService`, not installed by copying
   plist files to `~/Library/LaunchAgents` in the normal path.
3. Reboot + user-login recovery remains an intended property of the `SMAppService` design, but live reboot/re-login validation is explicitly deferred beyond v0.13.2 and is not a v0.13.2 release gate.
4. Closing the terminal/session that performed installation does not stop either service.
5. launchd restarts the intended service after a crash according to explicit policy.
6. There is exactly one managed tunnel and exactly one managed Agent Bridge.
7. v0.13.1 migration produces no duplicate job and leaves a recoverable failure path until the new
   service is proven healthy.
8. Login Items disable/re-enable live validation is explicitly deferred beyond v0.13.2 and is not a v0.13.2 release gate; the implementation must still preserve the native authorization/status model and must not bypass user control.
9. No root, LaunchDaemon, privileged helper, shell-profile dependency, or secret-bearing launchd
   plist is introduced.
10. Existing ServerFS filesystem semantics, Agent Bridge protocol/runtime behavior, proxy/Jev
    behavior and cross-platform behavior are unchanged except for the macOS lifecycle surface.
11. Full Python/Bridge test suites, macOS native CI/equivalent local gates, ruff and site build are
    green before release closeout.

## 10. Explicit non-goals

- pre-login/root system daemon mode;
- macOS menu-bar UI;
- rewriting ServerFS or Agent Bridge in Swift;
- PyObjC bridge to ServiceManagement;
- replacing launchd itself;
- generic process supervisor framework;
- changing Linux systemd/container deployment;
- changing Windows service/Agent architecture;
- packaging redesign unrelated to the lifecycle companion.

## 11. Implementation principle

The design rule for v0.13.2 is:

> Use macOS native machinery only where macOS owns the problem.

`SMAppService` + launchd own registration, authorization, login persistence and restart. ServerFS
continues to own configuration, MCP, Agent execution and application behavior. The native layer is
therefore a small replaceable boundary, not a second ServerFS implementation.
