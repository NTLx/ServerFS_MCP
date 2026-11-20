"""Configure one fresh named Windows ingress owner, without registration or start.

External Tunnel creation/routing and credential issuance are operator concerns.
Only private credential files and pinned, operator-trusted binaries are accepted.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.metadata as metadata
import importlib.util
import json
import os
import re
import secrets
import sys
from pathlib import Path
from uuid import UUID

VERSION = "0.25.0"


class Refusal(ValueError):
    """Nonsecret refusal."""


def require(ok, message):
    if not ok:
        raise Refusal(message)


def private_helper():
    path = Path(__file__).with_name("configure-v25-private.py")
    spec = importlib.util.spec_from_file_location("v25_initial_private", path)
    require(spec is not None and spec.loader is not None, "private helper missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--instance", required=True)
    p.add_argument("--connector-id", required=True)
    p.add_argument("--component", required=True, choices=("cloudflare", "openai"))
    p.add_argument("--cloudflared", required=True, type=Path)
    p.add_argument("--cloudflared-sha256", required=True)
    p.add_argument("--credential-file", required=True, type=Path)
    p.add_argument("--tunnel-client", type=Path)
    p.add_argument("--tunnel-client-sha256")
    p.add_argument("--tunnel-id")
    return p


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "ambiguous JSON document")
        result[key] = value
    return result


def single_line(data):
    require(type(data) is bytes and len(data) <= 4098, "credential size invalid")
    if data.endswith(b"\r\n"):
        data = data[:-2]
    elif data.endswith(b"\n"):
        data = data[:-1]
    require(
        32 <= len(data) <= 4096 and all(33 <= b <= 126 for b in data),
        "credential must be a single ASCII value",
    )
    return data


def cloudflare_token(data):
    data = single_line(data)
    try:
        info = json.loads(base64.b64decode(data, validate=True), object_pairs_hook=unique_pairs)
        require(
            type(info) is dict and set(info) in ({"a", "s", "t"}, {"a", "s", "t", "e"}),
            "Cloudflare token envelope invalid",
        )
        require(
            type(info["a"]) is str
            and re.fullmatch(r"[0-9a-fA-F]{32}", info["a"])
            and type(info["t"]) is str
            and str(UUID(info["t"])) == info["t"].lower()
            and type(info["s"]) is str
            and 16 <= len(base64.b64decode(info["s"], validate=True)) <= 128
            and ("e" not in info or type(info["e"]) is str),
            "Cloudflare token envelope invalid",
        )
        return data, info["t"].lower()
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise Refusal("Cloudflare token envelope invalid") from None


def header_for(auth):
    key = auth.get("SERVERFS_HTTP_API_KEY", "")
    require(
        isinstance(key, str)
        and re.fullmatch(r"[A-Za-z0-9_-]{40,4096}", key)
        and auth.get("SERVERFS_OAUTH_ENABLED", "false").lower() == "false",
        "OpenAI requires supported Core bearer authorization",
    )
    return ("Bearer " + key).encode("ascii")


def options(args):
    from serverfs_mcp.instance_identity import DEFAULT_INSTANCE_ID, valid_id

    require(valid_id(args.instance) != DEFAULT_INSTANCE_ID, "named Core required")
    valid_id(args.connector_id)
    require(args.component in ("cloudflare", "openai"), "unknown ingress component")
    require(
        args.cloudflared.name.lower() == "cloudflared.exe"
        and re.fullmatch(r"[0-9a-f]{64}", args.cloudflared_sha256),
        "Cloudflare executable or pin invalid",
    )
    extra = (args.tunnel_client, args.tunnel_client_sha256, args.tunnel_id)
    if args.component == "cloudflare":
        require(all(v is None for v in extra), "OpenAI options not allowed for Cloudflare")
    else:
        require(
            all(v is not None for v in extra)
            and args.tunnel_client.name.lower() == "tunnel-client.exe"
            and re.fullmatch(r"[0-9a-f]{64}", args.tunnel_client_sha256)
            and re.fullmatch(r"tunnel_[a-z0-9]{32}", args.tunnel_id),
            "OpenAI executable, pin or Tunnel ID invalid",
        )


def binary_digest(path):
    require(
        path.is_file() and 1024 <= path.stat().st_size <= 200 * 1024**2,
        "executable file size invalid",
    )
    h = hashlib.sha256()
    with path.open("rb") as stream:
        first = stream.read(1024 * 1024)
        require(first.startswith(b"MZ"), "Windows executable signature invalid")
        h.update(first)
        for data in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(data)
    return h.hexdigest()


def copy_binary(source, target, expected, sid):
    from serverfs_mcp.windows_private_artifacts import protect_new_file
    from serverfs_mcp.windows_v25 import storage

    storage.check_private(target.parent, directory=True, sid=sid)
    require(not os.path.lexists(target), "private binary target occupied")
    temp = target.with_name(".binary-" + secrets.token_hex(16) + ".tmp")
    created = False
    try:
        with source.open("rb") as reader, temp.open("xb") as writer:
            created = True
            protect_new_file(temp, sid)
            storage.check_private(temp, directory=False, sid=sid)
            h = hashlib.sha256()
            count = 0
            for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                count += len(chunk)
                require(count <= 200 * 1024**2, "executable grew during copy")
                h.update(chunk)
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        require(
            h.hexdigest() == expected and binary_digest(temp) == expected,
            "copied executable differs from pin",
        )
        os.link(temp, target)
    finally:
        if created:
            temp.unlink(missing_ok=True)
    storage.check_private(target, directory=False, sid=sid)
    require(
        target.lstat().st_nlink == 1 and binary_digest(target) == expected,
        "published executable differs from pin",
    )


def settings_for(core, component, home, tunnel_id=None):
    result = {"port": core.settings["port"], "hostname": core.settings["hostname"]}
    if component == "cloudflare":
        return result | {
            "cloudflared": str(home / "cloudflared.exe"),
            "token_file": str(home / "tunnel.token"),
        }
    require(component == "openai", "unknown ingress component")
    return result | {
        "tunnel_client": str(home / "tunnel-client.exe"),
        "tunnel_id": tunnel_id,
        "control_plane_key_file": str(home / "control-plane.key"),
        "core_auth_header_file": str(home / "core-auth.header"),
    }


def check_generic_cf_peers(peers, tunnel, sid):
    """Compare published generic audit identities without reading peer tokens.

    Older operator/manual manifests have no such record; their remote Tunnel
    identity is explicitly outside this local check.
    """
    from serverfs_mcp.windows_v25 import storage

    unchecked = 0
    for peer in peers:
        if peer.component != "cloudflare":
            continue
        audit = (
            storage.home(peer.component, peer.instance_id, peer.connector_id)
            / "connector-config-attempt/audit.json"
        )
        if not os.path.lexists(audit):
            unchecked += 1
            continue
        storage.check_private(audit, directory=False, sid=sid)
        require(audit.stat().st_size <= 16384, "peer Connector audit invalid")
        row = json.loads(audit.read_text("utf-8"), object_pairs_hook=unique_pairs)
        require(
            row.get("state") == "COMPLETE"
            and row.get("verdict") == "PASS"
            and row.get("instance") == peer.instance_id
            and row.get("connector_id") == peer.connector_id
            and row.get("component") == "cloudflare"
            and type(row.get("tunnel_identity")) is str,
            "peer Connector audit invalid",
        )
        require(row["tunnel_identity"] != tunnel, "Cloudflare Tunnel already allocated")
    return unchecked


def provision(args):
    from ctypes import windll

    import serverfs_mcp
    from serverfs_mcp.windows_cloudflare_security import validate_private_file
    from serverfs_mcp.windows_private_artifacts import protect_new_file
    from serverfs_mcp.windows_v25 import cli, runtime, storage
    from serverfs_mcp.windows_v25.bridge_v2_owner import load_owned_bridge_workdir_claims
    from serverfs_mcp.windows_v25.spec import Manifest
    from serverfs_mcp.windows_v25.tasks import Scheduler

    require(
        sys.platform == "win32"
        and os.name == "nt"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not windll.shell32.IsUserAnAdmin(),
        "ordinary native Windows -I -B controller required",
    )
    require(
        sys.prefix != sys.base_prefix
        and metadata.version("serverfs-mcp")
        == metadata.version("serverfs-windows-native")
        == VERSION
        and Path(serverfs_mcp.__file__)
        .resolve()
        .is_relative_to(Path(sys.prefix).resolve() / "Lib/site-packages"),
        "installed controller required",
    )
    options(args)
    helper = private_helper()
    sid = storage._sid()
    helper.installed(args.component, args.instance, sid)
    core = storage.load("core", args.instance)
    cli._preflight(core)
    cli._assert_instance_isolation(core)
    _, auth = runtime._core_configuration(core)
    home = storage.home(args.component, args.instance, args.connector_id)
    require(not os.path.lexists(home), "Connector private home already exists")
    roots = []
    for peer in storage.core_inventory():
        _, paths = runtime._roots(Path(str(peer.settings["config"])))
        roots.extend(paths)
    for peer in storage.component_inventory("bridge"):
        roots.extend(Path(claim.root) for claim in load_owned_bridge_workdir_claims(peer))
    private_root = home.resolve(strict=False)
    require(
        not any(private_root.is_relative_to(r) or r.is_relative_to(private_root) for r in roots),
        "Connector private home overlaps exposed workdir",
    )
    peers = (*storage.component_inventory("cloudflare"), *storage.component_inventory("openai"))
    credential_paths = {
        Path(str(v)).resolve(strict=True)
        for peer in peers
        for key, v in peer.settings.items()
        if key in ("token_file", "control_plane_key_file", "core_auth_header_file")
    }

    def source(path):
        require(path.is_absolute(), "absolute private source path required")
        path = validate_private_file(
            path,
            checkout_root=Path(__file__).resolve().parents[2],
            workdir_roots=roots,
            user_sid=sid,
        )
        require(path.lstat().st_nlink == 1, "source file is multiply linked")
        return path

    credential = source(args.credential_file)
    require(
        credential not in credential_paths and credential.stat().st_size <= 4098,
        "credential source is already allocated or oversized",
    )
    data = single_line(credential.read_bytes())
    cloudflared = source(args.cloudflared)
    require(binary_digest(cloudflared) == args.cloudflared_sha256, "Cloudflare binary pin mismatch")
    binaries = [(cloudflared, home / "cloudflared.exe", args.cloudflared_sha256)]
    unchecked = 0
    if args.component == "cloudflare":
        data, tunnel = cloudflare_token(data)
        unchecked = check_generic_cf_peers(peers, tunnel, sid)
        files = [(home / "tunnel.token", data)]
    else:
        tunnel = args.tunnel_id
        client = source(args.tunnel_client)
        require(binary_digest(client) == args.tunnel_client_sha256, "OpenAI binary pin mismatch")
        binaries.append((client, home / "tunnel-client.exe", args.tunnel_client_sha256))
        header = header_for(auth)
        require(data != header[7:], "control plane and Core key must differ")
        files = [(home / "control-plane.key", data), (home / "core-auth.header", header)]
    manifest = Manifest.parse(
        {
            "schema": 2,
            "instance_id": args.instance,
            "connector_id": args.connector_id,
            "component": args.component,
            "sid": sid,
            "pythonw": str(storage.pinned_pythonw(args.component, args.instance)),
            "settings": settings_for(core, args.component, home, tunnel),
        }
    )
    cli._assert_instance_isolation(manifest)
    require(not Scheduler().status(manifest)["registered"], "Connector task already allocated")
    storage.prepare(args.component, args.instance, args.connector_id)
    storage.check_private(home, directory=True, sid=sid)
    marker = home / "connector-config-attempt"
    marker.mkdir()
    protect_new_file(marker, sid)
    storage.check_private(marker, directory=True, sid=sid)
    for origin, target, pin in binaries:
        copy_binary(origin, target, pin, sid)
    for target, payload in files:
        helper.publish(target, payload, sid)
        require(target.lstat().st_nlink == 1, "published secret is multiply linked")
    cli._preflight(manifest)
    cli._assert_instance_isolation(manifest)
    require(storage.load("core", args.instance) == core, "Core target changed")
    if args.component == "openai":
        _, current = runtime._core_configuration(core)
        require(header_for(current) == files[1][1], "Core bearer changed during provisioning")
    storage.stage(manifest)
    require(
        storage.load(args.component, args.instance, args.connector_id) == manifest,
        "published Connector manifest differs",
    )
    result = {
        "state": "COMPLETE",
        "verdict": "PASS",
        "instance": args.instance,
        "component": args.component,
        "connector_id": args.connector_id,
        "registered": False,
        "manual_cloudflare_peers_unchecked": unchecked,
    }
    helper.publish(
        marker / "audit.json", helper.json_bytes(result | {"tunnel_identity": tunnel}), sid
    )
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        report = provision(args)
    except Exception as exc:
        print(
            "Connector configuration refused: "
            + (str(exc) if type(exc) is Refusal else type(exc).__name__),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
