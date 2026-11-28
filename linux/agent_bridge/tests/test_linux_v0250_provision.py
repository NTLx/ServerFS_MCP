from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import types
import uuid
from pathlib import Path

import pytest

from platform_contract import require_linux_kernel

require_linux_kernel("Linux v0.25 host Bridge provisioning and flock")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "agent_bridge/src"))
serverfs_package = types.ModuleType("serverfs_mcp")
serverfs_package.__path__ = [str(ROOT / "src/serverfs_mcp")]
serverfs_package.SERVER_VERSION = "0.24.2-test"
sys.modules.setdefault("serverfs_mcp", serverfs_package)

from serverfs_mcp.core_bridge_client_v2 import (  # noqa: E402
    CoreBridgeClientV2,
    CoreBridgeUnavailable,
)
from serverfs_mcp.linux_v0250_core_binding import load_linux_core_client  # noqa: E402
from serverfs_mcp.workdirs import Workdir  # noqa: E402

from serverfs_agent_bridge import linux_v0250_install as installer  # noqa: E402
from serverfs_agent_bridge.config import BridgeConfig  # noqa: E402
from serverfs_agent_bridge.core_grants import sign_core_request  # noqa: E402
from serverfs_agent_bridge.core_grants_private import load_private_core_grants  # noqa: E402
from serverfs_agent_bridge.errors import BridgeError  # noqa: E402
from serverfs_agent_bridge.linux_v0250_preflight import PreflightError, validate  # noqa: E402
from serverfs_agent_bridge.linux_v0250_provision import _read_input, provision  # noqa: E402
from serverfs_agent_bridge.main import _serve  # noqa: E402
from serverfs_agent_bridge.policy import PolicyRegistry  # noqa: E402
from serverfs_agent_bridge.store import TaskStore  # noqa: E402


def test_linux_v2_bridge_config_rejects_slot_lease_policy(tmp_path, monkeypatch):
    _manifest, _root, _work, config_path = _fixture(tmp_path, monkeypatch)
    document = json.loads(config_path.read_text(encoding="utf-8"))
    document["lease_key"] = "slot"
    document["workdirs"][0]["slot"] = 1
    config_path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="requires lease_key alias"):
        BridgeConfig.load(config_path)


def test_linux_v2_serve_rejects_directly_built_slot_policy(tmp_path, monkeypatch):
    _manifest, _root, _work, config_path = _fixture(tmp_path, monkeypatch)
    config = BridgeConfig.load(config_path)
    slot_policy = dataclasses.replace(next(iter(config.policies._by_alias.values())), slot=1)
    config = dataclasses.replace(config, policies=PolicyRegistry([slot_policy]))
    with pytest.raises(ValueError, match="alias-based workdir leases"):
        asyncio.run(_serve(config))


@pytest.fixture(autouse=True)
def short_home(monkeypatch):
    # AF_UNIX uses a byte-bounded path; pytest's long per-test names are unsuitable.
    with tempfile.TemporaryDirectory(prefix="sfv25-") as directory:
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path(directory)))
        yield


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = Path.home()
    (home / ".codex").mkdir(parents=True)
    instances = home / ".config/serverfs-agent-bridge/instances"
    instances.mkdir(parents=True, mode=0o700)
    instances.chmod(0o700)
    work = tmp_path / "work"
    work.mkdir()
    bridge_id = "bridge-blue"
    root = instances / bridge_id
    bridge_config = tmp_path / "trusted-bridge.json"
    config = {
        "socket_path": str(root / "bridge/run/bridge.sock"),
        "state_dir": str(root / "bridge/state"),
        "lock_dir": str(root / "bridge/locks"),
        "allowed_peer_uid": os.getuid(),
        "allowed_peer_gid": os.getgid(),
        "protocol_mode": "v2",
        "lease_key": "alias",
        "bridge_id": bridge_id,
        "codex": {"enabled": True},
        "workdirs": [
            {
                "alias": "Repo",
                "host_path": str(work),
                "read_only": False,
                "agent_mode": "workspace-write",
                "agent_runtimes": ["codex"],
            }
        ],
    }
    bridge_config.write_text(json.dumps(config), encoding="utf-8")
    bridge_config.chmod(0o600)
    manifest = {
        "bridge_id": bridge_id,
        "root": str(root),
        "bridge_config": str(bridge_config),
        "expected_uid": os.getuid(),
        "expected_gid": os.getgid(),
        "core": {
            "instance_id": "core-blue",
            "alias": "Repo",
            "container_path": "/workdirs/01",
            "host_path": str(work),
            "methods": ["bridge.ping", "runtime.list"],
        },
        "existing_writable_roots": [],
    }
    return manifest, root, work, bridge_config


