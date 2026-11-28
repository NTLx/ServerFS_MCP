"""Linux TOML policy translation for operator diagnostics.

The host Bridge lifecycle belongs to systemd, not a native stdio supervisor.
"""

from __future__ import annotations


class AgentLifecycleError(Exception):
    """Redacted operator policy error."""


def render_request_from_config(workdirs, settings) -> dict:
    """Turn the parsed native config into the Bridge renderer's input document.

    Only non-secret policy crosses here. The proxy endpoint is deliberately absent: it is handed to
    the Bridge over the bootstrap channel and never becomes part of a persisted config (§15 D2/D4).
    """
    agent = settings.agent
    agent_enabled = agent is not None and agent.enabled
    if not settings.bridge_required:
        raise AgentLifecycleError("the native configuration does not require the Agent Bridge")
    workdir_entries = []
    for workdir in workdirs:
        repo_actions = settings.repo_actions.get(workdir.alias, ())
        if (
            workdir.agent_mode == "disabled"
            and not workdir.agent_runtimes
            and not workdir.policy.repo_ops_enabled
        ):
            continue
        entry = {
            "alias": workdir.alias,
            "host_path": str(workdir.root),
            "read_only": workdir.read_only,
            "agent_mode": workdir.agent_mode,
            "agent_runtimes": sorted(workdir.agent_runtimes),
        }
        if workdir.policy.repo_ops_enabled:
            entry["repo_ops_enabled"] = True
            entry["repo_actions"] = {
                action.name: {
                    "argv": list(action.argv),
                    "mutates": action.mutates,
                    "argument": action.argument,
                    "integrity": dict(action.integrity),
                    **({"kind": action.kind} if action.kind != "command" else {}),
                    **({"repository": action.repository} if action.repository is not None else {}),
                    **({"github_cli": action.github_cli} if action.github_cli != "gh" else {}),
                }
                for action in repo_actions
            }
        workdir_entries.append(entry)
    if not workdir_entries:
        raise AgentLifecycleError(
            "the Agent Bridge is required but no workdir enables Agent delegation or Repo Ops"
        )
    request = {
        "workdirs": workdir_entries,
        # Per-runtime non-secret policy, not just names: the executable name and use_proxy both have
        # to survive into bridge.json, because use_proxy is what decides whether a provider child
        # receives the Agent proxy at all. Sending names alone silently dropped both.
    }
    if agent_enabled:
        assert agent is not None
        request.update(
            {
                "runtimes": {
                    name: {
                        "enabled": True,
                        _BIN_KEY[name]: agent.runtime_binary(name),
                        "use_proxy": agent.runtime_use_proxy(name),
                    }
                    for name in sorted(agent.enabled_runtimes)
                    if not name.startswith("acp-")
                },
                **(
                    {
                        "acp_profiles": [
                            {
                                "id": profile.id,
                                "command": profile.command,
                                "args": list(profile.args),
                                "use_proxy": profile.use_proxy,
                                "probe_timeout_seconds": profile.probe_timeout_seconds,
                                "shutdown_timeout_seconds": profile.shutdown_timeout_seconds,
                            }
                            for profile in agent.acp_profiles
                        ]
                    }
                    if agent.acp_profiles
                    else {}
                ),
                "limits": {
                    "task_timeout_seconds": agent.task_timeout_seconds,
                    "interaction_timeout_seconds": agent.interaction_timeout_seconds,
                    "max_active_tasks": agent.max_active_tasks,
                    "retention_seconds": agent.retention_seconds,
                },
            }
        )
    return request


#: Each runtime's executable key in the rendered document, matching the Bridge's own field names.
_BIN_KEY = {"codex": "codex_bin", "claude": "claude_bin", "qoder": "qoder_bin"}
