"""Test-only tunnel-client for the D9 native lifecycle acceptance.

The real tunnel-client is an external signed binary that speaks the Control Plane protocol to a
remote service. D9 is not testing that protocol -- it is testing the **launch chain** that runs
inside it:

    serverfs tunnel -> native_tunnel -> tunnel-client --mcp.command -> supervisor -> ...

So this stand-in accepts the same argv ``native_tunnel.run_native_tunnel`` builds, decodes
``--mcp.command`` with the *production* decoder, and execs the result with stdio inherited. Every
process downstream of it is the real implementation: the real supervisor, the real renderer, the
real Bridge, the real Named Pipe and the real MCP surface.

Two properties are deliberate rather than incidental:

**It really receives and decodes ``--mcp.command``.** Bypassing that step would skip the encoding
contract, and the encoding is Windows-specific -- ``encode_tunnel_command_argv`` exists precisely
because naive joining breaks on paths with spaces. The decoder here is the inverse of the
production encoder rather than a ``shlex`` call, so a mismatch in either direction fails loudly.

**It forwards its own environment, not a curated one.** The launcher chain is supposed to scrub the
Tunnel and Control Plane namespaces before the Bridge sees them, and that can only be observed if
    the
process at the top of the chain actually carries them. A harness that handed the supervisor a clean
environment would make the scrub untestable.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys


def decode_tunnel_command_argv(entry: str) -> list[str]:
    """The inverse of ``native_tunnel.encode_tunnel_command_argv``.

    The production encoder quotes every argument and escapes backslashes and double quotes, because
    tunnel-client's own parser is not shell-compatible and a Windows path would otherwise be split
        on
    its spaces. This walks that exact grammar rather than delegating to a shell, which is the point:
    a harness that shelled out would pass even if the encoder were wrong.
    """
    argv: list[str] = []
    current: list[str] = []
    inside = False
    index = 0
    while index < len(entry):
        char = entry[index]
        if char == '"':
            inside = not inside
        elif char == "\\" and index + 1 < len(entry):
            # The encoder escaped exactly backslashes and quotes; consume the pair verbatim.
            current.append(entry[index + 1])
            index += 2
            continue
        elif char == " " and not inside:
            if current:
                argv.append("".join(current))
                current = []
        else:
            current.append(char)
        index += 1
    if current:
        argv.append("".join(current))
    return argv


def main(argv: list[str]) -> int:
    command: list[str] | None = None
    for index, item in enumerate(argv):
        if item == "--mcp.command" and index + 1 < len(argv):
            command = decode_tunnel_command_argv(argv[index + 1])
            break
    if not command:
        sys.stderr.write("fake-tunnel-client: no --mcp.command was supplied\n")
        return 2

    # Recorded so a test can assert the launcher really did reach a child, and with which argv and
    # which environment.
    record = os.environ.get("SERVERFS_TEST_TUNNEL_RECORD")
    if record:
        forbidden = (
            "CONTROL_PLANE_",
            "TUNNEL_CLIENT_",
            "MCP_",
            "OPENAI_",
            "SERVERFS_PROXY_",
        )
        proxy_names = {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}
        with open(record, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "argv": command,
                    "received_argv": argv,
                    "env_names": sorted(os.environ),
                    # What the launcher handed this process. The production scrub in
                    # native_tunnel is expected to have removed these namespaces already, so the
                    # record is how a test observes that rather than assuming it.
                    "leaked_prefixes": sorted(
                        {
                            prefix
                            for name in os.environ
                            for prefix in forbidden
                            if name.upper().startswith(prefix)
                        }
                    ),
                    "leaked_proxy_names": sorted(
                        name for name in os.environ if name.lower() in proxy_names
                    ),
                },
                handle,
            )

    # stdio is inherited rather than piped: the MCP frames must flow through this process the way
    # they would through the real tunnel-client, and a pipe here would be a second transport that
    # production does not have.
    completed = subprocess.run(command, env=os.environ.copy(), check=False)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
