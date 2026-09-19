from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from serverfs_agent_bridge.adapters import FakeAdapter
from serverfs_agent_bridge.errors import BridgeError
from serverfs_agent_bridge.leases import LeaseManager
from serverfs_agent_bridge.models import AgentMode
from serverfs_agent_bridge.policy import PolicyRegistry, WorkdirAgentPolicy
from serverfs_agent_bridge.protocol import MAX_REQUEST_BYTES, BridgeProtocolServer
from serverfs_agent_bridge.service import BridgeService
from serverfs_agent_bridge.store import TaskStore


def make_protocol_service(tmp_path: Path) -> BridgeService:
    repo = tmp_path / "repo"
    repo.mkdir()
    return BridgeService(
        store=TaskStore(tmp_path / "state"),
        policies=PolicyRegistry(
            [
                WorkdirAgentPolicy(
                    slot=1,
                    alias="repo",
                    host_path=repo,
                    mode=AgentMode.WORKSPACE_WRITE,
                    runtimes=frozenset({"fake"}),
                    read_only=False,
                )
            ]
        ),
        adapters={"fake": FakeAdapter()},
        lease_manager=LeaseManager(tmp_path / "locks"),
    )


async def rpc(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    request_id: str,
    method: str,
    params: dict,
) -> dict:
    writer.write(
        json.dumps(
            {
                "protocol_version": 1,
                "request_id": request_id,
                "method": method,
                "params": params,
            }
        ).encode()
        + b"\n"
    )
    await writer.drain()
    return json.loads(await reader.readline())


async def wait_for_rpc_status(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    task_id: str,
    *statuses: str,
) -> dict:
    for index in range(500):
        response = await rpc(
            reader, writer, f"get_{task_id}_{index}", "task.get", {"task_id": task_id}
        )
        assert response["ok"] is True
        result = response["result"]
        if result["status"] in statuses:
            return result
        await asyncio.sleep(0.01)
    raise AssertionError(f"task did not reach {statuses}")


