---
title: Security Model
description: Independent authorization, filesystem, ingress and host execution boundaries.
---

ServerFS exposes narrow capabilities, not a general host shell. Each layer enforces its own policy; enabling a Connector, Bridge or capability does not authorize other operations.

## 1. Trust boundaries

| Boundary | Enforcement |
| --- | --- |
| Remote caller → Core | TLS at public ingress, exact Host/Origin admission, API Key or OAuth |
| Core → workdir | Explicit aliases, read-only by default, bounded operations, credential deny rules and OS permissions |
| Core → Bridge | Instance identity, private native IPC and scoped policy/grants |
| Bridge → Git / Agent | Fixed Repo Ops actions or authorized provider runtimes, writer leases, native approvals |
| Optional external fetch/advice | Isolated file ingress or advisory Jev; neither expands workdir authority |

A Tunnel key proves Connector identity, not caller identity. Independent instances need distinct credentials/private state. A common UID or SID is not proof of authorization; same-user component isolation is not a security sandbox against hostile same-user processes.

## 2. Filesystem and mutation

Core tools use descriptor-relative POSIX or native Windows HANDLE-relative traversal. Symlinks and redirected Windows paths are rejected; OS permissions still apply. The service exposes only configured workdir aliases and relative paths.

- Read-only is the default; mutations require an explicitly writable workdir.
- Text edits, deletion and binary overwrite use opaque revision guards. No generic overwrite/force switch or recursive directory deletion.
- Hidden paths and credential deny rules are separate. Linux extra deny rules are additive; disabling built-in deny is risky. Native TOML retains the built-in deny floor.
- Reads, searches and writes have bounded sizes and counts. Structured operation logs omit file contents; keep credentials out of prompts and logs.

Linux adds nonroot containers, read-only root filesystem, dropped capabilities and no-new-privileges. Native deployments use current-user permissions, private directories/ACLs and native lifecycle controls, not a container boundary. Never expose instance state or provider credential roots as workdirs.

## 3. HTTP admission

Streamable HTTP keeps DNS-rebinding protection enabled. The configured Host allowlist is authoritative; Linux's empty default derives the internal service authority, while named native Core admission uses the configured hostname and loopback authority. Unapproved Host/Origin values are rejected. Use exact entries rather than broadening policy to troubleshoot a proxy.

[API Key](../api-key/) checks a shared secret before dispatch with constant-time comparison. Prefer bearer headers; query credentials risk leaking into URL logs. The key does not identify individual users or change workdir policy.

[OAuth](../oauth/) uses opaque resource-bound tokens issued by the separate authorization service and validated over private introspection. Provider credentials stay with that service; provider-native tokens are not MCP tokens. Exact stable-principal admission defaults to deny, codes/state are single-use, refresh tokens rotate, and raw bearer values are not persisted. OAuth scopes do not replace workdir policy. API Key and OAuth are mutually exclusive; named OpenAI HTTP ingress requires static bearer admission.

## 4. File ingress and network isolation

Linux internal-only mode has no host-published Core port; direct HTTP publication adds a network that may permit ordinary container egress. Do not equate the two isolation levels.

Optional Linux file ingress uses a separate egress-enabled sidecar with no workdir mounts, OpenAI credentials or published port. Core calls its fixed internal endpoint, not the supplied temporary URL. Fetch requires HTTPS:443 and an exact configured hostname or explicitly enabled constrained OpenAI Blob family. The sidecar rejects non-global DNS answers, pins the connection to a validated IP, checks TLS for the original hostname and revalidates redirects. Byte and timeout caps apply. See [Binary Transfer](../binary-transfer/).

## 5. Host execution and component identity

Bridge is optional. Linux signed attachments and Windows named attachments use scoped grants; macOS Owned v1 uses private native socket and manifest/policy checks, while Shared and explicitly provisioned Owned v2 Cores use signed v2 grants. Shared owners retain control of grants and lifecycle; unauthorized methods, revoked/stale grants and foreign task access must fail closed. Independent Bridge owners must not claim overlapping writable roots.

[Repo Ops](../repo-ops/) executes deterministic Git and administrator-pinned named actions, never an Agent task. Callers cannot supply arbitrary argv, environment, remote, ref, hooks or force. Commit is explicit-path only and refuses pre-existing staged changes; push uses the configured current upstream. Named actions verify SHA-256 pins before execution and always acquire an exclusive writer lease, even when marked non-mutating. Process-tree cleanup must be proven before releasing the lease; Windows uses Job containment and refuses unsafe reparse traversal.

[Agent delegation](../agent-bridge/) permits only configured runtimes and workdir profiles through host IPC, not a generic MCP command endpoint. Native provider permissions, sandboxing and approvals still govern what an Agent can do. Linux Agent proxying is credentialless-only; proxied Codex uses a Bridge-owned standalone app-server. Never grant approval just to clear a failure or share credentials across instances. The Bridge records a private task-bound keyed identity for original approval requests before redaction, alongside action and resolution metadata. Recognized outside-workdir write requests cannot be approved remotely; deny or cancel them. Public redacted labels are not resource identities or a substitute for native permission checks. Private task-bound audit fingerprints are not public MCP data. The native provider still governs arbitrary commands and tools; this approval check is not a sandbox.

[Jev](../jev-advisors/) is advisory only. It cannot authorize tools, select permissions or answer native approval requests. Its private key stays with the Bridge; approval advice uses minimized, sanitized inputs. A failed advisor call does not change authority.

See [Architecture](../architecture/) for platform limits and [Configuration](../configuration/) for policy. Verify actual authorization and denial behavior independently of process health.
