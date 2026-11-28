"""Phase D tests for the Agent runtime egress proxy policy (§7.1/§7.2, Phase 0F).

These tests encode the two measured rules that make the proxy a boundary rather than a setting:

- the **Bridge** environment must be free of proxy variables and of Tunnel/Control Plane
  namespaces, because both provider SDKs copy it wholesale into the CLI child and Claude's SDK
  cannot unset an inherited name;
- per-runtime policy is applied strictly **downward**, so a ``use_proxy=false`` child is genuinely
  proxy-free regardless of what the parent carried.

Endpoint validation is asserted as refusal rather than as warning: an operator who asked for a
proxy and silently got direct egress would have no indication of it at all.
"""

from __future__ import annotations

import pytest

from serverfs_mcp.agent_proxy import (
    AGENT_NO_PROXY_ENV,
    AGENT_PROXY_URL_ENV,
    AgentProxyConfig,
    AgentProxyError,
    bridge_environment,
    build_runtime_environment,
    merge_no_proxy,
    parse_agent_proxy,
)
from serverfs_mcp.native_config import NativeProxySettings

# A loopback endpoint standing in for the real proxy. No test connects to it: every case here is
# about what the configuration and the environment contain, never about egress.
TEST_ENDPOINT = "http://127.0.0.1:18080"

# Marker values planted in the parent environment by the isolation tests. They are dummy strings,
# never real credentials, and they exist to be proven absent from the Bridge and child environments.
TUNNEL_MARKERS = {
    "CONTROL_PLANE_FAKE_SECRET": "marker-control-plane",
    "TUNNEL_CLIENT_FAKE_SECRET": "marker-tunnel-client",
    "SERVERFS_PROXY_PASSWORD": "marker-tunnel-proxy",
    "MCP_FAKE_MARKER": "marker-mcp",
    "OPENAI_API_KEY": "marker-openai",
}


def _enabled() -> NativeProxySettings:
    return NativeProxySettings(enabled=True, source="env")


def _disabled() -> NativeProxySettings:
    return NativeProxySettings(enabled=False, source="env")


