"""I12: actual Windows native write-isolation across two I11 owned Core/Bridge pairs.

One-use test, only preinstalled nonproduction i11-a and i11-b. Both private
Fixtures receive the SAME random basename via their OWN authenticated MCP
create_text_file/edit_text_file calls, but DIFFERENT nonsecret bytes. Verify
cross-Core Bearer 401, independent revisions/readbacks, denied overwrite
and stale edit; after stopping A Core, B remains healthy and can edit its
own marker. Only two newly created bounded markers are removed at cleanup.

Never uses Claude Provider, Repo Ops, Cloudflare/Connector, or production
workdirs. All four I11 Task Scheduler owners stop in finally; no retry.
Do not output Bearers, paths, file content, IDs or raw errors.
"""

from __future__ import annotations

import asyncio
import ctypes
import importlib.util
import os
import secrets
import sys
from pathlib import Path

IDS = ("i11-a", "i11-b")
PORTS = {"i11-a": 18881, "i11-b": 18882}
START = {"i11-a": "I12_OWNED_A_FIRST\n", "i11-b": "I12_OWNED_B_FIRST\n"}
EDITED = {"i11-a": "I12_OWNED_A_EDITED\n", "i11-b": "I12_OWNED_B_EDITED\n"}
SURVIVOR = "I12_OWNED_B_AFTER_A_STOP\n"
PHASE = "entry"


class I12WriteRefusal(RuntimeError):
    """Only sanitized failure phase and exception type reach stdout/stderr."""


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise I12WriteRefusal(reason)


def helper(filename: str, label: str):
    spec = importlib.util.spec_from_file_location(label, Path(__file__).with_name(filename))
    require(spec is not None and spec.loader is not None, "I12 fixed source dependency absent")
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def errored(reply) -> bool:
    return getattr(reply, "is_error", getattr(reply, "isError", None)) is True


def payload(reply) -> dict:
    smoke = helper("v0250-i11-two-owned-provider-no-model-smoke.py", "i12_http_result")
    return smoke.data(reply)


def bounded_fixture_file(path: Path, *, name: str, root: Path) -> bool:
    """Delete only our exact newly created file with one known nonsecret body."""
    if not path.exists() and not path.is_symlink():
        return True
    require(
        path.name == name
        and path.parent == root
        and path.is_file()
        and not path.is_symlink()
        and path.stat().st_size <= 128,
        "I12 private test probe cannot be safely deleted",
    )
    body = path.read_bytes()
    require(
        body in {s.encode("ascii") for s in (*START.values(), *EDITED.values(), SURVIVOR)},
        "I12 test probe was externally modified; preserve for investigation",
    )
    path.unlink()
    return not path.exists() and not path.is_symlink()


async def file_core(
    instance: str, token: str, other_token: str, filename: str, attempted: set[str]
) -> None:
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    require(instance in IDS and token != other_token, "I12 Core identity/secret overlap")
    url = f"http://127.0.0.1:{PORTS[instance]}/mcp"
    request = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "i12-owned-write-nonprod", "version": "1"},
        },
    }
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Authorization": "Bearer " + other_token,
    }
    async with httpx2.AsyncClient(trust_env=False, timeout=15, follow_redirects=False) as foreign:
        rejected = await foreign.post(url, headers=headers, json=request)
        require(rejected.status_code == 401, "I12 cross-Core Bearer accepted")

    async with (
        httpx2.AsyncClient(
            headers={"Authorization": "Bearer " + token},
            trust_env=False,
            timeout=30,
            follow_redirects=False,
        ) as http,
        streamable_http_client(url, http_client=http) as (reader, writer),
        ClientSession(reader, writer) as session,
    ):
        await session.initialize()
        names = {tool.name for tool in (await session.list_tools()).tools}
        require(
            {
                "list_workdirs",
                "read_text_file",
                "create_text_file",
                "edit_text_file",
                "delete_file",
                "list_agent_runtimes",
            }
            <= names,
            "I12 native Core missing expected File MCP API",
        )
        require(
            "run_repo_action" not in names and "push_repo" not in names,
            "I12 test Core unexpectedly exposes repository mutations",
        )
        roots = payload(await session.call_tool("list_workdirs", {}))
        fixture = next(
            (
                r
                for r in roots.get("workdirs", ())
                if isinstance(r, dict) and r.get("alias") == "Fixture"
            ),
            None,
        )
        require(
            isinstance(fixture, dict)
            and fixture.get("access") == "read-write"
            and fixture.get("agent_mode") == "workspace-write"
            and fixture.get("repo_ops") is False,
            "I12 writable disposable Core Fixture policy changed",
        )
        attempted.add(instance)
        created = payload(
            await session.call_tool(
                "create_text_file",
                {"workdir": "Fixture", "path": filename, "content": START[instance]},
            )
        )
        first_revision = created.get("revision")
        require(
            created.get("created") is True
            and isinstance(first_revision, str)
            and bool(first_revision),
            "I12 authentic HTTP MCP create returned no revision",
        )
        opened = payload(
            await session.call_tool("read_text_file", {"workdir": "Fixture", "path": filename})
        )
        require(
            opened.get("content") == START[instance] and opened.get("revision") == first_revision,
            "I12 native file after create has wrong bytes/revision",
        )
        # Creating the same name again must NEVER overwrite the first content.
        duplicate = await session.call_tool(
            "create_text_file",
            {"workdir": "Fixture", "path": filename, "content": "OVERWRITE_FORBIDDEN\n"},
        )
        require(errored(duplicate), "I12 create unexpectedly overwrote an existing file")
        stale = await session.call_tool(
            "edit_text_file",
            {
                "workdir": "Fixture",
                "path": filename,
                "expected_revision": "i12-intentionally-invalid-revision",
                "edits": [{"old_text": START[instance], "new_text": EDITED[instance]}],
            },
        )
        require(errored(stale), "I12 edit accepted stale revision")
        changed = payload(
            await session.call_tool(
                "edit_text_file",
                {
                    "workdir": "Fixture",
                    "path": filename,
                    "expected_revision": first_revision,
                    "edits": [{"old_text": START[instance], "new_text": EDITED[instance]}],
                },
            )
        )
        revision = changed.get("revision")
        require(
            isinstance(revision, str)
            and revision != first_revision
            and changed.get("edits_applied") == 1,
            "I12 edit did not return unique valid new revision",
        )
        after = payload(
            await session.call_tool("read_text_file", {"workdir": "Fixture", "path": filename})
        )
        require(
            after.get("content") == EDITED[instance] and after.get("revision") == revision,
            "I12 Core read after private write differs",
        )


