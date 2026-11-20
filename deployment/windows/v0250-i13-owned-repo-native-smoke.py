"""I13: one-use native Windows Repo-only E2E for two installed owned pairs.

Runs only in the two disposable, remote-free i13 Git Fixtures. Two distinct
HTTP Core Bearers must reach their own signed Bridge Repo Ops; no Agent,
Provider, commit, push, Connector, production Git or other workdirs. All four
test owners stop and only the two exact random disposable markers are cleaned.
"""

from __future__ import annotations

import asyncio
import ctypes
import importlib.util
import os
import secrets
import sys
from pathlib import Path

IDS = ("i13-a", "i13-b")
PORTS = {"i13-a": 18883, "i13-b": 18884}
BODIES = {"i13-a": "I13_A_REPO_ONLY\n", "i13-b": "I13_B_REPO_ONLY\n"}
ACTION = "status_probe"
PHASE = "entry"


class I13RepoRefusal(RuntimeError):
    """No secrets, private paths, actual Git output or raw SDK errors are printed."""


def require(ok: bool, message: str) -> None:
    if not ok:
        raise I13RepoRefusal(message)


def helper(filename: str, label: str):
    spec = importlib.util.spec_from_file_location(label, Path(__file__).with_name(filename))
    require(spec is not None and spec.loader is not None, "I13 trusted helper missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def data(response) -> dict:
    return helper("v0250-i11-two-owned-provider-no-model-smoke.py", "i13_mcp_decode").data(response)


def errored(response) -> bool:
    return getattr(response, "is_error", getattr(response, "isError", None)) is True


async def inspect_one(instance: str, bearer: str, other: str, filename: str) -> None:
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    require(instance in IDS and bearer != other, "I13 Core credential collision")
    url = f"http://127.0.0.1:{PORTS[instance]}/mcp"
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "i13-repo-only-native", "version": "1"},
        },
    }
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Authorization": "Bearer " + other,
    }
    async with httpx2.AsyncClient(trust_env=False, timeout=15, follow_redirects=False) as bad:
        result = await bad.post(url, json=initialize, headers=headers)
        require(result.status_code == 401, "I13 other Core Bearer was not denied")

    async with (
        httpx2.AsyncClient(
            headers={"Authorization": "Bearer " + bearer},
            trust_env=False,
            follow_redirects=False,
            timeout=40,
        ) as http,
        streamable_http_client(url, http_client=http) as (reader, writer),
        ClientSession(reader, writer) as session,
    ):
        await session.initialize()
        names = {tool.name for tool in (await session.list_tools()).tools}
        require(
            {"inspect_repo", "run_repo_action", "create_text_file", "list_workdirs"} <= names
            and "submit_agent_task" not in names
            and "list_agent_runtimes" not in names,
            "I13 Repo-only surface widened or incomplete",
        )
        roots = data(await session.call_tool("list_workdirs", {}))
        fixture = next(
            (
                row
                for row in roots.get("workdirs", ())
                if isinstance(row, dict) and row.get("alias") == "Fixture"
            ),
            None,
        )
        require(
            isinstance(fixture, dict)
            and fixture.get("access") == "read-write"
            and fixture.get("repo_ops") is True
            and fixture.get("agent_mode") == "disabled",
            "I13 private Repo policy widened",
        )
        actions = data(
            await session.call_tool("inspect_repo", {"workdir": "Fixture", "view": "actions"})
        )
        rows = actions.get("actions")
        require(
            isinstance(rows, list)
            and len(rows) == 1
            and isinstance(rows[0], dict)
            and rows[0].get("name") == ACTION,
            "I13 fixed named action inventory differs",
        )
        before = data(
            await session.call_tool("inspect_repo", {"workdir": "Fixture", "view": "status"})
        )
        require(
            filename not in before.get("untracked_paths", ()) and before.get("staged_paths") == [],
            "I13 isolated Git fixture already contains marker or staged work",
        )
        created = data(
            await session.call_tool(
                "create_text_file",
                {"workdir": "Fixture", "path": filename, "content": BODIES[instance]},
            )
        )
        require(created.get("created") is True, "I13 private marker not created")
        changed = data(
            await session.call_tool("inspect_repo", {"workdir": "Fixture", "view": "status"})
        )
        require(
            filename in changed.get("untracked_paths", ()) and changed.get("staged_paths") == [],
            "I13 Repo status did not see own private marker",
        )
        fixed = data(
            await session.call_tool("run_repo_action", {"workdir": "Fixture", "action": ACTION})
        )
        require(
            fixed.get("returncode") == 0
            and fixed.get("mutates") is False
            and filename in fixed.get("stdout", ""),
            "I13 fixed Git action not executed by owned Bridge",
        )
        denied = await session.call_tool(
            "run_repo_action", {"workdir": "Fixture", "action": "unconfigured_action"}
        )
        require(errored(denied), "I13 unknown Repo action unexpectedly authorized")


