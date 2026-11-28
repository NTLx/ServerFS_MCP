"""Linux/Compose Repo Ops policy rendering tests."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

from platform_contract import require_linux_kernel

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MODULE_PATH = _REPO_ROOT / "deployment" / "agent-bridge" / "render_config.py"
_SPEC = importlib.util.spec_from_file_location("serverfs_repo_ops_render_config", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
render = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(render)


def base_values(tmp_path: Path) -> dict[str, str]:
    require_linux_kernel("Repo Ops Linux renderer asserts POSIX login identity")
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return {
        "SERVERFS_UID": str(os.getuid()),
        "SERVERFS_GID": str(os.getgid()),
        "SERVERFS_AGENT_BRIDGE_ENABLED": "true",
        "SERVERFS_AGENT_PEER_UID": str(os.getuid()),
        "SERVERFS_AGENT_PEER_GID": str(os.getgid()),
        "SERVERFS_AGENT_BRIDGE_HOST_SOCKET_DIR": str(tmp_path / "runtime/socket"),
        "SERVERFS_AGENT_LOCK_HOST_DIR": str(tmp_path / "runtime/locks"),
        "SERVERFS_AGENT_BRIDGE_STATE_DIR": str(tmp_path / "state"),
        "WORKDIR_01_ALIAS": "repo",
        "WORKDIR_01_PATH": str(repo),
        "WORKDIR_01_READ_ONLY": "false",
        "WORKDIR_01_AGENT_MODE": "disabled",
        "WORKDIR_01_AGENT_RUNTIMES": "",
    }


def test_renderer_allows_repo_ops_only_without_agent_runtime(tmp_path: Path) -> None:
    values = base_values(tmp_path)
    values["WORKDIR_01_REPO_OPS_ENABLED"] = "true"
    values["WORKDIR_01_REPO_ACTIONS_JSON"] = json.dumps(
        {
            "wiki_lint": {
                "argv": ["uv", "run", "python", "tools/wiki-lint.py"],
                "mutates": False,
                "argument": "none",
                "integrity": {"tools/wiki-lint.py": "0" * 64},
            },
            "wiki_lint_fix_index": {
                "argv": ["uv", "run", "python", "tools/wiki-lint.py", "--fix-index"],
                "mutates": True,
                "argument": "none",
                "integrity": {"tools/wiki-lint.py": "0" * 64},
            },
        }
    )
    config = render.build_config(values)
    assert config["codex"]["enabled"] is False
    assert config["claude"]["enabled"] is False
    assert config["qoder"]["enabled"] is False
    workdir = config["workdirs"][0]
    assert workdir["repo_ops_enabled"] is True
    assert set(workdir["repo_actions"]) == {"wiki_lint", "wiki_lint_fix_index"}


def test_renderer_preserves_old_workdir_shape_when_repo_ops_is_not_enabled(tmp_path: Path) -> None:
    values = base_values(tmp_path)
    values["WORKDIR_01_AGENT_MODE"] = "workspace-write"
    values["WORKDIR_01_AGENT_RUNTIMES"] = "codex"
    executable = tmp_path / "codex"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    values["SERVERFS_CODEX_BIN"] = str(executable)
    workdir = render.build_config(values)["workdirs"][0]
    assert "repo_ops_enabled" not in workdir
    assert "repo_actions" not in workdir


def test_disabled_slot_cannot_enable_repo_ops(tmp_path: Path) -> None:
    values = base_values(tmp_path)
    values.pop("WORKDIR_01_ALIAS")
    values.pop("WORKDIR_01_PATH")
    values["WORKDIR_01_READ_ONLY"] = "true"
    values["WORKDIR_01_REPO_OPS_ENABLED"] = "true"
    with pytest.raises(render.ConfigRenderError, match="disabled slot cannot configure Repo Ops"):
        render.build_config(values)


def test_compose_agent_overlay_forwards_only_repo_ops_enablement_to_mcp() -> None:
    text = (_REPO_ROOT / "compose.agent.yml").read_text(encoding="utf-8")
    for slot in range(1, 17):
        key = f"WORKDIR_{slot:02d}_REPO_OPS_ENABLED"
        assert f"{key}: ${{{key}:-}}" in text
    assert "REPO_ACTIONS_JSON" not in text


def test_actions_require_repo_ops_enable_and_any_action_requires_writable_workdir(
    tmp_path: Path,
) -> None:
    actions = json.dumps(
        {
            "check": {
                "argv": ["tool", "--check"],
                "mutates": False,
                "argument": "none",
                "integrity": {"guard.txt": "0" * 64},
            }
        }
    )
    values = base_values(tmp_path)
    values["WORKDIR_01_REPO_ACTIONS_JSON"] = actions
    with pytest.raises(render.ConfigRenderError, match="REPO_OPS_ENABLED"):
        render.build_config(values)

    values = base_values(tmp_path)
    values["WORKDIR_01_READ_ONLY"] = "true"
    values["WORKDIR_01_REPO_OPS_ENABLED"] = "true"
    values["WORKDIR_01_REPO_ACTIONS_JSON"] = actions
    with pytest.raises(render.ConfigRenderError, match="read-only workdir"):
        render.build_config(values)


@pytest.mark.parametrize("kind", ["release_tag", "publish_npm"])
def test_renderer_release_policy(tmp_path: Path, kind: str) -> None:
    values = base_values(tmp_path)
    values["WORKDIR_01_REPO_OPS_ENABLED"] = "true"
    action = {
        "kind": kind,
        "argument": "release_tag",
        "mutates": True,
        "repository": "example/project",
        "integrity": {"package.json": "0" * 64, ".github/workflows/publish.yml": "1" * 64},
    }
    values["WORKDIR_01_REPO_ACTIONS_JSON"] = json.dumps({"release": action})
    rendered = render.build_config(values)["workdirs"][0]["repo_actions"]["release"]
    assert rendered == {**action, "argv": []}
    for invalid in (
        {"argv": ["sh"]},
        {"repository": "--help"},
        {"argument": "path"},
        {"kind": "workflow"},
        {"integrity": {"package.json": "0" * 64}},
    ):
        values["WORKDIR_01_REPO_ACTIONS_JSON"] = json.dumps({"release": {**action, **invalid}})
        with pytest.raises(render.ConfigRenderError):
            render.build_config(values)
