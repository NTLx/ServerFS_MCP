"""Client-side ACP stdio contract against the deterministic fixture Agent."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import acp
import pytest
from acp.schema import (
    AllowedOutcome,
    ClientCapabilities,
    FileSystemCapabilities,
    Implementation,
    RequestPermissionResponse,
)

FIXTURE = Path(__file__).parent / "fixtures" / "acp_stdio_agent.py"
TIMEOUT = 3


class FixtureClient:
    def __init__(self) -> None:
        self.chunks: list[str] = []
        self.permission_option_ids: list[str] = []

    async def session_update(self, session_id, update, **kwargs) -> None:
        if update.session_update == "agent_message_chunk":
            self.chunks.append(update.content.text)

    async def request_permission(self, session_id, tool_call, options, **kwargs):
        self.permission_option_ids.extend(option.option_id for option in options)
        return RequestPermissionResponse(
            outcome=AllowedOutcome(option_id="allow-exact", outcome="selected")
        )


def _client_capabilities() -> ClientCapabilities:
    return ClientCapabilities(
        fs=FileSystemCapabilities(read_text_file=False, write_text_file=False),
        terminal=False,
        session=None,
    )


async def _start(mode: str, cwd: Path, client: FixtureClient):
    stream_context = acp.spawn_stdio_transport(
        sys.executable,
        str(FIXTURE),
        mode,
        cwd=cwd,
        shutdown_timeout=0.5,
    )
    reader, writer, process = await stream_context.__aenter__()
    connection = acp.connect_to_agent(client, writer, reader)
    return stream_context, connection, process


@pytest.mark.asyncio
async def test_initialize_new_session_prompt_and_stderr_isolation(tmp_path: Path) -> None:
    cwd = (tmp_path / "worktree").resolve()
    cwd.mkdir()
    client = FixtureClient()
    context, connection, process = await _start("normal", cwd, client)
    try:
        initialized = await asyncio.wait_for(
            connection.initialize(
                protocol_version=acp.PROTOCOL_VERSION,
                client_capabilities=_client_capabilities(),
                client_info=Implementation(name="ServerFS test", version="0"),
            ),
            TIMEOUT,
        )
        assert initialized.protocol_version == acp.PROTOCOL_VERSION
        session = await asyncio.wait_for(
            connection.new_session(cwd=str(cwd), mcp_servers=[]), TIMEOUT
        )
        assert session.session_id == "fixture-session"
        result = await asyncio.wait_for(
            connection.prompt(session.session_id, [acp.text_block("hello")]), TIMEOUT
        )
        assert result.stop_reason == "end_turn"
        assert client.chunks == ["fixture response"]
        assert process.stderr is not None
        stderr = await asyncio.wait_for(process.stderr.readline(), TIMEOUT)
        record = json.loads(stderr)
        assert record == {"cwd": str(cwd), "mcp_servers": []}
    finally:
        await asyncio.wait_for(context.__aexit__(None, None, None), TIMEOUT)


@pytest.mark.asyncio
async def test_permission_round_trip_preserves_exact_option_id(tmp_path: Path) -> None:
    client = FixtureClient()
    context, connection, process = await _start("permission", tmp_path, client)
    try:
        await asyncio.wait_for(connection.initialize(acp.PROTOCOL_VERSION), TIMEOUT)
        session = await asyncio.wait_for(
            connection.new_session(cwd=str(tmp_path.resolve()), mcp_servers=[]), TIMEOUT
        )
        result = await asyncio.wait_for(
            connection.prompt(session.session_id, [acp.text_block("permission")]), TIMEOUT
        )
        assert client.permission_option_ids == ["allow-exact"]
        assert result.stop_reason == "end_turn"
        assert client.chunks == ["fixture response"]
    finally:
        await asyncio.wait_for(context.__aexit__(None, None, None), TIMEOUT)


@pytest.mark.asyncio
async def test_cancel_unblocks_prompt_and_child_is_cleaned_up(tmp_path: Path) -> None:
    client = FixtureClient()
    context, connection, process = await _start("blocked", tmp_path, client)
    try:
        await asyncio.wait_for(connection.initialize(acp.PROTOCOL_VERSION), TIMEOUT)
        session = await asyncio.wait_for(
            connection.new_session(cwd=str(tmp_path.resolve()), mcp_servers=[]), TIMEOUT
        )
        assert process.stderr is not None
        await asyncio.wait_for(process.stderr.readline(), TIMEOUT)
        prompt = asyncio.create_task(
            connection.prompt(session.session_id, [acp.text_block("wait")])
        )
        blocked_line = await asyncio.wait_for(process.stderr.readline(), TIMEOUT)
        assert blocked_line in {b"prompt-blocked\n", b"prompt-blocked\r\n"}
        await asyncio.wait_for(connection.cancel(session.session_id), TIMEOUT)
        result = await asyncio.wait_for(prompt, TIMEOUT)
        assert result.stop_reason == "cancelled"
    finally:
        await asyncio.wait_for(context.__aexit__(None, None, None), TIMEOUT)
    assert process.returncode is not None


@pytest.mark.asyncio
async def test_abrupt_child_exit_is_protocol_disconnect_and_cleanup_is_bounded(
    tmp_path: Path,
) -> None:
    client = FixtureClient()
    context, connection, process = await _start("exit", tmp_path, client)
    try:
        with pytest.raises(ConnectionError, match="Connection closed"):
            await asyncio.wait_for(connection.initialize(acp.PROTOCOL_VERSION), TIMEOUT)
        assert await asyncio.wait_for(process.wait(), TIMEOUT) == 23
    finally:
        await asyncio.wait_for(context.__aexit__(None, None, None), TIMEOUT)


@pytest.mark.asyncio
async def test_protocol_version_mismatch_is_returned_by_pinned_v1_sdk(tmp_path: Path) -> None:
    client = FixtureClient()
    context, connection, process = await _start("protocol-mismatch", tmp_path, client)
    try:
        initialized = await asyncio.wait_for(connection.initialize(acp.PROTOCOL_VERSION), TIMEOUT)
        # SDK 0.12.1 transports the v1 response; it does not negotiate/reject it itself.
        assert initialized.protocol_version == acp.PROTOCOL_VERSION + 1
    finally:
        await asyncio.wait_for(context.__aexit__(None, None, None), TIMEOUT)
    assert process.returncode is not None


@pytest.mark.asyncio
async def test_protocol_version_refusal_is_an_rpc_error(tmp_path: Path) -> None:
    client = FixtureClient()
    context, connection, process = await _start("protocol-refusal", tmp_path, client)
    try:
        with pytest.raises(Exception, match="Unsupported protocol version"):
            await asyncio.wait_for(connection.initialize(acp.PROTOCOL_VERSION), TIMEOUT)
    finally:
        await asyncio.wait_for(context.__aexit__(None, None, None), TIMEOUT)
    assert process.returncode is not None


@pytest.mark.asyncio
async def test_stdout_contamination_fails_protocol_instead_of_becoming_assistant_text(
    tmp_path: Path,
) -> None:
    client = FixtureClient()
    context, connection, process = await _start("malformed-initialize", tmp_path, client)
    try:
        # The pinned SDK logs and discards malformed JSON, leaving the request unanswered.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(connection.initialize(acp.PROTOCOL_VERSION), 0.25)
        assert client.chunks == []
    finally:
        await asyncio.wait_for(context.__aexit__(None, None, None), TIMEOUT)
    assert process.returncode is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode, operation", [("hang-initialize", "initialize"), ("hang-prompt", "prompt")]
)
async def test_unresponsive_peer_times_out_and_is_cleaned_up(
    tmp_path: Path, mode: str, operation: str
) -> None:
    client = FixtureClient()
    context, connection, process = await _start(mode, tmp_path, client)
    try:
        if operation == "initialize":
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(connection.initialize(acp.PROTOCOL_VERSION), 0.25)
        else:
            await asyncio.wait_for(connection.initialize(acp.PROTOCOL_VERSION), TIMEOUT)
            session = await asyncio.wait_for(
                connection.new_session(cwd=str(tmp_path.resolve()), mcp_servers=[]), TIMEOUT
            )
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    connection.prompt(session.session_id, [acp.text_block("hello")]), 0.25
                )
    finally:
        await asyncio.wait_for(context.__aexit__(None, None, None), TIMEOUT)
    assert process.returncode is not None


def test_bridge_core_import_does_not_import_acp_sdk() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import serverfs_agent_bridge.main; "
            "assert not any(name == 'acp' or name.startswith('acp.') for name in sys.modules)",
        ],
        cwd=Path(__file__).parents[1],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        check=False,
    )
    assert result.returncode == 0, result.stderr
