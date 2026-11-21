"""Windows-native ACP process containment using the Bridge's existing command Job."""

from __future__ import annotations

import asyncio
import subprocess

from ..errors import BridgeError
from ..repo_ops import RepoCleanupUnproven
from ..repo_ops_windows import WindowsCommandJob

_CREATE_SUSPENDED = 0x00000004
_STARTUP_CLEANUP_TIMEOUT = 5.0


class WindowsAcpProcess:
    """Own one anonymous kill-on-close Job and its exact suspended leader handle."""

    def __init__(self, job: WindowsCommandJob, process: subprocess.Popen[bytes]) -> None:
        self._job = job
        self._process = process
        self._stop_task: asyncio.Task[bool] | None = None

    def _stop(self, timeout: float) -> bool:
        try:
            self._job.stop(self._process, timeout=timeout)
            return True
        except (RepoCleanupUnproven, OSError):
            return False
        finally:
            self._job.close()

    async def stop(self, timeout: float) -> bool:
        # The caller can be cancelled while the native proof is in a thread.
        # Keep one shared proof task and never re-use a closed Job handle.
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(asyncio.to_thread(self._stop, timeout))
        return await asyncio.shield(self._stop_task)


async def spawn_windows_acp_process(
    command: str, args: tuple[str, ...], *, cwd, env
) -> tuple[asyncio.subprocess.Process, WindowsAcpProcess]:
    job = WindowsCommandJob()
    child = None
    process = None
    try:
        child = await asyncio.create_subprocess_exec(
            command,
            *args,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=_CREATE_SUSPENDED | subprocess.CREATE_NO_WINDOW,
        )
        # CPython's Windows asyncio transport retains the original Popen handle.
        # Fail closed instead of reopening a PID or allowing uncontained execution.
        process = child._transport.get_extra_info("subprocess")
        if not isinstance(process, subprocess.Popen):
            process = None
            raise OSError("ACP suspended process handle unavailable")
        job.assign(process)
        job.resume(process)
        return child, WindowsAcpProcess(job, process)
    except BaseException:
        try:
            if process is not None:
                # Synchronous cleanup here is deliberate: no cancellation can
                # interrupt the proof after a partial assignment/resume.
                job.stop(process, timeout=_STARTUP_CLEANUP_TIMEOUT)
            elif child is not None:
                # No resume was attempted; the exact suspended leader has no children.
                if child.returncode is None:
                    try:
                        child.kill()
                    except ProcessLookupError:
                        pass
                await asyncio.wait_for(child.wait(), _STARTUP_CLEANUP_TIMEOUT)
        except (RepoCleanupUnproven, OSError, TimeoutError) as exc:
            raise BridgeError(
                "AGENT_PROVIDER_STOP_UNKNOWN", "ACP startup cleanup could not be proven"
            ) from exc
        finally:
            job.close()
        raise
