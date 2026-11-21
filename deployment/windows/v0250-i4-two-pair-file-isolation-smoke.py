"""I4 installed topology C: actual independent file reads and write denials.

Two already-installed Windows Scheduler-owned Core/Bridge pairs i4-a/i4-b.
Creates ONE random-name SID-private nonsecret text marker in each disposable
I4 Fixture directory with DIFFERENT text, only after exact protected
Scheduler/production/manifest checks. Does not alter installed policies,
credentials, repository, Provider runtimes or production paths.

Each Core's own authenticated Streamable MCP reads its own marker. The SAME
basename resolves differently under the two independent Cores, proving
workdir-root isolation. Both reject cross-Core Bearer and all write attempts.
Stops all four owned tasks and removes only those two probe-created marker
files in finally. This is NOT Agent/Repo-Ops or public Tunnel acceptance.
"""

from __future__ import annotations

import asyncio
import ctypes
import importlib.util
import os
import secrets
import sys
from pathlib import Path

IDS = ("i4-a", "i4-b")
PORTS = {"i4-a": 18871, "i4-b": 18872}
PHASE = "entry"


class I4FileIsolationRefusal(RuntimeError):
    """All public errors sanitized; no path, key or file contents."""


def require(ok: bool, label: str) -> None:
    if not ok:
        raise I4FileIsolationRefusal(label)


def helper(name: str, label: str):
    source = Path(__file__).with_name(name)
    spec = importlib.util.spec_from_file_location(label, source)
    require(spec is not None and spec.loader is not None, "I4 approved helper missing")
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def is_disabled(status: dict) -> bool:
    return (
        status.get("registered") is True
        and status.get("enabled") is False
        and status.get("running") is False
        and status.get("state") == "Disabled"
    )


def tool_data(reply) -> dict:
    # No raw response text returned to caller on failure.
    require(
        getattr(reply, "is_error", getattr(reply, "isError", True)) is False,
        "I4 authenticated read_text_file rejected",
    )
    result = getattr(reply, "structured_content", None)
    if result is None:
        result = getattr(reply, "structuredContent", None)
    if not isinstance(result, dict):
        # Reuse the proven Windows MCP SDK wire fallback without printing.
        import json

        content = getattr(reply, "content", ())
        require(bool(content) and getattr(content[0], "type", None) == "text", "I4 MCP data absent")
        try:
            result = json.loads(content[0].text)
        except (ValueError, TypeError):
            raise I4FileIsolationRefusal("I4 MCP response not JSON") from None
    require(isinstance(result, dict), "I4 MCP response not object")
    row = result.get("result", result)
    require(isinstance(row, dict), "I4 MCP data unavailable")
    return row


