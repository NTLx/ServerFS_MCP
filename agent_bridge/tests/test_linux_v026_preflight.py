"""Linux v0.26 signed Core Compose preflight: synthetic private owner + model.

No Docker socket, running Bridge or public credentials are involved.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from serverfs_agent_bridge import linux_v0250_preflight as preflight
from serverfs_agent_bridge.core_grants import core_workdir_policy_digest
from serverfs_agent_bridge.models import AgentMode
from serverfs_agent_bridge.policy import PolicyRegistry, WorkdirAgentPolicy

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux signed preflight")


@pytest.fixture
def candidate(monkeypatch):
    with tempfile.TemporaryDirectory(prefix="serverfs-v26-") as directory:
        home = Path(directory)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        root = home / ".config/serverfs-agent-bridge/instances/bridge-blue"
        root.mkdir(parents=True, mode=0o700)
        root.chmod(0o700)
        for relative in (
            "bridge/state", "bridge/locks", "bridge/run", "core-bindings/core-blue"
        ):
            item = root / relative
            item.mkdir(parents=True, mode=0o700)
            item.chmod(0o700)
        repo, docs, disabled = (
            home / "repo", home / "docs", home / "disabled-placeholder"
        )
        for item in (repo, docs, disabled):
            item.mkdir(mode=0o700)
        (disabled / ".serverfs-disabled").touch()
        policies = PolicyRegistry([
            WorkdirAgentPolicy(
                slot=None, alias="Repo", host_path=repo,
                mode=AgentMode.WORKSPACE_WRITE, runtimes=frozenset({"claude"}),
                read_only=False,
            ),
            WorkdirAgentPolicy(
                slot=None, alias="Docs", host_path=docs,
                mode=AgentMode.DISABLED, runtimes=frozenset(), read_only=True,
                repo_ops_enabled=True,
            ),
        ])
        config = SimpleNamespace(
            protocol_mode="v2", bridge_id="bridge-blue", lease_key="alias",
            allowed_peer_uid=os.getuid(), allowed_peer_gid=os.getgid(),
            socket_path=root / "bridge/run/bridge.sock",
            lock_dir=root / "bridge/locks", state_dir=root / "bridge/state",
            policies=policies,
        )
        monkeypatch.setattr(preflight, "BridgeConfig", SimpleNamespace(load=lambda _: config))
        rev_repo = core_workdir_policy_digest(policies.get("Repo"))
        rev_docs = core_workdir_policy_digest(policies.get("Docs"))
        binding = {
            "schema_version": 2, "instance_id": "core-blue", "core_id": "core-blue",
            "bridge_id": "bridge-blue", "key_id": "key-blue",
            "secret_hex": "7a" * 32,
            "socket_path": "/run/serverfs-agent-bridge/bridge.sock",
            "workdirs": [
                {"alias": "Repo", "workdir_path": "/workdirs/01", "policy_revision": rev_repo},
                {"alias": "Docs", "workdir_path": "/workdirs/03", "policy_revision": rev_docs},
            ],
        }
        for name, doc in (
            ("config.json", {}),
            ("core-bindings/core-blue/core-bridge-binding.json", binding),
            ("bridge/state/core-grants.json", {}),
        ):
            path = root / name
            path.write_text(json.dumps(doc), encoding="utf-8")
            path.chmod(0o600)
        grant = SimpleNamespace(
            core_id="core-blue", key_id="key-blue", secret=bytes.fromhex("7a" * 32),
            methods=frozenset({"bridge.ping", "runtime.list", "task.submit", "repo.inspect"}),
            workdirs=(("Docs", rev_docs), ("Repo", rev_repo)),
        )
        monkeypatch.setattr(preflight, "_parse_grants", lambda *_a: [grant])
        volumes = []
        env = {
            "SERVERFS_LINUX_INSTANCE_ID": "core-blue",
            "SERVERFS_LINUX_BRIDGE_ID": "bridge-blue",
            "SERVERFS_LINUX_V25_BRIDGE_BINDING_FILE":
                "/run/serverfs-core-bridge-binding/core-bridge-binding.json",
            "SERVERFS_AGENT_BRIDGE_SOCKET": "/run/serverfs-agent-bridge/bridge.sock",
            "SERVERFS_AGENT_LOCK_DIR": "/run/serverfs-agent-locks",
            "SERVERFS_AGENT_BRIDGE_ENABLED": "true",
            "SERVERFS_AGENT_MODE": "disabled",
            "SERVERFS_AGENT_RUNTIMES": "",
        }
        for slot in range(1, 17):
            name = f"WORKDIR_{slot:02d}_"
            target = f"/workdirs/{slot:02d}"
            if slot == 1:
                alias, source, read_only, mode, runtimes, repoonly = (
                    "Repo", repo, False, "workspace-write", "claude", False
                )
            elif slot == 3:
                alias, source, read_only, mode, runtimes, repoonly = (
                    "Docs", docs, True, "disabled", "", True
                )
            else:
                alias, source, read_only, mode, runtimes, repoonly = (
                    "", disabled, True, "disabled", "", False
                )
            env.update({
                name + "ALIAS": alias, name + "READ_ONLY": str(read_only).lower(),
                name + "AGENT_MODE": mode, name + "AGENT_RUNTIMES": runtimes,
                name + "REPO_OPS_ENABLED": str(repoonly).lower(),
                name + "BINARY_TRANSFER_ENABLED": "false",
            })
            volumes.append({
                "type": "bind", "source": str(source), "target": target,
                "read_only": read_only, "bind": {"create_host_path": False},
            })
        for target, source in (
            ("/run/serverfs-core-bridge-binding", root / "core-bindings/core-blue"),
            ("/run/serverfs-agent-bridge", root / "bridge/run"),
            ("/run/serverfs-agent-locks", root / "bridge/locks"),
        ):
            volumes.append({
                "type": "bind", "source": str(source), "target": target,
                "read_only": True, "bind": {"create_host_path": False},
            })
        model = {
            "name": "fixture",
            "services": {"serverfs-mcp": {
                "environment": env, "volumes": volumes,
                "user": f"{os.getuid()}:{os.getgid()}",
                "read_only": True, "tmpfs": ["/tmp"],
                "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
            }},
        }
        yield model, binding, root


def _validate(model):
    preflight.validate(model, bridge_id="bridge-blue", core_id="core-blue", project="fixture")


def test_multi_alias_validates_slots_01_and_03(candidate):
    model, _binding, _root = candidate
    _validate(model)


@pytest.mark.parametrize(
    ("change", "stage"),
    [
        ("wrong-alias", "workdir-policy"),
        ("wrong-runtime", "runtime-policy"),
        ("wrong-repo-flag", "workdir-policy"),
        ("ungranted-agent", "additional-agent-slot"),
        ("ungranted-repo", "additional-agent-slot"),
        ("duplicate-alias", "additional-file-slot"),
        ("private-root", "private-root-exposure"),
        ("different-host", "workdir-policy"),
    ],
)
def test_multi_alias_compose_drift_is_rejected(candidate, change, stage):
    model, binding, root = candidate
    env = model["services"]["serverfs-mcp"]["environment"]
    vols = {v["target"]: v for v in model["services"]["serverfs-mcp"]["volumes"]}
    if change == "wrong-alias":
        env["WORKDIR_03_ALIAS"] = "Other"
    elif change == "wrong-runtime":
        env["WORKDIR_01_AGENT_RUNTIMES"] = "qoder"
    elif change == "wrong-repo-flag":
        env["WORKDIR_03_REPO_OPS_ENABLED"] = "false"
    elif change == "ungranted-agent":
        env["WORKDIR_02_AGENT_MODE"] = "workspace-write"
    elif change == "ungranted-repo":
        env["WORKDIR_02_REPO_OPS_ENABLED"] = "true"
    elif change == "duplicate-alias":
        env["WORKDIR_02_ALIAS"] = "Repo"
        vols["/workdirs/02"]["source"] = vols["/workdirs/01"]["source"]
    elif change == "private-root":
        vols["/workdirs/02"]["source"] = str(root)
    elif change == "different-host":
        vols["/workdirs/03"]["source"] = vols["/workdirs/01"]["source"]
    else:
        pytest.fail("unknown drift")
    with pytest.raises(preflight.PreflightError, match=stage):
        _validate(model)


@pytest.mark.parametrize("bad", ["duplicate", "wrong-slot", "wrong-revision", "missing-alias"])
def test_multi_alias_binding_rejected_before_public_mcp(candidate, bad):
    model, binding, root = candidate
    if bad == "duplicate":
        binding["workdirs"][1]["alias"] = "Repo"
    elif bad == "wrong-slot":
        binding["workdirs"][1]["workdir_path"] = "/workdirs/17"
    elif bad == "wrong-revision":
        binding["workdirs"][1]["policy_revision"] = "f" * 64
    else:
        binding["workdirs"].pop()
    file = root / "core-bindings/core-blue/core-bridge-binding.json"
    file.write_text(json.dumps(binding), encoding="utf-8")
    file.chmod(0o600)
    expected_stage = "grant-binding" if bad == "missing-alias" else "binding-policy"
    with pytest.raises(preflight.PreflightError, match=expected_stage):
        _validate(model)