async def concurrent_writes(keys: dict[str, str], filename: str, attempted: set[str]) -> None:
    await asyncio.gather(
        file_core(IDS[0], keys[IDS[0]], keys[IDS[1]], filename, attempted),
        file_core(IDS[1], keys[IDS[1]], keys[IDS[0]], filename, attempted),
    )


async def b_survives_and_writes(key: str, filename: str) -> None:
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    url = f"http://127.0.0.1:{PORTS[IDS[1]]}/mcp"
    async with (
        httpx2.AsyncClient(
            headers={"Authorization": "Bearer " + key},
            trust_env=False,
            timeout=30,
            follow_redirects=False,
        ) as http,
        streamable_http_client(url, http_client=http) as (reader, writer),
        ClientSession(reader, writer) as session,
    ):
        await session.initialize()
        before = payload(
            await session.call_tool("read_text_file", {"workdir": "Fixture", "path": filename})
        )
        require(before.get("content") == EDITED[IDS[1]], "I12 survivor Core B lost private bytes")
        revision = before.get("revision")
        require(isinstance(revision, str) and bool(revision), "I12 Core B lost revision")
        saved = payload(
            await session.call_tool(
                "edit_text_file",
                {
                    "workdir": "Fixture",
                    "path": filename,
                    "expected_revision": revision,
                    "edits": [{"old_text": EDITED[IDS[1]], "new_text": SURVIVOR}],
                },
            )
        )
        updated = payload(
            await session.call_tool("read_text_file", {"workdir": "Fixture", "path": filename})
        )
        require(
            updated.get("content") == SURVIVOR and updated.get("revision") == saved.get("revision"),
            "I12 B write after A stop not persisted independently",
        )


