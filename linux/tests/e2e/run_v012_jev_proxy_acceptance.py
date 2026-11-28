#!/usr/bin/env python3
"""v0.12 Linux live acceptance for Jev direct/proxy isolation.

This script reads only SERVERFS_JEV_API_KEY from the repository's untracked .env without shell
execution. It never prints the key or proxy URL. A local credentialless CONNECT observer proves
whether Jev traffic used the explicit httpx2 client proxy while the Bridge process environment stays
proxy-free.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
E2E_DIR = Path(__file__).resolve().parent
BRIDGE_SRC = REPO_ROOT / "agent_bridge" / "src"
sys.path.insert(0, str(E2E_DIR))
sys.path.insert(0, str(BRIDGE_SRC))

from serverfs_agent_bridge.preflight import JevTaskPreflight  # noqa: E402
from serverfs_agent_bridge.runtime_proxy import (  # noqa: E402
    scrub_process_proxy_environment,
)
from v012_connect_forwarder import ConnectForwarder  # noqa: E402


def emit(event: str, **payload: object) -> None:
    print(json.dumps({"event": event, **payload}, ensure_ascii=False, sort_keys=True), flush=True)


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def load_jev_key(path: Path) -> str:
    if not path.is_file():
        raise RuntimeError("repository .env is unavailable")
    found: str | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == "SERVERFS_JEV_API_KEY":
            found = _unquote(value)
    if not found:
        raise RuntimeError("SERVERFS_JEV_API_KEY is not configured in repository .env")
    if any(char.isspace() for char in found):
        raise RuntimeError("SERVERFS_JEV_API_KEY has invalid whitespace")
    return found


async def evaluate_once(api_key: str, *, proxy_url: str | None) -> dict[str, object]:
    advisor = JevTaskPreflight.from_api_key(api_key, proxy_url=proxy_url)
    try:
        result = await advisor.evaluate(
            runtime="codex",
            workdir="ServerFS",
            path="agent_bridge",
            profile="workspace-write",
            prompt=(
                "Validate one bounded v0.12 network-routing behavior without changing files, "
                "report concrete evidence, and stop."
            ),
            is_continuation=False,
        )
    finally:
        await advisor.close()
    return result


async def main_async() -> int:
    api_key = load_jev_key(REPO_ROOT / ".env")
    # Jev routing must be explicit; inherited standard proxy variables would invalidate both arms.
    scrub_process_proxy_environment()

    direct_observer = ConnectForwarder()
    direct_observer.start()
    try:
        direct = await evaluate_once(api_key, proxy_url=None)
        direct_summary = direct_observer.summary()
    finally:
        direct_observer.stop()
    if direct.get("status") != "completed":
        raise RuntimeError(f"direct Jev evaluation did not complete: {direct.get('status')}")
    if direct_summary.total_connects != 0:
        raise RuntimeError("direct Jev evaluation unexpectedly used the proxy observer")
    emit(
        "jev_direct_pass",
        status=direct.get("status"),
        model=direct.get("model"),
        total_connects=direct_summary.total_connects,
    )

    proxy_observer = ConnectForwarder()
    proxy_observer.start()
    if proxy_observer.port is None:
        proxy_observer.stop()
        raise RuntimeError("proxy observer did not expose a port")
    try:
        proxied = await evaluate_once(
            api_key,
            proxy_url=f"http://127.0.0.1:{proxy_observer.port}",
        )
        proxy_summary = proxy_observer.summary()
    finally:
        proxy_observer.stop()
    if proxied.get("status") != "completed":
        raise RuntimeError(f"proxied Jev evaluation did not complete: {proxied.get('status')}")
    if proxy_summary.external_connects < 1:
        raise RuntimeError("proxied Jev evaluation produced no external CONNECT traffic")
    if proxy_summary.loopback_connects != 0:
        raise RuntimeError("proxied Jev evaluation attempted to proxy a loopback destination")
    emit(
        "jev_proxy_pass",
        status=proxied.get("status"),
        model=proxied.get("model"),
        total_connects=proxy_summary.total_connects,
        external_connects=proxy_summary.external_connects,
        loopback_connects=proxy_summary.loopback_connects,
    )
    emit("verdict", case="jev_proxy", answer="PASS")
    return 0


def main() -> int:
    if sys.platform != "linux":
        raise SystemExit("this acceptance is Linux-only")
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
