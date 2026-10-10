"""Bridge-owned standalone Codex App Server for Linux proxy mode.

The normal Linux path keeps using the user's managed Codex daemon.  That path cannot prove which
proxy environment an already-running shared daemon inherited, so v0.12 uses this lifecycle only when
``CodexSettings.use_proxy`` is true.  One standalone app-server is shared by the Bridge: Codex
0.162.0 was measured accepting two simultaneous initialized Unix-WebSocket connections.
"""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

from ..bootstrap import RuntimeProxy
from ..config import CodexSettings
from ..errors import BridgeError
from ..runtime_proxy import build_runtime_environment
from .codex_transport import UnixSocketEndpoint

_START_TIMEOUT_SECONDS = 30.0
_STOP_TIMEOUT_SECONDS = 10.0


class LinuxCodexAppServer:
    """Own one proxy-scoped standalone Codex app-server child."""

    def __init__(
        self,
        settings: CodexSettings,
        *,
        state_dir: Path,
        runtime_proxy: RuntimeProxy | None,
    ) -> None:
        self.settings = settings
        self.state_dir = state_dir
        self.runtime_proxy = runtime_proxy
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._runtime_dir = state_dir / "codex-proxy-runtime"
        self._socket_path = self._runtime_dir / "app-server.sock"

    async def ensure_started(self) -> UnixSocketEndpoint:
        async with self._lock:
            process = self._process
            if process is not None and process.returncode is None and self._socket_is_ready():
                return UnixSocketEndpoint(self._socket_path)
            if process is not None:
                await self._stop_locked()
            await self._start_locked()
            return UnixSocketEndpoint(self._socket_path)

    async def close(self) -> None:
        async with self._lock:
            await self._stop_locked()

    def _prepare_runtime_dir(self) -> None:
        if self._runtime_dir.is_symlink():
            raise BridgeError(
                "AGENT_RUNTIME_NOT_READY",
                "Codex proxy runtime directory is unsafe",
            )
        try:
            self._runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self._runtime_dir, 0o700)
        except OSError as exc:
            raise BridgeError(
                "AGENT_RUNTIME_NOT_READY",
                "Codex proxy runtime directory is unavailable",
            ) from exc
        if not self._runtime_dir.is_dir():
            raise BridgeError(
                "AGENT_RUNTIME_NOT_READY",
                "Codex proxy runtime directory is unavailable",
            )

        try:
            info = os.lstat(self._socket_path)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise BridgeError(
                "AGENT_RUNTIME_NOT_READY",
                "Codex proxy runtime socket cannot be inspected",
            ) from exc
        # Codex <=0.162.0 created the requested Unix socket directly. Codex 0.162.1 may
        # instead create a symlink at the requested listener path that points to its private
        # per-user daemon socket. Both entries are safe to remove here because this path is a
        # fixed name inside the Bridge-owned 0700 runtime directory and unlink never follows a
        # symlink. Any other pre-existing filesystem object remains fail-closed.
        if not (stat.S_ISSOCK(info.st_mode) or stat.S_ISLNK(info.st_mode)):
            raise BridgeError(
                "AGENT_RUNTIME_NOT_READY",
                "Codex proxy runtime socket path is unsafe",
            )
        try:
            self._socket_path.unlink()
        except OSError as exc:
            raise BridgeError(
                "AGENT_RUNTIME_NOT_READY",
                "Codex stale proxy runtime socket cannot be removed",
            ) from exc

    async def _start_locked(self) -> None:
        if self.runtime_proxy is None:
            raise BridgeError(
                "AGENT_RUNTIME_NOT_READY",
                "Codex is configured to use the Agent proxy but no endpoint is available",
            )
        self._prepare_runtime_dir()
        env = build_runtime_environment(
            os.environ,
            runtime="codex",
            use_proxy=True,
            proxy=self.runtime_proxy,
        )
        env["CODEX_HOME"] = str(self.settings.codex_home)
        try:
            process = await asyncio.create_subprocess_exec(
                self.settings.codex_bin,
                "app-server",
                "--listen",
                f"unix://{self._socket_path}",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
            )
        except OSError as exc:
            raise BridgeError(
                "AGENT_RUNTIME_UNAVAILABLE",
                "Codex standalone app-server could not be started",
            ) from exc
        self._process = process

        loop = asyncio.get_running_loop()
        deadline = loop.time() + _START_TIMEOUT_SECONDS
        while loop.time() < deadline:
            if process.returncode is not None:
                await self._stop_locked()
                raise BridgeError(
                    "AGENT_RUNTIME_NOT_READY",
                    "Codex standalone app-server exited before becoming ready",
                )
            if self._socket_is_ready():
                return
            await asyncio.sleep(0.05)

        await self._stop_locked()
        raise BridgeError(
            "AGENT_RUNTIME_NOT_READY",
            "Codex standalone app-server did not become ready in time",
        )

    def _socket_is_ready(self) -> bool:
        """Whether Codex published a safe listener at the requested path.

        Codex <=0.162.0 binds the requested path directly. Codex 0.162.1 may publish a
        symlink there and keep the real 0600 socket in a per-user private directory. The
        indirection is accepted only when its target is an absolute, same-user, private Unix
        socket whose immediate parent is also same-user and not group/world writable.
        """
        try:
            listener = os.lstat(self._socket_path)
        except (FileNotFoundError, OSError):
            return False
        if stat.S_ISSOCK(listener.st_mode):
            return True
        if not stat.S_ISLNK(listener.st_mode):
            return False
        try:
            raw_target = os.readlink(self._socket_path)
            target = Path(raw_target)
            if not target.is_absolute():
                return False
            target_info = os.lstat(target)
            if (
                not stat.S_ISSOCK(target_info.st_mode)
                or target_info.st_uid != os.getuid()
                or target_info.st_mode & 0o077
            ):
                return False
            parent_info = os.lstat(target.parent)
            return (
                stat.S_ISDIR(parent_info.st_mode)
                and not stat.S_ISLNK(parent_info.st_mode)
                and parent_info.st_uid == os.getuid()
                and not parent_info.st_mode & 0o022
            )
        except (FileNotFoundError, OSError):
            return False

    async def _stop_locked(self) -> None:
        process = self._process
        self._process = None
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=_STOP_TIMEOUT_SECONDS)
            except TimeoutError:
                process.kill()
                await process.wait()
        elif process is not None:
            await process.wait()

        try:
            info = os.lstat(self._socket_path)
        except (FileNotFoundError, OSError):
            return
        if stat.S_ISSOCK(info.st_mode) or stat.S_ISLNK(info.st_mode):
            try:
                self._socket_path.unlink()
            except OSError:
                pass