class TestEndpointValidation:
    """A credentialless absolute http(s) endpoint with an explicit port, or startup fails."""

    def test_valid_endpoint_parses(self) -> None:
        config = parse_agent_proxy(_enabled(), {AGENT_PROXY_URL_ENV: TEST_ENDPOINT})
        assert config is not None
        assert config.url == TEST_ENDPOINT
        assert config.source == "env"

    def test_https_endpoint_is_accepted(self) -> None:
        config = parse_agent_proxy(_enabled(), {AGENT_PROXY_URL_ENV: "https://127.0.0.1:8443"})
        assert config is not None and config.url.startswith("https://")

    def test_config_repr_never_carries_the_endpoint(self) -> None:
        """A config object one repr() away from a transcript is an endpoint in a log line."""
        config = parse_agent_proxy(_enabled(), {AGENT_PROXY_URL_ENV: TEST_ENDPOINT})
        assert config is not None
        rendered = repr(config)
        assert "127.0.0.1" not in rendered
        assert "18080" not in rendered

    @pytest.mark.parametrize(
        "raw",
        [
            "socks5://127.0.0.1:1080",
            "socks://127.0.0.1:1080",
            "ftp://127.0.0.1:21",
        ],
    )
    def test_non_http_schemes_are_refused(self, raw: str) -> None:
        with pytest.raises(AgentProxyError, match="scheme must be one of"):
            parse_agent_proxy(_enabled(), {AGENT_PROXY_URL_ENV: raw})

    def test_credentials_in_url_are_refused(self) -> None:
        """Phase 0F §5: an env-injected credential is not a boundary, so the shape is refused."""
        with pytest.raises(AgentProxyError, match="must not contain credentials"):
            parse_agent_proxy(_enabled(), {AGENT_PROXY_URL_ENV: "http://user:pass@127.0.0.1:8080"})

    def test_username_only_userinfo_is_refused(self) -> None:
        with pytest.raises(AgentProxyError, match="must not contain credentials"):
            parse_agent_proxy(_enabled(), {AGENT_PROXY_URL_ENV: "http://user@127.0.0.1:8080"})

    def test_missing_port_is_refused(self) -> None:
        with pytest.raises(AgentProxyError, match="explicit port"):
            parse_agent_proxy(_enabled(), {AGENT_PROXY_URL_ENV: "http://127.0.0.1"})

    def test_missing_host_is_refused(self) -> None:
        with pytest.raises(AgentProxyError, match="must include a host"):
            parse_agent_proxy(_enabled(), {AGENT_PROXY_URL_ENV: "http://:8080"})

    def test_fragment_is_refused(self) -> None:
        with pytest.raises(AgentProxyError, match="fragment"):
            parse_agent_proxy(_enabled(), {AGENT_PROXY_URL_ENV: f"{TEST_ENDPOINT}/#frag"})

    def test_relative_url_is_refused(self) -> None:
        with pytest.raises(AgentProxyError, match="scheme must be one of"):
            parse_agent_proxy(_enabled(), {AGENT_PROXY_URL_ENV: "127.0.0.1:8080"})

    def test_whitespace_is_refused(self) -> None:
        with pytest.raises(AgentProxyError, match="invalid character"):
            parse_agent_proxy(_enabled(), {AGENT_PROXY_URL_ENV: "http://127.0.0.1 :8080"})

    def test_missing_endpoint_when_enabled_fails_closed(self) -> None:
        """Degrading to direct egress would leak provider traffic with no indication."""
        with pytest.raises(AgentProxyError, match="is required when"):
            parse_agent_proxy(_enabled(), {})

    def test_disabled_proxy_does_not_read_the_environment(self) -> None:
        """An ambient endpoint must not be adopted by a proxy nobody enabled."""
        config = parse_agent_proxy(
            _disabled(), {AGENT_PROXY_URL_ENV: TEST_ENDPOINT, AGENT_NO_PROXY_ENV: "x"}
        )
        assert config is None

    def test_absent_settings_reads_nothing(self) -> None:
        assert parse_agent_proxy(None, {AGENT_PROXY_URL_ENV: TEST_ENDPOINT}) is None


class TestNoProxyMerge:
    """The operator value merges with the mandatory set and can never remove from it."""

    def test_mandatory_entries_present_when_operator_absent(self) -> None:
        merged = merge_no_proxy(None)
        assert set(merged.split(",")) == {"127.0.0.1", "localhost", "::1"}

    def test_mandatory_entries_present_when_operator_empty(self) -> None:
        """Phase 0F measured an empty NO_PROXY re-enables proxying of loopback."""
        assert set(merge_no_proxy("").split(",")) >= {"127.0.0.1", "localhost", "::1"}

    def test_operator_entries_are_added(self) -> None:
        merged = merge_no_proxy("example.internal,.corp")
        parts = merged.split(",")
        assert "example.internal" in parts
        assert ".corp" in parts
        assert {"127.0.0.1", "localhost", "::1"} <= set(parts)

    def test_operator_cannot_remove_mandatory_entries(self) -> None:
        parts = merge_no_proxy("example.internal").split(",")
        assert {"127.0.0.1", "localhost", "::1"} <= set(parts)

    def test_operator_loopback_does_not_duplicate(self) -> None:
        merged = merge_no_proxy("localhost,127.0.0.1,::1")
        assert merged == "localhost,127.0.0.1,::1"

    def test_whitespace_and_empty_entries_are_normalized(self) -> None:
        merged = merge_no_proxy("  a.example ,, b.example ,,")
        parts = merged.split(",")
        assert "a.example" in parts
        assert "b.example" in parts
        assert "" not in parts
        assert all(part == part.strip() for part in parts)

    def test_semicolons_are_treated_as_separators(self) -> None:
        merged = merge_no_proxy("a.example;b.example")
        assert "a.example" in merged.split(",")
        assert "b.example" in merged.split(",")

    def test_output_is_stable(self) -> None:
        assert merge_no_proxy("b.example,a.example") == merge_no_proxy("b.example,a.example")

    def test_no_proxy_env_is_merged_into_the_config(self) -> None:
        config = parse_agent_proxy(
            _enabled(),
            {AGENT_PROXY_URL_ENV: TEST_ENDPOINT, AGENT_NO_PROXY_ENV: "corp.example"},
        )
        assert config is not None
        assert "corp.example" in config.no_proxy.split(",")
        assert "127.0.0.1" in config.no_proxy


