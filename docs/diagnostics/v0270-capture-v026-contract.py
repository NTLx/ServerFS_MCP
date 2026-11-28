"""One-off schema evidence capture for the exact pre-split v0.26.0 source.

Run from an isolated development environment containing the pinned Core SDK.
No production settings, provider calls, filesystem mutation tools or network.
This historical extractor is not a shared runtime module or conformance test.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import itertools
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASELINE = "fd1019498b281cdeb8c5805242f1a71222f5eebd"
OUTPUT = ROOT / "contracts/v0.26.0/mcp-tools.json"

BASE = {
    "list_workdirs", "list_directory", "find_files", "search_text",
    "read_text_file", "stat_file", "create_text_file", "edit_text_file",
    "delete_file", "create_directory", "delete_directory",
}
BINARY = {"download_binary_file", "upload_binary_file"}
AGENT = {
    "list_agent_runtimes", "list_agent_models", "submit_agent_task",
    "list_agent_tasks", "get_agent_task", "read_agent_task_events",
    "read_agent_task_result", "respond_agent_approval",
    "answer_agent_question", "send_agent_message", "cancel_agent_task",
}
REPO = {"inspect_repo", "run_repo_action", "commit_repo_changes", "push_repo"}


class NoBridgeCalls:
    async def call(self, *args, **kwargs):
        raise AssertionError("schema capture must not call a Bridge")


def git(*args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=ROOT, text=True
    ).strip()


async def capture() -> dict:
    from serverfs_mcp.config import Settings
    from serverfs_mcp.main import create_server
    from serverfs_mcp.workdirs import EffectiveWorkdirPolicy, Workdir, WorkdirRegistry

    variants = {}
    definitions = {}
    with tempfile.TemporaryDirectory(prefix="serverfs-v026-contract-") as scratch:
        root = Path(scratch)
        (root / "workdir").mkdir()
        for binary, agent, repo, ingress in itertools.product((False, True), repeat=4):
            if ingress and not binary:
                continue
            key = f"binary={int(binary)},agent={int(agent)},repo={int(repo)},ingress={int(ingress)}"
            policy = EffectiveWorkdirPolicy(
                binary_transfer_enabled=binary,
                repo_ops_enabled=repo,
                agent_mode="workspace-write" if agent else "disabled",
                agent_runtimes=frozenset({"codex"}) if agent else frozenset(),
            )
            wd = Workdir("fixture", root / "workdir", None, read_only=False, policy=policy)
            settings = Settings(
                agent_bridge_enabled=agent or repo,
                agent_bridge_socket=str(root / "unused.sock"),
                agent_lock_dir=str(root / "locks"),
                file_ingress_enabled=ingress,
            )
            server = create_server(
                settings, WorkdirRegistry([wd]), NoBridgeCalls() if agent or repo else None
            )
            tools = await server.list_tools()
            expected = BASE | (BINARY if binary else set()) | (AGENT if agent else set()) | (
                REPO if repo else set()
            )
            if len(tools) != len(expected) or {tool.name for tool in tools} != expected:
                raise AssertionError(f"unexpected registered tool surface: {key}")
            variant = {}
            for tool in sorted(tools, key=lambda item: item.name):
                schema = tool.model_dump(mode="json", by_alias=True, exclude_none=True)
                if schema.get("name") != tool.name or "inputSchema" not in schema:
                    raise AssertionError(f"unexpected SDK serialization for {tool.name}")
                entries = definitions.setdefault(tool.name, [])
                if schema not in entries:
                    entries.append(schema)
                variant[tool.name] = entries.index(schema)
            variants[key] = variant
    return {
        "source_sha": BASELINE,
        "mcp_sdk_version": importlib.metadata.version("mcp"),
        "format_version": 1,
        "description": "Registered tool definitions indexed by name and variant; synthetic workdir, no execution.",
        "variants": variants,
        "definitions": definitions,
    }


def main() -> None:
    if git("rev-parse", "HEAD") != BASELINE:
        raise SystemExit("unexpected baseline HEAD")
    if git("status", "--porcelain", "--", "src", "agent_bridge", "pyproject.toml", "uv.lock", "native"):
        raise SystemExit("product source or dependency inputs are dirty")
    if importlib.metadata.version("mcp") != "2.3.0":
        raise SystemExit("unexpected MCP SDK version")
    sys.path.insert(0, str(ROOT / "src"))
    result = asyncio.run(capture())
    encoded = json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    with OUTPUT.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(encoded)
    print(f"CAPTURE PASS: {len(result['variants'])} variants, {len(result['definitions'])} tools")


if __name__ == "__main__":
    main()
