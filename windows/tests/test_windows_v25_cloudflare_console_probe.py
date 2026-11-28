"""Synthetic, portable tests for v25 console-window observation semantics.

PowerShell and headless conhost.exe processes may coexist with Windows
CREATE_NO_WINDOW. Neither proves a visible popup; diagnostics must not
cause a service rollback. No real Windows processes or state changes.
"""

from __future__ import annotations

import runpy
from dataclasses import dataclass
from pathlib import Path

import pytest


@dataclass(frozen=True)
class Process:
    pid: int
    parent_pid: int
    executable_name: str


@pytest.fixture(scope="module")
def methods():
    file = (
        Path(__file__).resolve().parents[1]
        / "deployment/windows/v0250-w3-cloudflare-windowless-console-probe.py"
    )
    ns = runpy.run_path(str(file))
    return ns["classify_processes"], ns["sample_console_hosts"]


def test_powershell_child_is_not_a_console_window(methods):
    classify, _ = methods
    result = classify(
        [Process(100, 42, "powershell.exe")],
        owner_pid=42,
    )
    assert result.powershell_pids == {100}
    assert not result.console_host_pids


def test_direct_console_host_under_powershell_is_detected(methods):
    classify, _ = methods
    result = classify(
        [
            Process(100, 42, "PowerShell.exe"),
            Process(110, 100, "conhost.exe"),
        ],
        owner_pid=42,
    )
    assert result.powershell_pids == {100}
    assert result.console_host_pids == {110}


def test_console_host_under_owner_is_detected(methods):
    classify, _ = methods
    result = classify(
        [Process(110, 42, "ConHost.Exe")],
        owner_pid=42,
    )
    assert result.console_host_pids == {110}


def test_global_unrelated_console_host_is_not_a_false_positive(methods):
    classify, _ = methods
    result = classify(
        [
            Process(100, 42, "powershell.exe"),
            Process(110, 101, "conhost.exe"),
            Process(111, 999, "conhost.exe"),
        ],
        owner_pid=42,
    )
    assert not result.console_host_pids


def test_child_console_host_survives_powershell_in_following_sample(methods):
    classify, _ = methods
    first = classify([Process(100, 42, "powershell.exe")], owner_pid=42)
    later = classify(
        [Process(111, 100, "conhost.exe")],
        owner_pid=42,
        observed_powershell=first.powershell_pids,
    )
    assert later.console_host_pids == {111}


def test_sampling_power_shell_without_conhost_succeeds(methods):
    _, sample = methods
    sequence = [
        [Process(100, 42, "powershell.exe")],
        [],
        [Process(101, 42, "powershell.exe")],
    ]
    clock = [0.0]

    def snapshots():
        return sequence.pop(0) if sequence else []

    def monotonic():
        return clock[0]

    def sleep(_duration):
        clock[0] += 1.0

    result = sample(
        owner_pid=42,
        snapshots=snapshots,
        monotonic=monotonic,
        sleep=sleep,
        duration_seconds=3,
        interval_seconds=1,
    )
    assert result.powershell_pids == {100, 101}
    assert result.console_host_pids == set()


def test_sampling_with_headless_conhost_does_not_abort_early(methods):
    _, sample = methods
    sequence = [
        [Process(100, 42, "powershell.exe")],
        [Process(110, 100, "conhost.exe")],
    ]
    clock = [0.0]

    def snapshots():
        return sequence.pop(0) if sequence else []

    def monotonic():
        return clock[0]

    def sleep(_duration):
        clock[0] += 1.0

    result = sample(
        owner_pid=42,
        snapshots=snapshots,
        monotonic=monotonic,
        sleep=sleep,
        duration_seconds=10,
        interval_seconds=1,
    )
    assert result.console_host_pids == {110}
    assert clock[0] == 10.0
