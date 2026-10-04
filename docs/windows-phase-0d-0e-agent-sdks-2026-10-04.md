# Phase 0D and 0E — Qoder and Claude Agent SDKs on Windows (measured 2026-10-04)

Status: **BOTH GATES PASS.** Both official Python Agent SDKs install, import, expose the exact
surface the frozen adapters use, and start their real CLI transport on Windows against the
already-installed native CLIs. Claude therefore stays in v0.11 scope; the discovery report's
"no Windows wheel" premise is corrected in §3 below.

No model inference was executed anywhere in this experiment: no prompt, no `query`, no turn. The
only provider calls are the read-only model catalog (allowed in Phase 0D) and the transport's own
initialize/control round trip.

Harness: `%TEMP%\serverfs-phase0d\` and `%TEMP%\serverfs-phase0e\` (isolated `uv` venvs, Python
3.12.10, `sdk_probe.py`, `catalog_probe.py`, `session_probe.py`). Throwaway, not committed.

Host: Windows 11 Pro 10.0.26200, native `qodercli.EXE` at
`C:\Users\lx\.qoder\bin\qodercli\qodercli.EXE`, native `claude.EXE` at `C:\Users\lx\.local\bin\claude.EXE`.

## 1. Phase 0D — qoder-agent-sdk 1.0.15 (the frozen pin)

| Item | Measurement | Verdict |
| --- | --- | --- |
| Install | `uv pip install qoder-agent-sdk==1.0.15` into an isolated 3.12 venv | PASS (sdist build; wheel availability in §4) |
| Version | `importlib.metadata.version` | `1.0.15` — exactly the pin in `agent_bridge/pyproject.toml:8` |
| Imports | all 11 names used by `adapters/qoder.py:19-31` (`AssistantMessage`, `PermissionResultAllow`, `PermissionResultDeny`, `QoderAgentOptions`, `QoderSDKClient`, `ResultMessage`, `SystemMessage`, `TextBlock`, `ToolPermissionContext`, `ToolUseBlock`, `qodercli_auth`) | PASS, `missing=[]` |
| Options | `QoderAgentOptions` accepts every field the adapter sets: `auth`, `cwd`, `cli_path`, `setting_sources`, `resume`, `continue_conversation`, `session_id`, `permission_mode`, `can_use_tool`, `model` | PASS |
| Construct | `QoderAgentOptions(auth=qodercli_auth(), cwd=Path.home(), cli_path=<qodercli>, setting_sources=["user"])` | PASS |
| `qodercli_auth()` | returns `QoderCLIAuthOptions` using the existing native login; no login/logout performed | PASS |
| Client | `QoderSDKClient(options)` constructs; `connect`/`disconnect`/`get_available_models`/`interrupt`/`query`/`receive_messages`/`set_model` all present, signatures match adapter usage (`query(prompt, session_id, *, priority=...)`) | PASS |
| Transport | real `connect()` against the native CLI, then `get_server_info()` control round trip, `interrupt()` on an idle session, `disconnect()` | PASS — clean, no orphan child |
| Model catalog | `get_available_models()` after `connect()`: **17 entries** | PASS |
| Catalog shape | dataclass fields include `value, modelId, displayName, description, source, isDefault, isEnabled, isNew, isReasoning, isVl, efforts, defaultEffort, thinking_config, context_config, availableContextWindows, defaultContextWindow, maxInputTokens, maxOutputTokens, priceFactor, originalPriceFactor, promotion, scene, serverModel, strategies, supportsDisabled` | PASS |
| Pricing | `isFree`, `priceFactor`, `originalPriceFactor` exist **per model, per catalog response** — so cost status must be read live and must never be hardcoded from history | recorded |
| Permission types | `PermissionResultAllow{behavior, updated_input, updated_permissions, decision_classification}`, `PermissionResultDeny{behavior, message, interrupt, decision_classification}`, `ToolPermissionContext{signal, suggestions, blocked_path, decision_reason, decision_reason_type, classifier_approvable, title, display_name, description, tool_use_id, agent_id, exit_plan_mode}` | present (additive `decision_classification` / `classifier_approvable` fields vs the adapter's assumptions — additive, not breaking) |
| live_steer | untouched by this experiment; stays `false` per plan §11 | unchanged |

## 2. Phase 0E — claude-agent-sdk 0.2.156 (the frozen pin)

| Item | Measurement | Verdict |
| --- | --- | --- |
| Install | `uv pip install --no-binary claude-agent-sdk claude-agent-sdk==0.2.156` | PASS — uv resolved and **built the sdist on Windows with no compiler** (pure-Python hatchling build) |
| Imports | `claude_agent_sdk`: `AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, ResultMessage, TextBlock, ToolUseBlock`; `claude_agent_sdk.types`: `PermissionResultAllow, PermissionResultDeny, PermissionUpdate, ToolPermissionContext` | PASS, `missing=[]` for both modules |
| Options | `ClaudeAgentOptions` exposes `cli_path, cwd, setting_sources, permission_mode, can_use_tool, resume, continue_conversation, session_id, model, allowed_tools, env` (plus `max_budget_usd`, `sandbox`, `task_budget`, …) | PASS |
| Construct | `ClaudeAgentOptions(cli_path=<claude.EXE>, cwd=<temp>, setting_sources=["user"], permission_mode="default")` | PASS |
| Client | `ClaudeSDKClient` methods: `connect, disconnect, interrupt, query, receive_messages, receive_response, set_model, set_permission_mode, get_server_info, get_context_usage, get_mcp_status, rewind_files, stop_task, toggle_mcp_server, reconnect_mcp_server` | PASS |
| Transport | real `connect()` with the explicit native CLI, `get_server_info()` control round trip returned structured server data, `interrupt()` on an idle session, `disconnect()` clean, **no leftover `claude` child process** | PASS |
| CLI discovery | `ClaudeAgentOptions(cwd=…)` without `cli_path` also connected — the SDK can find a CLI on its own, but the adapters keep passing `cli_path` explicitly, which is the supported shape | INFO |
| Internal transport | `claude_agent_sdk._internal.transport.subprocess_cli` imports cleanly (the subprocess-over-stdio transport, no platform socket dependency) | PASS |
| Permission types | `PermissionResultAllow{behavior, updated_input, updated_permissions}`, `PermissionResultDeny{behavior, message, interrupt}`, `PermissionUpdate{type, rules, behavior, mode, directories, destination}`, `ToolPermissionContext{signal, suggestions, tool_use_id, agent_id, blocked_path, decision_reason, title, display_name, description}` | present, matches adapter usage |
| Model discovery | adapter stays `status: "unsupported"` per `AGENTS.md`; no CLI enumeration API was probed and none was used | unchanged |

So plan §12's escape hatch ("if the official SDK path is genuinely unusable on Windows, stop the
Claude track") is **not triggered**. No private daemon was reverse-engineered and no undocumented
API was used: everything above is the published SDK surface plus the native CLI the user already has.

## 3. Correction to the discovery report

`v0.11_windows_agent_bridge_discovery.md` §11.3 concluded "no Windows wheel" from the then-current
latest version. The real distribution matrix (read from the PyPI JSON API, most recent eight
releases) is:

| Package | win_amd64 wheel | sdist |
| --- | --- | --- |
| claude-agent-sdk 0.2.156 (**the pin**) | **present** (104 675 694 B) | present |
| claude-agent-sdk 0.2.157 | absent | present |
| claude-agent-sdk 0.2.158, 0.2.159 | present | present |
| claude-agent-sdk 0.2.160 – 0.2.163 (latest) | **absent** | present |
| qoder-agent-sdk 1.0.8 – 1.0.15 (pin) | **present in every version** | present |

Two consequences recorded in the plan:

1. Windows support is a property of the **pin**, not of the package in general: upgrading
   `claude-agent-sdk` to 0.2.163 would *lose* the Windows wheel. Any upgrade must re-check the
   wheel matrix, and plan §12's "upgrade if the pin fails" clause is now conditioned on that.
2. Even when no wheel exists, the sdist installs and the SDK works, because the Windows runtime
   path is the subprocess-over-stdio transport against a native CLI — which is exactly what the
   adapters already do with `cli_path`.

## 4. Dependency and packaging facts measured here

- `qoder-agent-sdk` deps: `anyio>=4`, `mcp>=1.0,<2.0`; `claude-agent-sdk` deps: `anyio>=4`,
  `jsonschema>=4.20`, `mcp>=1.23,<3.0`, `sniffio`. The intersection is satisfiable, and the frozen
  `agent_bridge/uv.lock` already resolves both, so the two SDKs coexist in one Bridge environment.
- Both probe environments pulled **`pywin32==312` transitively** (through `mcp`). Plan §21 requires
  Windows-specific dependencies to be declared explicitly rather than trusting a transitive
  coincidence — this is now a measured instance of exactly that trap.
- **What the sdist install actually contains.** `qoder-agent-sdk` 1.0.15 from sdist installs 41 files
  with **no bundled executable** (`has bundled cli: False`); `claude-agent-sdk` 0.2.156 from sdist
  installs 34 files and its `_bundled/` directory contains only a `.gitignore`. Both transports still
  connected, because the CLI resolver falls back to `shutil.which(...)` and this host has native
  `claude.EXE` and `qodercli.EXE` on `PATH`. Note the Claude resolver's own comment that an npm
  `claude.cmd` shim is refused — a real `.exe` is required.
- Packaging consequence for Phase H: the Windows release artifact must either ship the platform
  wheel that bundles the CLI, or ship the pure-Python distribution and require a native provider CLI
  plus the explicit `cli_path` the adapters already pass. Both shapes are measured working; the
  second one is what the current adapters assume.
- Bulk download reality on this host: PyPI metadata queries answered in ~0.5–1.2 s, but the CDN
  transfer of a 97.9 MB wheel crawled at a few KB/s and `uv pip install` stalled twice and had to be
  killed. The sdist paths (143 KB / 347 KB) completed. Packaging and CI work must not assume those
  ~100 MB platform wheels are fetchable here, and Phase H should pin explicit, small, verified
  artifacts where possible.

## 5. Gate decisions

- Phase 0D: **PASS** — Qoder is in v0.11 scope; `live_steer=false` unchanged; model catalog must be
  read live per account, never hardcoded; the SDK is a drop-in for the existing adapter on Windows.
- Phase 0E: **PASS** — Claude remains in v0.11 scope through the frozen pin, using the official SDK
  plus the native CLI; no upstream blocker exists.
- Both: the remaining unknown is real task execution with approvals, questions, cancellation and
  resume, which belongs to Phase F/G live smoke and requires explicit maintainer-approved model use.
