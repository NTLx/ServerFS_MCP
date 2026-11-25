"""macOS launchd-owned supervisor for a named independent Agent Bridge.

This is the only Bridge process entry point for named owned instances.
Never mutates another instance or invokes SMAppService. The service launcher
execs this supervisor, which holds the private bootstrap pipe to a child
Bridge and closes it on shutdown. Recovery containment is deliberately NOT
claimed: no process-tree proof is inferred from a clean parent exit.
"""

from __future__ import annotations

import argparse
import json
import signal
import subprocess
import sys
import threading
from pathlib import Path

from .agent_lifecycle import bootstrap_frame_bytes, bridge_child_environment
from .darwin_instance_owned_binding import resolve_owned_core_binding
from .darwin_instance_runtime import _check_private_file
from .darwin_instances import instance_service_home
from .instance_identity import InstanceIdentity


class OwnedBridgeSupervisorError(ValueError):
    """An owned Bridge cannot be supervised with verified identity."""


def verified_bridge_command(
    identity: InstanceIdentity,
    *,
    home: Path | None = None,
    default_bridge_config: Path | None = None,
    environ: dict[str, str] | None = None,
) -> tuple[str, ...]:
    """Read-only validation of the exact owner, policy and Bridge child argv."""
    root = instance_service_home(identity, home=home)
    path = root / "services.json"
    try:
        _check_private_file(path)
        document = json.loads(path.read_text(encoding="utf-8"))
        core = document["core"]
        port = core["port"]
        config = core["config"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise OwnedBridgeSupervisorError("owned Bridge manifest is not trustworthy") from exc
    try:
        from .darwin_owned_v2_bootstrap import load_owned_v2_clients, owned_protocol_marker

        protocol, verified_manifest = owned_protocol_marker(identity, home=home)
        if verified_manifest != document:
            raise ValueError("owned Bridge manifest changed during verification")
        if protocol == "v2":
            if type(port) is not int:
                raise ValueError("invalid owned Core port")
            load_owned_v2_clients(identity, Path(config), home=home)
        else:
            resolve_owned_core_binding(
                identity,
                Path(config),
                port,
                home=home,
                default_bridge_config=default_bridge_config,
                environ=environ,
            )
        bridge_spec = document["services"]["agent-bridge"]
        python = bridge_spec["bridge_python"]
        if (
            not isinstance(python, str)
            or not Path(python).is_absolute()
            or (protocol == "v2") != (bridge_spec.get("protocol_mode") == "v2")
        ):
            raise ValueError("invalid Bridge interpreter")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise OwnedBridgeSupervisorError(
            "owned Bridge policy or executable identity failed"
        ) from exc
    from .darwin_instance_bridge_identity import named_owned_bridge_resources

    resources = named_owned_bridge_resources(identity, home=home)
    return (
        python,
        "-B",
        "-m",
        "serverfs_agent_bridge.main",
        "--config",
        str(resources.config_path),
        "--supervised",
    )


def _graceful_stop(child: subprocess.Popen[bytes]) -> None:
    if child.stdin is not None and not child.stdin.closed:
        try:
            child.stdin.close()
        except OSError:
            pass
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)


def supervise(identity: InstanceIdentity) -> int:
    if sys.platform != "darwin":
        raise OwnedBridgeSupervisorError("owned Bridge launchd supervisor requires macOS")
    argv = verified_bridge_command(identity)
    # Copy only the already established sanitized child environment. The
    # Bridge's proxy credentials never travel through inherited env fields.
    environment = bridge_child_environment(None)
    try:
        child = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=None,
            stderr=None,
            env=environment,
            close_fds=True,
        )
    except OSError as exc:
        raise OwnedBridgeSupervisorError("owned Bridge child could not be started") from exc
    stop_event = threading.Event()

    def signal_stop(_sig: int, _frame: object) -> None:
        stop_event.set()

    old_term = signal.signal(signal.SIGTERM, signal_stop)
    old_int = signal.signal(signal.SIGINT, signal_stop)
    try:
        assert child.stdin is not None
        # No lock/job object proofs on macOS: NEVER claim that previous
        # provider execution was contained and fully stopped.
        child.stdin.write(bootstrap_frame_bytes(None, prior_bridge_execution_stopped=False))
        child.stdin.flush()
        while child.poll() is None and not stop_event.wait(0.2):
            pass
        if stop_event.is_set():
            return 0
        # Unsolicited Bridge exit is not a successful supervised lifecycle.
        return child.returncode if child.returncode not in (None, 0) else 1
    finally:
        _graceful_stop(child)
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="serverfs-owned-bridge-supervisor")
    parser.add_argument("--instance", required=True)
    args = parser.parse_args(argv)
    try:
        identity = InstanceIdentity(
            instance_id=args.instance, bridge_mode="owned", bridge_id=args.instance
        )
        return supervise(identity)
    except (OSError, ValueError, BrokenPipeError):
        # Never leak paths, command parameters, private config or credentials.
        print("owned Bridge supervision refused", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
