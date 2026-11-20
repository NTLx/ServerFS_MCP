"""Pure tests for bounded Windows Cloudflare Task Scheduler quiescence.

No native Windows operations, files outside the synthetic test checkout,
service restarts, network calls or real sleeps.
"""

from __future__ import annotations

import runpy
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def verify_quiescent():
    script = (
        Path(__file__).resolve().parents[1]
        / "deployment/windows/v0250-w3-cloudflare-windowless-quiescence.py"
    )
    return runpy.run_path(str(script))["verify_quiescent"]


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeScheduler:
    def __init__(self, state="Disabled", enabled=False, running=False):
        self.state = state
        self.enabled = enabled
        self.running = running
        self.calls = 0

    def status(self, manifest):
        self.calls += 1
        return {
            "registered": True,
            "state": self.state,
            "enabled": self.enabled,
            "running": self.running,
        }


def test_immediate_stopped_kernel_generation_is_accepted(verify_quiescent):
    clock, scheduler = FakeClock(), FakeScheduler()
    assert verify_quiescent(
        "cloudflare",
        scheduler=scheduler,
        identity=lambda _manifest: None,
        pid_present=lambda: False,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    assert scheduler.calls == 1


def test_task_disabled_before_kernel_process_exits_eventually_succeeds(verify_quiescent):
    clock, scheduler = FakeClock(), FakeScheduler()
    observations = [object(), object(), None]

    def identity(_manifest):
        return observations.pop(0)

    assert verify_quiescent(
        "cloudflare",
        scheduler=scheduler,
        identity=identity,
        pid_present=lambda: False,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    assert scheduler.calls == 3
    assert clock.now > 0


def test_still_alive_kernel_generation_exceeds_timeout_without_success(verify_quiescent):
    clock, scheduler = FakeClock(), FakeScheduler()
    assert not verify_quiescent(
        "cloudflare",
        scheduler=scheduler,
        identity=lambda _manifest: object(),
        pid_present=lambda: False,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        timeout_seconds=0.5,
    )
    assert clock.now >= 0.5


@pytest.mark.parametrize(
    ("state", "enabled", "running"),
    [
        ("Running", True, True),
        ("Ready", True, False),
        ("Disabled", True, False),
        ("Disabled", False, True),
        ("Queued", False, True),
    ],
)
def test_any_nonquiescent_scheduler_contract_is_refused(
    verify_quiescent,
    state,
    enabled,
    running,
):
    clock, scheduler = FakeClock(), FakeScheduler(state, enabled, running)
    assert not verify_quiescent(
        "cloudflare",
        scheduler=scheduler,
        identity=lambda _manifest: None,
        pid_present=lambda: False,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    assert clock.now == 0


def test_missing_readiness_stamp_does_not_prove_kernel_pid_exit(verify_quiescent):
    clock, scheduler = FakeClock(), FakeScheduler()
    assert not verify_quiescent(
        "cloudflare",
        scheduler=scheduler,
        identity=lambda _manifest: None,
        pid_present=lambda: True,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        timeout_seconds=0.5,
    )
    assert clock.now >= 0.5


def test_scheduler_drift_during_wait_is_refused(verify_quiescent):
    clock, scheduler = FakeClock(), FakeScheduler()

    def identity(_manifest):
        scheduler.enabled = True
        return object()

    assert not verify_quiescent(
        "cloudflare",
        scheduler=scheduler,
        identity=identity,
        pid_present=lambda: False,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    assert scheduler.calls == 2
