"""Independent I6 two-named-Connector installed-state postflight.

Read-only: attest native Task Scheduler XML, protected installed wheel,
private fake-token paths, lack of running owners, and unchanged I4
production/fixture baseline. No network or service lifecycle mutation.
"""

from __future__ import annotations

import ctypes
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


class I6PostflightError(RuntimeError):
    """Sanitized postflight refusal: never print paths or tokens."""


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise I6PostflightError(reason)


def helper(filename: str, name: str):
    source = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(name, source)
    require(spec is not None and spec.loader is not None, "I6 postflight helper is absent")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    from serverfs_mcp.windows_v25 import readiness, storage
    from serverfs_mcp.windows_v25.spec import Manifest
    from serverfs_mcp.windows_v25.tasks import Scheduler

    require(
        sys.platform == "win32"
        and os.name == "nt"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin(),
        "I6 postflight needs native non-elevated Python -I -B",
    )
    stage = helper("v0250-i6-named-connectors-stage.py", "i6_installed_source")
    require(
        stage.INSTANCE == "i4-a" and stage.IDS == ("i6-edge-a", "i6-edge-b"),
        "I6 fixed identities changed",
    )
    gate = helper("v0250-i4-postflight-readonly.py", "i6_protected_postflight")
    gate.main()
    stage.fixture_tasks(installed=True)
    print("I6_PROTECTED_SCHEDULER_AND_TWO_DISABLED_TASKS=PASS")

    sid = storage._sid()
    base = storage.root(stage.INSTANCE)
    runtime = base / "envs/cloudflare"
    interpreter = runtime / "Scripts/python.exe"
    launcher = runtime / "Scripts/pythonw.exe"
    for file in (interpreter, launcher):
        storage.check_private(file, directory=False, sid=sid)
    require(
        storage.pinned_pythonw("cloudflare", stage.INSTANCE) == launcher,
        "I6 installed Connector interpreter pin differs",
    )
    # Exact source = installed wheel snapshot, without importing from checkout
    # inside the installed child. Source files are never overwritten here.
    for name in ("spec.py", "storage.py", "cli.py", "runtime.py", "readiness.py", "isolation.py"):
        installed = runtime / "Lib/site-packages/serverfs_mcp/windows_v25" / name
        source = REPO / "src/serverfs_mcp/windows_v25" / name
        storage.check_private(installed, directory=False, sid=sid)
        require(installed.read_bytes() == source.read_bytes(), "I6 installed source parity differs")
    print("I6_INSTALLED_RUNTIME_EXACT_SOURCE=PASS")

    expected_binary = base / "services/cloudflare/bin/cloudflared.exe"
    storage.check_private(expected_binary, directory=False, sid=sid)
    require(
        expected_binary.read_bytes() == b"DISABLED_I6_NON_EXECUTABLE_FIXTURE\n",
        "I6 unexpected real tunnel executable",
    )
    expected = set(stage.IDS)
    manifests = tuple(
        storage.load("cloudflare", stage.INSTANCE, connector_id) for connector_id in stage.IDS
    )
    require(
        all(isinstance(item, Manifest) for item in manifests)
        and {item.connector_id for item in manifests} == expected
        and {item.task_name() for item in manifests}
        == {f"ServerFS v25 i4-a Cloudflare Connector {connector_id}" for connector_id in expected},
        "I6 installed Connector manifest identity differs",
    )
    require(
        {
            item.connector_id
            for item in storage.component_inventory("cloudflare")
            if item.instance_id == stage.INSTANCE
        }
        == expected,
        "I6 installed Connector inventory is not exact",
    )
    for manifest in manifests:
        require(
            manifest.pythonw == str(launcher)
            and Path(str(manifest.settings["cloudflared"])) == expected_binary,
            "I6 named Connector pin differs",
        )
        token = Path(str(manifest.settings["token_file"]))
        storage.check_private(token, directory=False, sid=sid)
        require(
            token
            == storage.home("cloudflare", stage.INSTANCE, manifest.connector_id) / "fixture.token"
            and token.read_bytes()
            == ("DISABLED_I6_FAKE_TOKEN_" + manifest.connector_id + "\n").encode("ascii"),
            "I6 named Connector credential is not a fake isolated token",
        )
        service_home = storage.home("cloudflare", stage.INSTANCE, manifest.connector_id)
        for name in ("task.xml", "service.json"):
            storage.check_private(service_home / name, directory=False, sid=sid)
        status = Scheduler().status(manifest)
        require(
            status.get("registered") is True
            and status.get("enabled") is False
            and status.get("state") == "Disabled"
            and status.get("running") is False
            and status.get("readiness") == "offline"
            and readiness._current_identity(manifest) is None,
            "I6 named Connector is not disabled/offline",
        )
        result = subprocess.run(
            [
                str(interpreter),
                "-I",
                "-B",
                "-m",
                "serverfs_mcp.windows_v25.cli",
                "cloudflare",
                "status",
                "--instance",
                stage.INSTANCE,
                "--connector-id",
                manifest.connector_id,
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        require(result.returncode == 0, "I6 private installed CLI status refused")
        try:
            row = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise I6PostflightError("I6 private CLI returned malformed status") from exc
        require(
            isinstance(row, dict)
            and row.get("task_name") == manifest.task_name()
            and row.get("state") == "Disabled"
            and row.get("enabled") is False
            and row.get("running") is False
            and row.get("readiness") == "offline",
            "I6 private installed CLI status differs",
        )
    print("I6_TWO_PRIVATE_PLACEHOLDERS_NEVER_PUBLIC=PASS")
    print("I6_TWO_INSTALLED_CLI_STATUSES_DISABLED=PASS")
    print("I6_INSTALLED_NAMED_CONNECTORS_POSTFLIGHT=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:
        print("I6_POSTFLIGHT=FAILED type=" + type(exc).__name__, file=sys.stderr)
        raise SystemExit(2) from None
