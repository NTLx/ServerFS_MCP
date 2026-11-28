"""The native doctor observes socket existence, not peer authorization."""

from __future__ import annotations

from serverfs_mcp import agent_doctor, native_endpoint


class Report:
    def __init__(self):
        self.lines = []

    def status(self, name, status, message):
        self.lines.append((name, status, message))

    def note(self, name, message):
        self.lines.append((name, message))


def test_socket_observation_does_not_claim_health(monkeypatch):
    monkeypatch.setattr(native_endpoint, "derive_endpoint", lambda: "/synthetic/bridge.sock")
    monkeypatch.setattr(agent_doctor, "_socket_is_served", lambda endpoint: True)
    report = Report()
    agent_doctor._probe_endpoint(report, {})
    assert ("agent endpoint", agent_doctor.OK, "derived from the per-user runtime directory") in (
        report.lines
    )
    message = report.lines[-1][1]
    assert "socket present" in message
    assert "identity and health are not established" in message
    assert "synthetic" not in repr(report.lines)


def test_missing_socket_does_not_start_bridge(monkeypatch):
    monkeypatch.setattr(native_endpoint, "derive_endpoint", lambda: "/synthetic/bridge.sock")
    monkeypatch.setattr(agent_doctor, "_socket_is_served", lambda endpoint: False)
    report = Report()
    agent_doctor._probe_endpoint(report, {})
    assert report.lines[-1] == (
        "agent bridge",
        "not running (start it with: serverfs agent-bridge start)",
    )
