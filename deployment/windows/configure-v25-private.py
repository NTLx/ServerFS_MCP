"""Provision private bearer policy and initial named Windows Core/Bridge manifests.

Use only with fresh, already installed private runtimes. No registration,
activation, rotation, existing-grant modification, or automatic rollback.
"""

from __future__ import annotations

import argparse
import importlib.metadata as metadata
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

VERSION = "0.25.0"


class Refusal(ValueError):
    """Fixed redacted configuration refusal."""


def require(ok, message):
    if not ok:
        raise Refusal(message)


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--core", action="append", nargs=4, required=True, metavar=("ID", "PORT", "HOST", "TOML")
    )
    p.add_argument("--bridge-id")
    p.add_argument("--bridge-policy", type=Path)
    p.add_argument("--agent-proxy-env-file", type=Path)
    return p


def validate_topology(cores, bridge_id, bridge_policy):
    from serverfs_mcp.instance_identity import DEFAULT_INSTANCE_ID, valid_id

    ids = tuple(c[0] for c in cores)
    ports = tuple(int(c[1]) for c in cores)
    require(
        1 <= len(ids) <= 16 and len(set(ids)) == len(ids) and len(set(ports)) == len(ports),
        "duplicate Core identity or port",
    )
    require(
        all(valid_id(i) != DEFAULT_INSTANCE_ID for i in ids)
        and all(1 <= p <= 65535 for p in ports),
        "invalid named Core",
    )
    if bridge_id is None:
        require(
            bridge_policy is None and len(ids) == 1,
            "file-only mode requires exactly one Core and no Bridge policy",
        )
        return "disabled"
    require(valid_id(bridge_id) != DEFAULT_INSTANCE_ID, "named Bridge required")
    if bridge_id in ids:
        require(
            ids == (bridge_id,) and bridge_policy is None, "owned Bridge uses its sole Core policy"
        )
        return "owned"
    require(bridge_policy is not None, "shared Bridge requires explicit owner policy")
    return "shared"


def json_bytes(value):
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode("utf-8")


def http_auth(host, port):
    from serverfs_mcp.windows_v25.spec import _host

    _host(host)
    require(type(port) is int and 1 <= port <= 65535, "invalid Core port")
    token = secrets.token_urlsafe(48)
    return (
        f"SERVERFS_HTTP_API_KEY={token}\n"
        f"SERVERFS_HTTP_ALLOWED_HOSTS={host},127.0.0.1:{port}\n"
        "SERVERFS_OAUTH_ENABLED=false\n"
    ).encode("ascii")


def read_private_proxy_source(source: Path, exposed_roots, sid: str) -> bytes:
    """Read only a bounded private source outside every known exposed root."""
    from serverfs_mcp.windows_cloudflare_security import _no_reparse_components
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.named_agent_proxy import parse_proxy_source

    require(source.is_absolute(), "absolute private Agent proxy source required")
    _no_reparse_components(source)
    resolved_source = source.resolve(strict=True)
    checkout = Path(__file__).resolve().parents[2]
    require(
        all(
            not resolved_source.is_relative_to(root.resolve(strict=True))
            for root in [*exposed_roots, checkout]
        ),
        "private Agent proxy source must stay outside exposed workdirs and checkout",
    )
    storage.check_private(source, directory=False, sid=sid)
    source_info = source.stat(follow_symlinks=False)
    require(
        0 < source_info.st_size <= 4096,
        "private Agent proxy input is unavailable or oversized",
    )
    with source.open("rb") as stream:
        proxy = parse_proxy_source(stream.read(4097))
    normalized = (
        f"SERVERFS_AGENT_PROXY_URL={proxy.url}\nSERVERFS_AGENT_NO_PROXY={proxy.no_proxy}\n"
    ).encode()
    require(len(normalized) <= 4096, "normalized Agent proxy input is oversized")
    return normalized


def render_bridge(request, bridge_id, sid, home, pipe):
    runtimes = request.get("runtimes", {})
    require(
        set(request) <= {"workdirs", "runtimes", "acp_profiles", "limits"}
        and set(runtimes) <= {"codex", "claude", "qoder"},
        "unsupported Bridge renderer input",
    )
    from serverfs_mcp.windows_v25.named_agent_proxy import uses_agent_proxy

    uses_agent_proxy(request)
    return {
        "bridge_id": bridge_id,
        "core_grants_enabled": True,
        "lease_key": "alias",
        "socket_path": pipe,
        "state_dir": str(home / "state"),
        "lock_dir": str(home / "locks"),
        "allowed_peer_sid": sid,
        "enable_fake_runtime": False,
        "workdirs": request["workdirs"],
        **runtimes,
        **{k: request[k] for k in ("limits", "acp_profiles") if k in request},
    }


