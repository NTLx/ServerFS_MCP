"""Read-only real-host check of owned Bridge preflight against the default policy.

Creates a synthetic temporary candidate and prints only redacted pass/refusal.
No registration, service mutation, provider execution or Git operation.
"""

from __future__ import annotations

import json
import secrets
import tempfile
from pathlib import Path

from serverfs_mcp.darwin_instance_bridge_identity import (
    DarwinBridgeIdentityError,
    preflight_named_owned_bridge,
)
from serverfs_mcp.instance_identity import InstanceIdentity


def main() -> int:
    identifier = "v25-preflight-" + secrets.token_hex(4)
    identity = InstanceIdentity(instance_id=identifier, bridge_mode="owned", bridge_id=identifier)
    with tempfile.TemporaryDirectory(prefix="serverfs-v25-owned-preflight-") as temp:
        root = Path(temp).resolve()
        workdir = root / "synthetic-workdir"
        workdir.mkdir(mode=0o700)
        config = root / "serverfs.toml"
        config.write_text(
            "[agent]\nenabled = true\n"
            "[agent.codex]\nenabled = true\n"
            "[[workdirs]]\nalias = 'synthetic'\n"
            f"path = '{workdir}'\n"
            "read_only = false\n"
            "agent_mode = 'workspace-write'\n"
            "agent_runtimes = ['codex']\n",
            encoding="utf-8",
        )
        try:
            result = preflight_named_owned_bridge(identity, config)
        except (DarwinBridgeIdentityError, ValueError, OSError) as exc:
            print(
                json.dumps(
                    {
                        "result": "REFUSED",
                        "error_type": type(exc).__name__,
                        "reason": str(exc),
                        "production_service_changed": False,
                    },
                    sort_keys=True,
                )
            )
            return 1
        print(
            json.dumps(
                {
                    **result,
                    "production_service_changed": False,
                    "synthetic_state_retained": False,
                },
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
