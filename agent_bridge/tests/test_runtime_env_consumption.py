"""Proof that the runtime egress policy is consumed by the product path, not merely available.

The maintainer review's P0 was precise: the bootstrap frame was parsed and then dropped. ``_serve``
accepted a ``bootstrap`` argument and never used it, so the parsed endpoint reached no provider
child — and a unit test of ``build_runtime_environment`` would have passed anyway, because the
helper was correct and simply unused.

Two properties are asserted separately, because they can fail independently. **The wiring**: a
supervised Bridge's parsed ``RuntimeProxy`` reaches the runtime adapter, proven by running the real
``_serve`` with a real bootstrap frame. **The consumption**: the policy that adapter applies
produces the environment a provider child would genuinely see, proven by having the adapter spawn a
*real* child and reading that child's own ``os.environ`` — so it cannot be satisfied by a
helper returning the right value without anything calling it.

A third case covers the negative: with the wiring removed, the adapter receives nothing, which
is the defect this file exists to catch.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from serverfs_agent_bridge.bootstrap import (
    RuntimeProxy,
    encode_bootstrap_frame,
    parse_bootstrap_frame,
)
from serverfs_agent_bridge.config import BridgeConfig
from serverfs_agent_bridge.main import _serve
from serverfs_agent_bridge.runtime_proxy import (
    build_runtime_environment,
    process_environment_is_clean,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from env_capture_runtime import (  # noqa: E402
    CAPTURE_PROMPT,
    RuntimeEnvCapturingFakeAdapter,
)

#: Dummy markers. A value that appears *only* in the bootstrap frame is what proves the frame was
#: consumed rather than the test having configured it by another route.
FRAME_ONLY_URL = "http://127.0.0.1:18443"


def _bootstrap_stub(url):
    """A frame-shaped double carrying the fields the supervised path reads.

    The real ``BootstrapFrame`` gained ``prior_bridge_execution_stopped`` in Phase F; this double
    models it rather than relying on the production code to tolerate a missing attribute.
    """
    return type("F", (), {"agent_proxy": _frame(url), "prior_bridge_execution_stopped": False})()


FRAME_ONLY_NO_PROXY = "frame-only.example"

PARENT_POLLUTION = {
    "SERVERFS_AGENT_PROXY_URL": "http://127.0.0.1:19999",
    "SERVERFS_AGENT_NO_PROXY": "parent-only.example",
    "SERVERFS_PROXY_PASSWORD": "marker-tunnel-proxy",
    "CONTROL_PLANE_FAKE_SECRET": "marker-control-plane",
    "HTTP_PROXY": "http://127.0.0.1:19080",
    "HTTPS_PROXY": "http://127.0.0.1:19080",
    "ALL_PROXY": "http://127.0.0.1:19080",
    "NO_PROXY": "parent-only.example",
}


@pytest.fixture()
def polluted(monkeypatch):
    """Plant the markers in this process, which is the Bridge environment under test."""
    for name, value in PARENT_POLLUTION.items():
        monkeypatch.setenv(name, value)
    return PARENT_POLLUTION


@pytest.fixture()
def bridge_config(tmp_path: Path) -> BridgeConfig:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    path = tmp_path / "bridge.json"
    path.write_text(
        json.dumps(
            {
                "lease_key": "alias",
                # An explicit endpoint, because omitting it falls back to the production
                # default -- a Named Pipe on Windows but "/run/serverfs-agent-bridge" on
                # Linux, where a test would then try to create a socket in a root-owned
                # directory. The wiring under test is the bootstrap frame, not the listener.
                "socket_path": str(tmp_path / "bridge.sock"),
                "state_dir": str(tmp_path / "state"),
                "lock_dir": str(tmp_path / "locks"),
                "allowed_peer_sid": "S-1-5-21-1-2-3-1001",
                "enable_fake_runtime": True,
                "workdirs": [
                    {
                        "alias": "repo",
                        "host_path": str(workdir),
                        "read_only": False,
                        "agent_mode": "workspace-write",
                        "agent_runtimes": ["fake"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return BridgeConfig.load(path)


def _frame(url: str | None) -> RuntimeProxy | None:
    raw = encode_bootstrap_frame(
        RuntimeProxy(url=url, no_proxy=FRAME_ONLY_NO_PROXY) if url else None
    )
    return parse_bootstrap_frame(raw).agent_proxy


class _NoopProtocolServer:
    """Keep adapter-wiring tests on the protocol seam, not the OS transport.

    Named Pipe lifecycle is covered by its dedicated suites. These tests only need `_serve` to
    construct the configured adapters and then follow its normal shutdown path.
    """

    def __init__(self, **_kwargs):
        pass

    async def start(self) -> None:
        return None

    async def serve_forever(self) -> None:
        await asyncio.Future()

    async def close(self) -> None:
        return None


async def _drive_until_adapter_constructed(
    config: BridgeConfig,
    captured: dict,
    *,
    bootstrap=None,
) -> None:
    """Run `_serve` until adapter construction is observable, then shut it down cleanly."""
    shutdown = asyncio.Event()
    task = asyncio.create_task(
        _serve(config, shutdown_event=shutdown, bootstrap=bootstrap, supervised=True)
    )
    try:
        for _ in range(100):
            if "proxy" in captured:
                break
            if task.done():
                await task
            await asyncio.sleep(0.01)
        assert "proxy" in captured, "the runtime was never constructed"
    finally:
        shutdown.set()
        await asyncio.wait_for(task, timeout=2.0)


class TestBootstrapReachesTheRuntime:
    """The wiring half: the parsed endpoint is handed to the adapter."""

    def test_serve_hands_the_bootstrap_proxy_to_the_runtime(self, bridge_config, monkeypatch):
        """This is the exact defect the review found: parsed, then dropped."""
        captured: dict = {}

        class _Recording(RuntimeEnvCapturingFakeAdapter):
            def __init__(self, *, runtime_proxy=None):
                super().__init__(runtime_proxy=runtime_proxy)
                captured["proxy"] = runtime_proxy

        from serverfs_agent_bridge import adapters as runtime_adapters

        monkeypatch.setattr(runtime_adapters, "FakeAdapter", _Recording)
        monkeypatch.setattr("serverfs_agent_bridge.main.BridgeProtocolServer", _NoopProtocolServer)

        asyncio.run(
            _drive_until_adapter_constructed(
                bridge_config,
                captured,
                bootstrap=_bootstrap_stub(FRAME_ONLY_URL),
            )
        )
        assert captured["proxy"] is not None, "the bootstrap proxy was dropped before the runtime"
        assert captured["proxy"].url == FRAME_ONLY_URL

    def test_no_bootstrap_frame_means_no_proxy(self, bridge_config, monkeypatch):
        """An unsupervised Bridge has no runtime material, and must not invent any."""
        from serverfs_agent_bridge import adapters as runtime_adapters

        captured: dict = {}

        class _Recording(RuntimeEnvCapturingFakeAdapter):
            def __init__(self, *, runtime_proxy=None):
                super().__init__(runtime_proxy=runtime_proxy)
                captured["proxy"] = runtime_proxy

        monkeypatch.setattr(runtime_adapters, "FakeAdapter", _Recording)
        monkeypatch.setattr("serverfs_agent_bridge.main.BridgeProtocolServer", _NoopProtocolServer)

        asyncio.run(_drive_until_adapter_constructed(bridge_config, captured))
        assert captured["proxy"] is None

    def test_serve_hands_the_bootstrap_proxy_to_the_claude_runtime(
        self, bridge_config, monkeypatch
    ):
        """The Claude construction line is held to the same wiring as Codex and Qoder.

        The defect this pins was found preparing Phase G: the Claude call site in ``_serve`` was
        the only adapter construction that did not pass ``runtime_proxy``, so the §7.2 frozen
        mapping had no route to the one runtime §7.2 names for it.
        """
        from dataclasses import replace

        from serverfs_agent_bridge import adapters as runtime_adapters
        from serverfs_agent_bridge.adapters.claude import ClaudeAdapter
        from serverfs_agent_bridge.config import ClaudeSettings

        captured: dict = {}

        class _Recording(ClaudeAdapter):
            def __init__(self, settings, *, client_factory=None, runtime_proxy=None):
                super().__init__(
                    settings, client_factory=client_factory, runtime_proxy=runtime_proxy
                )
                captured["proxy"] = runtime_proxy

        monkeypatch.setattr(runtime_adapters, "ClaudeAdapter", _Recording)
        monkeypatch.setattr("serverfs_agent_bridge.main.BridgeProtocolServer", _NoopProtocolServer)
        claude_config = replace(bridge_config, claude=ClaudeSettings(enabled=True))

        asyncio.run(
            _drive_until_adapter_constructed(
                claude_config,
                captured,
                bootstrap=_bootstrap_stub(FRAME_ONLY_URL),
            )
        )
        assert captured["proxy"] is not None, (
            "the bootstrap proxy was dropped before the Claude adapter"
        )
        assert captured["proxy"].url == FRAME_ONLY_URL


class TestPolicyIsConsumedByARealChild:
    """The consumption half: a spawned child reports its own environment."""

    def _capture(self, tmp_path: Path, *, use_proxy: bool, proxy: RuntimeProxy | None) -> dict:
        """Run one capture through the real policy and return the child's environment."""
        adapter = RuntimeEnvCapturingFakeAdapter(runtime_proxy=proxy)
        adapter._use_proxy = use_proxy
        target = tmp_path / "captured.json"

        class _Ctx:
            task_id = "agt_capture"
            prompt = f"{CAPTURE_PROMPT}{target}"
            continue_native_session_id = None

            async def emit_event(self, *_args, **_kwargs):
                return None

        asyncio.run(adapter.run_task(_Ctx()))
        assert target.is_file(), "the child never reported its environment"
        return json.loads(target.read_text(encoding="utf-8"))

    def test_use_proxy_true_gives_the_child_only_https_proxy_and_no_proxy(self, tmp_path, polluted):
        captured = self._capture(
            tmp_path, use_proxy=True, proxy=RuntimeProxy(FRAME_ONLY_URL, FRAME_ONLY_NO_PROXY)
        )
        assert captured["HTTPS_PROXY"] == FRAME_ONLY_URL
        assert FRAME_ONLY_NO_PROXY in captured["NO_PROXY"]
        assert {"127.0.0.1", "localhost", "::1"} <= set(captured["NO_PROXY"].split(","))
        # Phase 0F: HTTP_PROXY alone is insufficient for HTTPS destinations, so it is not injected.
        assert "HTTP_PROXY" not in captured
        assert "ALL_PROXY" not in captured
        assert "https_proxy" not in captured

    def test_the_child_gets_the_frame_value_not_the_parent_value(self, tmp_path, polluted):
        """The parent carries 19999; only the bootstrap frame carries 18443."""
        captured = self._capture(
            tmp_path, use_proxy=True, proxy=RuntimeProxy(FRAME_ONLY_URL, FRAME_ONLY_NO_PROXY)
        )
        assert "18443" in captured["HTTPS_PROXY"]
        assert "19999" not in json.dumps(captured)

    def test_use_proxy_false_gives_a_proxy_free_child(self, tmp_path, polluted):
        """Even with a configured endpoint, a use_proxy=false child sees no proxy at all."""
        captured = self._capture(
            tmp_path, use_proxy=False, proxy=RuntimeProxy(FRAME_ONLY_URL, FRAME_ONLY_NO_PROXY)
        )
        for name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY"):
            assert name not in captured, name

    def test_the_agent_and_tunnel_namespaces_never_reach_the_child(self, tmp_path, polluted):
        captured = self._capture(
            tmp_path, use_proxy=True, proxy=RuntimeProxy(FRAME_ONLY_URL, FRAME_ONLY_NO_PROXY)
        )
        for name in (
            "SERVERFS_AGENT_PROXY_URL",
            "SERVERFS_AGENT_NO_PROXY",
            "SERVERFS_PROXY_PASSWORD",
            "CONTROL_PLANE_FAKE_SECRET",
        ):
            assert name not in captured, name

    def test_provider_native_environment_survives(self, tmp_path, polluted):
        """An over-aggressive scrub would break provider auth as surely as a leak would leak.

        The identity variables are platform-specific by nature -- a Windows provider needs
        ``SystemRoot`` and a POSIX one needs ``HOME`` -- so the assertion names the ones that exist
        here rather than assuming one platform's set.
        """
        captured = self._capture(
            tmp_path, use_proxy=True, proxy=RuntimeProxy(FRAME_ONLY_URL, FRAME_ONLY_NO_PROXY)
        )
        assert captured.get("PATH"), "PATH is provider-native on every platform"
        if sys.platform.startswith("win"):
            assert captured.get("SystemRoot") or captured.get("SYSTEMROOT")
        else:
            assert captured.get("HOME"), "HOME is the POSIX provider-native identity"

    def test_use_proxy_true_without_an_endpoint_fails_closed(self, tmp_path, polluted):
        from serverfs_agent_bridge.runtime_proxy import RuntimeProxyError

        with pytest.raises(RuntimeProxyError, match="no Agent proxy endpoint"):
            build_runtime_environment(os.environ, runtime="codex", use_proxy=True, proxy=None)


