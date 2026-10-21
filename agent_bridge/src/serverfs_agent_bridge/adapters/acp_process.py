"""Task-owned ACP subprocess lifecycle and bounded stop proof."""

from __future__ import annotations

import asyncio
import errno
import os
import signal
from dataclasses import dataclass


@dataclass
class AcpChild:
    process: asyncio.subprocess.Process
    pgid: int | None
    _stop_lock: asyncio.Lock
    stopped: bool = False


async def spawn_acp_child(command: str, args: tuple[str, ...], *, cwd, env) -> AcpChild:
    process = await asyncio.create_subprocess_exec(
        command,
        *args,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=(os.name == "posix"),
    )
    return AcpChild(
        process=process,
        pgid=process.pid if os.name == "posix" else None,
        _stop_lock=asyncio.Lock(),
    )


async def stop_acp_child(child: AcpChild, timeout: float) -> bool:
    """Stop idempotently; POSIX success requires an absent group and a reaped child."""
    async with child._stop_lock:
        if child.stopped:
            return True
        process = child.process
        writer = process.stdin
        if writer is not None and not writer.is_closing():
            writer.close()
        if os.name == "posix" and child.pgid is not None:
            _signal_group(child.pgid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), timeout)
            except TimeoutError:
                pass
            if _group_exists(child.pgid):
                _signal_group(child.pgid, signal.SIGKILL)
            try:
                await asyncio.wait_for(process.wait(), timeout)
            except TimeoutError:
                return False
            # Give the kernel a short bounded interval to publish group disappearance.
            deadline = asyncio.get_running_loop().time() + timeout
            while _group_exists(child.pgid) and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(min(0.02, timeout))
            child.stopped = process.returncode is not None and not _group_exists(child.pgid)
            return child.stopped
        if os.name == "nt":
            if process.returncode is None:
                try:
                    await asyncio.wait_for(process.wait(), timeout)
                except TimeoutError:
                    pass
            from .acp_process_windows import stop_windows_process_tree

            tree_stopped = await asyncio.to_thread(
                stop_windows_process_tree,
                process.pid,
                timeout,
            )
            if process.returncode is None:
                try:
                    await asyncio.wait_for(process.wait(), timeout)
                except TimeoutError:
                    return False
            child.stopped = process.returncode is not None and tree_stopped
            return child.stopped
        if process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout)
            except TimeoutError:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout)
                except TimeoutError:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    try:
                        await asyncio.wait_for(process.wait(), timeout)
                    except TimeoutError:
                        return False
        child.stopped = process.returncode is not None
        return child.stopped


def _signal_group(pgid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        return True
    return True