async def both_repos(keys: dict[str, str], filename: str) -> None:
    await asyncio.gather(
        inspect_one(IDS[0], keys[IDS[0]], keys[IDS[1]], filename),
        inspect_one(IDS[1], keys[IDS[1]], keys[IDS[0]], filename),
    )


async def survivor(key: str, filename: str) -> None:
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    url = f"http://127.0.0.1:{PORTS[IDS[1]]}/mcp"
    async with (
        httpx2.AsyncClient(
            headers={"Authorization": "Bearer " + key},
            trust_env=False,
            timeout=35,
            follow_redirects=False,
        ) as http,
        streamable_http_client(url, http_client=http) as (reader, writer),
        ClientSession(reader, writer) as session,
    ):
        await session.initialize()
        result = data(
            await session.call_tool("run_repo_action", {"workdir": "Fixture", "action": ACTION})
        )
        require(
            result.get("returncode") == 0 and filename in result.get("stdout", ""),
            "I13 independent B Repo action failed after A Core stop",
        )


def remove_exact_marker(root: Path, filename: str, body: str) -> bool:
    marker = root / filename
    if not marker.exists() and not marker.is_symlink():
        return True
    require(
        marker.parent == root
        and marker.name == filename
        and marker.is_file()
        and not marker.is_symlink()
        and marker.stat().st_size == len(body.encode("ascii"))
        and marker.read_bytes() == body.encode("ascii"),
        "I13 disposable marker changed; preserve for investigation",
    )
    marker.unlink()
    return not marker.exists() and not marker.is_symlink()