class TestBridgeEnvironmentScrub:
    """The Bridge environment carries no proxy variable and no credential namespace (§7.2)."""

    def test_all_proxy_variables_are_removed_in_both_cases(self) -> None:
        env = {
            "HTTP_PROXY": "http://ambient:1",
            "HTTPS_PROXY": "http://ambient:2",
            "ALL_PROXY": "http://ambient:3",
            "NO_PROXY": "ambient.example",
            "http_proxy": "http://ambient:4",
            "https_proxy": "http://ambient:5",
            "all_proxy": "http://ambient:6",
            "no_proxy": "ambient.example",
        }
        scrubbed = bridge_environment(env)
        assert not [
            k
            for k in scrubbed
            if k.lower() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}
        ]

    def test_tunnel_and_control_namespaces_are_removed(self) -> None:
        scrubbed = bridge_environment(dict(TUNNEL_MARKERS))
        assert scrubbed == {}

    def test_agent_proxy_namespace_is_absent_from_the_bridge(self) -> None:
        config = parse_agent_proxy(
            _enabled(),
            {AGENT_PROXY_URL_ENV: TEST_ENDPOINT, AGENT_NO_PROXY_ENV: "corp.example"},
        )
        assert config is not None
        # The whole namespace, not just the endpoint variable. A scrub defined by variable name
        # would leave SERVERFS_AGENT_NO_PROXY behind, which is how this leaked once already.
        for name in bridge_environment(
            {
                AGENT_PROXY_URL_ENV: TEST_ENDPOINT,
                AGENT_NO_PROXY_ENV: "corp.example",
                "SERVERFS_AGENT_EXTRA": "marker",
            }
        ):
            assert not name.startswith("SERVERFS_AGENT_"), name

    def test_every_scrubbed_prefix_matches_its_namespace(self) -> None:
        """A prefix that matches only one member of its namespace is a silent partial scrub."""
        assert bridge_environment({"SERVERFS_AGENT_NO_PROXY": "corp.example"}) == {}
        assert bridge_environment({"SERVERFS_AGENT_ANYTHING": "x"}) == {}
        assert bridge_environment({"SERVERFS_PROXY_ANYTHING": "x"}) == {}

    def test_provider_native_environment_is_preserved(self) -> None:
        """Removing the proxy trust domain must not break provider auth/config needs."""
        env = {
            "PATH": r"C:\Windows\system32",
            "HOME": r"C:\Users\me",
            "USERPROFILE": r"C:\Users\me",
            "LOCALAPPDATA": r"C:\Users\me\AppData\Local",
            "CODEX_HOME": r"C:\Users\me\.codex",
            "SystemRoot": r"C:\Windows",
        }
        assert bridge_environment(env) == env

    def test_unrelated_variables_survive(self) -> None:
        assert bridge_environment({"MY_APP_SETTING": "keep"}) == {"MY_APP_SETTING": "keep"}

    def test_operating_system_environment_is_actually_scrubbed(self) -> None:
        """Prove the default path (os.environ) is scrubbed, not only an injected mapping."""
        import os

        os.environ["HTTPS_PROXY"] = "http://ambient:9"
        os.environ["SERVERFS_AGENT_PROXY_URL"] = TEST_ENDPOINT
        try:
            scrubbed = bridge_environment()
            assert "HTTPS_PROXY" not in scrubbed
            assert AGENT_PROXY_URL_ENV not in scrubbed
        finally:
            del os.environ["HTTPS_PROXY"]
            del os.environ[AGENT_PROXY_URL_ENV]

    def test_module_never_writes_to_os_environ(self) -> None:
        """The no-mutation rule is the module's central invariant, so it is asserted directly."""
        import os

        before = dict(os.environ)
        bridge_environment({**TUNNEL_MARKERS, "HTTPS_PROXY": "http://ambient:1"})
        parse_agent_proxy(_enabled(), {AGENT_PROXY_URL_ENV: TEST_ENDPOINT})
        merge_no_proxy("corp.example")
        assert dict(os.environ) == before


