"""Opt-in offline startup checks for the real Linux Tunnel client images."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCKER = shutil.which("docker")
OPENAI_IMAGE = "ghcr.io/openai/tunnel-client:v0.0.17"
CLOUDFLARED_IMAGE = "cloudflare/cloudflared:2026.11.0"
CANDIDATE_IMAGE = "serverfs-mcp:linux-v0250-candidate-20261120-53112a212491"
DOCKER_HOST_SOCKET = "unix:///var/run/docker.sock"
OPT_IN = os.environ.get("SERVERFS_LINUX_TUNNEL_RUNTIME_ACCEPT") == "1"


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    assert DOCKER
    result = subprocess.run(
        [DOCKER, "--host", DOCKER_HOST_SOCKET, *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if check:
        assert result.returncode == 0, result.stderr
    return result


def _require_image(image: str) -> None:
    result = _docker("image", "inspect", image, check=False)
    if result.returncode:
        pytest.skip(f"required local image unavailable: {image}")


def _run_container(
    image: str,
    args: list[str],
    mounts: list[str],
    *,
    entrypoint: str | None = None,
    user: str | None = None,
    env: list[str] | None = None,
) -> tuple[str, str, str]:
    label_value = f"{os.getpid()}-{time.time_ns()}"
    create = ["create", "--pull=never", "--network", "none"]
    create.extend(("--label", f"com.serverfs.linux-v0250.tunnel-runtime={label_value}"))
    for mount in mounts:
        create.extend(("--mount", mount))
    if entrypoint:
        create.extend(("--entrypoint", entrypoint))
    if user:
        create.extend(("--user", user))
    for value in env or []:
        create.extend(("--env", value))
    create.append(image)
    create.extend(args)
    container_id = _docker(*create).stdout.strip()
    try:
        _docker("start", container_id)
        time.sleep(2)
        log_result = _docker("logs", container_id, check=False)
        logs = log_result.stdout + log_result.stderr
        inspect = json.loads(_docker("inspect", container_id).stdout)[0]
        assert inspect["HostConfig"]["NetworkMode"] == "none"
        assert inspect["State"]["Status"] in {"running", "exited"}
        if user:
            assert inspect["Config"]["User"] == user
        return container_id, logs, label_value
    except BaseException:
        _docker("rm", "--force", container_id, check=False)
        raise


def _remove(container_id: str, label_value: str) -> None:
    _docker("rm", "--force", container_id, check=False)
    leftovers = _docker(
        "ps",
        "--all",
        "--quiet",
        "--filter",
        f"label=com.serverfs.linux-v0250.tunnel-runtime={label_value}",
    ).stdout.strip()
    assert not leftovers, f"tunnel runtime test container cleanup failed: {leftovers}"


def _render_cloudflare(tmp_path: Path, token_file: Path, uid: int, gid: int) -> dict[str, object]:
    empty_env = tmp_path / "empty-compose.env"
    empty_env.touch()
    env = {
        name: os.environ[name] for name in ("PATH", "HOME", "DOCKER_CONFIG") if name in os.environ
    }
    env.update(
        {
            "SERVERFS_IMAGE": CANDIDATE_IMAGE,
            "CLOUDFLARE_TUNNEL_TOKEN_FILE": str(token_file),
            "CLOUDFLARED_IMAGE": CLOUDFLARED_IMAGE,
            "CLOUDFLARED_UID": str(uid),
            "CLOUDFLARED_GID": str(gid),
        }
    )
    result = subprocess.run(
        [
            DOCKER,
            "--host",
            DOCKER_HOST_SOCKET,
            "compose",
            "--env-file",
            str(empty_env),
            "-p",
            f"serverfs-v0250-cf-offline-{os.getpid()}",
            "-f",
            "compose.yml",
            "-f",
            "compose.http.yml",
            "-f",
            "compose.cloudflare.yml",
            "-f",
            "deployment/linux/compose.cloudflare-profile.yml",
            "--profile",
            "cloudflare",
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.skipif(
    not OPT_IN, reason="set SERVERFS_LINUX_TUNNEL_RUNTIME_ACCEPT=1 for real offline client checks"
)
@pytest.mark.skipif(DOCKER is None, reason="Docker unavailable")
def test_real_openai_tunnel_client_parses_http_and_file_references_offline(tmp_path: Path) -> None:
    _require_image(OPENAI_IMAGE)
    key = tmp_path / "control-key"
    bearer = tmp_path / "core-bearer"
    launcher = tmp_path / "tunnel-launcher.sh"
    key.write_text("synthetic-control-key\n", encoding="utf-8")
    bearer.write_text("Bearer synthetic-core-key", encoding="utf-8")
    launcher.write_bytes((ROOT / "deployment/tunnel/tunnel-launcher.sh").read_bytes())
    for path in (key, bearer, launcher):
        path.chmod(0o400)
    assert bearer.read_text(encoding="utf-8") == "Bearer synthetic-core-key"
    container_id, logs, label_value = _run_container(
        OPENAI_IMAGE,
        [
            "/opt/serverfs/tunnel-launcher.sh",
            "--control-plane.tunnel-id",
            "tunnel_0123456789abcdef0123456789abcdef",
            "--control-plane.base-url",
            "http://127.0.0.1:9",
            "--control-plane.api-key",
            "file:/run/secrets/control-key",
            "--mcp.server-url",
            "url=http://serverfs-mcp:8000/mcp",
            "--mcp.extra-headers",
            "Authorization: file:/run/secrets/core-bearer",
            "--mcp.discovery-extra-headers",
            "Authorization: file:/run/secrets/core-bearer",
            "--health.listen-addr",
            "127.0.0.1:18080",
        ],
        [
            f"type=bind,src={launcher},dst=/opt/serverfs/tunnel-launcher.sh,readonly",
            f"type=bind,src={key},dst=/run/secrets/control-key,readonly",
            f"type=bind,src={bearer},dst=/run/secrets/core-bearer,readonly",
        ],
        entrypoint="/bin/sh",
        env=["SERVERFS_OPENAI_TUNNEL_USE_PROXY=false"],
    )
    try:
        # Offline failure must occur after configuration and file references parse.
        assert "unknown flag" not in logs.lower()
        assert "invalid" not in logs.lower() or "connection" in logs.lower()
        assert (
            "127.0.0.1:9" in logs
            or "connection refused" in logs.lower()
            or "lookup" in logs.lower()
        )
        health = _docker(
            "exec", container_id, "/usr/bin/tunnel-client", "health", "--port", "18080", check=False
        )
        # Health listener is bound to container loopback and is queried in its namespace.
        assert health.returncode in (0, 1, 2), health.stderr
        health_output = (health.stdout + health.stderr).lower()
        assert "healthz: pass" in health_output and "readyz: fail" in health_output
    finally:
        _remove(container_id, label_value)


@pytest.mark.skipif(
    not OPT_IN, reason="set SERVERFS_LINUX_TUNNEL_RUNTIME_ACCEPT=1 for real offline client checks"
)
@pytest.mark.skipif(DOCKER is None, reason="Docker unavailable")
def test_real_cloudflared_reads_token_file_under_compose_uid_offline(tmp_path: Path) -> None:
    _require_image(CANDIDATE_IMAGE)
    _require_image(CLOUDFLARED_IMAGE)
    uid, gid = os.getuid(), os.getgid()
    if uid == 0:
        pytest.skip("requires the current nonroot Linux owner used by the Compose UID override")
    image = json.loads(_docker("image", "inspect", CLOUDFLARED_IMAGE).stdout)[0]
    assert image["Config"]["Entrypoint"] == ["cloudflared"]
    token = tmp_path / "cloudflare-token"
    synthetic_secret = base64.b64encode(bytes.fromhex("42" * 32)).decode("ascii")
    token_payload = json.dumps(
        {
            "a": "0123456789abcdef0123456789abcdef",
            "s": synthetic_secret,
            "t": "01234567-89ab-cdef-0123-456789abcdef",
        },
        separators=(",", ":"),
    ).encode("utf-8")
    token.write_text(base64.b64encode(token_payload).decode("ascii"), encoding="ascii")
    os.chown(token, uid, gid)
    token.chmod(0o600)
    token_stat = token.stat()
    assert token_stat.st_uid == uid and token_stat.st_gid == gid
    assert token_stat.st_mode & 0o777 == 0o600
    rendered = _render_cloudflare(tmp_path, token, uid, gid)
    assert rendered["services"]["serverfs-mcp"]["image"] == CANDIDATE_IMAGE
    service = rendered["services"]["cloudflared"]
    assert service["user"] == f"{uid}:{gid}"
    assert service["image"] == CLOUDFLARED_IMAGE
    assert service["command"] == [
        "tunnel",
        "--no-autoupdate",
        "run",
        "--token-file",
        "/run/secrets/cloudflare-tunnel-token",
    ]
    container_id, logs, label_value = _run_container(
        CLOUDFLARED_IMAGE,
        [
            "tunnel",
            "--no-autoupdate",
            "run",
            "--token-file",
            "/run/secrets/cloudflare-tunnel-token",
        ],
        [f"type=bind,src={token},dst=/run/secrets/cloudflare-tunnel-token,readonly"],
        user=service["user"],
    )
    try:
        lower = logs.lower()
        assert "starting tunnel tunnelid=01234567-89ab-cdef-0123-456789abcdef" in lower
        assert "could not lookup srv records" in lower
        assert "network is unreachable" in lower
        assert "permission denied" not in lower
        assert "failed to read" not in lower and "no such file" not in lower
        assert "invalid tunnel token" not in lower
        assert "failed to parse tunnel token" not in lower
        assert "tunnel id" not in lower or "starting tunnel" in lower
        assert "unable to reach the edge" in lower or "failed to dial" in lower or "lookup" in lower
    finally:
        _remove(container_id, label_value)