@pytest.mark.asyncio
async def test_runtime_list_over_unix_socket(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    service = BridgeService(
        store=TaskStore(tmp_path / "state"),
        policies=PolicyRegistry(
            [
                WorkdirAgentPolicy(
                    slot=1,
                    alias="repo",
                    host_path=repo,
                    mode=AgentMode.REVIEW,
                    runtimes=frozenset({"fake"}),
                    read_only=True,
                )
            ]
        ),
        adapters={"fake": FakeAdapter()},
        lease_manager=LeaseManager(tmp_path / "locks"),
    )
    await service.start()

    socket_path = tmp_path / "run" / "bridge.sock"
    server = BridgeProtocolServer(service=service, socket_path=socket_path)
    await server.start()
    try:
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        request = {
            "protocol_version": 1,
            "request_id": "rpc_1",
            "method": "runtime.list",
            "params": {},
        }
        writer.write(json.dumps(request).encode() + b"\n")
        await writer.drain()
        response = json.loads(await reader.readline())
        assert response["request_id"] == "rpc_1"
        assert response["ok"] is True
        assert response["result"]["runtimes"][0]["name"] == "fake"
        bad = {
            "protocol_version": 1,
            "request_id": "rpc_2",
            "method": "runtime.list",
            "params": {},
            "unexpected": True,
        }
        writer.write(json.dumps(bad).encode() + b"\n")
        await writer.drain()
        invalid = json.loads(await reader.readline())
        assert invalid["ok"] is False
        assert invalid["request_id"] == "rpc_2"
        assert invalid["error"]["code"] == "INVALID_REQUEST"

        writer.close()
        await writer.wait_closed()
    finally:
        await server.close()
        await service.close()


@pytest.mark.asyncio
async def test_task_lifecycle_and_fake_interactions_over_uds(tmp_path: Path) -> None:
    service = make_protocol_service(tmp_path)
    await service.start()
    server = BridgeProtocolServer(
        service=service,
        socket_path=tmp_path / "run" / "bridge.sock",
        allowed_peer_uid=os.getuid(),
        allowed_peer_gid=os.getgid(),
    )
    await server.start()
    reader, writer = await asyncio.open_unix_connection(str(server.socket_path))
    try:
        complete = await rpc(
            reader,
            writer,
            "submit_complete",
            "task.submit",
            {
                "runtime": "fake",
                "workdir": "repo",
                "path": "",
                "profile": "review",
                "prompt": "complete:first",
            },
        )
        complete_id = complete["result"]["task_id"]
        complete_task = await wait_for_rpc_status(reader, writer, complete_id, "succeeded")
        assert complete_task["final_response"] == "first"
        assert "native_session_id" not in complete_task

        events = await rpc(
            reader,
            writer,
            "events_complete",
            "task.events",
            {"task_id": complete_id, "limit": 20},
        )
        assert any(event["event_type"] == "task.completed" for event in events["result"]["events"])

        approval = await rpc(
            reader,
            writer,
            "submit_approval",
            "task.submit",
            {
                "runtime": "fake",
                "workdir": "repo",
                "path": "",
                "profile": "workspace-write",
                "prompt": "approval:check",
            },
        )
        approval_id = approval["result"]["task_id"]
        approval_task = await wait_for_rpc_status(
            reader, writer, approval_id, "waiting_for_approval"
        )
        approval_request = approval_task["pending_request"]["request_id"]
        resolved = await rpc(
            reader,
            writer,
            "resolve_approval",
            "task.approval.respond",
            {
                "task_id": approval_id,
                "request_id": approval_request,
                "decision": "approve_once",
            },
        )
        assert resolved["result"]["resolved"] is True
        await wait_for_rpc_status(reader, writer, approval_id, "succeeded")

        question = await rpc(
            reader,
            writer,
            "submit_question",
            "task.submit",
            {
                "runtime": "fake",
                "workdir": "repo",
                "path": "",
                "profile": "review",
                "prompt": "question:pick",
            },
        )
        question_id = question["result"]["task_id"]
        question_task = await wait_for_rpc_status(
            reader, writer, question_id, "waiting_for_question"
        )
        question_request = question_task["pending_request"]["request_id"]
        answered = await rpc(
            reader,
            writer,
            "answer_question",
            "task.question.answer",
            {
                "task_id": question_id,
                "request_id": question_request,
                "answers": [{"question_id": "q1", "selected_option_ids": ["a"]}],
            },
        )
        assert answered["result"]["resolved"] is True
        await wait_for_rpc_status(reader, writer, question_id, "succeeded")

        steer = await rpc(
            reader,
            writer,
            "submit_steer",
            "task.submit",
            {
                "runtime": "fake",
                "workdir": "repo",
                "path": "",
                "profile": "review",
                "prompt": "steer:",
            },
        )
        steer_id = steer["result"]["task_id"]
        await wait_for_rpc_status(reader, writer, steer_id, "running")
        steered = await rpc(
            reader,
            writer,
            "send_steer",
            "task.message.send",
            {"task_id": steer_id, "message": "focus on tests"},
        )
        assert steered["result"]["accepted"] is True
        steer_result = await wait_for_rpc_status(reader, writer, steer_id, "succeeded")
        assert steer_result["final_response"] == "steered=focus on tests"

        waiting = await rpc(
            reader,
            writer,
            "submit_wait",
            "task.submit",
            {
                "runtime": "fake",
                "workdir": "repo",
                "path": "",
                "profile": "review",
                "prompt": "wait:",
            },
        )
        waiting_id = waiting["result"]["task_id"]
        await wait_for_rpc_status(reader, writer, waiting_id, "running")
        cancelled = await rpc(
            reader,
            writer,
            "cancel_wait",
            "task.cancel",
            {"task_id": waiting_id},
        )
        assert cancelled["result"]["status"] == "cancelled"
        await wait_for_rpc_status(reader, writer, waiting_id, "cancelled")

        continued = await rpc(
            reader,
            writer,
            "submit_continuation",
            "task.submit",
            {
                "runtime": "fake",
                "workdir": "repo",
                "path": "",
                "profile": "review",
                "prompt": "complete:second",
                "continue_from_task_id": complete_id,
            },
        )
        await wait_for_rpc_status(reader, writer, continued["result"]["task_id"], "succeeded")
    finally:
        writer.close()
        await writer.wait_closed()
        await server.close()
        await service.close()


@pytest.mark.asyncio
async def test_uds_peer_credentials_and_socket_path_safety(tmp_path: Path) -> None:
    service = make_protocol_service(tmp_path)
    await service.start()
    parent = tmp_path / "existing"
    parent.mkdir()
    os.chmod(parent, 0o755)
    socket_path = parent / "bridge.sock"
    server = BridgeProtocolServer(
        service=service,
        socket_path=socket_path,
        allowed_peer_uid=os.getuid(),
        allowed_peer_gid=os.getgid(),
    )
    await server.start()
    assert parent.stat().st_mode & 0o777 == 0o755
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    response = await rpc(reader, writer, "peer_ok", "runtime.list", {})
    assert response["ok"] is True
    writer.close()
    await writer.wait_closed()
    await server.close()

    wrong_uid_server = BridgeProtocolServer(
        service=service,
        socket_path=socket_path,
        allowed_peer_uid=os.getuid() + 1,
    )
    await wrong_uid_server.start()
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    writer.write(
        json.dumps(
            {
                "protocol_version": 1,
                "request_id": "wrong_uid",
                "method": "runtime.list",
                "params": {},
            }
        ).encode()
        + b"\n"
    )
    await writer.drain()
    rejected = json.loads(await reader.readline())
    assert rejected["error"]["code"] == "PEER_NOT_AUTHORIZED"
    writer.close()
    await writer.wait_closed()
    await wrong_uid_server.close()

    wrong_gid_server = BridgeProtocolServer(
        service=service,
        socket_path=socket_path,
        allowed_peer_gid=os.getgid() + 1,
    )
    await wrong_gid_server.start()
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    writer.write(
        json.dumps(
            {
                "protocol_version": 1,
                "request_id": "wrong_gid",
                "method": "runtime.list",
                "params": {},
            }
        ).encode()
        + b"\n"
    )
    await writer.drain()
    rejected = json.loads(await reader.readline())
    assert rejected["error"]["code"] == "PEER_NOT_AUTHORIZED"
    writer.close()
    await writer.wait_closed()
    await wrong_gid_server.close()

    malformed_server = BridgeProtocolServer(service=service, socket_path=socket_path)
    await malformed_server.start()
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    writer.write(b"not-json\n")
    await writer.drain()
    malformed = json.loads(await reader.readline())
    assert malformed["error"]["code"] == "INVALID_REQUEST"
    valid = await rpc(reader, writer, "after_bad", "runtime.list", {})
    assert valid["ok"] is True
    writer.write(b"x" * (MAX_REQUEST_BYTES + 1) + b"\n")
    await writer.drain()
    oversized = json.loads(await reader.readline())
    assert oversized["error"]["code"] == "REQUEST_TOO_LARGE"
    writer.close()
    await writer.wait_closed()
    await malformed_server.close()

    occupied = parent / "occupied.sock"
    occupied.write_text("not a socket", encoding="utf-8")
    occupied_server = BridgeProtocolServer(service=service, socket_path=occupied)
    with pytest.raises(BridgeError) as exc:
        await occupied_server.start()
    assert exc.value.code == "SOCKET_PATH_IN_USE"
    assert occupied.read_text(encoding="utf-8") == "not a socket"
    await service.close()
