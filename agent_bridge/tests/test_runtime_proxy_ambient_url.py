"""The ambient ``*_PROXY_URL`` trust-boundary defect, and the rule that closes it.

Phase E measured a real violation of the frozen §7.1 rule on WorkPC: a vendor tool exported a
loopback proxy under a name like ``CODEBUDDY_SERVICE_PROXY_URL``, and because both implementations
recognised only the four standard proxy variables, that name was **forwarded into the provider
child**. The child's effective egress was therefore decided partly by an unrelated ambient variable
rather than by policy, which is the thing the policy exists to prevent.

The fix recognises a second shape -- a case-insensitive ``_PROXY_URL`` suffix -- in both packages,
independently, as §23/§70 require. The tests here pin the behaviour from both sides and, more
importantly, pin what the rule must *not* match: ``PROXY_PROTOCOL_VERSION`` and ``PROXY_MODE`` are
provider configuration, and a substring rule would silently delete them.

Non-vacuity is proven by driving the real functions with the exact name that leaked, and by showing
that the suffix rule is what removes it rather than the test's own arrangement.
"""

from __future__ import annotations

import pytest

from serverfs_agent_bridge.bootstrap import RuntimeProxy
from serverfs_agent_bridge.runtime_proxy import (
    build_runtime_environment,
    process_environment_is_clean,
)

#: The name actually observed on the host, plus the shapes the rule is meant to generalise to.
AMBIENT_PROXY_URL_NAMES = (
    "CODEBUDDY_SERVICE_PROXY_URL",
    "VENDOR_PROXY_URL",
    "MY_PROXY_URL",
    "vendor_proxy_url",
)

#: Names that merely mention proxying. These are provider configuration, not endpoints, and a
#: substring rule would delete them -- which is why the rule is a suffix.
PRESERVED_NON_ENDPOINT_NAMES = (
    "PROXY_PROTOCOL_VERSION",
    "PROXY_MODE",
    "CODEX_PROXY_SETTINGS",
)

POLICY_ENDPOINT = "http://proxy.invalid:8080"


def _runtime_proxy() -> RuntimeProxy:
    return RuntimeProxy(url=POLICY_ENDPOINT, no_proxy="")


class TestAmbientProxyUrlIsScrubbed:
    @pytest.mark.parametrize("name", AMBIENT_PROXY_URL_NAMES)
    def test_an_ambient_proxy_url_never_reaches_a_provider_child(self, name: str) -> None:
        base = {"PATH": "x", name: "http://127.0.0.1:1"}
        for use_proxy in (False, True):
            env = build_runtime_environment(
                base, runtime="codex", use_proxy=use_proxy, proxy=_runtime_proxy()
            )
            assert name not in env, f"{name} survived into the child (use_proxy={use_proxy})"

    @pytest.mark.parametrize("name", PRESERVED_NON_ENDPOINT_NAMES)
    def test_a_name_that_only_mentions_proxying_is_preserved(self, name: str) -> None:
        """The rule is a suffix, not a substring. A substring rule would delete these."""
        env = build_runtime_environment(
            {"PATH": "x", name: "value"},
            runtime="codex",
            use_proxy=True,
            proxy=_runtime_proxy(),
        )
        assert env.get(name) == "value"

    def test_use_proxy_false_strips_every_proxy_shape(self) -> None:
        """A genuinely proxy-free child: no ambient endpoint and no policy variable either."""
        env = build_runtime_environment(
            {"PATH": "x", "CODEBUDDY_SERVICE_PROXY_URL": "http://127.0.0.1:1"},
            runtime="codex",
            use_proxy=False,
            proxy=_runtime_proxy(),
        )
        assert env.get("CODEBUDDY_SERVICE_PROXY_URL") is None
        assert "HTTPS_PROXY" not in env
        assert "NO_PROXY" not in env

    def test_use_proxy_true_keeps_only_the_policy_variables(self) -> None:
        env = build_runtime_environment(
            {"PATH": "x", "VENDOR_PROXY_URL": "http://127.0.0.1:1"},
            runtime="codex",
            use_proxy=True,
            proxy=_runtime_proxy(),
        )
        assert env.get("VENDOR_PROXY_URL") is None
        assert env["HTTPS_PROXY"] == POLICY_ENDPOINT
        # The mandatory loopback bypass is merged by policy, so it is present and complete.
        entries = {e.strip() for e in env["NO_PROXY"].replace(";", ",").split(",") if e.strip()}
        assert {"127.0.0.1", "localhost", "::1"} <= entries
        # ALL_PROXY is measurably harmful when a provider sees it, so policy must not add it.
        assert "ALL_PROXY" not in env


class TestProcessEnvironmentIsClean:
    @pytest.mark.parametrize("name", AMBIENT_PROXY_URL_NAMES)
    def test_an_ambient_proxy_url_makes_the_bridge_process_not_clean(self, name: str) -> None:
        """A Bridge process carrying an ambient proxy endpoint is a misconfiguration.

        The Bridge environment must be clean because both provider SDKs copy it wholesale, so this
        is the assertion that would catch the leak at startup rather than at the child.
        """
        assert not process_environment_is_clean({name: "http://127.0.0.1:1"})

    @pytest.mark.parametrize("name", PRESERVED_NON_ENDPOINT_NAMES)
    def test_a_preserved_name_does_not_make_the_process_dirty(self, name: str) -> None:
        assert process_environment_is_clean({name: "value"})

    def test_a_clean_environment_is_still_clean(self) -> None:
        assert process_environment_is_clean({"PATH": "x", "HOME": "y"})


class TestNonVacuity:
    """The suite must fail if the rule is removed, or weakened to a substring match."""

    def test_removing_the_suffix_rule_reintroduces_the_leak(self, monkeypatch) -> None:
        import serverfs_agent_bridge.runtime_proxy as module

        # Both spellings the predicate consults, or the lower-case path still matches and the case
        # looks like it passed for the wrong reason.
        monkeypatch.setattr(module, "PROXY_URL_SUFFIX", "_NOTHING_MATCHES_THIS_")
        monkeypatch.setattr(module, "_PROXY_URL_SUFFIX_LOWER", "_nothing_matches_this_")
        env = build_runtime_environment(
            {"CODEBUDDY_SERVICE_PROXY_URL": "http://127.0.0.1:1"},
            runtime="codex",
            use_proxy=False,
            proxy=_runtime_proxy(),
        )
        # With the rule disabled the ambient name survives -- which is the defect, reproduced.
        assert env.get("CODEBUDDY_SERVICE_PROXY_URL") == "http://127.0.0.1:1"

    def test_widening_the_rule_to_a_substring_would_delete_provider_configuration(
        self,
    ) -> None:
        """Why the suffix: a substring rule breaks names the phase never measured."""
        import serverfs_agent_bridge.runtime_proxy as module

        def widened(name: str) -> bool:
            return "proxy" in name.lower()

        # The widened predicate does capture the leak ...
        assert widened("CODEBUDDY_SERVICE_PROXY_URL")
        # ... and also destroys configuration the suffix rule preserves.
        assert widened("PROXY_PROTOCOL_VERSION")
        assert module._is_proxy_variable("CODEBUDDY_SERVICE_PROXY_URL")
        assert not module._is_proxy_variable("PROXY_PROTOCOL_VERSION")
