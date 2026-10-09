"""Phase H5 — wheel-only real-provider package smoke (one minimal turn per runtime).

Phase E/F/G proved the full semantic matrices in the development environment. This driver
proves the same implementation *as shipped*: the three candidate wheels installed into two
isolated clean venvs across the frozen ServerFS/Agent-Bridge process boundary, no PYTHONPATH,
no test-only sitecustomize, no editable checkout — and one real provider turn per runtime
through the public MCP surface.

The launch chain is the Phase E one (``phase_e_lifecycle.Lifecycle``), which injects nothing
test-only into the product: the environment pollution markers, the isolated data home, the
Bridge interpreter and the Agent proxy endpoint are the only additions, and the product's own
scrub/ownership machinery is what disposes of them. ``ROOT_PYTHON`` and ``BRIDGE_PYTHON`` are
repointed at the wheel-only venvs, so the chain exercises:

    .venv-serverfs python -m serverfs_mcp.cli tunnel   (product wheel + native wheel)
        └── supervisor → SERVERFS_BRIDGE_PYTHON
                └── .venv-bridge python -m serverfs_agent_bridge.main   (bridge wheel)
                        └── real provider CLI (codex / qodercli / claude)

Per maintainer instruction this is a package smoke, not a matrix rerun: probe + one real task
with an exact artifact per runtime, plus the invariants that are cheap to re-assert (TaskStore
attribution, public projection hiding native ids, no *new* provider children — compared
against a pre-chain baseline, because the operator's own sessions are never counted). The
Qoder branch re-reads the Phase F live model gate first and STOPS rather than paying for a
paid model. Nothing prints an endpoint, a token, an account, or a native session id.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tests" / "e2e"))
sys.path.insert(0, str(REPO_ROOT / "src"))

from phase_e_acceptance import McpStdioClient, ToolError, native_ids  # noqa: E402
from phase_e_lifecycle import Lifecycle, require_file_stderr  # noqa: E402
from run_phase_f_acceptance import live_model_gate  # noqa: E402

#: Wheel-only interpreters. Both default to the H3 split venvs; both are mechanically verified
#: to be wheel-only venvs under the repo before anything runs, so the smoke can never quietly
#: run against a development environment.
SERVERFS_PYTHON = Path(
    os.environ.get(
        "SERVERFS_TEST_ROOT_PYTHON", str(REPO_ROOT / ".venv-serverfs" / "Scripts" / "python.exe")
    )
)
BRIDGE_VENV_PYTHON = Path(
    os.environ.get(
        "SERVERFS_BRIDGE_PYTHON", str(REPO_ROOT / ".venv-bridge" / "Scripts" / "python.exe")
    )
)

TURN_TIMEOUT_S = 420
PROVIDER_IMAGES = {"codex": "codex", "qoder": "qodercli", "claude": "claude"}
RUNTIMES = ("codex", "qoder", "claude")
SUBSTANTIVE_GATES = ("probe", "turn", "attribution", "cleanup")


def emit(stage: str, **fields: Any) -> None:
    print(json.dumps({"stage": stage, **fields}, ensure_ascii=True), flush=True)


def gate_record(passed: bool, **fields: Any) -> dict[str, Any]:
    return {"passed": bool(passed), **fields}


def _run_gate(name: str, thunk):
    """Run one gate; an exception inside it fails that gate, not the whole run."""
    try:
        return thunk()
    except Exception as exc:  # noqa: BLE001 - the failure class is the gate's result
        emit("gate_failed", gate=name, error_class=type(exc).__name__, detail=str(exc)[:250])
        return gate_record(False, error_class=type(exc).__name__, detail=str(exc)[:250])


def _process_count(image: str) -> int:
    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            f"(Get-Process -Name '{image}' -ErrorAction SilentlyContinue | Measure-Object).Count",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    try:
        return int(completed.stdout.strip() or "0")
    except ValueError:
        return 0


def _python_probe(interpreter: Path, code: str) -> None:
    completed = subprocess.run(
        [str(interpreter), "-c", code], capture_output=True, text=True, timeout=120
    )
    if completed.returncode != 0:
        raise SystemExit(f"provenance probe failed ({interpreter}): {completed.stderr[:300]}")


def require_wheel_environment() -> None:
    """Mechanical provenance: both interpreters are wheel-only venvs under the repo.

    The same split-env packaging gate H3 established, asserted before any provider runs: each
    environment contains exactly its own distribution and can never shadow it from the
    checkout. The Phase E chain is also repointed here -- it must run the wheel-installed
    product, not the development venv. The Bridge interpreter is honoured through
    SERVERFS_BRIDGE_PYTHON (the product's own override name); the product interpreter is
    repointed explicitly, before any Lifecycle is constructed.
    """
    import phase_e_lifecycle

    if phase_e_lifecycle.BRIDGE_PYTHON != BRIDGE_VENV_PYTHON:
        raise SystemExit(
            f"SERVERFS_BRIDGE_PYTHON ({phase_e_lifecycle.BRIDGE_PYTHON}) does not point at the "
            f"wheel-only Bridge venv ({BRIDGE_VENV_PYTHON})"
        )
    phase_e_lifecycle.ROOT_PYTHON = SERVERFS_PYTHON
    repo = str(REPO_ROOT).replace("\\", "/").lower()
    for name, interpreter, marker in (
        ("ServerFS venv", SERVERFS_PYTHON, ".venv-serverfs"),
        ("Agent Bridge venv", BRIDGE_VENV_PYTHON, ".venv-bridge"),
    ):
        resolved = str(interpreter.resolve()).replace("\\", "/").lower()
        if repo not in resolved or marker not in resolved:
            raise SystemExit(
                f"{name} must be the wheel-only venv ({marker}); refusing to run the package "
                f"smoke against {interpreter}"
            )
    _python_probe(
        SERVERFS_PYTHON,
        "from importlib.util import find_spec\n"
        "import serverfs_mcp, serverfs_windows_native\n"
        "assert find_spec('serverfs_agent_bridge') is None, 'product env contains the Bridge'\n"
        "print('provenance: serverfs wheel-only ok')",
    )
    _python_probe(
        BRIDGE_VENV_PYTHON,
        "from importlib.util import find_spec\n"
        "import serverfs_agent_bridge\n"
        "assert find_spec('serverfs_mcp') is None, 'bridge env contains the product'\n"
        "assert find_spec('serverfs_windows_native') is None, 'bridge env contains native'\n"
        "print('provenance: bridge wheel-only ok')",
    )


def _submit_artifact_task(
    client: McpStdioClient, lifecycle: Lifecycle, *, artifact: str, marker: str
) -> tuple[str, str, bool]:
    """One real workspace-write turn that must produce an exact artifact."""
    path = lifecycle.workdir / artifact
    if path.exists():
        path.unlink()
    prompt = f"Create {artifact} containing exactly:\n\n{marker}\n\nDo not modify any other file."
    task_id = client.submit(prompt, runtime=lifecycle.runtime)
    status = client.wait_status(task_id, timeout=TURN_TIMEOUT_S, allow_approval=True)
    content = path.read_text(encoding="utf-8").strip() if path.exists() else None
    return task_id, status, content == marker


def _probe_gate(client: McpStdioClient, runtime: str) -> dict[str, Any]:
    catalog = client.call("list_agent_runtimes", {})
    runtimes = catalog.get("runtimes") or catalog.get("agents") or []
    entry = next((r for r in runtimes if r.get("name") == runtime), None)
    return gate_record(
        bool(entry and entry.get("available")),
        observed=[r.get("name") for r in runtimes],
        version=(entry or {}).get("version"),
    )


def _turn_gate(
    client: McpStdioClient, lifecycle: Lifecycle, runtime: str
) -> tuple[dict[str, Any], str | None]:
    marker = f"wheel-smoke-{runtime}-{os.urandom(4).hex()}"
    task_id, status, exact = _submit_artifact_task(
        client, lifecycle, artifact=f"wheel-smoke-{runtime}.txt", marker=marker
    )
    return (
        gate_record(status == "succeeded" and exact, status=status, artifact_exact=exact),
        task_id,
    )


def _attribution_gate(client: McpStdioClient, lifecycle: Lifecycle, task_id: str) -> dict[str, Any]:
    """TaskStore carries the native ids; the public projection hides them."""
    ids = native_ids(client, lifecycle, task_id)
    public = client.task(task_id)
    leaked = sorted(set(public) & {"native_session_id", "native_turn_id"})
    return gate_record(
        bool(ids.get("thread_id_present"))
        and not leaked
        and public.get("runtime") == lifecycle.runtime,
        session_present=bool(ids.get("thread_id_present")),
        turn_present=bool(ids.get("turn_id_present")),
        store_found=bool(ids.get("store_found")),
        runtime=public.get("runtime"),
        projection_leaks=leaked,
    )


def _cleanup_gate(lifecycle: Lifecycle, image: str, baseline_children: int) -> dict[str, Any]:
    """No Bridge left; no provider children beyond the operator's own pre-chain baseline."""
    bridges_gone = not lifecycle.bridge_pids()
    current = _process_count(image)
    return gate_record(
        bridges_gone and current <= baseline_children,
        bridge_stopped=bridges_gone,
        provider_children_now=current,
        provider_children_baseline=baseline_children,
    )


def _smoke_runtime(runtime: str, *, use_proxy: bool) -> dict[str, Any]:
    """One isolated chain, one real turn. Chains are serial: the lifecycle lease is per-user.

    The Agent proxy endpoint (when use_proxy) arrives from the operator's own ``.env`` through
    the Phase E chain's normal path -- no local forwarder, no override.
    """
    results: dict[str, Any] = {}
    tmp_root = Path(tempfile.mkdtemp(prefix=f"phase-h-smoke-{runtime}-"))
    lifecycle = Lifecycle(
        tmp_root,
        env_file=REPO_ROOT / ".env",
        codex_home=Path.home() / ".codex",
        use_proxy=use_proxy,
        read_only=False,
        runtime=runtime,
    )
    require_file_stderr(lifecycle)
    client: McpStdioClient | None = None
    task_id: str | None = None
    baseline_children = _process_count(PROVIDER_IMAGES[runtime])
    try:
        lifecycle.launch()
        client = McpStdioClient(lifecycle)
        client.initialize()

        results["probe"] = _run_gate("probe", lambda: _probe_gate(client, runtime))
        if runtime == "qoder":
            results["live_model_gate"] = _run_gate(
                "live_model_gate", lambda: asyncio.run(live_model_gate(client))
            )
            if results["live_model_gate"].get("gate") == "STOP":
                emit("qoder_stopped", clause=results["live_model_gate"].get("clause"))
                return results

        results["turn"], task_id = _run_gate("turn", lambda: _turn_gate(client, lifecycle, runtime))
        if isinstance(results["turn"], dict) and results["turn"].get("passed") and task_id:
            results["attribution"] = _run_gate(
                "attribution", lambda: _attribution_gate(client, lifecycle, task_id)
            )
    except (ToolError, RuntimeError, TimeoutError, OSError) as exc:
        emit("run_failed", runtime=runtime, error_class=type(exc).__name__, detail=str(exc)[:300])
        results["run_failed"] = gate_record(False, error_class=type(exc).__name__)
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            lifecycle.stop()
        except Exception:  # noqa: BLE001
            lifecycle.kill()
        # The cleanup gate runs *after* teardown: while the chain is alive the Bridge is
        # supposed to be there, and a gate run before stop would fail by construction.
        results["cleanup"] = _run_gate(
            "cleanup", lambda: _cleanup_gate(lifecycle, PROVIDER_IMAGES[runtime], baseline_children)
        )
        # A fully passed run's scene is disposable; a failed one keeps its evidence.
        present = [k for k in SUBSTANTIVE_GATES if k in results]
        if present and all(results[k].get("passed") for k in present):
            shutil.rmtree(tmp_root, ignore_errors=True)
        else:
            emit("scene_preserved", runtime=runtime, path=str(tmp_root))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runtimes", default=",".join(RUNTIMES), help="subset to smoke")
    args = parser.parse_args()

    require_wheel_environment()
    emit("environment", serverfs_python=str(SERVERFS_PYTHON), bridge_python=str(BRIDGE_VENV_PYTHON))

    selected = [r.strip() for r in args.runtimes.split(",") if r.strip()]
    unknown = [r for r in selected if r not in RUNTIMES]
    if unknown:
        raise SystemExit(f"unknown runtimes: {unknown}")

    all_results: dict[str, Any] = {}
    exit_code = 0
    for runtime in selected:
        # Codex is its real deployment shape: use_proxy=true with the operator's own Agent
        # proxy endpoint from .env, exactly as Phase E ran it (measured: the credentialless
        # CONNECT forwarder that proved Claude's proxy consumption in Phase G does not
        # interoperate with codex's client, and H5 needs no causality measurement -- Phase F
        # already proved the proxy is consumed). Claude and Qoder are direct on this host.
        use_proxy = runtime == "codex"
        results = _smoke_runtime(runtime, use_proxy=use_proxy)
        all_results[runtime] = results
        present = [k for k in SUBSTANTIVE_GATES if k in results]
        ok = bool(present) and all(results[k].get("passed") for k in present)
        stopped = results.get("live_model_gate", {}).get("gate") == "STOP"
        emit("runtime_verdict", runtime=runtime, passed=ok, model_gate_stopped=stopped)
        # A live-model STOP is an honest skip, not a failure -- the gate exists so the run never
        # pays for a model; a missing or unavailable free model stops the branch and reports.
        if not ok and not stopped:
            exit_code = 5

    emit(
        "verdict",
        answer="H5_PACKAGE_SMOKE_PASS" if exit_code == 0 else "H5_PACKAGE_SMOKE_FAIL",
        **all_results,
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
