# Host Agent Bridge deployment

For the Linux platform-local development candidate use the [Linux independent component runbook](../linux/README.md). For a released installation use its matching tagged source. Bridge stays on the host so installed native runtimes remain accessible.

## Choose the mode

| Mode | Meaning |
| --- | --- |
| `disabled` | File-only Core; no Bridge installation or state. |
| `owned` | Dedicated owner with private runtime/state. Use the Linux signed attachment mechanism. |
| `shared` | Explicit same-host grants per authorized Core. A shared Core does not manage the owner. |

On Linux use the named provisioning/install modules, the per-owner user systemd unit and signed Compose attachment. Verify actual host/container socket peer identity; matching UID/GID alone is not authorization. These singleton scripts are Linux compatibility procedures, not instructions for other platforms.

Enable Agent and Repo Ops independently per workdir. Repo Ops-only needs a ready Bridge, but not an available Agent provider. Keep credentials, bindings and `state.sqlite3` private, outside exposed workdirs. Independent owners must not overlap writable roots. Before work, verify attachment, allowed methods/aliases and a negative authorization case. Before stopping or retiring an owner, check dependent Cores, active tasks and recovery guards according to its runbook.

## Compatibility

The scripts in this directory implement the earlier single-owner deployment; use them only for an explicitly identified existing compatibility installation. Their exact commands, configuration, rollback and measured evidence remain in the [immutable integration-baseline guide](https://github.com/NTLx/ServerFS_MCP/blob/fffc0adb8341edb30ac7b03e72a017ca95b8c83d/deployment/agent-bridge/README.md). Do not apply its singleton installer or overlay to named signed attachments or remove a running owner to migrate implicitly.
