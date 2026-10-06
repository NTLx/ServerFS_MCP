"""Bridge-owned Windows ``codex app-server`` lifecycle.

On Linux the Codex runtime talks to the provider's *managed* app-server daemon over an AF_UNIX
control socket, and the Bridge never owns a provider process. Windows has no such socket: Phase 0C
measured and the maintainer selected an authenticated loopback WebSocket listener served by a child
the Bridge spawns itself. That makes three things Bridge-owned on Windows and provider-owned on
Linux: the transport endpoint, the app-server process, and the capability token.

This module owns exactly those three and nothing else. Provider semantics -- models, threads, turns,
approvals, questions, events, results, reconciliation -- stay in codex.py and are unchanged. A
second Windows adapter that copied them would be a second set of provider defects.

Two measured facts shape the implementation and are worth stating because the obvious alternative is
wrong in both cases:

* ``--listen ws://127.0.0.1:0`` is accepted and the CLI **announces its bound endpoint on stderr**
  (``listening on: ws://127.0.0.1:<port>``). The port is therefore read from the child's own output,
  which is an official channel. The usual fallback -- reserve an OS ephemeral port, close the
  reservation, spawn, and retry the bind race -- is unnecessary and would be strictly worse: it
  invents a race the provider does not have.
* The listener answers in well under a second on this host, but ``/readyz`` answers 200 on loopback
  *without* the capability token, so it cannot be the readiness signal. Readiness is an
  authenticated WebSocket connect plus ``initialize``, which is also the only check that proves the
  token works.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

from ..bootstrap import RuntimeProxy
from ..config import CodexSettings
from ..errors import BridgeError
from ..runtime_proxy import build_runtime_environment
from .codex_transport import CodexEndpoint, LoopbackWebSocketEndpoint

#: Bounded internal startup budget. Phase 0C recorded ~24 s for the listener on this host; a later
#: Phase E measurement found ~0.3 s. Both are far below this bound, which is kept generous on
#: purpose: a readiness budget that is too small produces a false "runtime not ready" on a slow or
#: loaded machine, and a budget that is too large only delays a failure that is already bounded by
#: the child's own exit. This is not an operator-facing knob.
STARTUP_TIMEOUT_SECONDS = 60.0

#: Bounded budget for the final terminate/kill escalation during teardown.
_TEARDOWN_TIMEOUT_SECONDS = 10.0

#: Bounded backoff between readiness attempts while the listener comes up.
_READINESS_BACKOFF_SECONDS = 0.2

#: A capability token is generated per child start. ``token_urlsafe(32)`` yields 256 bits.
_TOKEN_BYTES = 32

#: The endpoint the CLI announces. The host group is deliberately absent from the pattern: the
#: readiness gate re-checks the host separately, so a host here can only ever be a literal address.
_LISTENING_LINE = re.compile(r"listening on:\s*ws://(?P<host>\[[^\]]+\]|[^:/\s]+):(?P<port>\d+)")

_LOOPBACK_HOST = "127.0.0.1"


@dataclass(frozen=True)
class CodexAppServerEndpoint:
    """A ready Bridge-owned listener: where to dial and how to authenticate."""

    endpoint: LoopbackWebSocketEndpoint
    port: int
    process_id: int
    startup_seconds: float

    def as_codex_endpoint(self) -> CodexEndpoint:
        return self.endpoint


class WindowsCodexAppServer:
    """One Bridge-owned ``codex app-server`` child, shared by every connection on this adapter.

    A single child serves the probe connection, the model-list connection, every task connection and
    the reconciliation connection. That is what keeps ``max_active_tasks`` meaningful and lets
    native session state be shared: the alternative, one app-server per task, would multiply
    provider processes and make each task's view of ``codexHome`` independent.
    """

    def __init__(
        self,
        settings: CodexSettings,
        *,
        state_dir: Path,
        runtime_proxy: RuntimeProxy | None = None,
        base_env: dict[str, str] | None = None,
        client_version: str = "0.11.0",
        startup_timeout: float = STARTUP_TIMEOUT_SECONDS,
    ) -> None:
        self.settings = settings
        self.state_dir = state_dir
        self._runtime_proxy = runtime_proxy
        self._base_env = dict(os.environ if base_env is None else base_env)
        self._client_version = client_version
        self._startup_timeout = startup_timeout

        self._token_dir = state_dir / "codex"
        self._token_path = self._token_dir / "app-server-token"
        self._process: asyncio.subprocess.Process | None = None
        self._token: str | None = None
        self._ready: CodexAppServerEndpoint | None = None
        self._start_lock = asyncio.Lock()
        self._closed = False
        self._token_file_owned = False

    # ------------------------------------------------------------------ public surface

    @property
    def token_file(self) -> Path:
        """The private capability-token file path. Safe to report; the file holds a credential."""
        return self._token_path

    @property
    def endpoint(self) -> LoopbackWebSocketEndpoint | None:
        """The live endpoint, or ``None`` when no app-server is running."""
        ready = self._ready
        return ready.endpoint if ready is not None else None

    @property
    def process_id(self) -> int | None:
        ready = self._ready
        return ready.process_id if ready is not None else None

    async def ensure_started(self) -> LoopbackWebSocketEndpoint:
        """Return a live endpoint, starting the child if needed. Idempotent and single-flight.

        Concurrent ``runtime.list`` / ``model.list`` / ``task.submit`` / reconciliation can all
        arrive before any child exists. Without a lock each would spawn its own app-server and the
        operator would see a leak of provider processes racing to bind ports.
        """
        if self._closed:
            raise BridgeError("AGENT_RUNTIME_UNAVAILABLE", "Codex adapter is closed")
        async with self._start_lock:
            ready = self._ready
            if ready is not None and self._is_child_alive():
                return ready.endpoint
            if ready is not None:
                # The child died on its own. Existing connections see a provider disconnect and new
                # ones get a fresh app-server. A turn that already failed is never silently retried.
                await self._teardown_locked()
            return (await self._start_locked()).endpoint

    async def close(self) -> None:
        """Stop the child and destroy the token. Safe to call repeatedly and from a failed path."""
        async with self._start_lock:
            self._closed = True
            await self._teardown_locked()

    # ------------------------------------------------------------------ startup

    async def _start_locked(self) -> CodexAppServerEndpoint:
        loop = asyncio.get_running_loop()
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        # The token is published to the instance before the child starts: readiness authenticates
        # with it, and a probe that raced ahead of this assignment would fail every time.
        self._token = token
        self._write_token_file(token)
        argv = self._build_argv()
        env = self._build_env()

        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.DEVNULL,
                # stderr carries the endpoint announcement, so it is read through a pipe and parsed
                # as it arrives rather than inherited: provider stderr can contain account and path
                # detail that must not reach the Bridge's own stderr.
                stderr=asyncio.subprocess.PIPE,
                env=env,
                # No new process group and no second Job Object. The Bridge already runs
                # inside the Phase D supervisor Job Object and a standard child inherits that
                # containment, which is what makes an abnormal Bridge death reap this provider
                # process.
                creationflags=0,
            )
        except OSError as exc:
            await self._teardown_locked()
            raise BridgeError(
                "AGENT_RUNTIME_NOT_READY",
                "Codex App Server could not be started",
            ) from exc

        self._process = process
        started = loop.time()
        try:
            port = await self._await_listener(process)
        except BaseException:
            # Readiness failed: the child must not survive as an orphan whose token file is gone.
            await self._teardown_locked()
            raise

        ready = CodexAppServerEndpoint(
            endpoint=LoopbackWebSocketEndpoint(
                url=f"ws://{_LOOPBACK_HOST}:{port}/rpc",
                token=token,
            ),
            port=port,
            process_id=process.pid,
            startup_seconds=round(loop.time() - started, 3),
        )
        self._ready = ready
        return ready

    def _build_argv(self) -> list[str]:
        """The provider-official command line. The token is passed by file, never by value.

        ``--ws-token-file`` is used rather than ``--ws-token-sha256`` even though both authenticate:
        the file is the frozen Phase 0C choice, and switching to the digest form to avoid a file
        would trade a measured, reviewed mechanism for an unmeasured one.
        """
        return [
            self.settings.codex_bin,
            "app-server",
            # Port 0: the provider binds an OS-assigned loopback port and announces it. This is not
            # a fixed port, a port scan, or a non-loopback bind.
            "--listen",
            f"ws://{_LOOPBACK_HOST}:0",
            "--ws-auth",
            "capability-token",
            "--ws-token-file",
            str(self._token_path),
        ]

    def _build_env(self) -> dict[str, str]:
        """The child environment, decided by policy rather than inherited wholesale.

        ``build_runtime_environment`` already clears the proxy trust domain and every Tunnel /
        Control Plane namespace, then re-establishes only the Agent proxy when ``use_proxy`` is set.
        The one addition is ``CODEX_HOME``, which is the provider-native way to point the child at
        the configured Codex home -- and it is what makes ``thread/resume`` able to find state a
        previous child wrote.
        """
        env = build_runtime_environment(
            self._base_env,
            runtime="codex",
            use_proxy=self.settings.use_proxy,
            proxy=self._runtime_proxy,
        )
        env["CODEX_HOME"] = str(self.settings.codex_home)
        return env

    async def _await_listener(self, process: asyncio.subprocess.Process) -> int:
        """Wait for the announced loopback endpoint, bounded, watching for an early child exit.

        The endpoint comes from the child's own stderr because the CLI announces it officially. The
        port is then gated on a successful authenticated WebSocket handshake, not merely on the
        announcement: only the handshake proves the token file was accepted.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._startup_timeout
        assert process.stderr is not None
        announcement: asyncio.Task[int] = asyncio.ensure_future(
            _read_announced_port(process.stderr)
        )
        try:
            while True:
                if process.returncode is not None:
                    raise BridgeError(
                        "AGENT_RUNTIME_NOT_READY",
                        "Codex App Server exited before its listener became ready",
                    )
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise BridgeError(
                        "AGENT_RUNTIME_NOT_READY",
                        "Codex App Server did not become ready within the startup budget",
                    )
                if announcement.done():
                    announced = announcement.result()
                    host, port = announced
                    if not _is_loopback_host(host):
                        # The CLI bound something other than loopback. Refuse rather than dial it:
                        # an off-host listener is a different security posture from the one Phase 0C
                        # measured, and ``localhost`` is not a boundary.
                        raise BridgeError(
                            "AGENT_PROVIDER_ERROR",
                            "Codex App Server announced a non-loopback listener",
                        )
                    if await self._probe_ready(port):
                        return port
                await asyncio.sleep(min(_READINESS_BACKOFF_SECONDS, max(remaining, 0.0)))
        finally:
            if not announcement.done():
                announcement.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await announcement

    async def _probe_ready(self, port: int) -> bool:
        """One authenticated handshake attempt. ``False`` means "not yet", not "broken"."""
        from .codex_transport import CodexConnection

        token = self._token
        if token is None:
            # Cannot happen through _start_locked, which assigns before spawning. Asserting the
            # invariant beats dialling with an empty bearer and reporting a generic refusal.
            raise BridgeError(
                "AGENT_RUNTIME_NOT_READY",
                "Codex App Server capability token was not established",
            )
        connection = CodexConnection(
            endpoint=LoopbackWebSocketEndpoint(
                url=f"ws://{_LOOPBACK_HOST}:{port}/rpc",
                token=token,
            ),
            client_name="serverfs-agent-bridge",
            client_version=self._client_version,
            request_timeout=5.0,
            max_message_bytes=self.settings.max_message_bytes,
        )
        try:
            await connection.connect()
        except BridgeError:
            await connection.close()
            return False
        await connection.close()
        return True

    # ------------------------------------------------------------------ teardown

    async def _teardown_locked(self) -> None:
        """Stop the child, then destroy the token. The token is destroyed even if stopping fails."""
        process = self._process
        self._process = None
        self._ready = None
        self._token = None
        try:
            if process is not None:
                await _stop_process(process)
        finally:
            # Token cleanup must not be skipped because terminate or wait failed: a
            # surviving token beside a dead child is a credential with no remaining purpose.
            await self._destroy_token_file()

    async def _destroy_token_file(self) -> None:
        """Delete the token file this object created, and only if it created it."""
        if not self._token_file_owned:
            return
        self._token_file_owned = False
        path = self._token_path
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            # An undeletable token file is worth failing on, because the alternative
            # is leaving a credential behind. It is reported without the path's contents.
            raise BridgeError(
                "AGENT_RUNTIME_NOT_READY",
                "Codex capability token file could not be removed",
            ) from exc

    # ------------------------------------------------------------------ token file

    def _write_token_file(self, token: str) -> None:
        """Write a fresh token into private state, reusing the existing ACL machinery verbatim.

        The Bridge private-state helpers already implement the whole §6 contract -- owner SID,
        protected DACL, reparse refusal, and a pre-planted unsafe target refused before it is
        overwritten. Duplicating any of that here would create a second answer to the same security
        question.
        """
        from .. import private_state

        private_state.ensure_private_directory(
            self._token_dir,
            mode=0o700,
            parents=True,
            messages=private_state.DirectoryMessages(
                not_a_directory="Codex token directory must be a real directory",
                not_owned="Codex token directory must be owned by the bridge user and mode 0700",
            ),
        )
        # Refuses a reparse point, a directory target, or an existing object with a foreign owner or
        # a broad DACL -- before anything is written, so an unsafe pre-planted file is never
        # overwritten and never used.
        private_state.ensure_private_file(
            self._token_path,
            mode=0o600,
            not_regular="Codex capability token path must be a regular file",
        )
        # A stale file from a crashed Bridge is not a startup blocker and not a reason to refuse:
        # the object was just verified private, so overwriting it with a fresh secret is safe.
        private_state.protect_existing_file(
            self._token_path,
            mode=0o600,
            not_private="Codex capability token file must be private to the bridge user",
        )
        descriptor = os.open(
            self._token_path,
            private_state.open_flags("write"),
            0o600,
        )
        try:
            os.write(descriptor, token.encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self._token_file_owned = True

    # ------------------------------------------------------------------ helpers

    def _is_child_alive(self) -> bool:
        return self._process is not None and self._process.returncode is None


async def _read_announced_port(stream: asyncio.StreamReader) -> tuple[str, int]:
    """Consume the child's stderr, returning ``(host, port)`` from its ``listening on:`` line.

    stderr is drained to EOF rather than read once: the pipe must not fill while the child is alive,
    because a full pipe would block the child instead of informing us. Nothing from the stream is
    retained, so provider output cannot reach a log or an exception message.
    """
    while True:
        raw = await stream.readline()
        if not raw:
            raise BridgeError(
                "AGENT_RUNTIME_NOT_READY",
                "Codex App Server closed its output before announcing a listener",
            )
        line = raw.decode("utf-8", errors="replace")
        match = _LISTENING_LINE.search(line)
        if match is not None:
            return match.group("host"), int(match.group("port"))


def _is_loopback_host(host: str) -> bool:
    """Whether the announced host is a loopback address, verified rather than trusted.

    The CLI prints a note saying it binds localhost only. That note is documentation; the host in
    the URL is the fact. Checking it here means a changed or widened binding is refused by the
    Bridge instead of being dialled.
    """
    normalized = host.strip("[]").lower()
    if normalized in {"127.0.0.1", "::1"}:
        return True
    # Any other IPv4 literal in 127.0.0.0/8 is still loopback, but ServerFS deliberately dials
    # literal 127.0.0.1 only, so a broader loopback bind is not the endpoint this runtime wants.
    return False


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    """Terminate, then kill after a bounded wait. Never raises, so cleanup always continues."""
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError, OSError):
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=_TEARDOWN_TIMEOUT_SECONDS)
        return
    except TimeoutError:
        pass
    with contextlib.suppress(ProcessLookupError, OSError):
        process.kill()
    with contextlib.suppress(TimeoutError, asyncio.CancelledError):
        await asyncio.wait_for(process.wait(), timeout=_TEARDOWN_TIMEOUT_SECONDS)