class TestRuntimeChildEnvironment:
    """Per-runtime mapping is downward and decided by policy, not by inheritance."""

    def _proxy(self) -> AgentProxyConfig:
        config = parse_agent_proxy(
            _enabled(),
            {AGENT_PROXY_URL_ENV: TEST_ENDPOINT, AGENT_NO_PROXY_ENV: "corp.example"},
        )
        assert config is not None
        return config

    def test_use_proxy_true_injects_canonical_pair_only(self) -> None:
        child = build_runtime_environment({}, runtime="codex", use_proxy=True, proxy=self._proxy())
        assert child["HTTPS_PROXY"] == TEST_ENDPOINT
        assert "corp.example" in child["NO_PROXY"]
        # Phase 0F: HTTP_PROXY alone is insufficient, so injecting it would only widen the surface.
        assert "HTTP_PROXY" not in child
        assert "ALL_PROXY" not in child
        assert "https_proxy" not in child

    def test_use_proxy_true_forces_local_bypass(self) -> None:
        child = build_runtime_environment({}, runtime="codex", use_proxy=True, proxy=self._proxy())
        assert {"127.0.0.1", "localhost", "::1"} <= set(child["NO_PROXY"].split(","))

    def test_use_proxy_false_is_proxy_free_despite_parent(self) -> None:
        """Phase 0F §7.2 rule 2: a use_proxy=false runtime cannot be cleaned after the fact."""
        parent = {"HTTPS_PROXY": TEST_ENDPOINT, "HTTP_PROXY": TEST_ENDPOINT, "NO_PROXY": "x"}
        child = build_runtime_environment(
            parent, runtime="claude", use_proxy=False, proxy=self._proxy()
        )
        assert "HTTPS_PROXY" not in child
        assert "HTTP_PROXY" not in child
        assert "NO_PROXY" not in child

    def test_agent_namespace_never_reaches_the_child(self) -> None:
        parent = {AGENT_PROXY_URL_ENV: TEST_ENDPOINT, AGENT_NO_PROXY_ENV: "corp.example"}
        for use_proxy in (True, False):
            child = build_runtime_environment(
                parent, runtime="codex", use_proxy=use_proxy, proxy=self._proxy()
            )
            assert AGENT_PROXY_URL_ENV not in child
            assert AGENT_NO_PROXY_ENV not in child

    def test_tunnel_proxy_namespace_never_reaches_the_child(self) -> None:
        """The end-to-end invariant: a Bridge-scrubbed environment yields a credential-free child.

        The child builder only guards the proxy namespaces itself, because the Tunnel and Control
        Plane namespaces are removed one level earlier — when the Bridge environment is built. This
        test therefore asserts the composed result rather than either half in isolation, which is
        the property that actually matters.
        """
        bridge_env = bridge_environment({**TUNNEL_MARKERS, "HTTPS_PROXY": TEST_ENDPOINT})
        child = build_runtime_environment(
            bridge_env, runtime="codex", use_proxy=True, proxy=self._proxy()
        )
        assert not set(TUNNEL_MARKERS) & set(child)
        # And the one proxy variable that is deliberately re-injected is the Agent endpoint only.
        assert child["HTTPS_PROXY"] == TEST_ENDPOINT

    def test_child_builder_also_refuses_the_proxy_namespaces_directly(self) -> None:
        """Defence in depth: an unsanitized base still cannot forward a proxy credential."""
        child = build_runtime_environment(
            {
                "SERVERFS_PROXY_PASSWORD": "marker-tunnel-proxy",
                "SERVERFS_AGENT_PROXY_URL": TEST_ENDPOINT,
            },
            runtime="codex",
            use_proxy=True,
            proxy=self._proxy(),
        )
        assert "SERVERFS_PROXY_PASSWORD" not in child
        assert AGENT_PROXY_URL_ENV not in child
        assert child["HTTPS_PROXY"] == TEST_ENDPOINT

    def test_provider_native_environment_is_preserved(self) -> None:
        parent = {
            "PATH": r"C:\bin",
            "USERPROFILE": r"C:\Users\me",
            "CODEX_HOME": r"C:\Users\me\.codex",
        }
        child = build_runtime_environment(
            parent, runtime="codex", use_proxy=True, proxy=self._proxy()
        )
        assert child["PATH"] == r"C:\bin"
        assert child["USERPROFILE"] == r"C:\Users\me"
        assert child["CODEX_HOME"] == r"C:\Users\me\.codex"

    def test_lowercase_parent_proxy_forms_are_cleared_before_injection(self) -> None:
        parent = {"https_proxy": "http://ambient:2", "no_proxy": "ambient.example"}
        child = build_runtime_environment(
            parent, runtime="codex", use_proxy=True, proxy=self._proxy()
        )
        assert child["HTTPS_PROXY"] == TEST_ENDPOINT
        assert child["NO_PROXY"] == self._proxy().no_proxy

    def test_use_proxy_true_without_a_configured_endpoint_fails_closed(self) -> None:
        """Policy says proxy; nothing configured one. That must not silently become direct."""
        with pytest.raises(AgentProxyError, match="no Agent proxy endpoint is configured"):
            build_runtime_environment({}, runtime="codex", use_proxy=True, proxy=None)

    def test_use_proxy_false_needs_no_endpoint(self) -> None:
        child = build_runtime_environment({}, runtime="qoder", use_proxy=False, proxy=None)
        assert child == {}


