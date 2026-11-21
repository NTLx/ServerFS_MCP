"""v0.25 Windows W2d: IPC readiness must belong to the held owner Job.

No real Named Pipe, Agent, workdir or production Job is touched. Native Job
membership is also checked by the opt-in anonymous-Job W2 fixture.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from serverfs_mcp.agent_client import AgentBridgeClient, AgentBridgeUnavailable
from serverfs_mcp.agent_lifecycle import wait_for_bridge_readiness

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows pipe generation")

SID = "S-1-5-21-101-202-303-1001"
PIPE = Path(r"\\.\pipe\serverfs-w2d-fake")


class _Pipe:
    def __init__(self, *, pid: int = 200, sid: str = SID) -> None:
        self.pid = pid
        self.sid = sid
        self.sent: bytes | None = None
        self.closed = False

    def server_identity(self) -> SimpleNamespace:
        return SimpleNamespace(process_id=self.pid, user_sid=self.sid)

    def write_all(self, message: bytes, *, deadline: float) -> None:
        self.sent = message

    def read_line(self, *, limit: int, deadline: float) -> bytes:
        assert self.sent is not None
        request = json.loads(self.sent)
        return (
            json.dumps(
                {
                    "request_id": request["request_id"],
                    "ok": True,
                    "result": {"runtimes": []},
                }
            ).encode("utf-8")
            + b"\n"
        )

    def close(self) -> None:
        self.closed = True


def _inject(monkeypatch: pytest.MonkeyPatch, pipe: _Pipe) -> None:
    from serverfs_mcp import windows_agent_pipe as transport

    monkeypatch.setattr(transport, "current_user_sid", lambda: SID)
    monkeypatch.setattr(
        transport.BridgePipe,
        "open",
        classmethod(lambda cls, _name, *, deadline: pipe),
    )


def test_new_owner_rejects_legacy_same_sid_pipe_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_pipe = _Pipe(pid=200)
    _inject(monkeypatch, legacy_pipe)
    observed: list[int] = []

    def contains_pid(pid: int) -> bool:
        observed.append(pid)
        return pid == 333

    with pytest.raises(AgentBridgeUnavailable, match="different owner generation"):
        asyncio.run(
            AgentBridgeClient(PIPE, timeout_seconds=0.5, server_pid_validator=contains_pid).call(
                "runtime.list", {}
            )
        )
    assert observed == [200]
    assert legacy_pipe.sent is None
    assert legacy_pipe.closed


def test_same_sid_and_held_job_member_returns_authenticated_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = _Pipe(pid=333)
    _inject(monkeypatch, current)
    observed: list[int] = []

    def contains_pid(pid: int) -> bool:
        observed.append(pid)
        return pid == 333

    result = asyncio.run(
        AgentBridgeClient(PIPE, timeout_seconds=0.5, server_pid_validator=contains_pid).call(
            "runtime.list", {}
        )
    )
    assert result == {"runtimes": []}
    assert observed == [333]
    assert json.loads(current.sent or b"{}")["method"] == "runtime.list"
    assert current.closed


def test_wrong_sid_refuses_even_if_pid_belongs_to_owner_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wrong = _Pipe(pid=333, sid="S-1-5-18")
    _inject(monkeypatch, wrong)
    called = []

    def contains_pid(pid: int) -> bool:
        called.append(pid)
        return True

    with pytest.raises(AgentBridgeUnavailable, match="another identity"):
        asyncio.run(
            AgentBridgeClient(PIPE, timeout_seconds=0.5, server_pid_validator=contains_pid).call(
                "runtime.list", {}
            )
        )
    assert called == []
    assert wrong.sent is None
    assert wrong.closed


def test_job_membership_error_is_redacted_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = _Pipe(pid=333)
    _inject(monkeypatch, current)

    def inaccessible(pid: int) -> bool:
        raise OSError("private-job-handle-marker")

    with pytest.raises(AgentBridgeUnavailable, match="could not be verified") as exc:
        asyncio.run(
            AgentBridgeClient(PIPE, timeout_seconds=0.5, server_pid_validator=inaccessible).call(
                "runtime.list", {}
            )
        )
    assert "private-job-handle-marker" not in str(exc.value)
    assert current.sent is None
    assert current.closed


def test_unbound_legacy_client_still_accepts_same_sid_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = _Pipe(pid=200)
    _inject(monkeypatch, legacy)
    assert asyncio.run(AgentBridgeClient(PIPE, timeout_seconds=0.5).call("runtime.list", {})) == {
        "runtimes": []
    }
    assert legacy.sent is not None


def test_owner_readiness_threads_job_validator_into_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = _Pipe(pid=333)
    _inject(monkeypatch, current)

    class Alive:
        pid = 111

        def poll(self) -> None:
            return None

    async def scenario() -> None:
        await wait_for_bridge_readiness(
            PIPE,
            Alive(),  # type: ignore[arg-type]
            timeout=0.5,
            server_pid_validator=lambda pid: pid == 333,
        )

    asyncio.run(scenario())
    assert current.sent is not None
    assert current.closed
