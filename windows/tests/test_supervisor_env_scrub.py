"""Environment-capture regressions for endpoint propagation (§15 D4, D5; review P0/P1).

The invariant under test is a *location* claim, not a value transformation: the Agent proxy endpoint
must exist in exactly two places — the supervisor's own memory and the Bridge's memory (via the
stdin bootstrap frame) — and nowhere else.

Two of the three child environments were derived by inheritance rather than by scrub. The renderer
child used the v0.10 ``sanitized_environment``, which predates the Agent namespace and so passed
``SERVERFS_AGENT_PROXY_URL`` straight through to a process with no use for it. The ServerFS stdio
child was started from a copy of the supervisor's environment, inheriting the raw endpoint alongside
the three wiring values it actually needs.

Every case here asserts absence on the specific names the review named, and asserts that the
permitted values do survive — an over-aggressive scrub that dropped ``PATH`` would break the child
just as surely as one that leaks the endpoint.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from serverfs_mcp import supervisor
from serverfs_mcp.agent_lifecycle import bridge_child_environment

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows native lifecycle")

#: The exact names the maintainer required absent from the renderer and stdio child environments.
FORBIDDEN_IN_CHILD = (
    "SERVERFS_AGENT_PROXY_URL",
    "SERVERFS_AGENT_NO_PROXY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
)
FORBIDDEN_PREFIXES = ("SERVERFS_PROXY_", "CONTROL_PLANE_", "TUNNEL_CLIENT_", "MCP_")

#: Dummy marker values. Never real credentials; they exist to be proven absent.
PLANTED = {
    "SERVERFS_AGENT_PROXY_URL": "http://127.0.0.1:19999",
    "SERVERFS_AGENT_NO_PROXY": "corp.example",
    "SERVERFS_PROXY_PASSWORD": "marker-tunnel",
    "SERVERFS_PROXY_HOST": "proxy.internal",
    "CONTROL_PLANE_FAKE_SECRET": "marker-cp",
    "TUNNEL_CLIENT_FAKE_SECRET": "marker-tc",
    "MCP_FAKE_MARKER": "marker-mcp",
    "HTTP_PROXY": "http://127.0.0.1:19080",
    "HTTPS_PROXY": "http://127.0.0.1:19080",
    "ALL_PROXY": "http://127.0.0.1:19080",
    "NO_PROXY": "marker.example",
    "http_proxy": "http://127.0.0.1:19080",
    "https_proxy": "http://127.0.0.1:19080",
    "all_proxy": "http://127.0.0.1:19080",
    "no_proxy": "marker.example",
    # Values a child genuinely needs, which must survive.
    "PATH": r"C:\Windows\system32",
    "SYSTEMROOT": r"C:\Windows",
    "USERPROFILE": r"C:\Users\test",
    "LOCALAPPDATA": r"C:\Users\test\AppData\Local",
    "CODEX_HOME": r"C:\Users\test\.codex",
    "SERVERFS_DATA_HOME": r"C:\Users\test\AppData\Local\ServerFS",
}


class _Rendered:
    """The placement facts _start_stdio_child reads, without rendering anything."""

    def __init__(self, tmp_path: Path) -> None:
        self.socket_path = Path(r"\\.\pipe\serverfs-agent-bridge-v1-0123456789abcdef")
        self.lock_dir = tmp_path / "agent-bridge" / "locks"
        self.state_dir = tmp_path / "agent-bridge" / "state"
        self.config_path = tmp_path / "agent-bridge" / "bridge.json"


@pytest.fixture()
def planted(monkeypatch):
    for name, value in PLANTED.items():
        monkeypatch.setenv(name, value)
    return PLANTED


class TestRendererChildEnvironment:
    def test_no_endpoint_reaches_the_renderer(self, planted):
        env = supervisor._bridge_render_env()
        for name in FORBIDDEN_IN_CHILD:
            assert name not in env, name

    def test_no_tunnel_or_control_namespace_reaches_the_renderer(self, planted):
        env = supervisor._bridge_render_env()
        for name in env:
            assert not name.startswith(FORBIDDEN_PREFIXES), name

    def test_endpoint_value_is_absent_from_the_renderer_environment(self, planted):
        assert "19999" not in repr(supervisor._bridge_render_env())

    def test_data_home_is_readded_because_the_renderer_must_honour_it(self, planted):
        env = supervisor._bridge_render_env()
        assert env["SERVERFS_DATA_HOME"] == PLANTED["SERVERFS_DATA_HOME"]

    def test_provider_native_environment_survives(self, planted):
        env = supervisor._bridge_render_env()
        for name in ("PATH", "SYSTEMROOT", "USERPROFILE", "LOCALAPPDATA", "CODEX_HOME"):
            assert env[name] == PLANTED[name], name

    def test_no_data_home_means_none_is_added(self, monkeypatch):
        monkeypatch.delenv("SERVERFS_DATA_HOME", raising=False)
        assert "SERVERFS_DATA_HOME" not in supervisor._bridge_render_env()


class TestStdioChildEnvironment:
    """The stdio child gets three wiring values and nothing else from the Agent namespace."""

    def _child_env(self, tmp_path: Path) -> dict[str, str]:
        """Reproduce exactly what _start_stdio_child builds, without spawning a real serve."""
        rendered = _Rendered(tmp_path)
        env = bridge_child_environment(None)
        env["SERVERFS_AGENT_BRIDGE_ENABLED"] = "1"
        env["SERVERFS_AGENT_BRIDGE_SOCKET"] = str(rendered.socket_path)
        env["SERVERFS_AGENT_LOCK_DIR"] = str(rendered.lock_dir)
        return env

    def test_raw_agent_proxy_namespace_does_not_reach_the_stdio_child(self, planted, tmp_path):
        env = self._child_env(tmp_path)
        assert "SERVERFS_AGENT_PROXY_URL" not in env
        assert "SERVERFS_AGENT_NO_PROXY" not in env

    def test_endpoint_value_is_absent_from_the_stdio_child(self, planted, tmp_path):
        assert "19999" not in repr(self._child_env(tmp_path))

    def test_no_standard_proxy_variable_reaches_the_stdio_child(self, planted, tmp_path):
        env = self._child_env(tmp_path)
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
            assert name not in env, name

    def test_no_tunnel_or_control_namespace_reaches_the_stdio_child(self, planted, tmp_path):
        env = self._child_env(tmp_path)
        for name in env:
            assert not name.startswith(FORBIDDEN_PREFIXES), name

    def test_the_three_wiring_values_are_present(self, planted, tmp_path):
        """An over-aggressive scrub that dropped these would break the published Agent surface."""
        rendered = _Rendered(tmp_path)
        env = self._child_env(tmp_path)
        assert env["SERVERFS_AGENT_BRIDGE_ENABLED"] == "1"
        assert env["SERVERFS_AGENT_BRIDGE_SOCKET"] == str(rendered.socket_path)
        assert env["SERVERFS_AGENT_LOCK_DIR"] == str(rendered.lock_dir)

    def test_provider_native_environment_survives(self, planted, tmp_path):
        env = self._child_env(tmp_path)
        for name in ("PATH", "SYSTEMROOT", "USERPROFILE", "LOCALAPPDATA", "CODEX_HOME"):
            assert env[name] == PLANTED[name], name

    def test_lower_case_proxy_forms_are_removed(self, planted, tmp_path):
        env = self._child_env(tmp_path)
        assert not [
            n for n in env if n.lower() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}
        ]


class TestBridgeChildEnvironment:
    """The Bridge child: scrubbed, and the endpoint is not placed there either."""

    def test_bridge_child_has_no_agent_namespace(self, planted):
        env = bridge_child_environment(None)
        assert "SERVERFS_AGENT_PROXY_URL" not in env
        assert "SERVERFS_AGENT_NO_PROXY" not in env

    def test_bridge_child_has_no_proxy_variable(self, planted):
        env = bridge_child_environment(None)
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
            assert name not in env, name

    def test_a_configured_proxy_is_still_not_placed_in_the_environment(self, planted):
        """The endpoint reaches the Bridge over stdin, so even a configured proxy stays out."""
        from serverfs_mcp.agent_proxy import AgentProxyConfig

        proxy = AgentProxyConfig(url="http://127.0.0.1:19999", no_proxy="corp.example")
        env = bridge_child_environment(proxy, source=PLANTED)
        assert "19999" not in repr(env)
        assert "SERVERFS_AGENT_PROXY_URL" not in env


class TestEndpointLocations:
    """The endpoint exists in the supervisor's memory and in the bootstrap frame — nowhere else."""

    def test_bootstrap_frame_is_the_only_place_the_value_is_serialised(self, planted, tmp_path):
        from serverfs_mcp.agent_lifecycle import bootstrap_frame_bytes
        from serverfs_mcp.agent_proxy import AgentProxyConfig

        proxy = AgentProxyConfig(url="http://127.0.0.1:19999", no_proxy="corp.example")
        frame = bootstrap_frame_bytes(proxy)
        # It is in the frame...
        assert b"19999" in frame
        # ...and in no child environment.
        assert "19999" not in repr(supervisor._bridge_render_env())
        assert "19999" not in repr(bridge_child_environment(proxy, source=PLANTED))