def main() -> None:
    global PHASE
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_service import _read_env_file
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.readiness import _current_identity

    require(sys.argv[1:] == ["--run-once"], "I13 exact one-use flag required")
    require(
        sys.platform == "win32"
        and os.name == "nt"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin()
        and current_user_sid() == storage._sid(),
        "I13 native user SID, -I -B required",
    )
    expected_python = storage.root(IDS[0]) / "envs/bridge/Scripts/python.exe"
    require(
        Path(sys.executable).resolve(strict=True) == expected_python.resolve(strict=True),
        "I13 exact installed Bridge A Python required",
    )
    stage = helper("v0250-i13-owned-repo-stage.py", "i13_installed_postflight")
    runtime = helper("v0250-i11-two-owned-provider-no-model-smoke.py", "i13_scoped_services")
    old = helper("v0250-i4-independent-live-smoke.py", "i13_existing_baseline")
    gate = helper("v0250-i4-independent-preflight.py", "i13_protected_gate")
    PHASE = "protected-four-owners-installed-postflight"
    stage.postflight()
    baseline = old.protected_baseline(gate)
    production = storage.load("core")
    production_generation = _current_identity(production)
    require(production_generation is not None, "I13 protected production generation missing")
    roots: dict[str, Path] = {}
    keys: dict[str, str] = {}
    for instance in IDS:
        root = storage.root(instance) / "workdirs/fixture"
        storage.check_private(root, directory=True, sid=storage._sid())
        storage.check_private(root / "guard.txt", directory=False, sid=storage._sid())
        roots[instance] = root
        auth = storage.home("core", instance) / "http.env"
        storage.check_private(auth, directory=False, sid=storage._sid())
        key = _read_env_file(auth).get("SERVERFS_HTTP_API_KEY")
        require(isinstance(key, str) and len(key) >= 40, "I13 private Bearer not provisioned")
        keys[instance] = key
    require(
        roots[IDS[0]].resolve(strict=True) != roots[IDS[1]].resolve(strict=True)
        and keys[IDS[0]] != keys[IDS[1]],
        "I13 private Repo roots or Bearers overlap",
    )
    filename = "i13-owned-repo-" + secrets.token_hex(12) + ".txt"
    require(
        all(
            not (root / filename).exists() and not (root / filename).is_symlink()
            for root in roots.values()
        ),
        "I13 new marker collision",
    )
    started: list[tuple[str, str]] = []
    attempted: set[str] = set()
    failure: BaseException | None = None
    failed_phase = ""
    cleaned = True
    try:
        for instance in IDS:
            PHASE = "start-owned-Bridge-" + instance
            started.append(("bridge", instance))
            require(
                runtime.cli("bridge", instance, "start").get("readiness") == "owner_alive",
                "I13 owned Bridge readiness failed",
            )
        for instance in IDS:
            PHASE = "start-owned-Core-" + instance
            started.append(("core", instance))
            require(
                runtime.cli("core", instance, "start").get("readiness") == "verified",
                "I13 Core readiness failed",
            )
        PHASE = "two-concurrent-true-HTTP-Git-repo-actions"
        attempted.update(IDS)
        asyncio.run(asyncio.wait_for(both_repos(keys, filename), timeout=110))
        print("I13_TWO_SIGNED_OWNED_GIT_ACTIONS_NATIVE=PASS")
        print("I13_CROSS_CORE_BEARERS_AND_UNKNOWN_ACTIONS=DENIED")
        PHASE = "stop-only-A-Core-B-repo-survival"
        core_b = storage.load("core", IDS[1])
        bridge_b = storage.load("bridge", IDS[1])
        before = (_current_identity(core_b), _current_identity(bridge_b))
        require(all(x is not None for x in before), "I13 B Core/Bridge absent")
        require(
            runtime.is_disabled(runtime.cli("core", IDS[0], "stop")),
            "I13 A Core did not stop independently",
        )
        after = (_current_identity(core_b), _current_identity(bridge_b))
        require(
            all(runtime.same(a, b) for a, b in zip(before, after, strict=True)),
            "I13 B Core/Bridge generation changed after A stop",
        )
        asyncio.run(asyncio.wait_for(survivor(keys[IDS[1]], filename), timeout=60))
        print("I13_B_OWNED_REPO_ACTION_SURVIVED_A_STOP=PASS")
    except BaseException as exc:  # noqa: BLE001 - do not reveal exception text
        failure, failed_phase = exc, PHASE
    finally:
        PHASE = "stop-exact-I13-services-and-clean-only-created-markers"
        for component, instance in reversed(started):
            try:
                status = runtime.cli(component, instance, "status")
                if not runtime.is_disabled(status):
                    status = runtime.cli(component, instance, "stop")
                cleaned = runtime.is_disabled(status) and cleaned
            except BaseException:
                cleaned = False
        for instance in attempted:
            try:
                cleaned = (
                    remove_exact_marker(roots[instance], filename, BODIES[instance]) and cleaned
                )
            except BaseException:
                cleaned = False
    del keys
    print(
        "I13_EXACT_FOUR_TASKS_AND_TWO_PRIVATE_MARKERS_CLEANED="
        + ("PASS" if cleaned else "UNVERIFIED")
    )
    require(cleaned, "I13 exact cleanup failed")
    PHASE = "final-protected-native-postflight"
    stage.postflight()
    require(
        runtime.same(production_generation, _current_identity(production))
        and old.protected_baseline(gate) == baseline,
        "I13 protected production baseline changed",
    )
    print("I13_PRODUCTION_AND_I4_I11=UNCHANGED")
    if failure is not None:
        PHASE = failed_phase
        raise failure
    print("I13_TWO_INDEPENDENT_OWNED_REPO_ONLY_NATIVE_E2E=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001
        print(
            "I13_REAL_REPO=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None
