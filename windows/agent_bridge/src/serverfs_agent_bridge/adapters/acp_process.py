"""Task-owned ACP subprocess lifecycle and bounded stop proof."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .acp_process_windows import WindowsAcpProcess


@dataclass
class AcpChild:
    process: asyncio.subprocess.Process
    _stop_lock: asyncio.Lock
    stopped: bool = False
    windows_job: WindowsAcpProcess | None = None


async def spawn_acp_child(command: str, args: tuple[str, ...], *, cwd, env) -> AcpChild:
    from .acp_process_windows import spawn_windows_acp_process

    process, job = await spawn_windows_acp_process(command, args, cwd=cwd, env=env)
    return AcpChild(process, asyncio.Lock(), windows_job=job)


async def stop_acp_child(child: AcpChild, timeout: float) -> bool:
    """Stop idempotently; require the child reaped and the native Job stopped."""
    async with child._stop_lock:
        if child.stopped:
            return True
        process = child.process
        writer = process.stdin
        if writer is not None and not writer.is_closing():
            writer.close()
        if process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout)
            except TimeoutError:
                pass
        if child.windows_job is None:
            return False
        tree_stopped = await child.windows_job.stop(timeout)
        if process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout)
            except TimeoutError:
                return False
        child.stopped = process.returncode is not None and tree_stopped
        return child.stopped
