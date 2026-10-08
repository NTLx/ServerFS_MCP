"""F3/E1 raw-inheritance probe: can a provider tool see a proxy variable at all?

Run as a **subprocess under the Bridge interpreter**, not imported into the acceptance harness.

That is not a stylistic choice. `qoder-agent-sdk` is installed in `agent_bridge/.venv` only; the
harness itself runs under the repository's root `.venv`, where `import qoder_agent_sdk` raises
`ModuleNotFoundError` (measured, not assumed). An arm that imported the SDK in-process would either
fail on the import or quietly measure a different interpreter's installation than the one the Bridge
actually uses. A subprocess keeps the measurement on the same interpreter as production.

The endpoint and the model both arrive on stdin, so neither appears in the environment nor argv.
This probe applies no environment policy of its own: it passes no ``env`` to the SDK, so the
provider child inherits this process's environment wholesale. That is the whole point -- the caller
decides whether the sentinels are present, and this probe only reports what a tool could see.
Applying ServerFS's own deletion overlay here would delete the caller's sentinels and make the arm
vacuous; the boundary is pinned in ``tests/test_f3_harness_fidelity.py``, not left to a docstring.

Reads and writes exactly one artifact inside `E1_PROBE_DIR`, and prints one JSON line:

    {"stage": "e1_raw", "callback_fired": bool, "probe_present": bool, "visible_to_tool": [...]}

No value from the environment is printed, only which names were present.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

#: The names whose presence is being measured. Kept identical to the harness's SCRUB_NAMES; the
#: harness passes the list in the bootstrap frame so there is exactly one definition.
DEFAULT_NAMES: tuple[str, ...] = (
    "HTTPS_PROXY",
    "ALL_PROXY",
    "CODEBUDDY_SERVICE_PROXY_URL",
    "SERVERFS_AGENT_PROXY_URL",
)

ARTIFACT = "phase-f3-toolenv.json"

TURN_TIMEOUT = 300.0


def emit(stage: str, **fields: Any) -> None:
    print(json.dumps({"stage": stage, **fields}, ensure_ascii=False), flush=True)


async def main() -> int:
    from qoder_agent_sdk import (
        PermissionResultAllow,
        QoderAgentOptions,
        QoderSDKClient,
        qodercli_auth,
    )

    frame = json.loads(sys.stdin.readline() or "{}")
    endpoint = str(frame.get("endpoint", "")).strip()
    names = tuple(frame.get("names") or DEFAULT_NAMES)
    # The model is supplied by the caller rather than left to the host's Qoder configuration. The
    # acceptance is only ever approved to use one free model, and a default that happened to be
    # free today is not an acceptance guarantee -- it would silently become a paid turn.
    model = str(frame.get("model", "")).strip()
    if not model:
        emit("e1_raw_failed", error_class="MissingModel")
        return 2
    workspace = Path(os.environ["E1_PROBE_DIR"])
    outcome: dict[str, Any] = {
        "callback_fired": False,
        "probe_present": False,
        "visible_to_tool": [],
    }

    async def can_use_tool(
        tool_name: str, tool_input: dict[str, Any], permission_context: Any
    ) -> PermissionResultAllow:
        # Only the fact. The tool name and its input are the command, and a diagnostic that prints
        # commands becomes a transcript of the workspace.
        outcome["callback_fired"] = True
        return PermissionResultAllow(updated_input=tool_input)

    prompt = (
        "Run this exact shell command with the Bash tool and do nothing else: "
        f"python -c \"import json,os;open('{ARTIFACT}','w').write(json.dumps("
        "{k: (k in os.environ) for k in " + repr(list(names)) + '}))"'
    )

    # No `env` on purpose. This is the raw-control arm: the question is whether a variable in this
    # process's environment reaches a tool through the SDK's *natural* inheritance. Passing the
    # product's deletion overlay here would delete the sentinels the caller installed before this
    # subprocess was spawned, and the arm would then report `non_vacuous=false` for a reason of the
    # harness's own making -- the exact vacuity it exists to rule out. Pinned, not commented:
    # `tests/test_f3_harness_fidelity.py` fails if an `env` overlay reappears here.
    options = QoderAgentOptions(
        auth=qodercli_auth(),
        cwd=workspace,
        setting_sources=["user", "project", "local"],
        can_use_tool=can_use_tool,
        # Explicit, not inherited from the host's Qoder settings: see the `model` read above.
        model=model,
        # Deliberately None: setting it would put the endpoint into the qodercli argv. It is applied
        # after connect, through the local control request, exactly as the product does.
        proxy=None,
    )
    client = QoderSDKClient(options)
    try:
        async with asyncio.timeout(TURN_TIMEOUT):
            await client.connect()
            await client.set_proxy(endpoint)
            await client.query(prompt)
            async for _message in client.receive_response():
                pass
    except BaseException as exc:  # noqa: BLE001 - the failure class is the measurement
        emit("e1_raw_failed", error_class=type(exc).__name__)
        return 3
    finally:
        try:
            await client.disconnect()
        except BaseException:  # noqa: BLE001, S110 - teardown must not change a measurement
            pass

    # Read after disconnect: on Windows a live child holds the file, so a probe written by a tool
    # that then failed would otherwise look like one that never ran.
    artifact = workspace / ARTIFACT
    probe: dict[str, bool] | None = None
    if artifact.is_file():
        try:
            value = json.loads(artifact.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            value = None
        probe = value if isinstance(value, dict) else None

    outcome["probe_present"] = probe is not None
    outcome["visible_to_tool"] = sorted(n for n, present in (probe or {}).items() if present)
    outcome["non_vacuous"] = bool(outcome["visible_to_tool"])
    shutil.rmtree(workspace, ignore_errors=True)
    emit("e1_raw", **outcome)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