def test_provisioned_grant_and_binding_load_in_real_consumers(tmp_path, monkeypatch):
    manifest, root, work, _config_path = _fixture(tmp_path, monkeypatch)
    assert provision(manifest) == root
    grant_path = root / "bridge/state/core-grants.json"
    binding_path = root / "core-bindings/core-blue/core-bridge-binding.json"
    grants_doc = json.loads(grant_path.read_text(encoding="utf-8"))
    binding = json.loads(binding_path.read_text(encoding="utf-8"))
    assert grants_doc["grants"][0]["secret_hex"] == binding["secret_hex"]
    assert grants_doc["grants"][0]["workdirs"][0]["policy_sha256"] == binding["policy_revision"]
    assert binding["socket_path"] == "/run/serverfs-agent-bridge/bridge.sock"
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(binding_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(binding_path.stat().st_mode) == 0o400
    config = BridgeConfig.load(root / "config.json")
    verifier = load_private_core_grants(
        store=TaskStore(root / "bridge/state"),
        bridge_id="bridge-blue",
        policies=config.policies,
    )
    grant = verifier._grants[("core-blue", binding["key_id"])]
    assert grant.secret.hex() == binding["secret_hex"]
    client, alias = load_linux_core_client(
        str(binding_path),
        instance_id="core-blue",
        bridge_id="bridge-blue",
        socket_path=binding["socket_path"],
        workdirs=[Workdir("Repo", Path("/workdirs/01"), None, agent_mode="review")],
    )
    assert alias == "Repo"
    assert client._key_id == binding["key_id"]
    assert client._policy_revision == binding["policy_revision"]
    assert work.exists()
    assert binding["secret_hex"] not in repr(client)


def test_v026_one_core_two_authorized_workdirs_nonfirst_slot(tmp_path, monkeypatch):
    manifest, root, work, config_path = _fixture(tmp_path, monkeypatch)
    docs = tmp_path / "work-docs"
    docs.mkdir()
    owner_policy = json.loads(config_path.read_text(encoding="utf-8"))
    owner_policy["workdirs"].append(
        {
            "alias": "Docs",
            "host_path": str(docs),
            "read_only": False,
            "agent_mode": "workspace-write",
            "agent_runtimes": ["codex"],
        }
    )
    config_path.write_text(json.dumps(owner_policy), encoding="utf-8")
    manifest["core"] = {
        "instance_id": "core-blue",
        "workdirs": [
            {"alias": "Repo", "container_path": "/workdirs/01", "host_path": str(work)},
            {"alias": "Docs", "container_path": "/workdirs/03", "host_path": str(docs)},
        ],
        "methods": ["bridge.ping", "runtime.list", "task.submit", "task.get"],
    }
    assert provision(manifest) == root
    saved = json.loads((root / "core-bindings/core-blue/core-bridge-binding.json").read_text())
    assert saved["schema_version"] == 2
    assert {item["alias"] for item in saved["workdirs"]} == {"Repo", "Docs"}
    grants = json.loads((root / "bridge/state/core-grants.json").read_text())
    assert len(grants["grants"]) == 1
    assert len(grants["grants"][0]["workdirs"]) == 2
    assert "task.list" in grants["grants"][0]["methods"]
    workdirs = [
        Workdir(
            "Repo",
            Path("/workdirs/01"),
            None,
            agent_mode="review",
            agent_runtimes=frozenset({"codex"}),
        ),
        Workdir(
            "Docs",
            Path("/workdirs/03"),
            None,
            agent_mode="review",
            agent_runtimes=frozenset({"codex"}),
        ),
    ]
    clients, alias = load_linux_core_client(
        str(root / "core-bindings/core-blue/core-bridge-binding.json"),
        instance_id="core-blue",
        bridge_id="bridge-blue",
        socket_path=saved["socket_path"],
        workdirs=workdirs,
    )
    assert alias is None and set(clients) == {"Repo", "Docs"}
    assert clients["Docs"]._policy_revision != clients["Repo"]._policy_revision


@pytest.mark.parametrize("invalid_slot", ["/workdirs/00", "/workdirs/17", "/workdirs/01/escape"])
def test_v026_provision_refuses_unmounted_slots_before_writes(
    tmp_path,
    monkeypatch,
    invalid_slot,
):
    manifest, root, work, _ = _fixture(tmp_path, monkeypatch)
    manifest["core"] = {
        "instance_id": "core-blue",
        "workdirs": [{"alias": "Repo", "container_path": invalid_slot, "host_path": str(work)}],
        "methods": ["bridge.ping", "runtime.list"],
    }
    with pytest.raises(ValueError, match="Compose workdir slot"):
        provision(manifest)
    assert not root.exists()


def test_v026_single_alias_schema_two_may_bind_only_slot_03(tmp_path, monkeypatch):
    manifest, root, work, _config_path = _fixture(tmp_path, monkeypatch)
    manifest["core"] = {
        "instance_id": "core-blue",
        "workdirs": [{"alias": "Repo", "container_path": "/workdirs/03", "host_path": str(work)}],
        "methods": ["bridge.ping", "runtime.list"],
    }
    assert provision(manifest) == root
    saved = json.loads((root / "core-bindings/core-blue/core-bridge-binding.json").read_text())
    assert saved["schema_version"] == 2
    assert saved["workdirs"][0]["workdir_path"] == "/workdirs/03"
    client, alias = load_linux_core_client(
        str(root / "core-bindings/core-blue/core-bridge-binding.json"),
        instance_id="core-blue",
        bridge_id="bridge-blue",
        socket_path=saved["socket_path"],
        workdirs=[Workdir("Repo", Path("/workdirs/03"), None, agent_mode="review")],
    )
    assert alias == "Repo"
    assert client._workdir_alias == "Repo"


def test_v025_legacy_single_alias_binding_must_remain_in_slot_01(tmp_path, monkeypatch):
    manifest, root, _work, _config_path = _fixture(tmp_path, monkeypatch)
    manifest["core"]["container_path"] = "/workdirs/03"
    with pytest.raises(ValueError, match="legacy single-alias"):
        provision(manifest)
    assert not root.exists()


def test_two_cores_provision_independent_grants_and_real_bindings(tmp_path, monkeypatch):
    manifest, root, work, config_path = _fixture(tmp_path, monkeypatch)
    work_two = tmp_path / "work-two"
    work_two.mkdir()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["workdirs"].append(
        {
            "alias": "Docs",
            "host_path": str(work_two),
            "read_only": True,
            "agent_mode": "review",
            "agent_runtimes": [],
        }
    )
    config_path.write_text(json.dumps(config), encoding="utf-8")
    manifest["cores"] = [
        manifest.pop("core"),
        {
            "instance_id": "core-green",
            "alias": "Docs",
            "container_path": "/workdirs/01",
            "host_path": str(work_two),
            "methods": ["bridge.ping"],
        },
    ]

    assert provision(manifest) == root
    grants_doc = json.loads((root / "bridge/state/core-grants.json").read_text())
    assert len(grants_doc["grants"]) == 2
    assert len({g["key_id"] for g in grants_doc["grants"]}) == 2
    assert len({g["secret_hex"] for g in grants_doc["grants"]}) == 2
    config = BridgeConfig.load(root / "config.json")
    verifier = load_private_core_grants(
        store=TaskStore(root / "bridge/state"), bridge_id="bridge-blue", policies=config.policies
    )
    assert {identity[0] for identity in verifier._grants} == {"core-blue", "core-green"}
    bindings_by_core = {}
    for core_id, alias in (("core-blue", "Repo"), ("core-green", "Docs")):
        binding_path = root / f"core-bindings/{core_id}/core-bridge-binding.json"
        binding = json.loads(binding_path.read_text())
        grant = verifier._grants[(core_id, binding["key_id"])]
        assert grant.revision_for(alias) == binding["policy_revision"]
        assert len(grant.workdirs) == 1 and grant.workdirs[0][0] == alias
        workdir_path = Path("/workdirs/01")
        client, loaded_alias = load_linux_core_client(
            str(binding_path),
            instance_id=core_id,
            bridge_id="bridge-blue",
            socket_path=binding["socket_path"],
            workdirs=[Workdir(alias, workdir_path, None, agent_mode="review")],
        )
        assert loaded_alias == alias
        assert client._key_id == binding["key_id"]
        bindings_by_core[core_id] = binding
    blue = bindings_by_core["core-blue"]
    envelope = {
        "protocol_version": 2,
        "bridge_id": "bridge-blue",
        "core_id": "core-blue",
        "key_id": blue["key_id"],
        "request_id": "r1",
        "method": "bridge.ping",
        "params": {"workdir": "Repo"},
        "policy_revision": blue["policy_revision"],
        "issued_at": 100,
        "nonce": "1" * 32,
    }
    envelope["signature"] = sign_core_request(envelope, bytes.fromhex(blue["secret_hex"]))
    assert verifier.authorize(envelope, now=100).core_id == "core-blue"
    cross_key = {
        **{key: value for key, value in envelope.items() if key != "signature"},
        "core_id": "core-green",
        "nonce": "2" * 32,
    }
    cross_key["signature"] = sign_core_request(cross_key, bytes.fromhex(blue["secret_hex"]))
    with pytest.raises(BridgeError, match="not authorized"):
        verifier.authorize(cross_key, now=100)
    wrong_alias = {
        **{key: value for key, value in envelope.items() if key != "signature"},
        "params": {"workdir": "Docs"},
        "nonce": "3" * 32,
    }
    wrong_alias["signature"] = sign_core_request(wrong_alias, bytes.fromhex(blue["secret_hex"]))
    with pytest.raises(BridgeError, match="not authorized"):
        verifier.authorize(wrong_alias, now=100)


def test_shared_alias_across_cores_keeps_same_policy_lease(tmp_path, monkeypatch):
    manifest, root, work, _config = _fixture(tmp_path, monkeypatch)
    original = manifest.pop("core")
    manifest["cores"] = [original, {**original, "instance_id": "core-green"}]
    assert provision(manifest) == root
    bindings = [
        json.loads((root / f"core-bindings/{core}/core-bridge-binding.json").read_text())
        for core in ("core-blue", "core-green")
    ]
    assert bindings[0]["workdir_alias"] == bindings[1]["workdir_alias"] == "Repo"
    assert bindings[0]["policy_revision"] == bindings[1]["policy_revision"]
    assert bindings[0]["key_id"] != bindings[1]["key_id"]
    assert bindings[0]["secret_hex"] != bindings[1]["secret_hex"]
    assert work.exists()


@pytest.mark.parametrize("bad", ["duplicate_id", "bad_mapping", "bad_methods", "unused_policy"])
def test_multi_core_invalid_later_entry_publishes_nothing(tmp_path, monkeypatch, bad):
    manifest, root, _work, config_path = _fixture(tmp_path, monkeypatch)
    second = {**manifest["core"], "instance_id": "core-green"}
    manifest["cores"] = [manifest.pop("core"), second]
    if bad == "duplicate_id":
        second["instance_id"] = "core-blue"
    elif bad == "bad_mapping":
        second["container_path"] = "/workdirs/02"
    elif bad == "bad_methods":
        second["methods"] = ["runtime.list"]
    else:
        extra = tmp_path / "unused"
        extra.mkdir()
        doc = json.loads(config_path.read_text())
        doc["workdirs"].append({"alias": "Unused", "host_path": str(extra), "read_only": True})
        config_path.write_text(json.dumps(doc))
    with pytest.raises((ValueError, TypeError)):
        provision(manifest)
    assert not root.exists()
    assert not list(root.parent.glob(".serverfs-v0250-*"))


def test_instance_parent_is_never_exposed_as_workdir(tmp_path, monkeypatch):
    manifest, root, _work, config_path = _fixture(tmp_path, monkeypatch)
    instances = root.parent
    doc = json.loads(config_path.read_text())
    doc["workdirs"][0]["host_path"] = str(instances)
    doc["workdirs"][0]["read_only"] = True
    doc["workdirs"][0]["agent_mode"] = "review"
    doc["workdirs"][0]["agent_runtimes"] = []
    config_path.write_text(json.dumps(doc))
    manifest["core"]["host_path"] = str(instances)
    manifest["core"]["methods"] = ["bridge.ping"]
    with pytest.raises(ValueError, match="overlaps"):
        provision(manifest)
    assert not root.exists()


def test_cores_bounds_mutual_exclusion_and_bounded_nonblocking_inputs(tmp_path, monkeypatch):
    manifest, root, _work, _config = _fixture(tmp_path, monkeypatch)
    core = manifest.pop("core")
    manifest["cores"] = [core] * 33
    with pytest.raises(ValueError, match="between 1 and 32"):
        provision(manifest)
    assert not root.exists()

    fifo = tmp_path / "manifest.fifo"
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="regular file"):
        _read_input(fifo, "manifest")

    large = tmp_path / "large.json"
    large.write_bytes(b" " * (1024 * 1024 + 1))
    with pytest.raises(ValueError, match="bounded regular file"):
        _read_input(large, "manifest")


