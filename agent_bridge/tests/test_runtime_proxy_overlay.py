"""The deletion overlay: the same scrub, expressed for an SDK that inherits its environment.

The Qoder SDK builds its child's environment as ``{**os.environ}`` plus an overlay in which ``None``
deletes a name. That is the opposite shape from ``build_runtime_environment``, which returns a whole
environment for ``Popen(env=...)``, so the scrub could not simply be handed over unchanged.

``build_runtime_environment_overlay`` therefore sends a **diff** against that function's own result.
These tests pin three things, and the third is the one that keeps the policy from drifting:

1. Every name the rule must remove appears as a deletion.
2. Every name the rule must keep is untouched, including provider configuration that merely mentions
   proxying.
3. **Non-vacuity**: with the base environment already clean, the overlay is empty -- so a test that
   passes by producing an empty overlay for everything would be caught, and any later change that
   made the overlay stop tracking the real function would show up as a diff.
"""

from __future__ import annotations

import pytest

from serverfs_agent_bridge.runtime_proxy import (
    build_runtime_environment,
    build_runtime_environment_overlay,
)

#: Standard spellings, both cases, plus the ambient suffix shapes Phase E measured.
SCRUBBED_NAMES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "CODEBUDDY_SERVICE_PROXY_URL",
    "VENDOR_PROXY_URL",
    "vendor_proxy_url",
    "SERVERFS_AGENT_PROXY_URL",
    "SERVERFS_PROXY_TRACE",
)

#: Names that must survive: a child cannot run without the first three, and the rest are provider
#: configuration that a substring rule would silently delete.
PRESERVED_NAMES = (
    "PATH",
    "USERPROFILE",
    "LOCALAPPDATA",
    "SYSTEMROOT",
    "PROXY_PROTOCOL_VERSION",
    "PROXY_MODE",
    "CODEX_PROXY_SETTINGS",
    "QODER_HOME",
)

POLICY_ENDPOINT = "http://proxy.invalid:8080"


def _environment(*, scrubbed: bool, preserved: bool) -> dict[str, str]:
    env: dict[str, str] = {}
    if scrubbed:
        for index, name in enumerate(SCRUBBED_NAMES):
            env[name] = f"http://127.0.0.1:{index + 1}"
    if preserved:
        for index, name in enumerate(PRESERVED_NAMES):
            env[name] = f"value-{index}"
    return env


class TestOverlayRemovesWhatThePolicyRemoves:
    @pytest.mark.parametrize("name", SCRUBBED_NAMES)
    def test_a_scrubbed_name_is_a_deletion(self, name: str) -> None:
        overlay = build_runtime_environment_overlay({name: "http://127.0.0.1:1"}, runtime="qoder")
        assert overlay == {name: None}, f"{name} was not sent as a deletion"

    def test_nothing_is_ever_sent_as_a_value(self) -> None:
        """The overlay is deletion-only.

        A set value would be an *injection*, not a scrub: the SDK would add a variable the policy
        never asked for, and `use_proxy=false` would start configuring the child's network.
        """
        overlay = build_runtime_environment_overlay(
            _environment(scrubbed=True, preserved=True), runtime="qoder"
        )
        assert [name for name, value in overlay.items() if value is not None] == []


class TestOverlayPreservesWhatThePolicyPreserves:
    @pytest.mark.parametrize("name", PRESERVED_NAMES)
    def test_a_preserved_name_is_absent_from_the_overlay(self, name: str) -> None:
        overlay = build_runtime_environment_overlay(
            _environment(scrubbed=True, preserved=True), runtime="qoder"
        )
        assert name not in overlay, f"{name} would have been removed from the provider child"

    def test_the_sdk_still_inherits_its_own_configuration(self) -> None:
        """Only differences travel, so an unchanged variable needs no entry at all."""
        overlay = build_runtime_environment_overlay({"PATH": "x"}, runtime="qoder")
        assert overlay == {}


class TestOverlayNonVacuity:
    def test_a_clean_environment_produces_an_empty_overlay(self) -> None:
        """The control: an overlay that deleted everything would also "pass" a removal test.

        With nothing for the policy to remove there is nothing to send. If this ever becomes
        non-empty, the overlay has started inventing policy of its own.
        """
        assert build_runtime_environment_overlay({}, runtime="qoder") == {}

    def test_the_overlay_tracks_the_function_it_diffs_against(self) -> None:
        """Monkeypatch the function the overlay is defined in terms of.

        The overlay is only safe because it derives from `build_runtime_environment`. If it ever
        grew its own name list, this test would still pass while the two implementations drifted --
        which is exactly the failure Phase E had to fix once already. Breaking the underlying
        function must therefore change the overlay.
        """
        import serverfs_agent_bridge.runtime_proxy as module

        base = {"CODEBUDDY_SERVICE_PROXY_URL": "http://127.0.0.1:1"}
        assert build_runtime_environment_overlay(base, runtime="qoder") != {}

        original = module.build_runtime_environment
        try:
            module.build_runtime_environment = lambda *_a, **_k: dict(base)
            # The patched "scrub" keeps the name, so the overlay no longer needs to remove it.
            assert build_runtime_environment_overlay(base, runtime="qoder") == {}
        finally:
            module.build_runtime_environment = original

        assert build_runtime_environment_overlay(base, runtime="qoder") != {}


class TestOverlayAgreesWithTheEnvironmentItDiffs:
    def test_applying_the_overlay_reproduces_the_scrubbed_environment(self) -> None:
        """The overlay and the full environment must reach the same place.

        `build_runtime_environment` is the reference; applying the overlay the way the SDK does --
        inherit everything, then set or delete each key -- has to produce exactly its result.
        Without this, the two shapes could each be individually reasonable and still disagree.
        """
        base = _environment(scrubbed=True, preserved=True)
        expected = build_runtime_environment(base, runtime="qoder", use_proxy=False)

        applied = dict(base)
        for key, value in build_runtime_environment_overlay(base, runtime="qoder").items():
            if value is None:
                applied.pop(key, None)
            else:
                applied[key] = value

        assert applied == expected
