"""Test-only runtime that records the environment a provider child would actually receive.

Phase D requires that the Agent runtime egress policy be *consumed* by the product path, not merely
unit-tested. This module closes that gap for the phase: it is the same
``build_runtime_environment`` the Codex adapter uses, reached through the same chain a supervised
Bridge takes —

    stdin bootstrap frame -> _serve -> adapter -> child environment

— and it proves the result by spawning a real child process and capturing what that child sees in
``os.environ``. A test that only inspected the helper's return value would pass even if the policy
were never called; this cannot, because the captured text comes from a separate process.

The runtime is provider-neutral on purpose. It implements no Codex transport, speaks no provider
protocol, and performs no inference — it only spawns a Python child, which is exactly the shape of
the thing whose environment we need to observe.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from serverfs_agent_bridge.adapters import FakeAdapter
from serverfs_agent_bridge.bootstrap import RuntimeProxy
from serverfs_agent_bridge.runtime_proxy import build_runtime_environment

#: Prompt prefix that asks the runtime to capture a child's environment to a file.
CAPTURE_PROMPT = "runtime-env-capture:"

#: The child writes its own environment here. Using a real subprocess is the point: the values are
#: observed by a separate process rather than asserted against a dict the test already holds.
_CHILD_SCRIPT = "import json,os,sys\nsys.stdout.write(json.dumps(dict(os.environ)))\n"


class RuntimeEnvCapturingFakeAdapter(FakeAdapter):
    """A fake runtime that can prove which environment a provider child would receive.

    Constructed with the same ``use_proxy`` policy and the same in-memory ``RuntimeProxy`` the real
    adapters receive, so a difference in the captured environment is a difference in the policy
    rather than in the test's own setup.
    """

    def __init__(
        self,
        settings=None,
        *,
        client_version: str = "0.0.0",
        runtime_proxy: RuntimeProxy | None = None,
    ) -> None:
        super().__init__()
        # Mirrors the Codex adapter's signature so _serve can construct it the same way, which is
        # what makes the proxy arrive through the real wiring rather than around it.
        self.settings = settings
        self.client_version = client_version
        self._runtime_proxy = runtime_proxy
        if settings is not None and getattr(settings, "use_proxy", None) is not None:
            self.use_proxy = bool(settings.use_proxy)
        else:
            self.use_proxy = os.environ.get("SERVERFS_TEST_USE_PROXY") == "1"

    @property
    def use_proxy(self) -> bool:
        return self._use_proxy

    @use_proxy.setter
    def use_proxy(self, value: bool) -> None:
        self._use_proxy = value

    async def run_task(self, context):  # type: ignore[override]
        prompt = context.prompt
        if not prompt.startswith(CAPTURE_PROMPT):
            return await super().run_task(context)

        target = Path(prompt.removeprefix(CAPTURE_PROMPT).strip())
        child_env = build_runtime_environment(
            os.environ,
            runtime=self.name,
            use_proxy=self.use_proxy,
            proxy=self._runtime_proxy,
        )
        # A real child, so the captured mapping is what a provider process would genuinely observe.
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            _CHILD_SCRIPT,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env,
        )
        stdout, _stderr = await process.communicate()
        captured = json.loads(stdout.decode("utf-8"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(captured, indent=2, sort_keys=True), encoding="utf-8")

        response = f"captured {len(captured)} variables"
        await context.emit_event("agent.message", {"text": response})
        from serverfs_agent_bridge.adapters.base import AdapterResult

        return AdapterResult(
            final_response=response,
            native_session_id=context.continue_native_session_id
            or f"fake-session-{context.task_id}",
            native_turn_id=f"fake-turn-{context.task_id}",
        )


__all__ = ["CAPTURE_PROMPT", "RuntimeEnvCapturingFakeAdapter"]
