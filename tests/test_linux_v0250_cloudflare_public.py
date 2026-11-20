"""Opt-in public Cloudflare smoke for the isolated Linux v0.25 candidate."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parents[1]
DOCKER = shutil.which("docker")
DOCKER_HOST = "unix:///var/run/docker.sock"
OPT_IN = os.environ.get("SERVERFS_LINUX_CLOUDFLARE_PUBLIC_ACCEPT") == "1"
HOSTNAME = "vps-serverfs-v25-test.ntlx.top"
CANDIDATE_IMAGE = "serverfs-mcp:linux-v0250-candidate-20261120-53112a212491"
CANDIDATE_IMAGE_ID = "sha256:6a3692cff58a1a93a4364b3aa00c9816e5bfaacabc9b5c4c8d0354fe7596123c"
CLOUDFLARED_IMAGE = "cloudflare/cloudflared:2026.11.0"
CLOUDFLARED_IMAGE_ID = "sha256:41c38225ed72eb65a0c644f02c522f7a8c5e34f2a71568f0b763573ad47fd9f4"
CLOUDFLARE_FILES = (
    "compose.yml",
    "compose.http.yml",
    "deployment/linux/compose.publish-http.yml",
    "deployment/linux/compose.bridge-disabled.yml",
    "compose.cloudflare.yml",
    "deployment/linux/compose.cloudflare-profile.yml",
)
PUBLIC_TIMEOUT = 180.0


def _check_explicit_identity(hostname: str, approved_hostname: str, image: str) -> None:
    if hostname != HOSTNAME or approved_hostname != HOSTNAME:
        raise ValueError("Cloudflare test hostname is not the allocated Linux v0.25 hostname")
    if image != CANDIDATE_IMAGE:
        raise ValueError("Core image is not the packaged Linux v0.25 candidate")


def _check_token_metadata(token_path: Path, workdir: Path, owner_uid: int, owner_gid: int) -> Path:
    if owner_uid <= 0 or owner_gid <= 0:
        raise ValueError("Cloudflare token requires an explicit nonroot file owner")
    try:
        if token_path.is_symlink():
            raise ValueError("Cloudflare token path must not be a symlink")
        resolved = token_path.resolve(strict=True)
        if resolved.is_relative_to(ROOT.resolve()) or resolved.is_relative_to(workdir.resolve()):
            raise ValueError("Cloudflare token must be outside checkout and fixture workdir")
        metadata = resolved.stat()
    except OSError:
        raise ValueError("Cloudflare token metadata is unavailable") from None
    if not resolved.is_file() or metadata.st_mode & 0o777 != 0o600:
        raise ValueError("Cloudflare token must be a regular mode-0600 file")
    if metadata.st_uid != owner_uid or metadata.st_gid != owner_gid:
        raise ValueError("Cloudflare token owner does not match Compose UID/GID")
    return resolved


def _docker(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    assert DOCKER is not None
    result = subprocess.run(
        [DOCKER, "--host", DOCKER_HOST, *args],
        cwd=ROOT,
        env=env or _runner_env(Path("/tmp")),
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    return result


def _image_id(image: str, temp_root: Path) -> str:
    result = _docker(
        "image",
        "inspect",
        "--format",
        "{{.Id}}",
        image,
        env=_runner_env(temp_root),
    )
    if result.returncode:
        raise AssertionError("required image is not present locally")
    return result.stdout.strip()


def _runner_env(temp_root: Path) -> dict[str, str]:
    # Do not inherit proxy, Docker context, Compose profiles, or production env.
    return {"PATH": os.defpath, "HOME": str(temp_root)}


def _write_private_env(
    temp_root: Path,
    *,
    hostname: str,
    token_file: Path,
    image: str,
    cloudflared_image: str,
    port: int,
    api_key: str,
    workdir: Path,
    uid: int,
    gid: int,
) -> Path:
    values = {
        "SERVERFS_IMAGE": image,
        "SERVERFS_UID": str(uid),
        "SERVERFS_GID": str(gid),
        "SERVERFS_HTTP_API_KEY": api_key,
        "SERVERFS_HTTP_ALLOWED_HOSTS": f"serverfs-mcp,127.0.0.1:{port},127.0.0.1:8000,{hostname}",
        "SERVERFS_HTTP_PUBLISH_HOST": "127.0.0.1",
        "SERVERFS_HTTP_PUBLISH_PORT": str(port),
        "WORKDIR_01_ALIAS": "fixture",
        "WORKDIR_01_PATH": str(workdir),
        "WORKDIR_01_READ_ONLY": "true",
        "CLOUDFLARE_TUNNEL_TOKEN_FILE": str(token_file),
        "CLOUDFLARED_IMAGE": cloudflared_image,
        "CLOUDFLARED_UID": str(uid),
        "CLOUDFLARED_GID": str(gid),
    }
    for slot in range(2, 17):
        values[f"WORKDIR_{slot:02d}_PATH"] = str(temp_root / f"disabled-{slot:02d}")
    env_file = temp_root / "cloudflare-acceptance.env"
    env_file.write_text(
        "".join(f"{name}={json.dumps(value)}\n" for name, value in values.items()),
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    return env_file


@dataclass(frozen=True)
class PreparedAcceptance:
    temp_root: Path
    workdir: Path
    expected_marker: str
    port: int
    api_key: str
    project: str
    env_file: Path
    compose_base: list[str]
    model: dict[str, object]


def _prepare_acceptance(
    temp_root: Path,
    *,
    project: str,
    hostname: str,
    token_file: Path,
    image: str,
    cloudflared_image: str,
) -> PreparedAcceptance:
    """Shared, local-only fixture/env/Compose preparation for both runner paths."""
    _check_explicit_identity(hostname, hostname, image)
    if cloudflared_image != CLOUDFLARED_IMAGE:
        raise ValueError("Cloudflare image differs from the tested pinned image")
    temp_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp_root.chmod(0o700)
    workdir = temp_root / "workdir"
    workdir.mkdir(mode=0o700, exist_ok=True)
    expected_marker = "linux-v0250-cloudflare-" + uuid.uuid4().hex
    marker = workdir / "marker.txt"
    marker.write_text(expected_marker, encoding="utf-8")
    marker.chmod(0o400)
    for slot in range(2, 17):
        disabled = temp_root / f"disabled-{slot:02d}"
        disabled.mkdir(mode=0o700, exist_ok=True)
        disabled_marker = disabled / ".serverfs-disabled"
        disabled_marker.touch(mode=0o400, exist_ok=True)

    port = _free_loopback_port()
    api_key = secrets.token_urlsafe(32)
    env_file = _write_private_env(
        temp_root,
        hostname=hostname,
        token_file=token_file,
        image=image,
        cloudflared_image=cloudflared_image,
        port=port,
        api_key=api_key,
        workdir=workdir,
        uid=os.getuid(),
        gid=os.getgid(),
    )
    compose_base = _compose_base(project, env_file)
    model = _render(env_file, project, temp_root)
    _assert_compose_model(
        model,
        token_file=token_file,
        workdir=workdir,
        temp_root=temp_root,
        port=port,
        uid=os.getuid(),
        gid=os.getgid(),
    )
    return PreparedAcceptance(
        temp_root=temp_root,
        workdir=workdir,
        expected_marker=expected_marker,
        port=port,
        api_key=api_key,
        project=project,
        env_file=env_file,
        compose_base=compose_base,
        model=model,
    )


def _compose_base(project: str, env_file: Path) -> list[str]:
    assert DOCKER is not None
    command = [
        DOCKER,
        "--host",
        DOCKER_HOST,
        "compose",
        "--env-file",
        str(env_file),
        "-p",
        project,
    ]
    for compose_file in CLOUDFLARE_FILES:
        command.extend(("-f", str(ROOT / compose_file)))
    return command


def _render(env_file: Path, project: str, temp_root: Path) -> dict[str, object]:
    result = subprocess.run(
        [
            *_compose_base(project, env_file),
            "--profile",
            "cloudflare",
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        env=_runner_env(temp_root),
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    if result.returncode:
        raise AssertionError("isolated Cloudflare Compose render failed")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        raise AssertionError("isolated Cloudflare Compose model was not valid JSON") from None


def _assert_compose_model(
    model: dict[str, object],
    *,
    token_file: Path,
    workdir: Path,
    temp_root: Path,
    port: int,
    uid: int,
    gid: int,
) -> None:
    services = model["services"]
    if set(services) != {"serverfs-mcp", "cloudflared"}:
        raise AssertionError("rendered project contains an unexpected service")
    core = services["serverfs-mcp"]
    cloudflared = services["cloudflared"]
    if core["image"] != CANDIDATE_IMAGE:
        raise AssertionError("Core is not using the packaged candidate image")
    ports = core.get("ports", [])
    if len(ports) != 1 or ports[0].get("host_ip") != "127.0.0.1":
        raise AssertionError("Core port is not loopback-only")
    if str(ports[0].get("published")) != str(port):
        raise AssertionError("Core published port differs from the isolated fixture")
    core_env = core["environment"]
    if core_env.get("SERVERFS_AGENT_BRIDGE_ENABLED") != "false":
        raise AssertionError("Bridge is not disabled")
    if core_env.get("SERVERFS_FILE_INGRESS_ENABLED") != "false":
        raise AssertionError("file ingress must remain disabled")
    if core_env.get("WORKDIR_01_READ_ONLY") != "true":
        raise AssertionError("fixture workdir is not read-only")
    if core_env.get("WORKDIR_01_ALIAS") != "fixture":
        raise AssertionError("Core fixture alias differs from the acceptance client")
    if not core_env.get("SERVERFS_HTTP_API_KEY"):
        raise AssertionError("synthetic Core API key was not rendered")
    if core_env.get("SERVERFS_AGENT_MODE") not in {None, "", "disabled"}:
        raise AssertionError("Agent runtime mode must remain disabled")
    if any(
        name.endswith("_AGENT_MODE") and value not in {None, "", "disabled"}
        for name, value in core_env.items()
    ):
        raise AssertionError("an Agent workdir mode was enabled")
    if any(
        name.endswith("_REPO_OPS_ENABLED") and str(value).lower() not in {"false", ""}
        for name, value in core_env.items()
    ):
        raise AssertionError("Repo Ops must remain disabled")
    if any(
        volume.get("target") in {"/run/serverfs-agent-bridge", "/run/serverfs-agent-locks"}
        for volume in core.get("volumes", [])
    ):
        raise AssertionError("Core unexpectedly mounts a Bridge path")
    expected_mounts = {f"/workdirs/{slot:02d}" for slot in range(1, 17)}
    core_mounts = {volume.get("target"): volume for volume in core.get("volumes", [])}
    if set(core_mounts) != expected_mounts:
        raise AssertionError("Core has a source, credential, or non-workdir mount")
    if core_mounts["/workdirs/01"].get("source") != str(workdir):
        raise AssertionError("Core fixture workdir path differs from the isolated fixture")
    if core_mounts["/workdirs/01"].get("read_only") is not True:
        raise AssertionError("Core fixture workdir bind is not read-only")
    for slot in range(2, 17):
        expected_disabled = temp_root / f"disabled-{slot:02d}"
        mount = core_mounts[f"/workdirs/{slot:02d}"]
        marker = expected_disabled / ".serverfs-disabled"
        if mount.get("source") != str(expected_disabled) or not marker.is_file():
            raise AssertionError("non-fixture workdir slot is not disabled")
        if core_env.get(f"WORKDIR_{slot:02d}_ALIAS") not in {None, ""}:
            raise AssertionError("non-fixture workdir slot has an active alias")
        if mount.get("read_only") is not True:
            raise AssertionError("disabled workdir placeholder is not read-only")
    if cloudflared["user"] != f"{uid}:{gid}":
        raise AssertionError("Cloudflare user differs from token file owner")
    if cloudflared["image"] != "cloudflare/cloudflared:2026.11.0":
        raise AssertionError("Cloudflare image differs from the explicit local selection")
    token_mount = next(
        (
            volume
            for volume in cloudflared["volumes"]
            if volume.get("target") == "/run/secrets/cloudflare-tunnel-token"
        ),
        None,
    )
    if (
        token_mount is None
        or token_mount.get("source") != str(token_file)
        or token_mount.get("read_only") is not True
    ):
        raise AssertionError("Cloudflare token mount is not the explicit read-only file")
    if cloudflared.get("ports"):
        raise AssertionError("Cloudflare connector unexpectedly publishes a host port")


def _run_compose(base: list[str], args: list[str], temp_root: Path, *, stage: str) -> str:
    result = subprocess.run(
        [*base, "--profile", "cloudflare", *args],
        cwd=ROOT,
        env=_runner_env(temp_root),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode:
        raise AssertionError(f"Compose stage failed: {stage} (exit {result.returncode})")
    return result.stdout.strip()


def _service_container_id(
    base: list[str], service: str, temp_root: Path, *, include_stopped: bool = False
) -> str:
    args = ["ps"]
    if include_stopped:
        args.append("--all")
    args.extend(("-q", service))
    result = _run_compose(base, args, temp_root, stage=f"service-id-{service}")
    ids = result.splitlines()
    if len(ids) > 1:
        raise AssertionError(f"unexpected multiple containers for service {service}")
    return ids[0] if ids else ""


def _inspect(container_id: str, temp_root: Path) -> tuple[str, str]:
    result = _docker(
        "inspect",
        "--format",
        "{{.Id}} {{.State.StartedAt}}",
        container_id,
        env=_runner_env(temp_root),
    )
    if result.returncode:
        raise AssertionError("container identity inspection failed")
    identity, started_at = result.stdout.strip().split(" ", maxsplit=1)
    return identity, started_at


def _container_state(container_id: str, temp_root: Path) -> str:
    result = _docker(
        "inspect",
        "--format",
        "{{.State.Status}}",
        container_id,
        env=_runner_env(temp_root),
    )
    if result.returncode:
        raise AssertionError("container state inspection failed")
    return result.stdout.strip()


async def _status(url: str, bearer: str | None) -> int | None:
    headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
    try:
        async with httpx2.AsyncClient(
            headers=headers, timeout=5, trust_env=False, follow_redirects=False
        ) as client:
            response = await client.get(url)
            return response.status_code
    except Exception:
        return None


async def _mcp_read(url: str, api_key: str, expected_marker: str) -> None:
    try:
        async with httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}"}, timeout=15, trust_env=False
        ) as http:
            transport = streamable_http_client(url, http_client=http, terminate_on_close=False)
            async with Client(transport, mode="2026-07-28") as client:
                tools = {item.name for item in (await client.list_tools()).tools}
                if "read_text_file" not in tools:
                    raise AssertionError("MCP tools/list omitted read_text_file")
                result = await client.call_tool(
                    "read_text_file", {"workdir": "fixture", "path": "marker.txt"}
                )
                if result.is_error or expected_marker not in str(result.content):
                    raise AssertionError("MCP marker read did not match the synthetic fixture")
    except Exception:
        raise AssertionError("MCP initialize/list/read failed") from None


def _wait_for_auth_denial(url: str, *, stage: str) -> None:
    deadline = time.monotonic() + PUBLIC_TIMEOUT
    while time.monotonic() < deadline:
        status = asyncio.run(_status(url, "intentionally-wrong-synthetic-key"))
        if status == 401:
            return
        time.sleep(2)
    raise AssertionError(f"HTTP auth endpoint did not become ready: {stage}")


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _assert_no_project_resources(project: str, temp_root: Path) -> None:
    for resource, args in (
        ("container", ["ps", "--all", "--quiet"]),
        ("network", ["network", "ls", "--quiet"]),
    ):
        result = _docker(
            *args,
            "--filter",
            f"label=com.docker.compose.project={project}",
            env=_runner_env(temp_root),
        )
        if result.returncode or result.stdout.strip():
            raise AssertionError(f"unique project {resource} cleanup failed")


def test_cloudflare_public_identity_is_strictly_pinned() -> None:
    with pytest.raises(ValueError, match="hostname"):
        _check_explicit_identity("other.example", HOSTNAME, CANDIDATE_IMAGE)
    with pytest.raises(ValueError, match="candidate"):
        _check_explicit_identity(HOSTNAME, HOSTNAME, "ghcr.io/example/serverfs:latest")


def test_cloudflare_token_metadata_rejects_non_private_synthetic_file(tmp_path: Path) -> None:
    if os.getuid() == 0:
        pytest.skip("requires a nonroot test owner")
    workdir = tmp_path / "workdir"
    workdir.mkdir(mode=0o700)
    token = tmp_path / "synthetic-token"
    token.write_text("test-only", encoding="ascii")
    token.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        _check_token_metadata(token, workdir, os.getuid(), os.getgid())


def test_cloudflare_token_metadata_accepts_private_synthetic_file(tmp_path: Path) -> None:
    if os.getuid() == 0:
        pytest.skip("requires a nonroot test owner")
    workdir = tmp_path / "workdir"
    workdir.mkdir(mode=0o700)
    token = tmp_path / "synthetic-token"
    token.write_text("test-only", encoding="ascii")
    token.chmod(0o600)
    assert _check_token_metadata(token, workdir, os.getuid(), os.getgid()) == token.resolve()


def test_cloudflare_runner_preparation_runs_candidate_core_locally(tmp_path: Path) -> None:
    if DOCKER is None:
        pytest.skip("Docker CLI unavailable; local candidate Core acceptance not verified")
    root = tmp_path.resolve()
    root.chmod(0o700)
    token = root / "synthetic-config-only-token"
    token.write_text("synthetic-render-only", encoding="ascii")
    token.chmod(0o600)
    project = "serverfs-v0250-cf-local-" + uuid.uuid4().hex[:12]
    candidate_id = _image_id(CANDIDATE_IMAGE, root)
    assert candidate_id == CANDIDATE_IMAGE_ID
    assert _image_id(CLOUDFLARED_IMAGE, root) == CLOUDFLARED_IMAGE_ID
    prepared = _prepare_acceptance(
        root,
        project=project,
        hostname=HOSTNAME,
        token_file=token,
        image=CANDIDATE_IMAGE,
        cloudflared_image=CLOUDFLARED_IMAGE,
    )
    base = prepared.compose_base
    try:
        _run_compose(
            base,
            ["up", "-d", "--no-deps", "--no-build", "--pull", "never", "serverfs-mcp"],
            root,
            stage="local-core-start",
        )
        core_id = _service_container_id(base, "serverfs-mcp", root)
        if not core_id:
            raise AssertionError("local Core service did not create a container")
        container_image = _docker(
            "inspect", "--format", "{{.Image}}", core_id, env=_runner_env(root)
        )
        if container_image.returncode or container_image.stdout.strip() != candidate_id:
            raise AssertionError("local Core did not run the inspected candidate image")
        if _service_container_id(base, "cloudflared", root, include_stopped=True):
            raise AssertionError("local preparation unexpectedly started cloudflared")

        local_url = f"http://127.0.0.1:{prepared.port}/mcp"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if asyncio.run(_status(local_url, None)) == 401:
                break
            time.sleep(0.2)
        else:
            raise AssertionError("local unauthenticated MCP request did not return HTTP 401")
        if asyncio.run(_status(local_url, "deliberately-wrong-synthetic-key")) != 401:
            raise AssertionError("local Core did not reject the wrong synthetic key")
        asyncio.run(_mcp_read(local_url, prepared.api_key, prepared.expected_marker))
    finally:
        cleanup = subprocess.run(
            [*base, "--profile", "cloudflare", "down", "--remove-orphans"],
            cwd=ROOT,
            env=_runner_env(root),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        _assert_no_project_resources(project, root)
        if cleanup.returncode:
            raise AssertionError("local unique Compose project teardown failed")


@pytest.mark.skipif(
    not OPT_IN,
    reason="set SERVERFS_LINUX_CLOUDFLARE_PUBLIC_ACCEPT=1 only after secret handoff approval",
)
@pytest.mark.skipif(DOCKER is None or sys.platform != "linux", reason="Linux Docker unavailable")
def test_cloudflare_only_public_candidate_acceptance(tmp_path: Path) -> None:
    # Inputs are read only after the explicit opt-in; token content is never opened.
    hostname = os.environ.get("CLOUDFLARE_TEST_HOSTNAME", "")
    approved_hostname = os.environ.get("APPROVED_CLOUDFLARE_TEST_HOSTNAME", "")
    image = os.environ.get("SERVERFS_IMAGE", "")
    cloudflared_image = os.environ.get("CLOUDFLARED_IMAGE", "")
    token_value = os.environ.get("CLOUDFLARE_TUNNEL_TOKEN_FILE", "")
    if not all((hostname, approved_hostname, image, cloudflared_image, token_value)):
        raise AssertionError("required explicit Cloudflare acceptance inputs are missing")
    _check_explicit_identity(hostname, approved_hostname, image)
    if cloudflared_image != "cloudflare/cloudflared:2026.11.0":
        raise AssertionError("Cloudflare image must be explicitly pinned to the tested local image")
    if os.getuid() == 0:
        raise AssertionError("public acceptance must run as a nonroot file/workdir owner")

    temp_root = tmp_path.resolve()
    temp_root.chmod(0o700)
    workdir = temp_root / "workdir"
    workdir.mkdir(mode=0o700)
    uid, gid = os.getuid(), os.getgid()
    token_file = _check_token_metadata(Path(token_value), workdir, uid, gid)
    if _image_id(image, temp_root) != CANDIDATE_IMAGE_ID:
        raise AssertionError("local candidate image ID differs from the accepted build")
    if _image_id(cloudflared_image, temp_root) != CLOUDFLARED_IMAGE_ID:
        raise AssertionError("local Cloudflare image ID differs from the tested image")

    project = "serverfs-v0250-cf-public-" + uuid.uuid4().hex[:12]
    prepared = _prepare_acceptance(
        temp_root,
        project=project,
        hostname=hostname,
        token_file=token_file,
        image=image,
        cloudflared_image=cloudflared_image,
    )
    base = prepared.compose_base
    compose_env = _runner_env(temp_root)
    try:
        quiet = subprocess.run(
            [*base, "--profile", "cloudflare", "config", "--quiet"],
            cwd=ROOT,
            env=compose_env,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        if quiet.returncode:
            raise AssertionError("Compose validation failed")

        _run_compose(
            base,
            ["up", "-d", "--no-deps", "--no-build", "--pull", "never", "serverfs-mcp"],
            temp_root,
            stage="core-start",
        )
        core_id = _service_container_id(base, "serverfs-mcp", temp_root)
        if not core_id:
            raise AssertionError("Core container was not created")
        core_before = _inspect(core_id, temp_root)
        local_url = f"http://127.0.0.1:{prepared.port}/mcp"
        local_deadline = time.monotonic() + 30
        while time.monotonic() < local_deadline:
            if asyncio.run(_status(local_url, None)) == 401:
                break
            time.sleep(0.2)
        else:
            raise AssertionError("local unauthenticated origin did not return HTTP 401")
        asyncio.run(_mcp_read(local_url, prepared.api_key, prepared.expected_marker))

        _run_compose(
            base,
            ["up", "-d", "--no-deps", "--no-build", "--pull", "never", "cloudflared"],
            temp_root,
            stage="connector-start",
        )
        public_url = f"https://{hostname}/mcp"
        _wait_for_auth_denial(public_url, stage="initial-public-route")
        if asyncio.run(_status(public_url, None)) != 401:
            raise AssertionError("public endpoint did not deny a missing bearer key")
        if asyncio.run(_status(public_url, "deliberately-wrong-synthetic-key")) != 401:
            raise AssertionError("public endpoint did not deny an incorrect bearer key")
        asyncio.run(_mcp_read(public_url, prepared.api_key, prepared.expected_marker))

        cloudflared_id = _service_container_id(base, "cloudflared", temp_root)
        if not cloudflared_id:
            raise AssertionError("Cloudflare connector container was not created")
        connector_before = _inspect(cloudflared_id, temp_root)
        _run_compose(base, ["stop", "cloudflared"], temp_root, stage="connector-stop")
        if _container_state(cloudflared_id, temp_root) != "exited":
            raise AssertionError("stopped Cloudflare target remained active")
        if _service_container_id(base, "cloudflared", temp_root):
            raise AssertionError("stopped Cloudflare target remained in the active service listing")
        if _service_container_id(base, "serverfs-mcp", temp_root) != core_id:
            raise AssertionError("stopping Cloudflare changed the Core service container ID")
        if _inspect(core_id, temp_root) != core_before:
            raise AssertionError("stopping Cloudflare changed Core identity or StartedAt")
        _run_compose(
            base,
            ["up", "-d", "--no-deps", "--no-build", "--pull", "never", "cloudflared"],
            temp_root,
            stage="connector-restart",
        )
        restarted_connector_id = _service_container_id(base, "cloudflared", temp_root)
        if restarted_connector_id != cloudflared_id:
            raise AssertionError("ordinary connector restart changed its container ID")
        connector_after_restart = _inspect(restarted_connector_id, temp_root)
        if connector_after_restart[1] == connector_before[1]:
            raise AssertionError("ordinary connector restart did not change StartedAt")
        _wait_for_auth_denial(public_url, stage="public-route-after-restart")
        if _service_container_id(base, "serverfs-mcp", temp_root) != core_id:
            raise AssertionError("starting Cloudflare changed the Core service container ID")
        if _inspect(core_id, temp_root) != core_before:
            raise AssertionError("starting Cloudflare changed Core identity or StartedAt")
        asyncio.run(_mcp_read(public_url, prepared.api_key, prepared.expected_marker))

        previous_connector = connector_after_restart
        _run_compose(
            base,
            [
                "up",
                "-d",
                "--no-deps",
                "--no-build",
                "--pull",
                "never",
                "--force-recreate",
                "cloudflared",
            ],
            temp_root,
            stage="connector-recreate",
        )
        recreated_id = _run_compose(
            base, ["ps", "-q", "cloudflared"], temp_root, stage="connector-id-after-recreate"
        )
        if not recreated_id or recreated_id == cloudflared_id:
            raise AssertionError("Cloudflare connector was not force-recreated")
        recreated_connector = _inspect(recreated_id, temp_root)
        if recreated_connector[1] == previous_connector[1]:
            raise AssertionError("force-recreated connector StartedAt did not change")
        if _service_container_id(base, "serverfs-mcp", temp_root) != core_id:
            raise AssertionError("recreating Cloudflare changed the Core service container ID")
        if _inspect(core_id, temp_root) != core_before:
            raise AssertionError("recreating Cloudflare changed Core identity or StartedAt")
        _wait_for_auth_denial(public_url, stage="public-route-after-recreate")
        asyncio.run(_mcp_read(public_url, prepared.api_key, prepared.expected_marker))
    finally:
        cleanup = subprocess.run(
            [*base, "--profile", "cloudflare", "down", "--remove-orphans"],
            cwd=ROOT,
            env=compose_env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        _assert_no_project_resources(project, temp_root)
        if cleanup.returncode:
            raise AssertionError("unique project teardown failed")
