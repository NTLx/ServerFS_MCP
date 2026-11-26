# Windows native deployment

ServerFS supports Windows 11 x64 and local NTFS with a matching prebuilt Rust/ABI3 native wheel. Persistent components run as ordinary current-user Task Scheduler tasks with windowless launchers. No WSL, VM, SCM service or elevation is required.

## Choose a deployment

| Requirement | Guide |
| --- | --- |
| Local MCP client | Installed `serverfs serve --config <absolute TOML path>` (stdio) |
| Persistent HTTP file access | File-only Core |
| Agent delegation or Repo Ops | Owned or shared Bridge with explicit signed Core grants |
| Public ingress | Optional Cloudflare / OpenAI Connector tasks |

For the complete ordered installation procedure, use the [native component operations guide](../../docs/deployment/windows/v0250-native-operations.md). It includes initial offline Wheel installation, a minimal TOML, private configuration, named instance activation, Connector provisioning and scoped retirement. Start with only the capabilities you need.

The initial configuration helpers create fresh instances; they are not in-place upgrade or key-rotation tools. Existing default-owner deployments use their version-matched compatibility commands instead. See the [integration baseline](https://github.com/NTLx/ServerFS_MCP/blob/fffc0adb8341edb30ac7b03e72a017ca95b8c83d/deployment/windows/README.md) for historical procedures and [release checklist](../../docs/v0.26.0-release-checklist.md) for actual acceptance.
