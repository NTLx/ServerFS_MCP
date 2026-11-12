#!/usr/bin/env python3
"""Prepare (not perform) macOS v0.25.0 Cloudflare cutover with private rollback state.

Run with the repository's existing .venv Python. This file never registers,
stops, launches, or replaces a service; secrets remain in private host files.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import stat
from pathlib import Path

from serverfs_mcp import darwin_service as ds
from serverfs_mcp.darwin_bridge_policy import policy_is_synced
from serverfs_mcp.native_config import load_native_config

HOSTNAME = "macbook-serverfs.ntlx.top"
PORT = 8765
EXPECTED_PIDS = {
    "tunnel": 1779,
    "agent-bridge": 1790,
    "http-origin": 1786,
    "cloudflare": 1784,
}
PRIVATE_ENV_KEYS = ds._HTTP_ORIGIN_ENV_KEYS


class CutoverPreflightError(RuntimeError):
    pass


def require(condition: bool, label: str) -> None:
    if not condition:
        raise CutoverPreflightError(label)


def owned_private_file(path: Path, label: str) -> None:
    ds._reject_symlink_components(path, label)
    st = path.stat(follow_symlinks=False)
    require(
        stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid() and stat.S_IMODE(st.st_mode) == 0o600,
        f"{label}: expected owned regular mode 0600 file",
    )


def run_checks() -> tuple[dict[str, object], dict[str, object]]:
    manifest_file = ds.manifest_path()
    snapshot_file = ds.service_home() / ds._HTTP_ORIGIN_ENV_SNAPSHOT
    state: dict[str, object] = {}

    def check(name: str, fn) -> None:
        try:
            state[name] = "PASS"
            fn()
        except Exception as exc:
            state[name] = f"FAIL:{type(exc).__name__}"
            print(json.dumps({"gate": name, "status": state[name]}, sort_keys=True))
            raise CutoverPreflightError(f"preflight gate {name} failed") from exc

    items: dict[str, object] = {}

    def manifest_gate() -> None:
        owned_private_file(manifest_file, "services.json")
        owned_private_file(snapshot_file, "HTTP snapshot")
        document = ds._load_manifest_document()
        require(document.get("schema_version") == ds.MANIFEST_SCHEMA_VERSION, "manifest schema")
        services = document["services"]
        require(set(EXPECTED_PIDS).issubset(services), "missing service entry")
        for label in ("http-origin", "cloudflare"):
            spec = services[label]
            require(
                isinstance(spec, dict)
                and not spec.get("origin_owner")
                and not spec.get("connector_owner"),
                "legacy pair identity",
            )
        items["document"] = document
        items["services"] = services

    check("legacy_manifest", manifest_gate)

    def process_gate() -> None:
        for label, baseline in EXPECTED_PIDS.items():
            require(ds.native_status(label).get(label) == "enabled", f"{label} not enabled")
            require(ds._managed_job_pid(label) == baseline, f"{label} pid changed")
        ds._refuse_unmanaged_cloudflared()

    check("four_live_service_owners", process_gate)

    def app_gate() -> None:
        app = ds.installed_app_path()
        ds._verify_app(app)
        ds._verify_opt_in_agents(app)
        require(ds.manager_executable().is_file(), "manager executable missing")

    check("signed_native_app", app_gate)

    def policy_gate() -> None:
        config = Path(__file__).resolve().parents[2] / "serverfs.toml"
        ds._reject_symlink_components(config, "canonical TOML")
        require(config.is_file(), "canonical TOML missing")
        config = config.resolve(strict=True)
        workdirs, settings = load_native_config(config)
        require(
            {x.alias for x in workdirs} == {"serverfs", "codexsatellites"}, "workdir aliases differ"
        )
        require(
            all(not x.read_only and x.agent_mode == "workspace-write" for x in workdirs),
            "workdir mode mismatch",
        )
        require(
            all({"codex", "claude", "qoder"}.issubset(set(x.agent_runtimes)) for x in workdirs),
            "Agent runtimes differ",
        )
        require(settings.agent_enabled, "Agent disabled")
        require(all(x.policy.binary_transfer_enabled for x in workdirs), "binary transfer disabled")
        require(policy_is_synced(workdirs, settings), "Bridge policy stale")
        tunnel_args = items["services"]["tunnel"]["arguments"]
        require(
            isinstance(tunnel_args, list) and tunnel_args.count("--config") == 1,
            "Tunnel config missing",
        )
        raw = tunnel_args[tunnel_args.index("--config") + 1]
        require(Path(raw).resolve(strict=True) == config, "canonical config mismatch")
        items["canonical_config"] = str(config)

    check("openai_bridge_policy_parity", policy_gate)

    def snapshot_gate() -> None:
        document = json.loads(snapshot_file.read_text(encoding="utf-8"))
        require(isinstance(document, dict), "snapshot not a dict")
        require(set(document).issubset(PRIVATE_ENV_KEYS), "unexpected private env keys")
        require(all(isinstance(v, str) for v in document.values()), "invalid env values")
        require(bool(document.get("SERVERFS_HTTP_API_KEY")), "API Key missing")
        require(document.get("SERVERFS_HTTP_ALLOWED_HOSTS") == HOSTNAME, "old Host mismatch")
        require(document.get("SERVERFS_OAUTH_ENABLED", "false").lower() != "true", "OAuth snapshot")
        ds._validated_http_origin_environment(document, HOSTNAME)
        for value in document.values():
            require(
                not any(ord(c) < 32 or ord(c) == 127 for c in value), "control char in HTTP env"
            )
        items["old_environment"] = document

    check("legacy_api_key_snapshot", snapshot_gate)

    def connector_gate() -> None:
        spec = items["services"]["cloudflare"]
        cloudflared = Path(spec["executable"])
        require(cloudflared.is_absolute(), "cloudflared path relative")
        ds._reject_symlink_components(cloudflared, "cloudflared")
        st = cloudflared.stat(follow_symlinks=False)
        require(
            st.st_uid == os.getuid() and stat.S_ISREG(st.st_mode) and not (st.st_mode & 0o022),
            "insecure cloudflared",
        )
        require(os.access(cloudflared, os.X_OK), "cloudflared not executable")
        arguments = spec["arguments"]
        require(arguments.count("--token-file") == 1, "no token file argument")
        token = Path(arguments[arguments.index("--token-file") + 1])
        require(token.is_absolute(), "token path relative")
        owned_private_file(token, "Cloudflare token")
        config = Path(items["canonical_config"])
        roots = [Path(x.root).resolve(strict=True) for x in load_native_config(config)[0]]
        ds._validate_private_location(token, (config.parent, *roots), "Cloudflare token")
        origin = items["services"]["http-origin"]["arguments"]
        require(origin.count("--http-port") == 1, "origin port missing")
        require(int(origin[origin.index("--http-port") + 1]) == PORT, "origin port differs")
        items["cloudflared_path"] = str(cloudflared)
        items["token_file"] = str(token)

    check("cloudflare_token_binary_and_port", connector_gate)
    return state, items


def prepare(items: dict[str, object]) -> Path:
    base = ds.service_home().parent / "migrations"
    ds._reject_symlink_components(base, "migration private home")
    ds._ensure_private_dir(base)
    dest = base / f"v0250-macbook-cloudflare-20261112-{secrets.token_hex(6)}"
    dest.mkdir(mode=0o700)
    backup_manifest = ds.manifest_path().read_bytes()
    backup_snapshot = (ds.service_home() / ds._HTTP_ORIGIN_ENV_SNAPSHOT).read_bytes()
    require(bool(backup_manifest) and bool(backup_snapshot), "empty backup source")
    env = dict(items["old_environment"])
    env["SERVERFS_HTTP_ALLOWED_HOSTS"] = f"{HOSTNAME},127.0.0.1:{PORT}"
    ds._validated_http_origin_environment(env, env["SERVERFS_HTTP_ALLOWED_HOSTS"])
    for name, payload in (
        ("original-services.json", backup_manifest),
        ("original-http-origin-environment.json", backup_snapshot),
    ):
        ds._atomic_private_write(dest / name, payload)
    lines = [f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in sorted(env.items())]
    ds._atomic_private_write(dest / "core-http.env", ("\n".join(lines) + "\n").encode("utf-8"))
    reopened = ds._read_env_file(dest / "core-http.env")
    require(reopened == env, "Core env roundtrip failed")
    source = {
        "config_path": items["canonical_config"],
        "cloudflared_path": items["cloudflared_path"],
        "token_file": items["token_file"],
        "hostname": HOSTNAME,
        "port": PORT,
        "baseline_pids": EXPECTED_PIDS,
    }
    ds._atomic_private_write(
        dest / "source-paths.json", json.dumps(source, sort_keys=True).encode("utf-8")
    )
    require(
        (dest / "original-services.json").read_bytes() == backup_manifest,
        "manifest backup mismatch",
    )
    require(
        (dest / "original-http-origin-environment.json").read_bytes() == backup_snapshot,
        "snapshot backup mismatch",
    )
    for name in (
        "original-services.json",
        "original-http-origin-environment.json",
        "core-http.env",
        "source-paths.json",
    ):
        owned_private_file(dest / name, name)
    return dest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--prepare", action="store_true", help="write protected backups; no service mutations"
    )
    args = parser.parse_args()
    try:
        results, items = run_checks()
        if args.prepare:
            destination = prepare(items)
            # Pre- and post-write invariants. This script never changes any service.
            for service, pid in EXPECTED_PIDS.items():
                require(ds._managed_job_pid(service) == pid, "service changed during prepare")
            print(
                json.dumps(
                    {
                        "prepared": True,
                        "backup_dir": str(destination),
                        "gates": results,
                        "services_untouched": True,
                    },
                    sort_keys=True,
                )
            )
        else:
            print(
                json.dumps(
                    {"checks_passed": True, "gates": results, "wrote_files": False}, sort_keys=True
                )
            )
        return 0
    except Exception as exc:
        # Do not echo exception details: third-party messages might include private paths.
        print(json.dumps({"prepared": False, "error_class": type(exc).__name__}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