class TestJevProxyDecision:
    """The §17 Jev question, answered by measurement rather than assumption.

    Jev may reuse the same operator source but not the same mechanism: it is an HTTP client *inside*
    the Bridge process, so it can be handed an endpoint explicitly, whereas a provider gets a mapped
    child environment. The rule that forbids the shortcut is the same either way — the Bridge
    environment must stay proxy-free, because both provider SDKs copy it wholesale.

    The installed ``typesafe-sdk==0.7.1`` exposes ``api_key``, ``model``, ``retry``, ``timeout``,
    ``headers``, ``transport``, ``http_client`` and ``base_url`` — and **no proxy parameter**. So
    there is no supported explicit-proxy construction, and §17's fallback applies: Jev stays direct
    under its existing fail-open semantics. Reaching for a transport or a monkeypatched client is
    explicitly forbidden, and so is setting ``HTTPS_PROXY`` in the Bridge environment to make Jev
    work.
    """

    def test_installed_sdk_exposes_no_explicit_proxy_parameter(self) -> None:
        pytest.importorskip("typesafe_sdk", reason="Jev SDK is an optional Bridge dependency")
        import inspect

        from typesafe_sdk import AsyncTypeSafeClient

        params = {
            name.lower() for name in inspect.signature(AsyncTypeSafeClient.__init__).parameters
        }
        assert not [name for name in params if "proxy" in name]

    def test_bridge_environment_has_no_proxy_for_jev_to_inherit(self) -> None:
        """Whatever Jev does, it cannot pick up a proxy variable from the Bridge process."""
        scrubbed = bridge_environment({"HTTPS_PROXY": TEST_ENDPOINT, "HTTP_PROXY": TEST_ENDPOINT})
        assert "HTTPS_PROXY" not in scrubbed
        assert "HTTP_PROXY" not in scrubbed