class TestBridgeProcessEnvironmentIsClean:
    """§7.2: the Bridge's own environment carries no proxy and no Agent namespace."""

    def test_polluted_environment_is_reported_as_unclean(self, polluted):
        assert process_environment_is_clean() is False

    def test_clean_environment_is_reported_clean(self):
        # An explicit environment rather than the live one with the fixture's names deleted. The
        # previous version deleted a hardcoded set and then asserted the *process* was clean, which
        # silently depended on this host exporting nothing else proxy-shaped; a vendor tool here
        # does, and the assertion started failing for a reason that had nothing to do with the rule
        # under test. The os.environ path stays covered by the polluted case above.
        assert process_environment_is_clean({"PATH": "x", "HOME": "y"}) is True

    def test_an_ambient_proxy_url_alone_makes_the_process_unclean(self, monkeypatch):
        """The regression this rule exists to prevent, asserted on the process environment.

        Naming the variable explicitly is the point: the assertion must keep working on a host that
        happens to export something proxy-shaped, which is exactly when this matters.
        """
        monkeypatch.setenv("VENDOR_PROXY_URL", "http://127.0.0.1:19998")
        assert process_environment_is_clean() is False

    def test_building_a_child_never_reads_the_bridges_own_namespace(self, tmp_path, polluted):
        """The parent namespace is stripped, so a child cannot inherit SERVERFS_AGENT_*."""
        env = build_runtime_environment(os.environ, runtime="codex", use_proxy=False, proxy=None)
        assert not [n for n in env if n.upper().startswith("SERVERFS_AGENT_")]
