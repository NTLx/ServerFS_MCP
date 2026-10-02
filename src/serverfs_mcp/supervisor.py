"""Minimal stdio supervisor that strips tunnel-only environment from ServerFS."""

# Keep this module limited to stdlib imports: it runs inside tunnel-client's
# inherited environment and must create the sanitized child environment before
# importing any ServerFS runtime module.
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


def _copy(source: BinaryIO, destination: BinaryIO, *, close_destination: bool = False) -> None:
    try:
        read = getattr(source, "read1", source.read)
        while chunk := read(64 * 1024):
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
    input_thread = threading.Thread(
        target=_copy,
        args=(sys.stdin.buffer, child.stdin),
        kwargs={"close_destination": True},
        daemon=True,
    )
    input_thread.start()
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
    # Construct command and sanitized environment before importing cli/runtime.
    env = sanitized_environment()
    command = [sys.executable, "-m", "serverfs_mcp.cli", "serve", "--config", args.config]
    return forward_stdio(command, env)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
