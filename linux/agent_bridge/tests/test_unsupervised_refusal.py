"""The Windows supervised-only rule, pinned by decision rather than by launch.

Why the rule exists: on Windows the supervisor is what holds the per-user lifecycle ownership and
the Job Object, and those two facts are what let a *later* start prove that the previous
generation's provider tree was reaped. An unsupervised Bridge holds neither, and can still run
providers and create recovery guards. If it died leaving an orphan, the next supervisor would take
the lease, conclude containment, and clear a guard for a provider that is still alive -- a false
proof, produced by the very mechanism added to make the proof honest.

These cases pin the decision itself, so they run on every platform, including the Linux CI where the
Windows behaviour test in ``test_supervised_bridge.py`` is skipped. That test remains the one that
observes the real process refusing; this file is what keeps the *rule* from being silently dropped
on a host that cannot run it.
"""

from __future__ import annotations

import pytest

from serverfs_agent_bridge.main import unsupervised_refusal


class TestTheWindowsSupervisedOnlyRule:
    def test_an_unsupervised_windows_launch_is_refused(self) -> None:
        refusal = unsupervised_refusal(supervised=False, platform="win32")
        assert refusal is not None

    def test_a_supervised_windows_launch_is_allowed(self) -> None:
        """The supervisor's own argv is the supported shape, so it must not be refused."""
        assert unsupervised_refusal(supervised=True, platform="win32") is None

    @pytest.mark.parametrize("supervised", [True, False])
    def test_other_platforms_are_untouched(self, supervised: bool) -> None:
        """Linux keeps its deployment shape: the containment argument is Windows's."""
        for platform in ("linux", "darwin"):
            assert unsupervised_refusal(supervised=supervised, platform=platform) is None

    def test_the_refusal_is_redacted(self) -> None:
        """It reaches operator-visible stderr, so it names the failure class and nothing else."""
        message = unsupervised_refusal(supervised=False, platform="win32") or ""
        assert "\\" not in message
        assert "http" not in message
        assert ".exe" not in message
