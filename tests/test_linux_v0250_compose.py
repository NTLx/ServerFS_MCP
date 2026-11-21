"""Linux v0.25.0 Compose source contracts, using Docker Compose's renderer."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCKER = shutil.which("docker")
BASE = ["compose.yml", "compose.http.yml"]
PUBLISH = "deployment/linux/compose.publish-http.yml"
DIRECT = [*BASE, PUBLISH]
OPENAI = "deployment/linux/compose.openai-http.yml"
CLOUDFLARE = ["compose.cloudflare.yml", "deployment/linux/compose.cloudflare-profile.yml"]
BRIDGE_V2 = "deployment/linux/compose.bridge-v2.yml"


def _compose(
    tmp_path: Path,
    *files: str,
    profiles: tuple[str, ...] = (),
    expect_ok: bool = True,
    extra_env: dict[str, str] | None = None,
    project_name: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict[str, object] | None]:
    assert DOCKER is not None
    env = {
        name: os.environ[name]
        for name in ("PATH", "HOME", "DOCKER_CONFIG", "DOCKER_HOST", "DOCKER_CONTEXT")
        if name in os.environ
    }
    env["COMPOSE_PROJECT_NAME"] = "serverfs-linux-v0250-test"
    if "openai-http" in profiles:
        env["CONTROL_PLANE_TUNNEL_ID"] = "tunnel_0123456789abcdef0123456789abcdef"
        env["OPENAI_CONTROL_PLANE_KEY_FILE"] = str(tmp_path / "control-key")
        env["SERVERFS_CORE_BEARER_FILE"] = str(tmp_path / "core-bearer")
    if "cloudflare" in profiles:
        env["CLOUDFLARE_TUNNEL_TOKEN_FILE"] = str(tmp_path / "cloudflare-token")
    if BRIDGE_V2 in files:
        for name, value in {
            "SERVERFS_UID": "1000",
            "SERVERFS_GID": "1000",
            "SERVERFS_LINUX_INSTANCE_ID": "v0250-test-instance",
            "SERVERFS_LINUX_BRIDGE_ID": "v0250-test-bridge",
            "SERVERFS_LINUX_BRIDGE_SOCKET_DIR": str(tmp_path / "bridge-socket"),
            "SERVERFS_LINUX_BRIDGE_LOCK_DIR": str(tmp_path / "bridge-locks"),
            "SERVERFS_LINUX_CORE_BINDING_DIR": str(tmp_path / "core-binding"),
        }.items():
            env.setdefault(name, value)
        for name in ("bridge-socket", "bridge-locks", "core-binding"):
            (tmp_path / name).mkdir(exist_ok=True)
    if extra_env:
        env.update(extra_env)
    for name in ("control-key", "core-bearer", "cloudflare-token"):
        if ("openai-http" in profiles and name != "cloudflare-token") or (
            "cloudflare" in profiles and name == "cloudflare-token"
        ):
            (tmp_path / name).write_text("test-only-compose-fixture", encoding="utf-8")

    empty_env_file = tmp_path / "empty-compose.env"
    empty_env_file.touch(exist_ok=True)
    command = [DOCKER, "compose", "--env-file", str(empty_env_file)]
    if project_name is not None:
        command.extend(("-p", project_name))
    for compose_file in files:
        command.extend(("-f", compose_file))
    if profiles:
        env["COMPOSE_PROFILES"] = ",".join(profiles)
    result = subprocess.run(
        [*command, "config", "--format", "json"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if not expect_ok:
        return result, None
    assert result.returncode == 0, result.stderr
    return result, json.loads(result.stdout)


@pytest.mark.skipif(DOCKER is None, reason="Docker CLI unavailable; Compose rendering not verified")
def test_core_only_and_direct_http_need_no_tunnel_credentials(tmp_path: Path) -> None:
    _, internal = _compose(
        tmp_path,
        *BASE,
        "deployment/linux/compose.internal.yml",
        "deployment/linux/compose.bridge-disabled.yml",
    )
    services = internal["services"]
    assert set(services) == {"serverfs-mcp"}
    assert services["serverfs-mcp"].get("ports", []) == []
    assert "serverfs_http_ingress" not in services["serverfs-mcp"]["networks"]
    assert all(
        internal["networks"][name]["internal"] for name in services["serverfs-mcp"]["networks"]
    )
    assert services["serverfs-mcp"]["environment"]["SERVERFS_AGENT_BRIDGE_ENABLED"] == "false"
    assert all(
        volume.get("target") != "/run/serverfs-agent-bridge"
        for volume in services["serverfs-mcp"]["volumes"]
    )

    _, direct = _compose(tmp_path, *DIRECT)
    ports = direct["services"]["serverfs-mcp"]["ports"]
    assert ports[0]["host_ip"] == "127.0.0.1"
    assert ports[0]["published"] == "8000"
    assert "serverfs_http_ingress" in direct["services"]["serverfs-mcp"]["networks"]
    assert direct["networks"]["serverfs_http_ingress"].get("internal", False) is False
    assert direct["networks"]["serverfs_internal"]["internal"] is True
    assert "cloudflared" not in direct["services"]


@pytest.mark.skipif(DOCKER is None, reason="Docker CLI unavailable; Compose rendering not verified")
@pytest.mark.parametrize(
    ("profiles", "extra_files", "expected"),
    [
        (("openai-http",), (OPENAI,), {"serverfs-mcp", "openai-tunnel"}),
        (("cloudflare",), (*CLOUDFLARE,), {"serverfs-mcp", "cloudflared"}),
        (
            ("openai-http", "cloudflare"),
            (*CLOUDFLARE, OPENAI),
            {"serverfs-mcp", "cloudflared", "openai-tunnel"},
        ),
    ],
)
@pytest.mark.parametrize("publish", [False, True], ids=["internal", "published"])
def test_ingress_profiles_render_independently(
    tmp_path: Path,
    publish: bool,
    profiles: tuple[str, ...],
    extra_files: tuple[str, ...],
    expected: set[str],
) -> None:
    ingress = DIRECT if publish else [*BASE, "deployment/linux/compose.internal.yml"]
    _, config = _compose(tmp_path, *ingress, *extra_files, profiles=profiles)
    services = config["services"]
    assert set(services) == expected
    assert bool(services["serverfs-mcp"].get("ports")) == publish
    assert ("serverfs_http_ingress" in services["serverfs-mcp"]["networks"]) == publish
    assert "container_name" not in services["serverfs-mcp"]
    for name in expected - {"serverfs-mcp"}:
        assert "container_name" not in services[name]
        assert not services[name].get("ports")


@pytest.mark.skipif(DOCKER is None, reason="Docker CLI unavailable; Compose rendering not verified")
def test_cloudflare_overlay_requires_its_own_token_file(tmp_path: Path) -> None:
    result, _ = _compose(
        tmp_path,
        *DIRECT,
        *CLOUDFLARE,
        expect_ok=False,
    )
    assert result.returncode != 0
    assert "CLOUDFLARE_TUNNEL_TOKEN_FILE" in result.stderr


@pytest.mark.skipif(DOCKER is None, reason="Docker CLI unavailable; Compose rendering not verified")
def test_openai_http_uses_explicit_server_url_flags(tmp_path: Path) -> None:
    _, config = _compose(
        tmp_path,
        *DIRECT,
        OPENAI,
        profiles=("openai-http",),
    )
    service = config["services"]["openai-tunnel"]
    command = service["command"]
    assert command[command.index("--mcp.server-url") + 1] == "url=http://serverfs-mcp:8000/mcp"
    assert command.count("--control-plane.api-key") == 1
    assert not service["environment"].get("MCP_SERVER_URL")
    assert not service["environment"].get("CONTROL_PLANE_API_KEY")
    assert service["entrypoint"] == ["/bin/sh", "/opt/serverfs/tunnel-launcher.sh"]
    assert command[0] == "--control-plane.tunnel-id"
    assert command[command.index("--health.listen-addr") + 1] == "127.0.0.1:8080"
    assert service["environment"]["HEALTH_LISTEN_ADDR"] == "127.0.0.1:8080"
    assert service["healthcheck"]["test"] == [
        "CMD",
        "/usr/bin/tunnel-client",
        "health",
        "--port",
        "8080",
        "--require-control-plane-poll",
    ]
    mounts = {volume["target"]: volume for volume in service["volumes"]}
    overlay = (ROOT / OPENAI).read_text(encoding="utf-8")
    for target, filename in (
        ("/run/secrets/openai-control-plane-key", "control-key"),
        ("/run/secrets/serverfs-core-bearer", "core-bearer"),
    ):
        assert mounts[target]["source"] == str(tmp_path / filename)
        assert mounts[target]["read_only"]
        # Compose 2.40.3 omits explicitly false bind options in normalized JSON.
        # Require the source to opt out, and reject any rendered true value.
        assert mounts[target].get("bind", {}).get("create_host_path") is not True
        assert (
            f"target: {target}\n        read_only: true\n"
            "        bind:\n          create_host_path: false"
        ) in overlay


@pytest.mark.skipif(DOCKER is None, reason="Docker CLI unavailable; Compose rendering not verified")
@pytest.mark.parametrize(
    "variable",
    ["CONTROL_PLANE_TUNNEL_ID", "OPENAI_CONTROL_PLANE_KEY_FILE", "SERVERFS_CORE_BEARER_FILE"],
)
def test_openai_http_requires_explicit_credentials(tmp_path: Path, variable: str) -> None:
    result, _ = _compose(
        tmp_path,
        *DIRECT,
        OPENAI,
        profiles=("openai-http",),
        extra_env={variable: ""},
        expect_ok=False,
    )
    assert result.returncode != 0
    assert variable in result.stderr


@pytest.mark.skipif(DOCKER is None, reason="Docker CLI unavailable; Compose rendering not verified")
def test_independent_core_projects_render_unique_ports_and_workdir_paths(tmp_path: Path) -> None:
    rendered: list[tuple[str, dict[str, object]]] = []
    for instance, port in (("one", "18001"), ("two", "18002")):
        _, config = _compose(
            tmp_path,
            *DIRECT,
            "deployment/linux/compose.bridge-disabled.yml",
            extra_env={
                "COMPOSE_PROJECT_NAME": f"serverfs-v0250-{instance}",
                "SERVERFS_HTTP_PUBLISH_PORT": port,
                "WORKDIR_01_PATH": f"/srv/serverfs/{instance}",
                "WORKDIR_01_ALIAS": f"workspace-{instance}",
            },
            project_name=f"serverfs-v0250-{instance}",
        )
        rendered.append((instance, config))

    (first_name, first), (second_name, second) = rendered
    assert first_name != second_name
    assert first["name"] == "serverfs-v0250-one"
    assert second["name"] == "serverfs-v0250-two"
    for config, expected_port, expected_path, expected_alias in (
        (first, "18001", "/srv/serverfs/one", "workspace-one"),
        (second, "18002", "/srv/serverfs/two", "workspace-two"),
    ):
        core = config["services"]["serverfs-mcp"]
        assert core["ports"][0]["published"] == expected_port
        assert core["environment"]["WORKDIR_01_ALIAS"] == expected_alias
        workdir_mount = next(
            volume for volume in core["volumes"] if volume["target"] == "/workdirs/01"
        )
        assert workdir_mount["source"] == expected_path
        assert core["environment"]["SERVERFS_AGENT_BRIDGE_ENABLED"] == "false"
        assert all(
            volume["target"] not in {"/run/serverfs-agent-bridge", "/run/serverfs-agent-locks"}
            for volume in core["volumes"]
        )


@pytest.mark.skipif(DOCKER is None, reason="Docker CLI unavailable; Compose rendering not verified")
def test_signed_v2_overlay_is_opt_in_and_mounts_private_paths_read_only(tmp_path: Path) -> None:
    _, disabled = _compose(tmp_path, *DIRECT, "deployment/linux/compose.bridge-disabled.yml")
    disabled_env = disabled["services"]["serverfs-mcp"]["environment"]
    assert disabled_env["SERVERFS_AGENT_BRIDGE_ENABLED"] == "false"
    assert all(
        volume.get("target") != "/run/serverfs-core-bridge-binding"
        for volume in disabled["services"]["serverfs-mcp"]["volumes"]
    )

    _, config = _compose(
        tmp_path,
        *DIRECT,
        BRIDGE_V2,
        extra_env={"WORKDIR_01_AGENT_MODE": "review"},
    )
    core = config["services"]["serverfs-mcp"]
    env = core["environment"]
    assert env["SERVERFS_AGENT_BRIDGE_ENABLED"] == "true"
    assert env["SERVERFS_AGENT_MODE"] == "disabled"
    assert env["WORKDIR_01_AGENT_MODE"] == "review"
    assert env["WORKDIR_01_REPO_OPS_ENABLED"] == "false"
    assert env["SERVERFS_LINUX_V25_BRIDGE_BINDING_FILE"] == (
        "/run/serverfs-core-bridge-binding/core-bridge-binding.json"
    )
    assert core["user"] == "1000:1000"
    assert "privileged" not in core or core["privileged"] is not True
    assert all("/var/run/docker.sock" not in str(volume) for volume in core["volumes"])
    mounts = {volume["target"]: volume for volume in core["volumes"]}
    for target in (
        "/run/serverfs-agent-bridge",
        "/run/serverfs-agent-locks",
        "/run/serverfs-core-bridge-binding",
    ):
        assert mounts[target]["read_only"] is True
    overlay_text = (ROOT / BRIDGE_V2).read_text()
    assert overlay_text.count("create_host_path: false") == 3
    assert mounts["/run/serverfs-core-bridge-binding"]["source"] == str(tmp_path / "core-binding")


@pytest.mark.skipif(DOCKER is None, reason="Docker CLI unavailable; Compose rendering not verified")
@pytest.mark.parametrize(
    ("variable", "expected_error"),
    [
        ("SERVERFS_UID", "SERVERFS_UID"),
        ("SERVERFS_GID", "SERVERFS_GID"),
        ("SERVERFS_LINUX_INSTANCE_ID", "SERVERFS_LINUX_INSTANCE_ID"),
        ("SERVERFS_LINUX_BRIDGE_ID", "SERVERFS_LINUX_BRIDGE_ID"),
        ("SERVERFS_LINUX_BRIDGE_SOCKET_DIR", "SERVERFS_LINUX_BRIDGE_SOCKET_DIR"),
        ("SERVERFS_LINUX_BRIDGE_LOCK_DIR", "SERVERFS_LINUX_BRIDGE_LOCK_DIR"),
        ("SERVERFS_LINUX_CORE_BINDING_DIR", "SERVERFS_LINUX_CORE_BINDING_DIR"),
    ],
)
def test_signed_v2_overlay_rejects_missing_required_values(
    tmp_path: Path, variable: str, expected_error: str
) -> None:
    # Use the helper's fixture values, then remove the one value under test.
    result, _ = _compose(
        tmp_path,
        *DIRECT,
        BRIDGE_V2,
        expect_ok=False,
        extra_env={variable: ""},
    )
    assert result.returncode != 0
    assert expected_error in result.stderr


@pytest.mark.skipif(DOCKER is None, reason="Docker CLI unavailable; Compose rendering not verified")
def test_signed_v2_overlay_combines_with_both_optional_connectors(tmp_path: Path) -> None:
    _, config = _compose(
        tmp_path,
        *DIRECT,
        BRIDGE_V2,
        *CLOUDFLARE,
        OPENAI,
        profiles=("cloudflare", "openai-http"),
    )
    assert set(config["services"]) == {"serverfs-mcp", "cloudflared", "openai-tunnel"}


@pytest.mark.skipif(DOCKER is None, reason="Docker CLI unavailable; Compose rendering not verified")
def test_signed_v2_instances_render_distinct_projects_ports_and_bridge_paths(
    tmp_path: Path,
) -> None:
    rendered = []
    for instance, port in (("blue", "18101"), ("green", "18102")):
        _, config = _compose(
            tmp_path,
            *DIRECT,
            BRIDGE_V2,
            project_name=f"serverfs-v0250-{instance}",
            extra_env={
                "SERVERFS_LINUX_INSTANCE_ID": f"v0250-{instance}",
                "SERVERFS_LINUX_BRIDGE_ID": f"bridge-{instance}",
                "SERVERFS_LINUX_BRIDGE_SOCKET_DIR": str(tmp_path / f"{instance}-socket"),
                "SERVERFS_LINUX_BRIDGE_LOCK_DIR": str(tmp_path / f"{instance}-locks"),
                "SERVERFS_LINUX_CORE_BINDING_DIR": str(tmp_path / f"{instance}-binding"),
                "SERVERFS_HTTP_PUBLISH_PORT": port,
                "WORKDIR_01_PATH": f"/srv/serverfs/{instance}",
            },
        )
        rendered.append(config)

    first, second = rendered
    assert first["name"] == "serverfs-v0250-blue"
    assert second["name"] == "serverfs-v0250-green"
    for config, port, identity, socket_dir, locks_dir, binding_dir in (
        (first, "18101", "v0250-blue", "blue-socket", "blue-locks", "blue-binding"),
        (second, "18102", "v0250-green", "green-socket", "green-locks", "green-binding"),
    ):
        core = config["services"]["serverfs-mcp"]
        assert core["ports"][0]["published"] == port
        assert core["environment"]["SERVERFS_LINUX_INSTANCE_ID"] == identity
        mount_sources = {item["target"]: item["source"] for item in core["volumes"]}
        assert mount_sources["/run/serverfs-agent-bridge"] == str(tmp_path / socket_dir)
        assert mount_sources["/run/serverfs-agent-locks"] == str(tmp_path / locks_dir)
        assert mount_sources["/run/serverfs-core-bridge-binding"] == str(tmp_path / binding_dir)