async def one_core(
    instance: str,
    own_key: str,
    other_key: str,
    filename: str,
    expected: str,
) -> None:
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    smoke = helper("v0250-i4-independent-live-smoke.py", "i4_filerc_native_source")
    require(instance in IDS and own_key != other_key, "I4 wrong instance or Bearer overlap")
    await smoke.wrong_bearer_is_refused(PORTS[instance], own_key)
    # Cross-Core Bearer is also refused; never exposed in output.
    async with httpx2.AsyncClient(
        headers={
            "Authorization": "Bearer " + other_key,
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
        trust_env=False,
        timeout=10,
    ) as foreign:
        wrong = await foreign.post(
            f"http://127.0.0.1:{PORTS[instance]}/mcp",
            json=smoke.INITIALIZE_REQUEST,
        )
        require(wrong.status_code == 401, "I4 cross-Core Bearer was accepted")

    url = f"http://127.0.0.1:{PORTS[instance]}/mcp"
    async with (
        httpx2.AsyncClient(
            headers={"Authorization": "Bearer " + own_key},
            trust_env=False,
            follow_redirects=False,
            timeout=25,
        ) as http,
        streamable_http_client(url, http_client=http) as (reader, writer),
        ClientSession(reader, writer) as session,
    ):
        await session.initialize()
        names = {tool.name for tool in (await session.list_tools()).tools}
        require(
            {"list_workdirs", "read_text_file", "create_text_file", "list_agent_runtimes"} <= names,
            "I4 file MCP tools absent",
        )
        require(
            "run_repo_action" not in names and "push_repo" not in names,
            "I4 read-only fixture unexpectedly exposes Agent or Repo mutation",
        )
        runtimes = tool_data(await session.call_tool("list_agent_runtimes", {}))
        require(runtimes.get("runtimes") == [], "I4 unexpectedly exposes Provider runtime")
        listing = tool_data(await session.call_tool("list_workdirs", {}))
        fixture = next(
            (
                row
                for row in listing.get("workdirs", ())
                if isinstance(row, dict) and row.get("alias") == "Fixture"
            ),
            None,
        )
        require(
            fixture is not None
            and fixture.get("access") == "read-only"
            and fixture.get("repo_ops") is False,
            "I4 file Fixture policy drift",
        )
        read = tool_data(
            await session.call_tool(
                "read_text_file",
                {"workdir": "Fixture", "path": filename},
            )
        )
        require(
            read.get("content") == expected,
            "I4 file read returned wrong private workdir's bytes",
        )
        denied = await session.call_tool(
            "create_text_file",
            {
                "workdir": "Fixture",
                "path": "denied-" + filename,
                "content": "I4_DENY_EXPECTED\n",
            },
        )
        require(
            getattr(denied, "is_error", getattr(denied, "isError", False)) is True,
            "I4 read-only Core accepted unexpected file write",
        )
        again = tool_data(
            await session.call_tool(
                "read_text_file",
                {"workdir": "Fixture", "path": filename},
            )
        )
        require(
            again.get("content") == expected,
            "I4 denied file write still altered existing fixture",
        )


async def both(keys: dict[str, str], filename: str, text: dict[str, str]) -> None:
    await asyncio.gather(
        one_core(IDS[0], keys[IDS[0]], keys[IDS[1]], filename, text[IDS[0]]),
        one_core(IDS[1], keys[IDS[1]], keys[IDS[0]], filename, text[IDS[1]]),
    )


def main() -> None:
    global PHASE
    from serverfs_mcp.native_config import load_native_config
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_service import _read_env_file
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.readiness import _current_identity

    require(sys.argv[1:] == ["--run-once"], "I4 one-use native file isolation required")
    require(
        os.name == "nt"
        and sys.platform == "win32"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin()
        and current_user_sid() == storage._sid(),
        "nonadmin native Windows -I -B required",
    )
    private = storage.root(IDS[0]) / "envs/bridge/Scripts/python.exe"
    require(
        Path(sys.executable).resolve(strict=True) == private.resolve(strict=True),
        "I4 native installed owner interpreter required",
    )
    smoke = helper("v0250-i4-independent-live-smoke.py", "i4_file_native_lifecycle")
    gate = helper("v0250-i4-independent-preflight.py", "i4_file_protected_gate")
    PHASE = "protected-scheduler-and-I4-pair-offline-preflight"
    protected = smoke.protected_baseline(gate)
    smoke.four_disabled(gate)
    production = storage.load("core")
    prod_gen = _current_identity(production)
    require(prod_gen is not None, "protected production process unavailable")

    keys: dict[str, str] = {}
    fixtures: dict[str, Path] = {}
    for instance in IDS:
        manifest = storage.load("core", instance)
        workdirs, policy = load_native_config(Path(str(manifest.settings["config"])))
        require(policy.bridge_required and len(workdirs) == 1, "I4 Core Bridge policy drift")
        require(manifest.settings["port"] == PORTS[instance], "I4 port mapping drift")
        fixture = storage.root(instance) / "workdirs/fixture"
        storage.check_private(fixture, directory=True, sid=storage._sid())
        require(fixture.is_dir() and not fixture.is_symlink(), "I4 fixture path differs")
        require(
            Path(workdirs[0].root).resolve(strict=True) == fixture.resolve(strict=True)
            and workdirs[0].read_only is True,
            "I4 policy no longer points to read-only disposable Fixture",
        )
        auth = storage.home("core", instance) / "http.env"
        storage.check_private(auth, directory=False, sid=storage._sid())
        key = _read_env_file(auth).get("SERVERFS_HTTP_API_KEY")
        require(isinstance(key, str) and len(key) >= 32, "I4 private Bearer missing")
        keys[instance] = key
        fixtures[instance] = fixture
    require(len(set(keys.values())) == 2, "I4 cross-Core Bearers are not distinct")
    name = "i4-owned-files-" + secrets.token_hex(10) + ".txt"
    # Same relative basename, different bytes for independent Core roots.
    content = {
        IDS[0]: "I4_C_NATIVE_READ_A\n",
        IDS[1]: "I4_C_NATIVE_READ_B\n",
    }
    require(
        all(
            not (path / name).exists() and not (path / name).is_symlink()
            for path in fixtures.values()
        ),
        "I4 canary file already occupies a private fixture",
    )
    installer = helper("v0250-i2-stage.py", "i4_bounded_private_marker_writer")
    created: list[Path] = []
    started: list[tuple[str, str]] = []
    failure: BaseException | None = None
    failure_phase = ""
    cleaned = True
    try:
        PHASE = "two-exact-private-test-only-fixture-markers"
        for instance in IDS:
            path = fixtures[instance] / name
            installer._new_private_file(path, content[instance].encode("ascii"), storage._sid())
            storage.check_private(path, directory=False, sid=storage._sid())
            created.append(path)
        for instance in IDS:
            PHASE = "start-owned-Bridge-" + instance
            started.append(("bridge", instance))
            require(
                smoke.cli("bridge", instance, "start").get("readiness") == "owner_alive",
                "I4 Bridge not ready",
            )
        PHASE = "two-independent-HMAC-owned-Bridge-pipes"
        # Existing accepted signed-pair test includes cross-Bridge negative HMAC.
        bindings = {}
        from serverfs_mcp.windows_v25.core_bridge_binding import load_native_core_bridge_binding

        for instance in IDS:
            manifest = storage.load("core", instance)
            workdirs, _ = load_native_config(Path(str(manifest.settings["config"])))
            bindings[instance] = load_native_core_bridge_binding(manifest, workdirs)
        asyncio.run(asyncio.wait_for(smoke.signed_pair(bindings, storage._sid()), timeout=40))
        print("I4_FILE_TWO_SIGNED_OWNED_PIPES=PASS")
        for instance in IDS:
            PHASE = "start-independent-Core-" + instance
            started.append(("core", instance))
            require(
                smoke.cli("core", instance, "start").get("readiness") == "verified",
                "I4 Core not ready",
            )
        PHASE = "simultaneous-file-read-and-write-refusal"
        asyncio.run(asyncio.wait_for(both(keys, name, content), timeout=100))
        print("I4_TWO_INDEPENDENT_INSTALLED_FILE_READS=PASS")
        print("I4_SAME_BASENAME_DIFFERENT_ROOT_CONTENT=PASS")
        print("I4_BOTH_WRITES_AND_CROSS_BEARERS=DENIED")
        PHASE = "Core-A-stop-Core-B-reads-independently"
        b = storage.load("core", IDS[1])
        bridge_b = storage.load("bridge", IDS[1])
        before = (_current_identity(b), _current_identity(bridge_b))
        require(all(item is not None for item in before), "I4 peer B generation unavailable")
        require(
            smoke.cli("core", IDS[0], "stop").get("state") == "Disabled", "I4 Core A stop failed"
        )
        after = (_current_identity(b), _current_identity(bridge_b))
        require(
            all(item is not None for item in after)
            and all(
                smoke.same_generation(prev, current)
                for prev, current in zip(before, after, strict=True)
            ),
            "I4 Core/Bridge B generation changed after A stop",
        )
        asyncio.run(
            asyncio.wait_for(
                one_core(IDS[1], keys[IDS[1]], keys[IDS[0]], name, content[IDS[1]]),
                timeout=55,
            )
        )
        print("I4_FILE_CORE_B_SURVIVES_CORE_A_STOP=PASS")
    except BaseException as exc:  # noqa: BLE001 - raw details remain Windows local
        failure, failure_phase = exc, PHASE
    finally:
        PHASE = "reverse-stop-only-four-I4-components-and-remove-two-probes"
        for component, instance in reversed(started):
            try:
                status = smoke.cli(component, instance, "status")
                if not is_disabled(status):
                    status = smoke.cli(component, instance, "stop")
                cleaned = is_disabled(status) and cleaned
            except BaseException:
                cleaned = False
        for path in reversed(created):
            try:
                storage.check_private(path, directory=False, sid=storage._sid())
                require(
                    path.is_file()
                    and not path.is_symlink()
                    and path.name == name
                    and path.parent in fixtures.values()
                    and path.stat().st_size <= 128,
                    "I4 temporary probe identity drift",
                )
                path.unlink()
                cleaned = not path.exists() and cleaned
            except BaseException:
                cleaned = False
    print("I4_FILE_ALL_FOUR_TASKS_AND_MARKERS_RESTORED=" + ("PASS" if cleaned else "UNVERIFIED"))
    require(cleaned, "I4 scoped process/fixture cleanup failed")
    PHASE = "protected-production-native-postflight"
    smoke.four_disabled(gate)
    now = _current_identity(production)
    require(
        now is not None
        and smoke.same_generation(prod_gen, now)
        and smoke.protected_baseline(gate) == protected,
        "I4 protected state changed",
    )
    print("I4_FILE_PRODUCTION_AND_ALL_OTHER_TASKS=UNCHANGED")
    if failure is not None:
        PHASE = failure_phase
        raise failure
    print("I4_TWO_OWNED_PAIRS_REAL_FILE_ISOLATION_E2E=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - only fixed phase/class released
        print(
            "I4_TWO_PAIRS_FILE_CANARY=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None
