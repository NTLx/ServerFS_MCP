"""The Bridge-side scrub must recognise the same ambient ``*_PROXY_URL`` shape.

§23/§70 freeze `serverfs_mcp` and `serverfs_agent_bridge` as independent packages, so the rule is
implemented twice and the agreement is pinned by tests rather than by a shared import. This file is
the `serverfs_mcp` half; `agent_bridge/tests/test_runtime_proxy_ambient_url.py` is the other half.

Why the rule exists at all: during Phase E a vendor tool on the host exported a loopback proxy under
a `*_PROXY_URL` name. Both implementations recognised only the four standard proxy variables, so
that name reached the Bridge process and would have been forwarded on to a provider child — which is
the inheritance §7.1 forbids. The child's egress must be decided by policy, not by whatever
unrelated proxy configuration the machine happens to export.

The rule is a case-insensitive `_PROXY_URL` **suffix**, not a substring match. A substring rule
would also swallow names that merely mention proxying, and deleting provider configuration nobody
measured is its own silent regression.
"""

from __future__ import annotations

import pytest

from serverfs_mcp.agent_proxy import _is_proxy_variable, bridge_environment

#: The shape actually observed on the host, plus the variants the rule is meant to generalise to.
AMBIENT_PROXY_URL_NAMES = (
    "CODEBUDDY_SERVICE_PROXY_URL",
    "VENDOR_PROXY_URL",
    "MY_PROXY_URL",
    "vendor_proxy_url",
)

#: Names that mention proxying without being an endpoint. These must survive.
PRESERVED_NON_ENDPOINT_NAMES = (
    "PROXY_PROTOCOL_VERSION",
    "PROXY_MODE",
    "CODEX_PROXY_SETTINGS",
)


class TestAmbientProxyUrlIsScrubbed:
    @pytest.mark.parametrize("name", AMBIENT_PROXY_URL_NAMES)
    def test_an_ambient_proxy_url_never_reaches_the_bridge_process(self, name: str) -> None:
        env = bridge_environment({name: "http://127.0.0.1:1"})
        assert name not in env, f"{name} survived the Bridge scrub"

    @pytest.mark.parametrize("name", PRESERVED_NON_ENDPOINT_NAMES)
    def test_a_name_that_only_mentions_proxying_is_preserved(self, name: str) -> None:
        env = bridge_environment({name: "value"})
        assert env.get(name) == "value"

    def test_the_bridge_process_ends_up_with_no_proxy_domain_at_all(self) -> None:
        """The Bridge process must carry no proxy variable of any shape.

        The Agent endpoint is included deliberately: it reaches the Bridge over the private
        bootstrap channel and is mapped downward per child, so it must never sit in an environment
        that provider SDKs copy wholesale. The ambient name is the one this phase added.
        """
        env = bridge_environment(
            {
                "SERVERFS_AGENT_PROXY_URL": "http://proxy.invalid:8080",
                "CODEBUDDY_SERVICE_PROXY_URL": "http://127.0.0.1:1",
                "HTTPS_PROXY": "http://127.0.0.1:1",
                "NO_PROXY": "127.0.0.1",
            }
        )
        for name in (
            "SERVERFS_AGENT_PROXY_URL",
            "CODEBUDDY_SERVICE_PROXY_URL",
            "HTTPS_PROXY",
            "NO_PROXY",
        ):
            assert name not in env, f"{name} survived into the Bridge environment"
        # Provider-native environment a runtime legitimately needs is untouched.
        assert bridge_environment({"PATH": "x", "CODEX_HOME": "y"}).get("CODEX_HOME") == "y"


class TestPredicateDirectly:
    """The predicate itself, so a refactor cannot keep the behaviour by accident elsewhere."""

    @pytest.mark.parametrize("name", AMBIENT_PROXY_URL_NAMES)
    def test_the_predicate_matches_the_suffix(self, name: str) -> None:
        assert _is_proxy_variable(name)

    @pytest.mark.parametrize("name", PRESERVED_NON_ENDPOINT_NAMES)
    def test_the_predicate_does_not_match_a_mention(self, name: str) -> None:
        assert not _is_proxy_variable(name)

    @pytest.mark.parametrize(
        "name", ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "no_proxy"]
    )
    def test_the_standard_names_still_match(self, name: str) -> None:
        assert _is_proxy_variable(name)

    def test_a_substring_rule_would_be_wrong(self) -> None:
        """Why the suffix: a substring rule captures the leak and destroys real configuration."""
        widened = lambda name: "proxy" in name.lower()  # noqa: E731
        assert widened("CODEBUDDY_SERVICE_PROXY_URL")
        assert widened("PROXY_PROTOCOL_VERSION")
        assert not _is_proxy_variable("PROXY_PROTOCOL_VERSION")

    def test_removing_the_suffix_rule_reintroduces_the_leak(self, monkeypatch) -> None:
        import serverfs_mcp.agent_proxy as module

        monkeypatch.setattr(module, "PROXY_URL_SUFFIX", "_NOTHING_MATCHES_THIS_")
        env = module.bridge_environment({"CODEBUDDY_SERVICE_PROXY_URL": "http://127.0.0.1:1"})
        assert env.get("CODEBUDDY_SERVICE_PROXY_URL") == "http://127.0.0.1:1"
