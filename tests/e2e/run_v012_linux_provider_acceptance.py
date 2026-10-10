#!/usr/bin/env python3
"""Linux v0.12 live provider acceptance against the current Agent Bridge source.

This driver intentionally bypasses the production deployment and creates one disposable Bridge
instance under a temporary directory. It proves provider behavior through the real local UDS RPC
contract while leaving the repository, provider configuration and managed services untouched.

Examples (run with the agent_bridge Python environment):

    python tests/e2e/run_v012_linux_provider_acceptance.py --runtime codex --case approval
    python tests/e2e/run_v012_linux_provider_acceptance.py --runtime qoder --case approval
    python tests/e2e/run_v012_linux_provider_acceptance.py --runtime claude --case spool
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from v012_connect_forwarder import ConnectForwarder

TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}
APPROVAL_MARKER = "approval-marker.txt"
DENY_MARKER = "deny-marker.txt"


def emit(event: str, **fields: Any) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=False, sort_keys=True), flush=True)


def runtime_binary(runtime: str) -> str:
    names = {"codex": "codex", "claude": "claude", "qoder": "qodercli"}
    resolved = shutil.which(names[runtime])
    if not resolved:
        raise RuntimeError(f"{names[runtime]} is not available on PATH")
    return str(Path(resolved).resolve())


class BridgeClient:
    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path
        self.next_id = 1

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = f"accept-{self.next_id}"
        self.next_id += 1
        payload = {
            "protocol_version": 1,
            "request_id": request_id,
            "method": method,
            "params": params,
        }
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            # A first proxy-mode Codex runtime probe may include the bounded 30s standalone
            # app-server startup plus its own initialize request. Keep the acceptance transport
            # outside that product budget so the driver observes the Bridge result rather than
            # racing its own transport timeout.
            conn.settimeout(60)
            conn.connect(str(self.socket_path))
            conn.sendall(encoded)
            chunks = bytearray()
            while not chunks.endswith(b"\n"):
                data = conn.recv(1_048_576)
                if not data:
                    raise RuntimeError("Bridge closed the RPC connection")
                chunks.extend(data)
        response = json.loads(bytes(chunks))
        if response.get("request_id") != request_id:
            raise RuntimeError("Bridge response request id mismatch")
        if response.get("ok") is not True:
            error = response.get("error") or {}
            raise RuntimeError(f"Bridge RPC failed: {error.get('code')}: {error.get('message')}")
        result = response.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("Bridge returned a non-object result")
        return result


def wait_task(
    client: BridgeClient,
    task_id: str,
    wanted: set[str],
    *,
    timeout: float = 180.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        last = client.call("task.get", {"task_id": task_id})
        if last.get("status") in wanted:
            return last
        time.sleep(0.1)
    raise TimeoutError(f"task did not reach {sorted(wanted)}; last={last}")


def submit(client: BridgeClient, runtime: str, prompt: str) -> str:
    params: dict[str, Any] = {
        "runtime": runtime,
        "workdir": "acceptance",
        "path": "",
        "profile": "workspace-write",
        "prompt": prompt,
        "correlation_id": f"v012-linux-{runtime}-{time.time_ns()}",
    }
    # Qoder's current account exposes qfmodel/Qwen3.8-Flash as the known free live model. Keep live
    # acceptance off paid models; absence/unavailability is reported honestly by the provider.
    if runtime == "qoder":
        params["model"] = "qfmodel"
    result = client.call("task.submit", params)
    return str(result["task_id"])


def approval_prompt(runtime: str, marker: str) -> str:
    command = (
        "git ls-remote https://github.com/openai/codex.git HEAD >/dev/null "
        f"&& printf ran > {marker}"
    )
    if runtime == "codex":
        return (
            "Before running any shell command, explicitly use your native request_permissions "
            "mechanism to request network access for this turn and wait for the human decision. "
            "Do not infer permission from the environment and do not run the command before that "
            "request is resolved. After approval, use Bash exactly once to run this exact command "
            f"as written:\n{command}\nDo not create the marker by any other mechanism. After the "
            "tool attempt, reply with a short status only."
        )
    return (
        "Use your normal Bash/shell tool exactly once to run the following command exactly as "
        "written. Do not split it, do not use another tool to create the marker, and do not bypass "
        "or suppress your native approval mechanism. If an approval is required, wait for it. "
        f"Command:\n{command}\nAfter the tool attempt, reply with a short status only."
    )


def run_one_approval(
    client: BridgeClient,
    workdir: Path,
    runtime: str,
    *,
    marker: str,
    decision: str,
) -> dict[str, Any]:
    target = workdir / marker
    target.unlink(missing_ok=True)
    task_id = submit(client, runtime, approval_prompt(runtime, marker))
    task = wait_task(client, task_id, {"waiting_for_approval", *TERMINAL})
    if task.get("status") != "waiting_for_approval":
        raise RuntimeError(
            f"provider emitted no approval request; status={task.get('status')} "
            f"error={task.get('error_code')}"
        )
    pending = task.get("pending_request")
    if not isinstance(pending, dict) or pending.get("kind") != "approval":
        raise RuntimeError("waiting task has no normalized approval request")
    request_id = pending.get("request_id")
    payload = pending.get("payload")
    if not isinstance(request_id, str) or not isinstance(payload, dict):
        raise RuntimeError("approval request has invalid shape")
    decisions = payload.get("available_decisions")
    if not isinstance(decisions, list) or decision not in decisions:
        raise RuntimeError(f"provider did not offer {decision}: {decisions}")

    emit(
        "approval_requested",
        runtime=runtime,
        task_id=task_id,
        decision=decision,
        category=payload.get("category"),
        available_decisions=decisions,
    )
    client.call(
        "task.approval.respond",
        {"task_id": task_id, "request_id": request_id, "decision": decision},
    )
    final = wait_task(client, task_id, TERMINAL)
    marker_exists = target.exists()
    if decision == "approve_once":
        if final.get("status") != "succeeded" or not marker_exists:
            raise RuntimeError(
                "approved action did not complete: "
                f"status={final.get('status')} marker={marker_exists}"
            )
        if target.read_text(encoding="utf-8") != "ran":
            raise RuntimeError("approved marker has unexpected content")
    elif marker_exists:
        raise RuntimeError("denied provider action still created its marker")
    return {
        "task_id": task_id,
        "status": final.get("status"),
        "marker_exists": marker_exists,
    }


def run_approval(client: BridgeClient, workdir: Path, runtime: str) -> None:
    approved = run_one_approval(
        client,
        workdir,
        runtime,
        marker=APPROVAL_MARKER,
        decision="approve_once",
    )
    denied = run_one_approval(
        client,
        workdir,
        runtime,
        marker=DENY_MARKER,
        decision="deny",
    )
    emit("approval_pass", runtime=runtime, approved=approved, denied=denied)


def run_proxy_completion(client: BridgeClient, runtime: str) -> dict[str, Any]:
    task_id = submit(
        client,
        runtime,
        "Do not use tools. Reply with one short sentence confirming the provider turn completed.",
    )
    task = wait_task(client, task_id, TERMINAL, timeout=300)
    if task.get("status") != "succeeded":
        events = client.call("task.events", {"task_id": task_id, "limit": 100})
        emit(
            "provider_failure",
            runtime=runtime,
            task_id=task_id,
            status=task.get("status"),
            error_code=task.get("error_code"),
            error_message=task.get("error_message"),
            events=events.get("events"),
        )
        raise RuntimeError(
            "provider completion failed: "
            f"status={task.get('status')} error={task.get('error_code')} "
            f"message={task.get('error_message')}"
        )
    return {"task_id": task_id, "status": task.get("status")}


def run_spool(client: BridgeClient, runtime: str, threshold: int) -> None:
    prompt = (
        "Do not use tools. Write a self-contained technical explanation of why deterministic "
        "acceptance tests matter for distributed systems. Use at least 1200 words of plain prose, "
        "with multiple paragraphs. Do not summarize or shorten it."
    )
    task_id = submit(client, runtime, prompt)
    task = wait_task(client, task_id, TERMINAL, timeout=300)
    if task.get("status") != "succeeded":
        raise RuntimeError(
            f"provider task failed: status={task.get('status')} error={task.get('error_code')}"
        )
    result = task.get("result")
    if not isinstance(result, dict) or result.get("storage") != "spool":
        size = result.get("size_bytes") if isinstance(result, dict) else None
        raise RuntimeError(
            f"real provider result did not cross spool threshold {threshold}; size={size}"
        )
    expected_size = result.get("size_bytes")
    expected_sha = result.get("sha256")
    if not isinstance(expected_size, int) or not isinstance(expected_sha, str):
        raise RuntimeError("spooled result metadata is incomplete")

    reconstructed = bytearray()
    offset = 0
    while True:
        chunk = client.call(
            "task.result.read",
            {"task_id": task_id, "offset_bytes": offset, "max_bytes": 65_536},
        )
        text = chunk.get("text")
        next_offset = chunk.get("next_offset_bytes")
        if not isinstance(text, str) or not isinstance(next_offset, int):
            raise RuntimeError("result chunk has invalid shape")
        reconstructed.extend(text.encode("utf-8"))
        offset = next_offset
        if chunk.get("eof") is True:
            break
    actual_sha = hashlib.sha256(reconstructed).hexdigest()
    if len(reconstructed) != expected_size or actual_sha != expected_sha:
        raise RuntimeError("reconstructed spool bytes do not match stored metadata")
    if task.get("final_response_truncated") is not True or result.get("retrievable") is not True:
        raise RuntimeError("spooled task did not expose the public retrieval contract")
    emit(
        "spool_pass",
        runtime=runtime,
        task_id=task_id,
        threshold=threshold,
        size_bytes=expected_size,
        sha256=expected_sha,
    )


def write_config(
    root: Path,
    workdir: Path,
    runtime: str,
    threshold: int,
    *,
    use_proxy: bool = False,
    proxy_url: str | None = None,
) -> Path:
    provider = runtime_binary(runtime)
    config: dict[str, Any] = {
        "socket_path": str(root / "bridge.sock"),
        "state_dir": str(root / "state"),
        "lock_dir": str(root / "locks"),
        "allowed_peer_uid": os.getuid(),
        "allowed_peer_gid": os.getgid(),
        "enable_fake_runtime": False,
        "limits": {
            "task_timeout_seconds": 300,
            "interaction_timeout_seconds": 120,
            "max_active_tasks": 1,
            "retention_seconds": 3600,
            "result_spool_threshold_bytes": threshold,
        },
        "codex": {"enabled": False},
        "claude": {"enabled": False},
        "qoder": {"enabled": False},
        "workdirs": [
            {
                "slot": 1,
                "alias": "acceptance",
                "host_path": str(workdir),
                "read_only": False,
                "agent_mode": "workspace-write",
                "agent_runtimes": [runtime],
            }
        ],
    }
    if use_proxy:
        if proxy_url is None:
            raise RuntimeError("proxy mode requires a proxy URL")
        config["proxy"] = {"url": proxy_url, "authenticated": False}

    if runtime == "codex":
        config["codex"] = {
            "enabled": True,
            "autostart": False,
            "codex_home": str(Path.home() / ".codex"),
            "codex_bin": provider,
            "use_proxy": use_proxy,
            "request_timeout_seconds": 15.0,
            "event_idle_timeout_seconds": None,
            "max_message_bytes": 128 * 1024 * 1024,
        }
    elif runtime == "claude":
        config["claude"] = {
            "enabled": True,
            "claude_bin": provider,
            "use_proxy": use_proxy,
            "probe_timeout_seconds": 5.0,
            "event_idle_timeout_seconds": None,
        }
    else:
        config["qoder"] = {
            "enabled": True,
            "qoder_bin": provider,
            "use_proxy": use_proxy,
            "probe_timeout_seconds": 5.0,
            "event_idle_timeout_seconds": None,
        }
    path = root / "bridge.json"
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return path


def wait_socket(path: Path, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Bridge exited before ready with code {process.returncode}")
        if path.exists():
            return
        time.sleep(0.05)
    raise TimeoutError("Bridge socket did not become ready")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runtime", choices=("codex", "claude", "qoder"), required=True)
    parser.add_argument(
        "--case",
        choices=("approval", "spool", "direct", "proxy"),
        required=True,
    )
    parser.add_argument("--threshold", type=int, default=1024)
    args = parser.parse_args()
    if sys.platform != "linux":
        raise SystemExit("this acceptance is Linux-only")
    if not 1 <= args.threshold <= 8 * 1024 * 1024:
        raise SystemExit("threshold must be between 1 and 8388608")

    forwarder: ConnectForwarder | None = None
    if args.case in {"direct", "proxy"}:
        forwarder = ConnectForwarder()
        forwarder.start()
        if forwarder.port is None:
            raise RuntimeError("CONNECT forwarder did not expose a port")

    try:
        with tempfile.TemporaryDirectory(prefix="serverfs-v012-linux-") as raw:
            root = Path(raw)
            workdir = root / "workdir"
            workdir.mkdir()
            subprocess.run(["git", "init", "-q", str(workdir)], check=True)
            (workdir / "README.md").write_text("v0.12 Linux acceptance\n", encoding="utf-8")
            proxy_url = (
                f"http://127.0.0.1:{forwarder.port}" if forwarder is not None else None
            )
            config_path = write_config(
                root,
                workdir,
                args.runtime,
                args.threshold,
                use_proxy=args.case == "proxy",
                proxy_url=proxy_url,
            )
            socket_path = root / "bridge.sock"
            stderr_path = root / "bridge.stderr"
            child_env = os.environ.copy()
            if args.case == "direct" and proxy_url is not None:
                # Prove direct mode does not inherit ambient proxy routing into provider children.
                child_env["HTTP_PROXY"] = proxy_url
                child_env["HTTPS_PROXY"] = proxy_url
                child_env["http_proxy"] = proxy_url
                child_env["https_proxy"] = proxy_url
            with stderr_path.open("wb") as stderr:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "serverfs_agent_bridge.main",
                        "--config",
                        str(config_path),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=stderr,
                    env=child_env,
                )
            try:
                wait_socket(socket_path, process)
                client = BridgeClient(socket_path)
                runtimes = client.call("runtime.list", {})
                emit("runtime", selected=args.runtime, reported=runtimes.get("runtimes"))
                if args.case == "approval":
                    run_approval(client, workdir, args.runtime)
                elif args.case == "spool":
                    run_spool(client, args.runtime, args.threshold)
                else:
                    completion = run_proxy_completion(client, args.runtime)
                    assert forwarder is not None
                    summary = forwarder.summary()
                    if args.case == "direct" and summary.total_connects != 0:
                        raise RuntimeError("direct Agent path unexpectedly used the proxy observer")
                    if args.case == "proxy" and summary.external_connects < 1:
                        raise RuntimeError(
                            "proxied Agent path produced no external CONNECT traffic"
                        )
                    emit(
                        "agent_proxy_pass",
                        runtime=args.runtime,
                        mode=args.case,
                        completion=completion,
                        total_connects=summary.total_connects,
                        external_connects=summary.external_connects,
                        loopback_connects=summary.loopback_connects,
                    )
            except Exception:
                stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace")[-4000:]
                if stderr_text:
                    emit("bridge_stderr", text=stderr_text)
                raise
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)
    finally:
        if forwarder is not None:
            forwarder.stop()
    emit("verdict", answer="PASS", runtime=args.runtime, case=args.case)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