def core_scope(core_request, bridge_request):
    """Require exact effective workdir action and runtime policy parity."""
    owner = {w["alias"]: w for w in bridge_request["workdirs"]}
    require(len(owner) == len(bridge_request["workdirs"]), "duplicate Bridge alias")
    selected = core_request["workdirs"]
    require(
        1 <= len(selected) <= 16 and all(owner.get(w["alias"]) == w for w in selected),
        "Core/Bridge workdir policy differs",
    )
    # The Bridge is the runtime authority. A Core must not claim a different
    # provider executable or limit configuration for the same grant.
    for key in ("runtimes", "acp_profiles", "limits"):
        if key in core_request:
            require(
                core_request[key] == bridge_request.get(key), "Core/Bridge runtime policy differs"
            )
    methods = {"bridge.ping", "runtime.list"}
    if any(w["agent_runtimes"] for w in selected):
        from serverfs_mcp.agent_core_grant_client import _METHODS

        methods.update(m for m in _METHODS if m.startswith("task."))
        methods.add("runtime.models")
    if any(w.get("repo_ops_enabled") for w in selected):
        methods.update(("repo.inspect", "repo.action", "repo.commit", "repo.push"))
    return tuple(sorted(w["alias"] for w in selected)), tuple(sorted(methods))


def grant_pair(core_id, bridge_id, mode, revisions, methods):
    from serverfs_mcp.instance_identity import InstanceIdentity
    from serverfs_mcp.windows_v25.shared_core_grants import issue_shared_core_capabilities

    InstanceIdentity(core_id, mode, bridge_id)
    if mode == "shared":
        (issued,) = issue_shared_core_capabilities(
            bridge_id=bridge_id,
            core_ids=(core_id,),
            workdir_policy_revisions=revisions,
            methods=methods,
        )
        return issued.core_binding(), issued.bridge_grant()
    require(mode == "owned", "invalid capability mode")
    scope = [{"alias": alias, "policy_sha256": value} for alias, value in sorted(revisions.items())]
    key = "key-" + secrets.token_hex(8)
    secret = secrets.token_hex(32)
    binding = {
        "schema_version": 1,
        "instance_id": core_id,
        "bridge_mode": mode,
        "bridge_id": bridge_id,
        "key_id": key,
        "secret_hex": secret,
        "workdirs": scope,
    }
    grant = {
        "core_id": core_id,
        "key_id": key,
        "secret_hex": secret,
        "methods": list(methods),
        "workdirs": scope,
    }
    return binding, grant


def absent(path):
    require(not os.path.lexists(path), "private target already exists")


def publish(path, data, sid):
    """Create-only atomic publication into an already verified private home."""
    from serverfs_mcp.windows_private_artifacts import protect_new_file
    from serverfs_mcp.windows_v25 import storage

    storage.check_private(path.parent, directory=True, sid=sid)
    absent(path)
    tmp = path.with_name(".provision-" + secrets.token_hex(16) + ".tmp")
    created = False
    try:
        with tmp.open("xb") as stream:
            created = True
            protect_new_file(tmp, sid)
            storage.check_private(tmp, directory=False, sid=sid)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(tmp, path)
        storage.check_private(path, directory=False, sid=sid)
        require(path.read_bytes() == data, "private publication differs")
    finally:
        if created:
            tmp.unlink(missing_ok=True)


