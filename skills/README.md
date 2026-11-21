# ServerFS Agent Skills

Select **one Skill for the current objective**. Load another only when the task crosses its boundary.

| Objective | Skill |
| --- | --- |
| Install a new instance | [serverfs-install](serverfs-install/SKILL.md) |
| Change Core/workdir policy | [serverfs-configure](serverfs-configure/SKILL.md) |
| Upgrade a component | [serverfs-upgrade](serverfs-upgrade/SKILL.md) |
| Diagnose a failure | [serverfs-diagnose](serverfs-diagnose/SKILL.md) |
| Manage a Connector | [serverfs-tunnel](serverfs-tunnel/SKILL.md) |
| Configure HTTP admission | [serverfs-auth](serverfs-auth/SKILL.md) |
| Manage Bridge, grants or runtimes | [serverfs-agent-bridge](serverfs-agent-bridge/SKILL.md) |

## Agent loading contract

1. Read the selected SKILL.md. Resolve platform, installed version, exact component and instance identity.
2. Load only the matching platform reference and relevant authoritative config/schema or runbook section. Repository paths refer to the version-matched checkout, not the Plugin ZIP.
3. Prefer bounded inspection: status and effective policy first, exact tool schemas and relevant file ranges next. Follow pagination when necessary for correctness; never substitute a search hit for a required full review.
4. Retain a compact working state: target IDs, scope, desired change, observations, failed checks and next action. Do not repeatedly load historical reports, dump credentials, large logs, raw binary/base64 or unrelated platform procedures.

Stop on unclear ownership or authorization. File-only needs no Bridge; Agent and Repo Ops are separate. A Shared Core cannot manage its owner. Preserve unrelated components, verify actual outcomes and report unverified gates.

## Maintainer contract

Keep Skill descriptions specific to invocation intent. Put decision rules and stop conditions in SKILL.md; put platform-specific details in linked local references. Use precise paths instead of broad reading lists. Prefer concise imperative steps and one canonical source per fact. Preserve all required packaging headings and standalone safety rules; do not depend on this index being loaded at runtime.

The portable Plugin includes the manifest and seven Skills with their references, not this index, checkout, endpoints or credentials. Validate with the repository packaging tool and use version-matched artifacts.
