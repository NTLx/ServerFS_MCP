"""Phase 1 static dependency and isolated client smoke evidence.

Run from repository root with Python 3.12+; no third-party installs, real
providers, production IPC, or service operations. Native tests remain separate.
"""
from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs/diagnostics/v0270-platform-split-audit.json"
PLATFORMS = ("linux", "macos", "windows")
NATIVE = {"linux": "linux", "macos": "darwin", "windows": "win32"}


def owner(name):
    words = name.lower().replace(".", "_").split("_")
    for key, aliases in (
        ("linux", {"linux"}), ("macos", {"darwin", "macos"}),
        ("windows", {"windows", "win32", "ntfs"}),
    ):
        if set(words) & aliases:
            return key
    return None


def inventory(platform):
    base = ROOT / platform
    result = {"source_modules": 0, "test_modules": 0, "foreign_named_modules": [],
              "foreign_imports": [], "test_foreign_imports": []}
    for top in ("src", "agent_bridge/src", "tests", "agent_bridge/tests"):
        is_test = top.endswith("tests")
        for path in sorted((base / top).rglob("*.py")):
            rel = path.relative_to(base).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
            result["test_modules" if is_test else "source_modules"] += 1
            if not is_test and owner(path.stem) not in (None, platform):
                result["foreign_named_modules"].append(rel)
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if node.level or module.startswith(("serverfs_mcp", "serverfs_agent_bridge")):
                        names = [module] + [module + "." + a.name for a in node.names]
                for name in names:
                    if owner(name) not in (None, platform):
                        key = "test_foreign_imports" if is_test else "foreign_imports"
                        result[key].append({"path": rel, "line": node.lineno, "target": name})
    return result


def load(platform, module):
    # Load actual candidate source without package metadata or third-party dependencies.
    # The synthetic package exists only within this diagnostic process.
    prefix = "_v0270_" + platform
    if prefix not in sys.modules:
        pkg = types.ModuleType(prefix)
        pkg.__path__ = [str(ROOT / platform / "src/serverfs_mcp")]
        sys.modules[prefix] = pkg
    name = prefix + "." + module
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, ROOT / platform / "src/serverfs_mcp" / (module + ".py")
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def reject_foreign(platform):
    backend = load(platform, "backends")
    client = load(platform, "agent_client")
    saved = sys.platform
    count = 0
    try:
        for other in ("linux", "darwin", "win32", "freebsd"):
            if other == NATIVE[platform]:
                continue
            sys.platform = other
            try:
                backend.get_backend()
            except backend.BackendError as exc:
                assert exc.code == "NATIVE_PLATFORM_UNSUPPORTED"
            else:
                raise AssertionError("foreign backend accepted")
            try:
                client.AgentBridgeClient(Path("unused"))
            except ValueError as exc:
                assert "Agent Bridge client requires" in str(exc)
            else:
                raise AssertionError("foreign IPC client accepted")
            count += 2
    finally:
        sys.platform = saved
    return count


async def linux_wire():
    assert sys.platform.startswith("linux")
    mod = load("linux", "agent_client")
    signed = load("linux", "agent_core_grant_client")
    with tempfile.TemporaryDirectory(prefix="v0270-uds-") as tmp:
        sock = Path(tmp) / "b.sock"
        seen = []
        async def reply(reader, writer):
            request = json.loads(await reader.readline())
            seen.append(request)
            writer.write((json.dumps({"request_id": request["request_id"], "ok": True,
                                      "result": {"accepted": True}}) + "\n").encode())
            await writer.drain()
            writer.close()
            await writer.wait_closed()
        server = await asyncio.start_unix_server(reply, path=str(sock))
        try:
            v1 = mod.AgentBridgeClient(sock)
            assert await v1.call("runtime.list", {}) == {"accepted": True}
            v2 = signed.CoreGrantBridgeClient(
                sock, core_id="core-a", bridge_id="bridge-a", key_id="key-a",
                secret=b"R" * 32, policy_revisions={"Repo": "a" * 64},
            )
            assert await v2.call("repo.inspect", {"workdir": "Repo", "view": "status"}) == {"accepted": True}
            try:
                await v2.call("task.submit", {"workdir": "Repo", "continue_from_task_id": "old"})
            except mod.AgentBridgeInvalidRequest:
                pass
            else:
                raise AssertionError("v2 unsupported continuation accepted")
            assert len(seen) == 2
            assert seen[0]["protocol_version"] == 1
            assert seen[1]["protocol_version"] == 2
            assert seen[1]["core_id"] == "core-a"
            assert len(seen[1]["signature"]) == 64
        finally:
            server.close()
            await server.wait_closed()
    return "PASS: actual Linux UDS v1/v2 exchange, refusal before wire; synthetic server only"


def main():
    result = {"platforms": {}, "limitations": [
        "AST filename/import hints are conservative, not reachability or deletion authorization.",
        "Foreign-platform gates exercised with synthetic platform labels; no native macOS/Windows proof.",
        "Linux test uses isolated synthetic UDS responder, not the production Bridge or provider.",
        "No build, full test suite or Gate A claim.",
    ]}
    for platform in PLATFORMS:
        data = inventory(platform)
        data["foreign_host_refusal_checks"] = reject_foreign(platform)
        result["platforms"][platform] = data
    result["linux_uds"] = asyncio.run(linux_wire())
    with OUT.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    for platform, data in result["platforms"].items():
        print(platform, {k: len(v) if isinstance(v, list) else v for k, v in data.items()})
    print(result["linux_uds"])


if __name__ == "__main__":
    main()