def main() -> None:
    global PHASE
    from serverfs_mcp.native_config import load_native_config
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_service import _read_env_file
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.readiness import _current_identity

    require(sys.argv[1:] == ["--run-once"], "I12 one-use native file-write gate required")
    require(
        os.name == "nt"
        and sys.platform == "win32"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin()
        and current_user_sid() == storage._sid(),
        "I12 Windows ordinary-user -I -B interpreter required",
    )
    expected = storage.root(IDS[0]) / "envs/bridge/Scripts/python.exe"
    require(
        Path(sys.executable).resolve(strict=True) == expected.resolve(strict=True),
        "I12 exact preinstalled Bridge A Owner Python required",
    )

    stage = helper("v0250-i11-two-owned-provider-stage.py", "i12_protected_native_postflight")
    runtime = helper("v0250-i11-two-owned-provider-no-model-smoke.py", "i12_owned_lifecycle")
    i4 = helper("v0250-i4-independent-live-smoke.py", "i12_protected_source")
    gate = helper("v0250-i4-independent-preflight.py", "i12_protected_host")
    PHASE = "protected-four-disabled-I11-tasks-and-private-fixture-preflight"
    stage.postflight()
    baseline = i4.protected_baseline(gate)
    prod = storage.load("core")
    generation = _current_identity(prod)
    require(generation is not None, "I12 protected production process unavailable")
    fixtures = {}
    keys = {}
    for instance, port in PORTS.items():
        base = storage.root(instance)
        manifest = storage.load("core", instance)
        workdirs, policy = load_native_config(Path(str(manifest.settings["config"])))
        root = base / "workdirs/fixture"
        storage.check_private(root, directory=True, sid=storage._sid())
        require(
            manifest.settings["port"] == port
            and policy.bridge_required
            and len(workdirs) == 1
            and workdirs[0].alias == "Fixture"
            and workdirs[0].root.resolve(strict=True) == root.resolve(strict=True)
            and workdirs[0].read_only is False
            and workdirs[0].policy.repo_ops_enabled is False
            and workdirs[0].agent_runtimes == frozenset({"claude"}),
            "I12 independent writable Core workdir drift",
        )
        fixtures[instance] = root
        auth = storage.home("core", instance) / "http.env"
        storage.check_private(auth, directory=False, sid=storage._sid())
        key = _read_env_file(auth).get("SERVERFS_HTTP_API_KEY")
        require(isinstance(key, str) and len(key) >= 40, "I12 HTTP private Bearer unavailable")
        keys[instance] = key
    require(
        fixtures[IDS[0]].resolve(strict=True) != fixtures[IDS[1]].resolve(strict=True)
        and keys[IDS[0]] != keys[IDS[1]],
        "I12 independent Core roots/credentials overlap",
    )
    filename = "i12-owned-write-" + secrets.token_hex(12) + ".txt"
    require(
        all(
            not (directory / filename).exists() and not (directory / filename).is_symlink()
            for directory in fixtures.values()
        ),
        "I12 random private file basename already occupied",
    )

    started: list[tuple[str, str]] = []
    attempted: set[str] = set()
    failure: BaseException | None = None
    failed_phase = ""
    cleanup = True
    try:
        for instance in IDS:
            PHASE = "scoped-start-Bridge-" + instance
            started.append(("bridge", instance))
            require(
                runtime.cli("bridge", instance, "start").get("readiness") == "owner_alive",
                "I12 installed owned Bridge readiness failed",
            )
        for instance in IDS:
            PHASE = "scoped-start-Core-" + instance
            started.append(("core", instance))
            require(
                runtime.cli("core", instance, "start").get("readiness") == "verified",
                "I12 installed owned Core readiness failed",
            )
        PHASE = "simultaneous-private-MCP-write-and-revision-isolation"
        asyncio.run(asyncio.wait_for(concurrent_writes(keys, filename, attempted), timeout=110))
        print("I12_TWO_OWNED_CORES_CONCURRENT_PRIVATE_MCP_WRITES=PASS")
        print("I12_TWO_CORE_STALE_EDITS_AND_OVERWRITES=DENIED")
        PHASE = "stop-Core-A-and-confirm-B-write-survives"
        core_b = storage.load("core", IDS[1])
        bridge_b = storage.load("bridge", IDS[1])
        before = (_current_identity(core_b), _current_identity(bridge_b))
        require(all(item is not None for item in before), "I12 peer generations absent")
        require(
            runtime.is_disabled(runtime.cli("core", IDS[0], "stop")),
            "I12 native Core A stop denied",
        )
        after = (_current_identity(core_b), _current_identity(bridge_b))
        require(
            all(runtime.same(old, new) for old, new in zip(before, after, strict=True)),
            "I12 peer B process generation changed after stopping A",
        )
        asyncio.run(asyncio.wait_for(b_survives_and_writes(keys[IDS[1]], filename), timeout=65))
        print("I12_B_OWNED_CORE_WRITE_SURVIVES_A_STOP=PASS")
    except BaseException as exc:  # noqa: BLE001 - no raw SDK/private context
        failure, failed_phase = exc, PHASE
    finally:
        PHASE = "stop-only-I11-four-services-and-remove-created-probes"
        for component, instance in reversed(started):
            try:
                status = runtime.cli(component, instance, "status")
                if not runtime.is_disabled(status):
                    status = runtime.cli(component, instance, "stop")
                cleanup = runtime.is_disabled(status) and cleanup
            except BaseException:
                cleanup = False
        for instance in IDS:
            if instance not in attempted:
                continue
            path = fixtures[instance] / filename
            try:
                cleanup = (
                    bounded_fixture_file(path, name=filename, root=fixtures[instance]) and cleanup
                )
            except BaseException:
                cleanup = False
    del keys
    print(
        "I12_EXACT_FOUR_I11_TASKS_AND_TWO_PROBES_CLEANED=" + ("PASS" if cleanup else "UNVERIFIED")
    )
    require(cleanup, "I12 safe final cleanup was not independently verified")
    PHASE = "protected-native-final-postflight-and-generation"
    stage.postflight()
    require(
        runtime.same(generation, _current_identity(prod))
        and i4.protected_baseline(gate) == baseline,
        "I12 protected Scheduler/production baseline changed",
    )
    print("I12_PROTECTED_PRODUCTION_I4_I7=UNCHANGED")
    if failure is not None:
        PHASE = failed_phase
        raise failure
    print("I12_TWO_INDEPENDENT_OWNED_WRITABLE_CORES_NATIVE_E2E=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - fixed phase/type only
        print(
            "I12_WRITABLE_OWNED_PAIRS=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None
