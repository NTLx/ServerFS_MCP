"""One-off Phase 1 Bridge transport regression, Linux host, stdlib only.

Exercise the actual Linux listener, Core clients, grant verifier and SQLite
nonce store in an isolated scratch directory. No provider, production
state, real Agent task or production listener. Other platform dispatch ASTs
are compared to the exact baseline but native IPC is not simulated as proof.
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASE = "fd1019498b281cdeb8c5805242f1a71222f5eebd"
OUT = ROOT / "docs/diagnostics/v0270-bridge-transport-audit.json"


def dispatch_projection(tree):
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef)
               and n.name == "BridgeProtocolServer")
    return {
        n.name: ast.dump(n, include_attributes=False)
        for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name.startswith("_dispatch")
    }


def verify_sources():
    result = {}
    for platform in ("linux", "macos", "windows"):
        base = ROOT / platform
        count = 0
        for group in ("src", "agent_bridge/src", "tests", "agent_bridge/tests"):
            for path in (base / group).rglob("*.py"):
                ast.parse(path.read_text(encoding="utf-8"), filename=path.relative_to(base).as_posix())
                count += 1
        name = "protocol_windows_native.py" if platform == "windows" else "protocol.py"
        rel = "agent_bridge/src/serverfs_agent_bridge/" + name
        original = subprocess.check_output(
            ["git", "show", BASE + ":" + rel], cwd=ROOT, text=True
        )
        candidate = (base / rel).read_text(encoding="utf-8")
        before = dispatch_projection(ast.parse(original))
        after = dispatch_projection(ast.parse(candidate))
        assert before == after and before, platform + " dispatch changed"
        result[platform] = {"parsed_modules": count, "unchanged_dispatch_methods": sorted(after)}
    return result


def package(name, path):
    mod = types.ModuleType(name)
    mod.__path__ = [str(path)]
    sys.modules[name] = mod


async def linux_ipc():
    assert sys.platform.startswith("linux")
    package("serverfs_agent_bridge", ROOT / "linux/agent_bridge/src/serverfs_agent_bridge")
    package("serverfs_mcp", ROOT / "linux/src/serverfs_mcp")
    # The transport needs BridgeService only as a type; use a minimal service
    # with the actual scratch TaskStore instead of importing provider adapters.
    service_module = types.ModuleType("serverfs_agent_bridge.service")
    service_module.BridgeService = types.SimpleNamespace
    sys.modules[service_module.__name__] = service_module
    from serverfs_agent_bridge.protocol import BridgeProtocolServer
    from serverfs_agent_bridge.core_grants import CoreGrant, CoreGrantVerifier, sign_core_request
    from serverfs_agent_bridge.store import TaskStore
    from serverfs_agent_bridge.errors import BridgeError
    from serverfs_mcp.agent_client import AgentBridgeClient, AgentBridgeClientError
    from serverfs_mcp.agent_core_grant_client import CoreGrantBridgeClient

    checks = []
    async def raw_rpc(path, request):
        reader, writer = await asyncio.open_unix_connection(str(path))
        try:
            writer.write((json.dumps(request) + "\n").encode())
            await writer.drain()
            return json.loads(await reader.readline())
        finally:
            writer.close()
            await writer.wait_closed()

    with tempfile.TemporaryDirectory(prefix="v0270-bridge-") as tmp:
        root = Path(tmp)
        store = TaskStore(root / "state")
        service = types.SimpleNamespace(store=store)
        socket_path = root / "run" / "bridge.sock"
        v1 = BridgeProtocolServer(service=service, socket_path=socket_path)
        await v1.start()
        try:
            assert await AgentBridgeClient(socket_path).call("bridge.ping", {}) == {"status": "ok"}
            checks.append("real Linux v1 UDS")
            assert socket_path.stat().st_mode & 0o777 == 0o600
            checks.append("private socket mode")
        finally:
            await v1.close()
        assert not socket_path.exists()
        checks.append("owned socket cleanup")

        denied = BridgeProtocolServer(service=service, socket_path=socket_path,
                                      allowed_peer_uid=os.getuid() + 1)
        await denied.start()
        try:
            request = {"protocol_version": 1, "request_id": "r1",
                       "method": "bridge.ping", "params": {}}
            response = await raw_rpc(socket_path, request)
            assert response["error"]["code"] == "PEER_NOT_AUTHORIZED", response
            checks.append("wrong measured UID refused before dispatch")
        finally:
            await denied.close()

        secret = b"R" * 32
        digest = "a" * 64
        grant = CoreGrant("core-a", "key-a", secret, (("Repo", digest),),
                          frozenset({"bridge.ping"}))
        verifier = CoreGrantVerifier("bridge-a", (grant,), nonce_claim=store.claim_core_nonce)
        v2 = BridgeProtocolServer(service=service, socket_path=socket_path,
                                  v2_only=True, core_grant_verifier=verifier)
        await v2.start()
        try:
            client = CoreGrantBridgeClient(socket_path, core_id="core-a", bridge_id="bridge-a",
                                          key_id="key-a", secret=secret,
                                          policy_revisions={"Repo": digest})
            assert await client.call("bridge.ping", {"workdir": "Repo"}) == {"status": "ok"}
            checks.append("real signed-v2 UDS and actual nonce store")
            raw = {"protocol_version": 2, "request_id": "r2", "bridge_id": "bridge-a",
                   "core_id": "core-a", "key_id": "key-a", "method": "bridge.ping",
                   "params": {"workdir": "Repo"}, "policy_revision": digest,
                   "issued_at": int(time.time()), "nonce": secrets.token_hex(16)}
            signed = {**raw, "signature": sign_core_request(raw, secret)}
            assert (await raw_rpc(socket_path, signed))["ok"] is True
            assert (await raw_rpc(socket_path, signed))["error"]["code"] == "CORE_NOT_AUTHORIZED"
            checks.append("signed nonce replay denied")
            wrong = {**raw, "nonce": secrets.token_hex(16)}
            bad = {**wrong, "signature": sign_core_request(wrong, b"X" * 32)}
            assert (await raw_rpc(socket_path, bad))["error"]["code"] == "CORE_NOT_AUTHORIZED"
            checks.append("invalid HMAC denied")
            assert (await raw_rpc(socket_path, request))["error"]["code"] == "CORE_NOT_AUTHORIZED"
            checks.append("v1 fallback denied on v2 endpoint")
        finally:
            await v2.close()

        # Recreate verifier and listener with the same persisted DB; the
        # previously accepted envelope must still be denied after restart.
        restarted = BridgeProtocolServer(
            service=service, socket_path=socket_path, v2_only=True,
            core_grant_verifier=CoreGrantVerifier(
                "bridge-a", (grant,), nonce_claim=TaskStore(root / "state").claim_core_nonce
            ),
        )
        await restarted.start()
        try:
            assert (await raw_rpc(socket_path, signed))["error"]["code"] == "CORE_NOT_AUTHORIZED"
            checks.append("nonce replay denied after reopening durable store")
        finally:
            await restarted.close()
    return checks


def main():
    result = {
        "baseline": BASE,
        "source_validation": verify_sources(),
        "linux_checks": asyncio.run(linux_ipc()),
        "limitations": [
            "Linux listener and SQLite store are real; service/provider is a minimal fixture.",
            "macOS and Windows dispatch equivalence is static, not native acceptance.",
            "Not a full pytest, wheel, deployment, or Gate A result.",
        ],
    }
    with OUT.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
