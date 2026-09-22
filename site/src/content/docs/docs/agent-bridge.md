---
title: Agent Bridge
description: Optional structured delegation to native Codex and Claude runtimes.
---

The Agent Bridge is an **optional host-side boundary**. It lets ServerFS expose structured Agent task tools without putting Codex or Claude inside the MCP container.

```text
ChatGPT
  │
  ▼
ServerFS MCP
  │  structured Agent RPC
  ▼
Unix socket
  │
  ▼
Host Agent Bridge
  ├── Codex
  └── Claude Code
```

## Why it is separate

The base `compose.yml` remains Agent-unaware. Agent deployments explicitly add `compose.agent.yml`.

This keeps:

- the filesystem MCP usable without native Agent runtimes
- the host socket and writer locks out of the base deployment
- the security boundary visible in deployment configuration
- existing filesystem-only deployments backward compatible

## Runtime behavior

ServerFS exposes eight structured Agent tools when Agent policy is enabled. They cover runtime discovery, task submission, status/events, approval/question handling, steering where supported, and cancellation.

They are **not** a shell, argv passthrough, or generic command executor.

Current deployments can expose:

- 19 tools: filesystem + Agent
- 21 tools: filesystem + binary + Agent

## Optional Jev advisors

The host Bridge can optionally use TypeSafe Jev as an **advisory-only** decision layer. It does not add any MCP tool or Agent runtime.

With `SERVERFS_JEV_API_KEY` configured, one task-submission request provides both Agent Task Preflight and Runtime Router advice. If the native provider later creates a concrete approval request, Approval Advisor may make one additional Jev request; identical approvals within the same task reuse cached advice. With no key, none of these Jev paths exist.

Jev never overrides the explicit runtime, workdir policy, writer lease, provider approval state, or `respond_agent_approval`. See [Jev Advisors](./jev-advisors/) for the model contract, request flow, data minimization, and failure behavior.

## Delegation hygiene

`submit_agent_task` is intentionally an objective-level interface, not a place to embed an
entire operational transcript. For normal authorized engineering or operations, keep each
delegation narrow and explicit:

- state the user-owned workdir or existing deployment being operated on;
- give one concrete objective;
- state the allowed mutation scope and stop conditions;
- ask for the minimum evidence needed to verify completion;
- prefer existing structured ServerFS/project actions over unrelated shell, network or
  security-analysis detail;
- split implementation, validation, deployment and Git publication when they are genuinely
  separate capability boundaries.

A compact pattern is:

```text
Objective: <one thing to accomplish>
Scope: <workdir/path or existing deployment component>
Allowed changes: <exact mutation boundary>
Stop if: <failure/ambiguity condition>
Return: <specific evidence>
```

This is a clarity and least-authority rule, not a mechanism for evading provider safety
checks. ServerFS forwards the submitted prompt as written; it does not automatically
rewrite, encode or sanitize instructions to change provider safety outcomes. Do not encode,
disguise, fragment or relocate instructions merely to bypass a classifier. If a legitimate
task is blocked, narrow it at a real capability boundary or use a more structured existing
ServerFS primitive without weakening authorization, audit, writer-lease or network-isolation
controls.
