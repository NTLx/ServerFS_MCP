# macOS v0.25.0 post-inventory artifact build — 2026-11-16

## Scope and result

This is bounded artifact-build evidence for the current dirty source tree after the path-only inventory. It does not establish a clean release SHA or deployed v0.25.0. No repository source, version, lock, private credential, service, Git ref, staging area, or production process was changed. No network operation, service registration, or runtime launch was performed. The project and wheel metadata remain `0.24.2`.

The native ServiceManager app build and both offline wheel builds succeeded in one new private temporary directory outside the repository. The app signature and all four bundled LaunchAgent plists passed the requested checks. The archive gate passed for both wheels at `0.24.2`; its deliberate `0.25.0` check rejected them with the expected version-path mismatch (exit 2). All requested current-source byte comparisons passed. The temporary tree was then verified as task-owned and symlink-free and removed.

## Temporary tree and commands

The unique tree was `/tmp/serverfs-v0250-artifact-build-hjmRzU`, created by `mktemp -d /tmp/serverfs-v0250-artifact-build-XXXXXX`. Immediately after creation it was set to mode `0700`; `lstat` verified owner UID `501`, mode `0700`, and that the directory was not a symlink. The native app output was under `native/`; wheels were under the absolute paths `root-wheel/` and `bridge-wheel/`.

Commands executed from the repository root unless indicated:

```bash
bash native/macos/service_manager/build.sh /tmp/serverfs-v0250-artifact-build-hjmRzU/native
uv build --offline --wheel --out-dir /tmp/serverfs-v0250-artifact-build-hjmRzU/root-wheel
(cd agent_bridge && uv build --offline --wheel --out-dir /tmp/serverfs-v0250-artifact-build-hjmRzU/bridge-wheel)
python3 deployment/macos/v0250_wheel_archive_gate.py --root-wheel /tmp/serverfs-v0250-artifact-build-hjmRzU/root-wheel/serverfs_mcp-0.24.2-py3-none-any.whl --bridge-wheel /tmp/serverfs-v0250-artifact-build-hjmRzU/bridge-wheel/serverfs_agent_bridge-0.24.2-py3-none-any.whl --expected-version 0.24.2
python3 deployment/macos/v0250_wheel_archive_gate.py --root-wheel /tmp/serverfs-v0250-artifact-build-hjmRzU/root-wheel/serverfs_mcp-0.24.2-py3-none-any.whl --bridge-wheel /tmp/serverfs-v0250-artifact-build-hjmRzU/bridge-wheel/serverfs_agent_bridge-0.24.2-py3-none-any.whl --expected-version 0.25.0
```

Observed toolchain: Apple Silicon `arm64`, macOS `27.2.0`, `uv 0.9.7`. Exit codes:

| Operation | Exit | Result |
| --- | ---: | --- |
| Native build script | 0 | Built `/tmp/serverfs-v0250-artifact-build-hjmRzU/native/ServerFSServiceManager.app` |
| Root offline wheel build | 0 | Built `serverfs_mcp-0.24.2-py3-none-any.whl` |
| Bridge offline wheel build from `agent_bridge/` | 0 | Built `serverfs_agent_bridge-0.24.2-py3-none-any.whl` |
| Archive gate, expected `0.24.2` | 0 | Both wheel archives passed |
| Archive gate, expected `0.25.0` | 2 | Expected rejection: root distribution metadata path mismatch |
| Byte comparison | 0 | All six requested/current changed source comparisons matched |
| Final app codesign verification | 0 | Valid on disk and satisfies designated requirement |

The native build script used local `xcrun swiftc`, `plutil`, and `codesign`. Its output reported both the nested launcher and app signatures replaced with ad-hoc signatures, then verified the app as valid on disk and satisfying its designated requirement. All four bundled plists existed and `plutil`-validated during the build; a subsequent extraction check confirmed each has a `KeepAlive` key: `com.ntlx.serverfs.tunnel`, `com.ntlx.serverfs.bridge`, `com.ntlx.serverfs.http-origin`, and `com.ntlx.serverfs.cloudflare`.

## Wheel archive and source evidence

The current archive gate validated safe member paths/types, excluded secret-named members, and checked every `RECORD` SHA-256 digest and size. Results:

| Wheel | Members | Uncompressed bytes | Whole-wheel SHA-256 |
| --- | ---: | ---: | --- |
| Root `serverfs_mcp-0.24.2-py3-none-any.whl` | 113 | 1,445,223 | `34838ce33d635fb0b4508269672df1e8c08a3212a831c453c7a39a22e440c83d` |
| Bridge `serverfs_agent_bridge-0.24.2-py3-none-any.whl` | 54 | 767,102 | `19e06bf9de5c2a298335eeb8fb1affdd1acd0319438efd9fe757e28a96ee199f` |

Wheel payloads were compared directly to the current source bytes. Each comparison was equal:

| Source | Wheel member | SHA-256 |
| --- | --- | --- |
| `src/serverfs_mcp/darwin_instance_runtime.py` | `serverfs_mcp/darwin_instance_runtime.py` | `183a2df56d38f52fbbf4cd4ae7fa0942ac41da8a6772baf29fad40de0cde8d14` |
| `src/serverfs_mcp/darwin_instance_shared_lifecycle.py` | `serverfs_mcp/darwin_instance_shared_lifecycle.py` | `323a5a3c45c0c8547bee2e86fa5a75d3760e26c57a7ee63e3350953b678c9917` |
| `src/serverfs_mcp/core_bridge_client_v2.py` | `serverfs_mcp/core_bridge_client_v2.py` | `2fc43bae75264ea8643eff4b07fcb1d1486af20fcee679d57779aab7451c376f` |
| `native/macos/service_manager/Sources/ServiceLauncher/SharedManifestGate.swift` | `serverfs_mcp/_darwin_service_manager/Sources/ServiceLauncher/SharedManifestGate.swift` | `6c6148f6bfe9e5fe630b235be0611ac98bbbea5923ebc2738399fc351ec2e736` |
| `native/macos/service_manager/Sources/ServiceLauncher/main.swift` | `serverfs_mcp/_darwin_service_manager/Sources/ServiceLauncher/main.swift` | `60b974ec4f2d066115cc01b29dd547e677757a3ec8b3bb8a167fd42ed117de9a` |
| `native/macos/service_manager/Sources/ServiceManager/main.swift` | `serverfs_mcp/_darwin_service_manager/Sources/ServiceManager/main.swift` | `392e50930253e76579b825e29f806920426cf35b319976b1c110cc5837eb1574` |

The `0.25.0` gate invocation used the same wheel bytes and failed at the root metadata path check, as required: those archives identify version `0.24.2`. This expected negative result is not a wheel build or `0.24.2` archive-integrity failure.

## Cleanup and limitations

Before cleanup, the exact task temp root was checked with `lstat`; its owner was UID `501`, permissions were `0700`, and it was not a symlink. A recursive `lstat` inventory confirmed every contained path was owned by UID `501` and no contained path was a symlink. Only `/tmp/serverfs-v0250-artifact-build-hjmRzU` was removed. A subsequent existence check confirmed no task test/build artifacts remained in that tree. Existing repository scratch trees were not targeted.

This verifies local candidate artifact construction, static archive integrity, source parity for the listed payloads, and native app packaging/signature. It does not verify installation, app launch, service registration, runtime behavior, a coordinated `0.25.0` release version, a clean/exact release SHA, or deployment. No release or production acceptance claim follows from this report.
