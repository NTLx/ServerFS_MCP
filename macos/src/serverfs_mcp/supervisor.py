"""macOS stdio supervisor; launchd owns the independent persistent Bridge.

Keep this module stdlib-only. The tunnel's environment is sanitized before
starting the Core child, regardless of whether its configuration enables Agent.
The supervisor does not launch or own an Agent Bridge.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from collections.abc import Sequence
from typing import BinaryIO


def sanitized_environment(source: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if source is None else source)
    prefixes = ("CONTROL_PLANE_", "TUNNEL_CLIENT_", "OPENAI_", "MCP_", "SERVERFS_PROXY_")
    proxy_names = {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}
    return {
        key: value
        for key, value in env.items()
        if not key.upper().startswith(prefixes) and key.lower() not in proxy_names
    }


def _copy(
    source: BinaryIO,
    destination: BinaryIO,
    *,
    close_destination: bool = False,
    eof_event: threading.Event | None = None,
) -> None:
    try:
        read = getattr(source, "read1", source.read)
        while True:
            chunk = read(64 * 1024)
            if not chunk:
                if eof_event is not None:
                    eof_event.set()
                break
            destination.write(chunk)
            destination.flush()
    except (BrokenPipeError, OSError):
        pass
    finally:
        if close_destination:
            try:
                destination.close()
            except OSError:
                pass


def _start_stdin_forwarder(destination: BinaryIO) -> threading.Event:
    """Start the existing macOS buffered stdin forwarding path."""
    eof = threading.Event()
    source = getattr(sys.stdin, "buffer", sys.stdin)
    threading.Thread(
        target=_copy,
        args=(source, destination),
        kwargs={"close_destination": True, "eof_event": eof},
        daemon=True,
        name="stdin-forward-buffered",
    ).start()
    return eof


def forward_stdio(command: Sequence[str], env: dict[str, str] | None = None) -> int:
    """Start a separate child and forward its protocol streams transparently."""
    child = subprocess.Popen(
        list(command),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=None,
        env=sanitized_environment() if env is None else env,
        bufsize=0,
    )
    assert child.stdin is not None and child.stdout is not None
    _start_stdin_forwarder(child.stdin)
    try:
        _copy(child.stdout, sys.stdout.buffer)
        return child.wait()
    except BaseException:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        raise


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="serverfs-supervisor")
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)

    env = sanitized_environment()
    command = [sys.executable, "-m", "serverfs_mcp.cli", "serve", "--config", args.config]
    return forward_stdio(command, env)


if __name__ == "__main__":  # pragma: no cover
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException as _exc:  # noqa: BLE001 - the last line of defence for the launcher
        # Nothing may reach the operator as a traceback from this entry point: the process is
        # launched by a supervisor whose own diagnostics are the operator's only window into a
        # failure. The class name identifies the failure without leaking a path.
        sys.stderr.write(f"serverfs-supervisor: {type(_exc).__name__}\n")
        raise SystemExit(2) from None