def test_distinct_alias_roots_may_not_overlap_even_if_readonly(tmp_path, monkeypatch):
    manifest, root, work, config_path = _fixture(tmp_path, monkeypatch)
    child = work / "child"
    child.mkdir()
    doc = json.loads(config_path.read_text())
    doc["workdirs"][0]["read_only"] = True
    doc["workdirs"][0]["agent_mode"] = "review"
    doc["workdirs"][0]["agent_runtimes"] = []
    doc["workdirs"].append(
        {"alias": "Child", "host_path": str(child), "read_only": True, "agent_mode": "review"}
    )
    config_path.write_text(json.dumps(doc))
    original = manifest.pop("core")
    original["methods"] = ["bridge.ping"]
    manifest["cores"] = [
        original,
        {
            **original,
            "instance_id": "core-green",
            "alias": "Child",
            "host_path": str(child),
            "methods": ["bridge.ping"],
        },
    ]
    with pytest.raises(ValueError, match="overlap"):
        provision(manifest)
    assert not root.exists()


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda m: m["core"].update(instance_id="default"), "instance_id"),
        (lambda m: m["core"].update(container_path="/workdirs/02"), "container_path"),
        (lambda m: m["core"].update(methods=["runtime.list"]), "methods"),
        (lambda m: m.update(bridge_id="default"), "bridge_id"),
    ],
)
def test_bad_identity_mapping_and_methods_fail_before_publication(
    tmp_path, monkeypatch, change, message
):
    manifest, root, _work, _config_path = _fixture(tmp_path, monkeypatch)
    change(manifest)
    with pytest.raises(ValueError, match=message):
        provision(manifest)
    assert not root.exists()


def test_instance_root_overlap_with_readonly_workdir_is_rejected(tmp_path, monkeypatch):
    manifest, root, _work, _config = _fixture(tmp_path, monkeypatch)
    config_doc = json.loads(_config.read_text(encoding="utf-8"))
    config_doc["workdirs"][0]["host_path"] = str(root.parent)
    config_doc["workdirs"][0]["read_only"] = True
    config_doc["workdirs"][0]["agent_mode"] = "review"
    config_doc["workdirs"][0]["agent_runtimes"] = []
    _config.write_text(json.dumps(config_doc), encoding="utf-8")
    manifest["core"]["host_path"] = str(root.parent)
    manifest["core"]["methods"] = ["bridge.ping"]
    with pytest.raises(ValueError, match="overlaps"):
        provision(manifest)
    assert not root.exists()


def test_declared_workdir_ancestor_overlap_and_symlink_paths_fail(tmp_path, monkeypatch):
    manifest, root, work, _config = _fixture(tmp_path, monkeypatch)
    config_doc = json.loads(_config.read_text(encoding="utf-8"))
    config_doc["workdirs"][0]["read_only"] = False
    config_doc["workdirs"][0]["agent_mode"] = "workspace-write"
    _config.write_text(json.dumps(config_doc), encoding="utf-8")
    child = work / "nested"
    child.mkdir()
    manifest["existing_writable_roots"] = [str(child)]
    with pytest.raises(ValueError, match="overlap"):
        provision(manifest)
    assert not root.exists()

    link = tmp_path / "work-link"
    link.symlink_to(work, target_is_directory=True)
    manifest["existing_writable_roots"] = [str(link)]
    with pytest.raises(ValueError, match="symlink"):
        provision(manifest)
    assert not root.exists()


def test_existing_destination_is_never_overwritten(tmp_path, monkeypatch):
    manifest, root, _work, _config = _fixture(tmp_path, monkeypatch)
    root.mkdir(mode=0o700)
    marker = root / "keep"
    marker.write_text("untouched", encoding="utf-8")
    with pytest.raises(FileExistsError):
        provision(manifest)
    assert marker.read_text(encoding="utf-8") == "untouched"


def test_linux_socket_path_limit_rejected_before_publication(tmp_path, monkeypatch):
    long_home = tmp_path / ("h" * 100)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: long_home))
    manifest, root, _work, _config = _fixture(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="AF_UNIX limit"):
        provision(manifest)
    assert not root.exists()
    assert not list(root.parent.glob(".serverfs-v0250-*"))


