"""I7 final optional handoff: register two *disabled* real Connector tasks.

Only after a trusted operator independently placed TWO distinct, real,
user-SID-private Tunnel tokens into the prepared connector homes.
No API, DNS, cloudflared subprocess, Core/Bridge start or token output.
The CLI audit deliberately reports PENDING while credentials are absent.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import ctypes
import hashlib
import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from uuid import UUID

INSTANCE = "i7-public"
IDS = ("i7-edge-a", "i7-edge-b")
# Actual non-secret UUIDs of the two independent I7 nonproduction
# remotely managed Cloudflare Tunnels. Never accept a peer or production token.
EXPECTED_TUNNELS = {
    "i7-edge-a": "aad24669-98ae-4eea-a54c-d89b872981a5",
    "i7-edge-b": "85ea7057-ea6c-4d2e-b71a-63074fa31441",
}
TOKEN_NAME = "tunnel.token"
PHASE = "entry"
REPO = Path(__file__).resolve().parents[2]


class I7RegistrationRefusal(RuntimeError):
    """Nonsecret refusal of an incomplete/untrusted private handoff."""


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise I7RegistrationRefusal(reason)


def helper(name: str, module: str):
    file = Path(__file__).with_name(name)
    spec = importlib.util.spec_from_file_location(module, file)
    require(spec is not None and spec.loader is not None, "fixed helper is unavailable")
    object_ = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(object_)
    return object_


def host() -> None:
    require(
        os.name == "nt"
        and sys.platform == "win32"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin(),
        "I7 requires native non-elevated Python -I -B",
    )


def base() -> tuple[object, Path, Path]:
    from serverfs_mcp.windows_v25 import storage

    host()
    prepare = helper("v0250-i7-connector-prepare.py", "i7_register_owner")
    require(
        prepare.INSTANCE == INSTANCE and prepare.CONNECTORS == IDS,
        "I7 prepared Connector identities differ",
    )
    # This *stage-2* protected check does not require homes to be empty.
    # Do not call prepare.postflight() after credential provision.
    prepare._installed_core_and_binary()
    prepare.candidates_absent()
    root = storage.root(INSTANCE)
    sid = storage._sid()
    owner = root / "envs/cloudflare"
    for path in (
        root / "services/cloudflare/connectors",
        root / "build/cloudflare-owner",
        owner,
    ):
        storage.check_private(path, directory=True, sid=sid)
    for name in IDS:
        storage.check_private(
            storage.home("cloudflare", INSTANCE, name),
            directory=True,
            sid=sid,
        )
    require(
        {item.name for item in (root / "services/cloudflare/connectors").iterdir()} == set(IDS),
        "I7 unexpected Connector identity in private directory",
    )
    for exe in ("python.exe", "pythonw.exe"):
        storage.check_private(owner / "Scripts" / exe, directory=False, sid=sid)
    for rel in (
        "windows_v25/spec.py",
        "windows_v25/runtime.py",
        "windows_v25/cli.py",
        "windows_v25/isolation.py",
    ):
        installed = owner / "Lib/site-packages/serverfs_mcp" / rel
        source = REPO / "src/serverfs_mcp" / rel
        storage.check_private(installed, directory=False, sid=sid)
        require(installed.read_bytes() == source.read_bytes(), "I7 owner code parity differs")
    from serverfs_mcp.windows_v25 import storage as private_storage

    require(
        not any(
            peer.instance_id == INSTANCE
            for peer in private_storage.component_inventory("cloudflare")
        ),
        "I7 Connector identity is already configured",
    )
    return prepare, root, owner


def private_token_record(path: Path, *, sid: str) -> tuple[bytes, str, str]:
    """Check Cloudflared's Base64 JSON Token envelope without disclosing secrets.

    Upstream cloudflared TunnelToken encodes `{"a": account, "s": secret,
    "t": UUID, "e": optional endpoint}` as standard Base64 JSON.
    Decoding its fields is NOT cryptographic or Cloudflare account proof.
    """
    from serverfs_mcp.windows_v25 import storage

    storage.check_private(path, directory=False, sid=sid)
    size = path.stat().st_size
    require(80 <= size <= 4096, "I7 token file size is invalid")
    content = path.read_bytes()
    require(
        len(content) == size
        and content.startswith(b"eyJ")
        and re.fullmatch(rb"[A-Za-z0-9+/=]+", content) is not None,
        "I7 Tunnel token format does not pass minimum sanity check",
    )
    try:
        payload = json.loads(base64.b64decode(content, validate=True))
        if type(payload) is not dict or set(payload) not in ({"a", "s", "t"}, {"a", "s", "t", "e"}):
            raise ValueError("invalid Tunnel envelope")
        account, tunnel, secret = payload["a"], payload["t"], payload["s"]
        if (
            type(account) is not str
            or re.fullmatch(r"[0-9a-fA-F]{32}", account) is None
            or type(tunnel) is not str
            or str(UUID(tunnel)) != tunnel.lower()
            or type(secret) is not str
        ):
            raise ValueError("invalid Tunnel identity")
        raw_secret = base64.b64decode(secret, validate=True)
        if not 16 <= len(raw_secret) <= 128:
            raise ValueError("invalid Tunnel secret")
        if "e" in payload and type(payload["e"]) is not str:
            raise ValueError("invalid optional endpoint")
    except (ValueError, UnicodeError, TypeError, KeyError, binascii.Error):
        raise I7RegistrationRefusal("I7 Tunnel token envelope is malformed") from None
    # Return ONLY a hash and public account/Tunnel identity for local comparison.
    # The secret, decoded bytes, or complete payload are never logged/persisted.
    return hashlib.sha256(content).digest(), account.lower(), tunnel.lower()


def private_token_digest(path: Path, *, sid: str) -> bytes:
    return private_token_record(path, sid=sid)[0]


def token_gate(*, allow_missing: bool) -> bool:
    from serverfs_mcp.windows_v25 import storage

    host()
    digests: set[bytes] = set()
    tunnels: set[str] = set()
    account: str | None = None
    found = 0
    for connector in IDS:
        home = storage.home("cloudflare", INSTANCE, connector)
        storage.check_private(home, directory=True)
        expected = home / TOKEN_NAME
        entries = tuple(home.iterdir())
        require(
            all(p == expected for p in entries),
            "I7 Connector home contains unrecognized artifacts",
        )
        if not entries:
            continue
        digest, token_account, tunnel_id = private_token_record(expected, sid=storage._sid())
        require(digest not in digests, "I7 Tunnel Tokens are duplicated")
        require(tunnel_id not in tunnels, "I7 Tunnel UUID is reused by both Connectors")
        require(
            tunnel_id == EXPECTED_TUNNELS[connector],
            "I7 credential is for the wrong named Cloudflare Tunnel",
        )
        require(account is None or account == token_account, "I7 Tunnel accounts differ")
        digests.add(digest)
        tunnels.add(tunnel_id)
        account = token_account
        found += 1
    require(allow_missing or found == len(IDS), "two private Tunnel Tokens are required")
    if found < len(IDS):
        print("I7_TOKEN_HANDOFF=AWAITING_PRIVATE_PROVISION")
        return False
    print("I7_TWO_PRIVATE_DISTINCT_TOKENS=VERIFIED_FILE_ONLY")
    return True


def audit() -> bool:
    base()
    has_tokens = token_gate(allow_missing=True)
    print("I7_DISABLED_CONNECTOR_REGISTER_AUDIT=PASS")
    return has_tokens


def stage() -> None:
    global PHASE
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.cli import _assert_instance_isolation, _preflight
    from serverfs_mcp.windows_v25.spec import Manifest
    from serverfs_mcp.windows_v25.tasks import Scheduler

    PHASE = "private-handoff-preflight"
    _prepare, root, owner = base()
    require(token_gate(allow_missing=False), "I7 credentials are incomplete")
    core = storage.load("core", INSTANCE)
    sid = storage._sid()
    executable = root / "services/cloudflare/bin/cloudflared.exe"
    storage.check_private(executable, directory=False, sid=sid)
    manifests = []
    PHASE = "sealed-connector-manifests"
    for connector in IDS:
        token = storage.home("cloudflare", INSTANCE, connector) / TOKEN_NAME
        manifest = Manifest.parse(
            {
                "schema": 2,
                "component": "cloudflare",
                "instance_id": INSTANCE,
                "connector_id": connector,
                "sid": sid,
                "pythonw": str(owner / "Scripts/pythonw.exe"),
                "settings": {
                    "cloudflared": str(executable),
                    "token_file": str(token),
                    "port": core.settings["port"],
                    "hostname": core.settings["hostname"],
                },
            }
        )
        _preflight(manifest)
        _assert_instance_isolation(manifest)
        manifests.append(manifest)
    require(
        len({m.task_name() for m in manifests}) == len(IDS),
        "I7 Connector Scheduler identities overlap",
    )
    for manifest in manifests:
        storage.stage(manifest)
    print("I7_TWO_PROTECTED_MANIFESTS=PASS")

    PHASE = "create-only-disabled-task-registration"
    scheduler = Scheduler()
    for manifest in manifests:
        result = scheduler.install(manifest)
        require(
            result.get("registered") is True
            and result.get("running") is False
            and result.get("enabled") is False,
            "I7 Connector registered unexpectedly enabled",
        )
    PHASE = "postflight"
    postflight()
    print("I7_TWO_REAL_CONNECTORS_REGISTERED_DISABLED=PASS")


def postflight() -> None:
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.readiness import _current_identity
    from serverfs_mcp.windows_v25.spec import Manifest
    from serverfs_mcp.windows_v25.tasks import Scheduler

    host()
    prepare = helper("v0250-i7-connector-prepare.py", "i7_register_postflight_owner")
    prepare._installed_core_and_binary()
    root = storage.root(INSTANCE)
    sid = storage._sid()
    owner = root / "envs/cloudflare"
    manifests = []
    digests: set[bytes] = set()
    tunnels: set[str] = set()
    account: str | None = None
    require(
        {
            m.connector_id
            for m in storage.component_inventory("cloudflare")
            if m.instance_id == INSTANCE
        }
        == set(IDS),
        "I7 public Connector manifests not exact",
    )
    for name in IDS:
        manifest = storage.load("cloudflare", INSTANCE, name)
        require(
            isinstance(manifest, Manifest)
            and manifest.connector_id == name
            and manifest.pythonw == str(owner / "Scripts/pythonw.exe")
            and manifest.settings["hostname"] == "i7-public.local"
            and manifest.settings["port"] == 18875
            and manifest.settings["cloudflared"]
            == str(root / "services/cloudflare/bin/cloudflared.exe"),
            "I7 protected Connector manifest identity changed",
        )
        path = storage.home("cloudflare", INSTANCE, name) / TOKEN_NAME
        require(
            manifest.settings["token_file"] == str(path),
            "I7 Connector credential path differs",
        )
        digest, token_account, tunnel_id = private_token_record(path, sid=sid)
        require(digest not in digests, "I7 Connector Tokens duplicated")
        require(tunnel_id not in tunnels, "I7 Tunnel UUID is reused by both Connectors")
        require(
            tunnel_id == EXPECTED_TUNNELS[name],
            "I7 credential is for the wrong named Cloudflare Tunnel",
        )
        require(account is None or account == token_account, "I7 Tunnel accounts differ")
        digests.add(digest)
        tunnels.add(tunnel_id)
        account = token_account
        status = Scheduler().status(manifest)
        require(
            status.get("registered") is True
            and status.get("state") == "Disabled"
            and status.get("enabled") is False
            and status.get("running") is False
            and status.get("readiness") == "offline"
            and _current_identity(manifest) is None,
            "I7 Connector task is not Disabled/offline",
        )
        manifests.append(manifest)
    require(len(manifests) == len(IDS), "I7 Connector task count invalid")
    print("I7_TWO_INSTALLED_CONNECTOR_IDENTITIES=PASS")
    print("I7_TWO_CONNECTOR_TASKS_DISABLED_OFFLINE=PASS")
    print("I7_CONNECTOR_REGISTER_POSTFLIGHT=PASS")


def main() -> None:
    parser = argparse.ArgumentParser()
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--audit", action="store_true")
    actions.add_argument("--stage", action="store_true")
    actions.add_argument("--postflight", action="store_true")
    args = parser.parse_args()
    if args.audit:
        audit()
    elif args.stage:
        stage()
    else:
        postflight()


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - never render Token or absolute paths
        print(
            "I7_CONNECTOR_REGISTER=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None