def worker_call(worker, code, config, payload=None):
    allowed = {
        "SYSTEMROOT",
        "WINDIR",
        "PATH",
        "PATHEXT",
        "COMSPEC",
        "TEMP",
        "TMP",
        "APPDATA",
        "LOCALAPPDATA",
        "USERPROFILE",
        "PROGRAMDATA",
    }
    env = {k.upper(): v for k, v in os.environ.items() if k.upper() in allowed}
    proc = subprocess.run(
        [str(worker), "-I", "-B", "-c", code, str(config)],
        input=payload or b"",
        capture_output=True,
        env=env,
        cwd=config.parent,
        timeout=60,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    require(
        proc.returncode == 0 and len(proc.stdout) <= 16384,
        "installed Bridge worker refused private policy",
    )
    return proc.stdout


_DIGEST = """
import importlib.metadata as md, json, sys
from pathlib import Path
from serverfs_agent_bridge.config import BridgeConfig
from serverfs_agent_bridge.core_grants import core_workdir_policy_digest
import serverfs_agent_bridge
assert md.version("serverfs-agent-bridge") == "0.25.0"
assert Path(serverfs_agent_bridge.__file__).resolve().is_relative_to(
    Path(sys.prefix).resolve() / "Lib/site-packages")
path = Path(sys.argv[1])
config = BridgeConfig.load(path)
raw = json.loads(path.read_text(encoding="utf-8"))
print(json.dumps({w["alias"]:core_workdir_policy_digest(config.policies.get(w["alias"]))
                  for w in raw["workdirs"]}, sort_keys=True))
"""
_PROVISION = """
import json, sys
from pathlib import Path
from serverfs_agent_bridge.config import BridgeConfig
from serverfs_agent_bridge.core_grants_private import provision_private_core_grants
from serverfs_agent_bridge.store import TaskStore
config = BridgeConfig.load(Path(sys.argv[1]))
assert not (config.state_dir / "core-grants.json").exists()
provision_private_core_grants(store=TaskStore(config.state_dir), bridge_id=config.bridge_id,
                              policies=config.policies, document=json.load(sys.stdin))
print("VERIFIED")
"""


def load_policy(path):
    import tomllib

    from serverfs_mcp.native_config import load_native_config
    from serverfs_mcp.windows_cloudflare_security import _no_reparse_components

    require(path.is_absolute() and path.suffix.lower() == ".toml", "absolute TOML policy required")
    _no_reparse_components(path)
    require(
        path.is_file() and 1 <= path.stat().st_size <= 256 * 1024,
        "policy file missing or oversized",
    )
    data = path.read_bytes()
    parsed = tomllib.loads(data.decode("utf-8"))
    require(
        set(parsed) <= {"server", "defaults", "agent", "workdirs"},
        "unsupported TOML top-level field",
    )
    workdirs, settings = load_native_config(path)
    require(path.read_bytes() == data, "policy changed while parsing")
    roots = []
    for wd in workdirs:
        _no_reparse_components(wd.root)
        root = wd.root.resolve(strict=True)
        require(root.is_dir(), "workdir must be an existing directory")
        roots.append(root)
    return data, workdirs, settings, roots


def disjoint_private_roots(roots, serverfs_home):
    """Allow dedicated instance workdirs, never an ancestor of service state."""
    home = serverfs_home.resolve(strict=True)
    for root in roots:
        root = root.resolve(strict=True)
        require(not home.is_relative_to(root), "workdir exposes private ServerFS storage")
        if root.is_relative_to(home):
            parts = root.relative_to(home).parts
            require(
                len(parts) >= 4 and parts[:2] == ("v25", "instances") and parts[3] == "workdirs",
                "workdir overlaps private ServerFS storage",
            )


def installed(component, instance, sid):
    from serverfs_mcp.windows_v25 import storage

    home = storage.home(component, instance)
    storage.check_private(home, directory=True, sid=sid)
    audit = home / "runtime-install-attempt/audit.json"
    storage.check_private(audit, directory=False, sid=sid)
    report = json.loads(audit.read_text(encoding="utf-8"))
    roles = ["bridge", "agent-bridge"] if component == "bridge" else [component]
    require(
        report.get("state") == "COMPLETE"
        and report.get("verdict") == "PASS"
        and report.get("component") == component
        and report.get("instance") == instance
        and report.get("installed") == roles,
        "verified runtime installation required",
    )
    for role in roles:
        env = storage.root(instance) / "envs" / role
        storage.check_private(env, directory=True, sid=sid)
        for executable in ("python.exe", "pythonw.exe"):
            storage.check_private(env / "Scripts" / executable, directory=False, sid=sid)
    return home


def provision(args):
    from ctypes import windll

    from serverfs_mcp.agent_lifecycle import render_request_from_config
    from serverfs_mcp.windows_core_listener import _windows_ipv4_tcp_listeners
    from serverfs_mcp.windows_private_artifacts import protect_new_file
    from serverfs_mcp.windows_v25 import cli, storage
    from serverfs_mcp.windows_v25.bridge_resources import WindowsBridgeResources
    from serverfs_mcp.windows_v25.core_bridge_binding import (
        parse_core_bridge_binding,
        verify_core_bridge_workdirs,
        verify_core_peer_bridge_owner,
    )
    from serverfs_mcp.windows_v25.isolation import assert_core_isolation
    from serverfs_mcp.windows_v25.runtime import _roots
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
    import serverfs_mcp

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
    mode = validate_topology(args.core, args.bridge_id, args.bridge_policy)
    if mode == "disabled":
        require(args.agent_proxy_env_file is None, "file-only mode cannot configure Agent proxy")
    sid = storage._sid()
    existing = storage.core_inventory()
    occupied = {item.port for item in _windows_ipv4_tcp_listeners()}
    prepared = []
    roots = []
    scheduler = Scheduler()
    for instance, port, host, source in args.core:
        port = int(port)
        home = installed("core", instance, sid)
        policy = load_policy(Path(source))
        require(
            policy[2].bridge_required == (mode != "disabled"), "Core policy/Bridge mode differs"
        )
        roots.extend(policy[3])
        config, auth = home / "serverfs.toml", home / "http.env"
        manifest = Manifest.parse(
            {
                "schema": 2,
                "instance_id": instance,
                "component": "core",
                "sid": sid,
                "pythonw": str(storage.pinned_pythonw("core", instance)),
                "settings": {
                    "config": str(config),
                    "auth_file": str(auth),
                    "port": port,
                    "hostname": host,
                },
            }
        )
        require(
            not any(m.instance_id == instance for m in existing)
            and not scheduler.status(manifest)["registered"]
            and port not in occupied,
            "Core task or port already allocated",
        )
        assert_core_isolation(manifest, (*existing, *(p[0] for p in prepared)))
        targets = [
            home / "service.json",
            config,
            auth,
            home / "bridge-binding.json",
            home / "private-config-attempt",
        ]
        for target in targets:
            absent(target)
        request = render_request_from_config(policy[1], policy[2]) if mode != "disabled" else None
        prepared.append((manifest, home, policy, request))
    # Existing policies may have legacy paths. Check them against the specific
    # new private targets rather than invalidating unrelated installations.
    peer_roots = []
    for peer in existing:
        _, exposed = _roots(Path(str(peer.settings["config"])))
        peer_roots.extend(exposed)
    from serverfs_mcp.windows_v25.bridge_v2_owner import load_owned_bridge_workdir_claims

    for peer in storage.component_inventory("bridge"):
        peer_roots.extend(Path(claim.root) for claim in load_owned_bridge_workdir_claims(peer))
    bridge = None
    scopes = []
    if mode != "disabled":
        bid = args.bridge_id
        home = installed("bridge", bid, sid)
        worker = storage.root(bid) / "envs/agent-bridge/Scripts/python.exe"
        owner_policy = prepared[0][2] if mode == "owned" else load_policy(args.bridge_policy)
        roots.extend(owner_policy[3])
        owner_request = render_request_from_config(owner_policy[1], owner_policy[2])
        from serverfs_mcp.windows_v25.named_agent_proxy import uses_agent_proxy

        wants_proxy = uses_agent_proxy(owner_request)
        require(
            wants_proxy == (args.agent_proxy_env_file is not None),
            "explicit private Agent proxy source required iff a runtime opts in",
        )
        proxy_source = None
        if wants_proxy:
            proxy_source = read_private_proxy_source(
                args.agent_proxy_env_file, [*roots, *peer_roots], sid
            )
        document = render_bridge(
            owner_request, bid, sid, home, WindowsBridgeResources(sid, bid).pipe_name
        )
        config = home / "bridge.json"
        manifest = Manifest.parse(
            {
                "schema": 2,
                "instance_id": bid,
                "component": "bridge",
                "sid": sid,
                "pythonw": str(storage.pinned_pythonw("bridge", bid)),
                "settings": {"config": str(config), "bridge_python": str(worker)},
            }
        )
        require(
            not any(m.instance_id == bid for m in storage.component_inventory("bridge"))
            and not scheduler.status(manifest)["registered"],
            "Bridge already allocated",
        )
        for target in (
            home / "service.json",
            config,
            home / "state/core-grants.json",
            home / "agent-proxy.env",
            home / "private-config-attempt",
        ):
            absent(target)
        # Only empty freshly prepared state; no existing databases or grants.
        for path in (home / "state", home / "locks"):
            storage.check_private(path, directory=True, sid=sid)
        require(
            not any((home / "state").iterdir()) and not any((home / "locks").iterdir()),
            "Bridge state must be fresh",
        )
        verify_core_peer_bridge_owner(manifest, document, owner_home=home)
        for _core, _h, policy, req in prepared:
            verify_core_bridge_workdirs(
                policy[1], document, canonicalize=lambda p: Path(p).resolve(strict=True)
            )
            scopes.append(core_scope(req, owner_request))
        bridge = manifest, home, worker, document
    disjoint_private_roots(roots, storage.root().parent)
    target_ids = {p[0].instance_id for p in prepared}
    if bridge:
        target_ids.add(bridge[0].instance_id)
    private_roots = [storage.root(i) / name for i in target_ids for name in ("services", "envs")]
    for root in peer_roots:
        root = root.resolve(strict=True)
        require(
            not any(root.is_relative_to(p) or p.is_relative_to(root) for p in private_roots),
            "existing policy overlaps new private state",
        )
    # Exclusive markers on every target first. Failure leaves only new private
    # evidence and is not automatically retried; never adopt/update a live owner.
    for home in ([bridge[1]] if bridge else []) + [p[1] for p in prepared]:
        marker = home / "private-config-attempt"
        marker.mkdir()
        protect_new_file(marker, sid)
        storage.check_private(marker, directory=True, sid=sid)
    for manifest, home, policy, _req in prepared:
        publish(home / "serverfs.toml", policy[0], sid)
        publish(
            home / "http.env",
            http_auth(manifest.settings["hostname"], manifest.settings["port"]),
            sid,
        )
        # Parse the exact published policy and validate HTTP authorization.
        from serverfs_mcp.windows_v25.runtime import _core_configuration

        _core_configuration(manifest)
    if bridge:
        owner, home, worker, document = bridge
        config = home / "bridge.json"
        publish(config, json_bytes(document), sid)
        if proxy_source is not None:
            publish(home / "agent-proxy.env", proxy_source, sid)
        revisions = json.loads(worker_call(worker, _DIGEST, config))
        require(
            set(revisions) == {w["alias"] for w in document["workdirs"]}
            and all(
                isinstance(v, str) and len(v) == 64 and all(ch in "0123456789abcdef" for ch in v)
                for v in revisions.values()
            ),
            "Bridge policy digest mismatch",
        )
        grants, bindings = [], []
        for (core, core_home, _, _), (aliases, methods) in zip(prepared, scopes, strict=True):
            binding, grant = grant_pair(
                core.instance_id,
                owner.instance_id,
                mode,
                {a: revisions[a] for a in aliases},
                methods,
            )
            parse_core_bridge_binding(
                core,
                binding,
                local_aliases=frozenset(aliases),
                verified_bridge=owner,
                bridge_lock_dir=home / "locks",
            )
            bindings.append((core_home, binding))
            grants.append(grant)
        require(
            len({g["secret_hex"] for g in grants}) == len(grants)
            and len({g["key_id"] for g in grants}) == len(grants),
            "duplicate issued key",
        )
        require(
            worker_call(
                worker,
                _PROVISION,
                config,
                json_bytes(
                    {
                        "schema_version": 1,
                        "bridge_id": owner.instance_id,
                        "grants": grants,
                    }
                ),
            ).strip()
            == b"VERIFIED",
            "Bridge grant publication refused",
        )
        storage.check_private(home / "state/core-grants.json", directory=False, sid=sid)
        for core_home, binding in bindings:
            publish(core_home / "bridge-binding.json", json_bytes(binding), sid)
        cli._preflight(owner)
        cli._assert_instance_isolation(owner)
        from serverfs_mcp.windows_v25.legacy_bridge_ownership import (
            assert_legacy_writable_roots_disjoint,
        )

        assert_legacy_writable_roots_disjoint(owner)
        storage.stage(owner)
        require(storage.load("bridge", owner.instance_id) == owner, "Bridge manifest differs")
    for core, _, _, _ in prepared:
        cli._preflight(core)
        cli._assert_instance_isolation(core)
        storage.stage(core)
        require(storage.load("core", core.instance_id) == core, "Core manifest differs")
    result = {
        "state": "COMPLETE",
        "verdict": "PASS",
        "bridge_mode": mode,
        "bridge_id": args.bridge_id,
        "cores": [p[0].instance_id for p in prepared],
        "registered": False,
    }
    for home in ([bridge[1]] if bridge else []) + [p[1] for p in prepared]:
        publish(home / "private-config-attempt/audit.json", json_bytes(result), sid)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = provision(args)
    except Exception as exc:
        print(
            "private configuration refused: "
            + (str(exc) if type(exc) is Refusal else type(exc).__name__),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