def test_null_cores_is_a_validation_error(tmp_path, monkeypatch):
    manifest, root, _work, _config = _fixture(tmp_path, monkeypatch)
    manifest.pop("core")
    manifest["cores"] = None
    with pytest.raises(ValueError, match="between 1 and 32"):
        provision(manifest)
    assert not root.exists()


def test_duplicate_config_key_rejected(tmp_path, monkeypatch):
    manifest, root, _work, config = _fixture(tmp_path, monkeypatch)
    raw = config.read_text()
    config.write_text(raw[:-1] + ', "bridge_id": "bridge-blue"}')
    with pytest.raises(ValueError, match="duplicate JSON field"):
        provision(manifest)
    assert not root.exists()


def test_file_only_policy_does_not_create_unusable_signed_binding(tmp_path, monkeypatch):
    manifest, root, _work, config = _fixture(tmp_path, monkeypatch)
    doc = json.loads(config.read_text())
    doc["workdirs"][0].update(agent_mode="disabled", agent_runtimes=[])
    config.write_text(json.dumps(doc))
    manifest["core"]["methods"] = ["bridge.ping"]
    with pytest.raises(ValueError, match="Agent or Repo Ops-enabled"):
        provision(manifest)
    assert not root.exists()


def test_workdir_cannot_expose_instance_runtime_parent(tmp_path, monkeypatch):
    manifest, root, _work, config = _fixture(tmp_path, monkeypatch)
    runtime = Path.home() / ".local/share/serverfs-agent-bridge/instances"
    runtime.mkdir(parents=True)
    doc = json.loads(config.read_text())
    doc["workdirs"][0]["host_path"] = str(runtime)
    config.write_text(json.dumps(doc))
    manifest["core"]["host_path"] = str(runtime)
    with pytest.raises(ValueError, match="overlaps"):
        provision(manifest)
    assert not root.exists()


@pytest.mark.parametrize("systemd", [False, True], ids=["process", "systemd-user"])
def test_provisioned_bridge_cli_lifecycle(tmp_path, monkeypatch, systemd):
    """Actual CLI, persisted grants/state, isolated fake runtime; no provider credentials."""
    if systemd and os.environ.get("SERVERFS_LINUX_SYSTEMD_TEST") != "1":
        pytest.skip("explicit opt-in to transient systemd user unit required")
    manifest, root, work, config_path = _fixture(tmp_path, monkeypatch)
    doc = json.loads(config_path.read_text())
    doc.pop("codex")
    doc["enable_fake_runtime"] = True
    doc["workdirs"][0]["agent_runtimes"] = ["fake"]
    config_path.write_text(json.dumps(doc))
    manifest["core"]["methods"] = ["bridge.ping", "task.submit", "task.get"]
    assert provision(manifest) == root
    binding = json.loads((root / "core-bindings/core-blue/core-bridge-binding.json").read_text())
    socket_path = root / "bridge/run/bridge.sock"
    client = CoreBridgeClientV2(
        socket_path,
        bridge_id=binding["bridge_id"],
        core_id=binding["core_id"],
        key_id=binding["key_id"],
        secret=bytes.fromhex(binding["secret_hex"]),
        workdir_alias=binding["workdir_alias"],
        policy_revision=binding["policy_revision"],
        timeout_seconds=1,
    )
    clean_env = {
        "PATH": os.defpath,
        "HOME": str(Path.home()),
        "PYTHONPATH": str(ROOT / "agent_bridge/src"),
        "PYTHONUNBUFFERED": "1",
    }
    argv = [
        sys.executable,
        "-m",
        "serverfs_agent_bridge.main",
        "--config",
        str(root / "config.json"),
    ]
    log = tmp_path / "bridge.log"
    process = None
    unit = "serverfs-v25-fixture-" + uuid.uuid4().hex[:12] + ".service"
    systemctl = shutil.which("systemctl")
    systemd_run = shutil.which("systemd-run")
    unit_created = False
    bus_env = {
        "PATH": os.defpath,
        "HOME": os.environ["HOME"],
        "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"),
    }
    if "DBUS_SESSION_BUS_ADDRESS" in os.environ:
        bus_env["DBUS_SESSION_BUS_ADDRESS"] = os.environ["DBUS_SESSION_BUS_ADDRESS"]

    def ctl(*args, check=True):
        return subprocess.run(
            [systemctl, "--user", *args],
            env=bus_env,
            capture_output=True,
            text=True,
            timeout=20,
            check=check,
        )

    def start():
        nonlocal process, unit_created
        if systemd:
            assert systemctl and systemd_run
            manager = ctl("show", "--property=Version", check=False)
            assert manager.returncode == 0, "systemd user manager unavailable"
            absent = ctl("show", unit, "--property=LoadState", "--value", check=False)
            assert absent.stdout.strip() == "not-found"
            result = subprocess.run(
                [
                    systemd_run,
                    "--user",
                    "--collect",
                    "--unit",
                    unit,
                    "--property=Type=exec",
                    "--property=Restart=on-failure",
                    "--property=KillMode=control-group",
                    "--property=TimeoutStopSec=45s",
                    "--working-directory",
                    str(work),
                    "/usr/bin/env",
                    "-i",
                    *(f"{key}={value}" for key, value in clean_env.items()),
                    *argv,
                ],
                env=bus_env,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            # Never stop an unrelated unit if the create request was refused.
            assert result.returncode == 0, result.stderr
            unit_created = True
        else:
            with log.open("ab") as output:
                process = subprocess.Popen(
                    argv, cwd=work, env=clean_env, stdout=output, stderr=subprocess.STDOUT
                )

    def stop():
        nonlocal process, unit_created
        if systemd and unit_created:
            ctl("stop", unit)
            assert ctl("is-active", unit, check=False).returncode != 0
            unit_created = False
        elif process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                    pytest.fail("Bridge CLI did not stop gracefully")
            assert process.returncode == 0
            process = None

    async def ready():
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            try:
                assert await client.call("bridge.ping", {}) == {"status": "ok"}
                return
            except CoreBridgeUnavailable:
                if process is not None and process.poll() is not None:
                    pytest.fail("Bridge CLI exited before readiness: " + log.read_text()[-2000:])
                await asyncio.sleep(0.1)
        pytest.fail("provisioned Bridge did not become ready")

    async def exercise():
        try:
            start()
            await ready()
            submitted = await client.call(
                "task.submit",
                {
                    "runtime": "fake",
                    "path": ".",
                    "profile": "review",
                    "prompt": "complete:provisioned-cli-ok",
                },
            )
            task_id = submitted["task_id"]
            for _ in range(100):
                task = await client.call("task.get", {"task_id": task_id})
                if task["status"] == "succeeded":
                    break
                assert task["status"] not in {"failed", "cancelled", "timed_out"}
                await asyncio.sleep(0.05)
            assert task["status"] == "succeeded"
            assert task["final_response"] == "provisioned-cli-ok"
            before = socket_path.parent.stat()
            if systemd:
                old_pid = ctl("show", unit, "--property=MainPID", "--value").stdout.strip()
                ctl("restart", unit)
                await ready()
                new_pid = ctl("show", unit, "--property=MainPID", "--value").stdout.strip()
                assert old_pid != new_pid and int(new_pid) > 0
            else:
                stop()
                assert not socket_path.exists()
                with pytest.raises(CoreBridgeUnavailable):
                    await client.call("bridge.ping", {})
                start()
                await ready()
            after = socket_path.parent.stat()
            assert (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
            task = await client.call("task.get", {"task_id": task_id})
            assert task["status"] == "succeeded"
            assert task["final_response"] == "provisioned-cli-ok"
        finally:
            stop()
            assert not socket_path.exists()

    asyncio.run(exercise())


@pytest.fixture
def rendered_attachment(tmp_path, monkeypatch):
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker Compose CLI unavailable")
    manifest, root, work, config = _fixture(tmp_path, monkeypatch)
    doc = json.loads(config.read_text())
    doc["workdirs"][0]["repo_ops_enabled"] = True
    config.write_text(json.dumps(doc))
    provision(manifest)
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / ".serverfs-disabled").touch()
    env_file = tmp_path / "compose.env"
    env_file.touch(mode=0o600)
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(Path.home()),
        "SERVERFS_UID": str(os.getuid()),
        "SERVERFS_GID": str(os.getgid()),
        "SERVERFS_LINUX_INSTANCE_ID": "core-blue",
        "SERVERFS_LINUX_BRIDGE_ID": "bridge-blue",
        "SERVERFS_LINUX_BRIDGE_SOCKET_DIR": str(root / "bridge/run"),
        "SERVERFS_LINUX_BRIDGE_LOCK_DIR": str(root / "bridge/locks"),
        "SERVERFS_LINUX_CORE_BINDING_DIR": str(root / "core-bindings/core-blue"),
        "WORKDIR_01_ALIAS": "Repo",
        "WORKDIR_01_PATH": str(work),
        "WORKDIR_01_READ_ONLY": "false",
        "WORKDIR_01_AGENT_MODE": "workspace-write",
        "WORKDIR_01_AGENT_RUNTIMES": "codex",
        "WORKDIR_01_REPO_OPS_ENABLED": "true",
        "SERVERFS_HTTP_API_KEY": "synthetic-preflight-token",
    }
    for slot in range(2, 17):
        env[f"WORKDIR_{slot:02d}_PATH"] = str(empty)
    result = subprocess.run(
        [
            docker,
            "compose",
            "--env-file",
            str(env_file),
            "-p",
            "preflight-fixture",
            "-f",
            str(ROOT / "compose.yml"),
            "-f",
            str(ROOT / "compose.http.yml"),
            "-f",
            str(ROOT / "deployment/linux/compose.publish-http.yml"),
            "-f",
            str(ROOT / "deployment/linux/compose.bridge-v2.yml"),
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    return json.loads(result.stdout), root, work


def _validate_rendered(model):
    validate(model, bridge_id="bridge-blue", core_id="core-blue", project="preflight-fixture")


def test_real_compose_preflight_is_read_only(rendered_attachment):
    model, root, _work = rendered_attachment
    before = {str(p): (p.stat().st_mtime_ns, p.stat().st_size) for p in root.rglob("*")}
    _validate_rendered(model)
    after = {str(p): (p.stat().st_mtime_ns, p.stat().st_size) for p in root.rglob("*")}
    assert before == after


@pytest.mark.parametrize(
    ("change", "stage"),
    [
        ("project", "project"),
        ("uid", "container-user"),
        ("privileged", "container-isolation"),
        ("wrong-id", "bridge-environment"),
        ("wrong-alias", "workdir-policy"),
        ("mode", "workdir-policy"),
        ("runtimes", "runtime-policy"),
        ("repo", "workdir-policy"),
        ("global-agent", "global-agent-policy"),
        ("wrong-host", "workdir-policy"),
        ("write-flag", "workdir-policy"),
        ("binding-rw", "bridge-mount"),
        ("wrong-binding", "bridge-mount"),
        ("extra-mount", "mount-layout"),
        ("extra-agent", "additional-agent-slot"),
        ("disabled-rw", "disabled-slot"),
        ("private-root", "private-root-exposure"),
        ("overlap", "workdir-overlap"),
        ("duplicate-alias", "additional-file-slot"),
    ],
)
def test_preflight_rejects_drift(rendered_attachment, change, stage):
    original, root, work = rendered_attachment
    model = copy.deepcopy(original)
    core = model["services"]["serverfs-mcp"]
    env = core["environment"]
    mounts = {item["target"]: item for item in core["volumes"]}
    if change == "project":
        model["name"] = "wrong-project"
    elif change == "uid":
        core["user"] = "0:0"
    elif change == "privileged":
        core["privileged"] = True
    elif change == "wrong-id":
        env["SERVERFS_LINUX_INSTANCE_ID"] = "core-other"
    elif change == "wrong-alias":
        env["WORKDIR_01_ALIAS"] = "Wrong"
    elif change == "mode":
        env["WORKDIR_01_AGENT_MODE"] = "review"
    elif change == "runtimes":
        env["WORKDIR_01_AGENT_RUNTIMES"] = "claude"
    elif change == "repo":
        env["WORKDIR_01_REPO_OPS_ENABLED"] = "false"
    elif change == "global-agent":
        env["SERVERFS_AGENT_MODE"] = "workspace-write"
    elif change == "wrong-host":
        mounts["/workdirs/01"]["source"] = str(work.parent)
    elif change == "write-flag":
        mounts["/workdirs/01"]["read_only"] = True
    elif change == "binding-rw":
        mounts["/run/serverfs-core-bridge-binding"]["read_only"] = False
    elif change == "wrong-binding":
        mounts["/run/serverfs-core-bridge-binding"]["source"] = str(root)
    elif change == "extra-mount":
        core["volumes"].append({"type": "bind", "source": str(root), "target": "/extra"})
    elif change == "extra-agent":
        env["WORKDIR_02_AGENT_MODE"] = "workspace-write"
    elif change == "disabled-rw":
        mounts["/workdirs/02"]["read_only"] = False
    elif change == "private-root":
        mounts["/workdirs/02"]["source"] = str(root)
    elif change in {"overlap", "duplicate-alias"}:
        env["WORKDIR_02_ALIAS"] = "Repo" if change == "duplicate-alias" else "Other"
        mounts["/workdirs/02"]["source"] = str(work)
    else:
        pytest.fail("unknown test mutation")
    with pytest.raises(PreflightError, match=stage):
        _validate_rendered(model)


def test_preflight_detects_revoked_or_mismatched_key(rendered_attachment):
    model, root, _work = rendered_attachment
    path = root / "core-bindings/core-blue/core-bridge-binding.json"
    doc = json.loads(path.read_text())
    path.chmod(0o600)
    doc["secret_hex"] = "ff" * 32
    path.write_text(json.dumps(doc))
    with pytest.raises(PreflightError, match="grant-binding") as exc:
        _validate_rendered(model)
    assert doc["secret_hex"] not in str(exc.value)


def test_preflight_allows_disjoint_file_only_slot(rendered_attachment, tmp_path):
    model, _root, _work = rendered_attachment
    extra = tmp_path / "extra-files"
    extra.mkdir()
    core = model["services"]["serverfs-mcp"]
    core["environment"]["WORKDIR_02_ALIAS"] = "Documents"
    mount = next(v for v in core["volumes"] if v["target"] == "/workdirs/02")
    mount["source"] = str(extra)
    _validate_rendered(model)


@pytest.fixture
def release_fixture(tmp_path, monkeypatch):
    manifest, config_root, _work, _config = _fixture(tmp_path, monkeypatch)
    provision(manifest)
    source = tmp_path / "release-source"
    source.mkdir()
    (source / "pyproject.toml").write_text("[project]\nname='fixture'\n")
    (source / "uv.lock").write_text("fixture")
    (source / "src").mkdir()
    (source / "src" / "fixture.py").write_text("pass\n")
    uv = tmp_path / "uv"
    uv.touch(mode=0o700)
    legacy = Path.home() / ".local/share/serverfs-agent-bridge"
    legacy.mkdir(parents=True)
    (legacy / "old-release").mkdir()
    (legacy / "current").symlink_to(legacy / "old-release")
    calls = []

    def execute(argv, *, cwd, stage, **kwargs):
        calls.append((stage, cwd))
        if stage == "uv-sync":
            assert kwargs["env"]["UV_PROJECT_ENVIRONMENT"] == str(cwd / ".venv")
            assert "VIRTUAL_ENV" not in kwargs["env"]
            assert "--frozen" in argv and "--no-editable" in argv
            bindir = cwd / ".venv/bin"
            bindir.mkdir(parents=True)
            (bindir / "serverfs-agent-bridge").touch(mode=0o700)

    monkeypatch.setattr(installer, "_run", execute)
    monkeypatch.setattr(installer, "_require_stopped", lambda bridge_id: None)
    return source, uv, config_root, legacy, calls


def test_release_stage_select_upgrade_and_explicit_rollback(release_fixture):
    source, uv, config_root, legacy, calls = release_fixture
    before = {str(p): (p.stat().st_mtime_ns, p.stat().st_size) for p in config_root.rglob("*")}
    first = installer.stage("bridge-blue", "candidate-1", source, uv)
    app = installer._app_root("bridge-blue")
    assert not (app / "current").exists()
    assert (first / installer._MARKER).is_file()
    assert installer.select("bridge-blue", "candidate-1") == first
    assert (app / "current").resolve() == first
    second = installer.stage("bridge-blue", "candidate-2", source, uv, offline=True)
    assert (app / "current").resolve() == first
    installer.select("bridge-blue", "candidate-2")
    assert (app / "current").resolve() == second
    installer.select("bridge-blue", "candidate-1")
    assert (app / "current").resolve() == first
    assert first.is_dir() and second.is_dir()
    assert (legacy / "current").resolve() == legacy / "old-release"
    assert sum(stage == "release-config" for stage, _ in calls) == 5
    assert before == {
        str(p): (p.stat().st_mtime_ns, p.stat().st_size) for p in config_root.rglob("*")
    }


def test_failed_stage_does_not_replace_current(release_fixture, monkeypatch):
    source, uv, _config_root, _legacy, _calls = release_fixture
    first = installer.stage("bridge-blue", "stable", source, uv)
    installer.select("bridge-blue", "stable")

    def fail(*args, **kwargs):
        raise installer.InstallError("uv-sync")

    monkeypatch.setattr(installer, "_run", fail)
    with pytest.raises(installer.InstallError, match="uv-sync"):
        installer.stage("bridge-blue", "broken", source, uv)
    assert (first.parent.parent / "current").resolve() == first
    assert not (first.parent / "broken").exists()
    with pytest.raises(FileExistsError):
        installer.stage("bridge-blue", "stable", source, uv)


def test_select_refuses_unready_and_active_release(release_fixture, monkeypatch):
    source, uv, _config_root, _legacy, _calls = release_fixture
    first = installer.stage("bridge-blue", "stable", source, uv)
    installer.select("bridge-blue", "stable")
    second = installer.stage("bridge-blue", "second", source, uv)
    marker = second / installer._MARKER
    marker.unlink()
    with pytest.raises((ValueError, OSError)):
        installer.select("bridge-blue", "second")

    def active(_bridge_id):
        raise installer.InstallError("unit-not-stopped")

    monkeypatch.setattr(installer, "_require_stopped", active)
    with pytest.raises(installer.InstallError, match="unit-not-stopped"):
        installer.select("bridge-blue", "stable")
    assert (first.parent.parent / "current").resolve() == first


@pytest.mark.parametrize(
    ("load", "active", "code", "ok"),
    [
        ("loaded", "inactive", 0, True),
        ("not-found", "inactive", 1, True),
        ("loaded", "active", 0, False),
        ("loaded", "activating", 0, False),
        ("loaded", "failed", 0, False),
        ("", "", 1, False),
    ],
)
def test_service_state_parser(monkeypatch, load, active, code, ok):
    monkeypatch.setattr(
        installer.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            [], code, f"LoadState={load}\nActiveState={active}\n", ""
        ),
    )
    if ok:
        installer._require_stopped("bridge-blue")
    else:
        with pytest.raises(installer.InstallError, match="unit-not-stopped"):
            installer._require_stopped("bridge-blue")


def test_template_publication_idempotent_and_never_overwrites(tmp_path):
    source = ROOT / "deployment/linux/serverfs-agent-bridge@.service"
    path = installer.install_template(source)
    assert path.read_bytes() == source.read_bytes()
    assert installer.install_template(source) == path
    path.write_text("operator custom unit")
    with pytest.raises(installer.InstallError, match="template-conflict"):
        installer.install_template(source)
    assert path.read_text() == "operator custom unit"


@pytest.mark.skipif(
    os.environ.get("SERVERFS_LINUX_INSTALL_TEST") not in {"1", "offline", "online"},
    reason="opt-in uv release packaging",
)
def test_actual_release_packaging(tmp_path, monkeypatch):
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv unavailable")
    manifest, old_root, work, config_path = _fixture(tmp_path, monkeypatch)
    bridge_id = "fixture-" + uuid.uuid4().hex[:10]
    root = old_root.parent / bridge_id
    manifest.update(bridge_id=bridge_id, root=str(root))
    doc = json.loads(config_path.read_text())
    doc.update(
        bridge_id=bridge_id,
        socket_path=str(root / "bridge/run/bridge.sock"),
        state_dir=str(root / "bridge/state"),
        lock_dir=str(root / "bridge/locks"),
        enable_fake_runtime=True,
    )
    doc.pop("codex")
    doc["workdirs"][0]["agent_runtimes"] = ["fake"]
    config_path.write_text(json.dumps(doc))
    manifest["core"]["methods"] = ["bridge.ping", "task.submit", "task.get"]
    provision(manifest)
    # Preserve the actual installer and state store across both selections.
    # An external controller can provide the exact historical release source.
    offline = os.environ["SERVERFS_LINUX_INSTALL_TEST"] != "online"
    baseline_source = Path(
        os.environ.get("SERVERFS_LINUX_BASELINE_BRIDGE_SOURCE", str(ROOT / "agent_bridge"))
    )
    baseline = installer.stage(
        bridge_id, "fixture-baseline", baseline_source, Path(uv), offline=offline
    )
    target = installer.stage(
        bridge_id, "fixture-candidate", ROOT / "agent_bridge", Path(uv), offline=offline
    )
    assert (baseline / installer._MARKER).is_file()
    assert (target / installer._MARKER).is_file()
    current = target.parent.parent / "current"
    assert not current.exists()
    if "XDG_RUNTIME_DIR" not in os.environ:
        monkeypatch.setenv("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    template = ROOT / "deployment/linux/serverfs-agent-bridge@.service"
    assert installer.install_template(template).read_bytes() == template.read_bytes()
    # Run the installed entrypoint as a disposable process, not a manager unit.
    # The real systemd lifecycle has a separate opt-in acceptance test.
    binding_path = root / "core-bindings/core-blue/core-bridge-binding.json"
    binding_bytes = binding_path.read_bytes()
    binding = json.loads(binding_bytes)
    socket_path = root / "bridge/run/bridge.sock"
    state_dir = root / "bridge/state"
    state_identity = (state_dir.stat().st_dev, state_dir.stat().st_ino)
    socket_identity = (socket_path.parent.stat().st_dev, socket_path.parent.stat().st_ino)
    client = CoreBridgeClientV2(
        socket_path,
        bridge_id=bridge_id,
        core_id=binding["core_id"],
        key_id=binding["key_id"],
        secret=bytes.fromhex(binding["secret_hex"]),
        workdir_alias=binding["workdir_alias"],
        policy_revision=binding["policy_revision"],
        timeout_seconds=1,
    )
    retained: dict[str, str] = {}

    def run_selected(label: str, release: Path, marker: str | None) -> None:
        assert installer.select(bridge_id, label) == release
        assert current.resolve() == release
        executable = current / ".venv/bin/serverfs-agent-bridge"
        log = tmp_path / f"{label}.log"
        with log.open("wb") as output:
            process = subprocess.Popen(
                [str(executable), "--config", str(root / "config.json")],
                cwd=work,
                env={"HOME": str(Path.home()), "PATH": os.defpath, "PYTHONUNBUFFERED": "1"},
                stdout=output,
                stderr=subprocess.STDOUT,
            )

        async def exercise():
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    assert await client.call("bridge.ping", {}) == {"status": "ok"}
                    break
                except CoreBridgeUnavailable:
                    assert process.poll() is None, log.read_text()[-2000:]
                    await asyncio.sleep(0.1)
            else:
                pytest.fail("installed Bridge not ready")
            for task_id, expected in retained.items():
                task = await client.call("task.get", {"task_id": task_id})
                assert task["status"] == "succeeded"
                assert task["final_response"] == expected
            if marker is None:
                return
            submitted = await client.call(
                "task.submit",
                {
                    "runtime": "fake",
                    "path": ".",
                    "profile": "review",
                    "prompt": f"complete:{marker}",
                },
            )
            task_id = submitted["task_id"]
            for _ in range(100):
                task = await client.call("task.get", {"task_id": task_id})
                if task["status"] == "succeeded":
                    assert task["final_response"] == marker
                    retained[task_id] = marker
                    return
                assert task["status"] not in {"failed", "cancelled", "timed_out"}
                await asyncio.sleep(0.05)
            pytest.fail("installed Bridge task did not succeed")

        try:
            asyncio.run(exercise())
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                    pytest.fail("installed Bridge did not stop gracefully")
            assert process.returncode == 0
            assert not socket_path.exists()
        assert (state_dir.stat().st_dev, state_dir.stat().st_ino) == state_identity
        assert (
            socket_path.parent.stat().st_dev,
            socket_path.parent.stat().st_ino,
        ) == socket_identity
        assert binding_path.read_bytes() == binding_bytes

    run_selected("fixture-baseline", baseline, "baseline-result")
    run_selected("fixture-candidate", target, "candidate-result")
    run_selected("fixture-baseline", baseline, None)
    assert len(retained) == 2
    assert current.resolve() == baseline


@pytest.mark.skipif(
    os.environ.get("SERVERFS_LINUX_INSTALL_TEST") not in {"1", "offline", "online"}
    or os.environ.get("SERVERFS_LINUX_INSTALL_SYSTEMD_ACCEPT") != "1",
    reason="explicit opt-in to installed release and transient systemd lifecycle acceptance",
)
def test_actual_release_systemd_upgrade_rollback(tmp_path, monkeypatch):
    """Exercise two installed releases through the exact per-instance unit identity."""
    uv = shutil.which("uv")
    systemctl = shutil.which("systemctl")
    systemd_run = shutil.which("systemd-run")
    if not uv or not systemctl or not systemd_run:
        pytest.skip("uv or systemd user commands unavailable")

    manifest, initial_root, work, config_path = _fixture(tmp_path, monkeypatch)
    bridge_id = "r" + uuid.uuid4().hex[:8]
    root = initial_root.parent / bridge_id
    manifest.update(bridge_id=bridge_id, root=str(root))
    doc = json.loads(config_path.read_text(encoding="utf-8"))
    doc.update(
        bridge_id=bridge_id,
        socket_path=str(root / "bridge/run/bridge.sock"),
        state_dir=str(root / "bridge/state"),
        lock_dir=str(root / "bridge/locks"),
        enable_fake_runtime=True,
    )
    doc.pop("codex")
    doc["workdirs"][0]["agent_runtimes"] = ["fake"]
    config_path.write_text(json.dumps(doc), encoding="utf-8")
    manifest["core"]["methods"] = ["bridge.ping", "task.submit", "task.get"]
    provision(manifest)

    # Use the fixture HOME for both installer path resolution and the service process.
    fixture_home = Path.home()
    original_home = os.environ.get("HOME", str(fixture_home))
    uv_cache = os.environ.get("UV_CACHE_DIR", str(Path(original_home) / ".cache/uv"))
    monkeypatch.setenv("UV_CACHE_DIR", uv_cache)
    # The isolated HOME hides uv-managed Python installations. Use the already
    # running test interpreter's base executable, not an offline download.
    monkeypatch.setenv("UV_PYTHON", str(Path(sys._base_executable).resolve(strict=True)))
    monkeypatch.setenv("HOME", str(fixture_home))
    if "XDG_RUNTIME_DIR" not in os.environ:
        monkeypatch.setenv("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    bus_env = {
        "PATH": os.defpath,
        "HOME": str(fixture_home),
        "XDG_RUNTIME_DIR": os.environ["XDG_RUNTIME_DIR"],
    }
    if "DBUS_SESSION_BUS_ADDRESS" in os.environ:
        bus_env["DBUS_SESSION_BUS_ADDRESS"] = os.environ["DBUS_SESSION_BUS_ADDRESS"]

    unit = f"serverfs-agent-bridge@{bridge_id}.service"

    def ctl(*args, check=True):
        return subprocess.run(
            [systemctl, "--user", *args],
            env=bus_env,
            capture_output=True,
            text=True,
            timeout=20,
            check=check,
        )

    manager = ctl("show", "--property=Version", check=False)
    assert manager.returncode == 0, "systemd user manager unavailable"
    initial_unit = ctl(
        "show",
        unit,
        "--property=LoadState",
        "--property=ActiveState",
        "--property=MainPID",
        check=False,
    )
    initial_fields = dict(
        line.split("=", 1) for line in initial_unit.stdout.splitlines() if "=" in line
    )
    assert initial_fields.get("LoadState") == "not-found", (
        "release acceptance requires a clean isolated systemd user manager; "
        "an installed template or unit must not be overwritten"
    )
    assert initial_fields.get("ActiveState") == "inactive"
    assert initial_fields.get("MainPID") in {None, "0"}

    offline = os.environ["SERVERFS_LINUX_INSTALL_TEST"] != "online"
    first = installer.stage(
        bridge_id, "fixture-release-a", ROOT / "agent_bridge", Path(uv), offline=offline
    )
    second = installer.stage(
        bridge_id, "fixture-release-b", ROOT / "agent_bridge", Path(uv), offline=offline
    )
    assert (first / installer._MARKER).is_file()
    assert (second / installer._MARKER).is_file()
    app_root = first.parent.parent
    assert not (app_root / "current").exists()
    assert installer.select(bridge_id, "fixture-release-a") == first
    current = app_root / "current"
    assert current.resolve() == first

    binding = json.loads(
        (root / "core-bindings/core-blue/core-bridge-binding.json").read_text(encoding="utf-8")
    )
    socket_path = root / "bridge/run/bridge.sock"
    state_dir = root / "bridge/state"
    socket_dir = socket_path.parent
    state_identity = (state_dir.stat().st_dev, state_dir.stat().st_ino)
    socket_dir_identity = (socket_dir.stat().st_dev, socket_dir.stat().st_ino)
    client = CoreBridgeClientV2(
        socket_path,
        bridge_id=bridge_id,
        core_id=binding["core_id"],
        key_id=binding["key_id"],
        secret=bytes.fromhex(binding["secret_hex"]),
        workdir_alias=binding["workdir_alias"],
        policy_revision=binding["policy_revision"],
        timeout_seconds=1,
    )
    unit_created = False

    def start(selected: Path) -> int:
        nonlocal unit_created
        state = ctl(
            "show",
            unit,
            "--property=LoadState",
            "--property=ActiveState",
            "--property=MainPID",
            check=False,
        )
        fields = dict(line.split("=", 1) for line in state.stdout.splitlines() if "=" in line)
        assert fields.get("LoadState") == "not-found", (
            "transient acceptance unit is already loaded or has a fragment"
        )
        assert fields.get("ActiveState") == "inactive" and fields.get("MainPID") in {None, "0"}
        executable = selected / ".venv/bin/serverfs-agent-bridge"
        command = [
            systemd_run,
            "--user",
            "--collect",
            "--unit",
            unit,
            "--property=Type=exec",
            "--property=Restart=on-failure",
            "--property=KillMode=control-group",
            "--property=TimeoutStopSec=45s",
            "--working-directory",
            str(work),
            "/usr/bin/env",
            "-i",
            f"HOME={fixture_home}",
            f"PATH={os.defpath}",
            "PYTHONUNBUFFERED=1",
            str(executable),
            "--config",
            str(root / "config.json"),
        ]
        result = subprocess.run(
            command, env=bus_env, capture_output=True, text=True, timeout=30, check=False
        )
        assert result.returncode == 0, "transient unit start failed: " + result.stderr
        # Only a successful creation grants this fixture ownership for cleanup.
        unit_created = True
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            state = ctl("show", unit, "--property=ActiveState", "--property=MainPID", check=False)
            fields = dict(line.split("=", 1) for line in state.stdout.splitlines() if "=" in line)
            if fields.get("ActiveState") == "active" and fields.get("MainPID", "0") != "0":
                return int(fields["MainPID"])
            time.sleep(0.1)
        pytest.fail("transient Bridge unit did not become active")

    def stop():
        nonlocal unit_created
        if not unit_created:
            return
        ctl("stop", unit, check=False)
        deadline = time.monotonic() + 12
        stopped = False
        while time.monotonic() < deadline:
            state = ctl("show", unit, "--property=ActiveState", "--property=MainPID", check=False)
            fields = dict(line.split("=", 1) for line in state.stdout.splitlines() if "=" in line)
            if fields.get("ActiveState") == "inactive" and fields.get("MainPID") in {None, "0"}:
                stopped = True
                break
            time.sleep(0.1)
        assert stopped, "transient Bridge unit did not stop"
        assert not socket_path.exists(), "Bridge socket remained after unit stop"
        unit_created = False

    async def wait_ready():
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            try:
                assert await client.call("bridge.ping", {}) == {"status": "ok"}
                return
            except CoreBridgeUnavailable:
                await asyncio.sleep(0.1)
        pytest.fail("selected installed Bridge did not answer signed Ping")

    async def create_task(marker: str) -> str:
        submitted = await client.call(
            "task.submit",
            {
                "runtime": "fake",
                "path": ".",
                "profile": "review",
                "prompt": f"complete:{marker}",
            },
        )
        task_id = submitted["task_id"]
        for _ in range(120):
            task = await client.call("task.get", {"task_id": task_id})
            if task["status"] == "succeeded":
                assert task["final_response"] == marker
                return task_id
            assert task["status"] not in {"failed", "cancelled", "timed_out"}
            await asyncio.sleep(0.05)
        pytest.fail(f"synthetic task did not finish: {marker}")

    def assert_selected_executable(selected: Path) -> int:
        detail = ctl("show", unit, "--property=ExecStart", "--property=MainPID")
        fields = dict(line.split("=", 1) for line in detail.stdout.splitlines() if "=" in line)
        assert str(selected / ".venv/bin/serverfs-agent-bridge") in fields["ExecStart"]
        return int(fields["MainPID"])

    task_a = task_b = None
    pid_a = pid_b = None
    try:
        pid_a = start(first)
        awaitable = asyncio.run(wait_ready())
        assert awaitable is None
        assert assert_selected_executable(first) == pid_a
        task_a = asyncio.run(create_task("release-a-task"))

        with pytest.raises(installer.InstallError, match="unit-not-stopped"):
            installer.select(bridge_id, "fixture-release-b")
        assert current.resolve() == first

        stop()
        assert ctl(
            "show", unit, "--property=ActiveState", "--value", check=False
        ).stdout.strip() in {
            "inactive",
            "",
        }
        assert installer.select(bridge_id, "fixture-release-b") == second
        assert current.resolve() == second
        pid_b = start(second)
        asyncio.run(wait_ready())
        assert assert_selected_executable(second) == pid_b
        assert pid_b != pid_a
        assert (state_dir.stat().st_dev, state_dir.stat().st_ino) == state_identity
        assert (socket_dir.stat().st_dev, socket_dir.stat().st_ino) == socket_dir_identity
        old_task = asyncio.run(client.call("task.get", {"task_id": task_a}))
        assert old_task["status"] == "succeeded"
        assert old_task["final_response"] == "release-a-task"
        task_b = asyncio.run(create_task("release-b-task"))

        stop()
        assert installer.select(bridge_id, "fixture-release-a") == first
        assert current.resolve() == first
        pid_rollback = start(first)
        asyncio.run(wait_ready())
        assert assert_selected_executable(first) == pid_rollback
        assert pid_rollback not in {pid_a, pid_b}
        for task_id, marker in ((task_a, "release-a-task"), (task_b, "release-b-task")):
            task = asyncio.run(client.call("task.get", {"task_id": task_id}))
            assert task["status"] == "succeeded"
            assert task["final_response"] == marker
        assert (state_dir.stat().st_dev, state_dir.stat().st_ino) == state_identity
        assert (socket_dir.stat().st_dev, socket_dir.stat().st_ino) == socket_dir_identity
    finally:
        stop()

    final_state = ctl("show", unit, "--property=LoadState", "--property=ActiveState", check=False)
    final_fields = dict(
        line.split("=", 1) for line in final_state.stdout.splitlines() if "=" in line
    )
    assert final_fields.get("ActiveState") in {"inactive", None}
    assert not socket_path.exists()
    assert current.resolve() == first
    print(f"systemd release acceptance cleaned: bridge_id={bridge_id} unit={unit}")
