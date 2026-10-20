---
title: 受限网络 ARM64 部署
description: 在 GHCR 或 GitHub 需要代理、或只能离线传输镜像时部署 Linux ARM64 ServerFS。
---

ServerFS 发布 `linux/arm64` 镜像。在受限网络中，需要明确区分两条完全不同的网络路径：

1. **Docker daemon 访问镜像仓库**——`docker pull` / Compose 从 GHCR 拉镜像时使用。
2. **Tunnel control-plane 出站**——可选 `openai-tunnel` 容器启动后访问 control plane 时使用。

`SERVERFS_OPENAI_TUNNEL_USE_PROXY` 只配置第二条路径。它**不会**配置 Docker daemon，也不能让原本被阻断的 `docker pull` 自动成功。

## 确认目标架构

在目标主机执行：

```bash
uname -m
```

受支持的 Linux ARM64 路径应看到 `aarch64` 或 `arm64`。不要根据负责下载镜像的那台机器来推断目标镜像架构。

## 路径 A：Docker daemon 可以通过 HTTP/HTTPS proxy 访问 GHCR

如果目标主机可以通过代理访问 GHCR，应按 Docker 官方方式配置 Docker daemon proxy。Docker Engine 支持在 `daemon.json` 中配置，也支持 systemd 服务环境变量方式。

官方参考：[Docker daemon proxy configuration](https://docs.docker.com/engine/daemon/proxy/)。

这是宿主机 Docker 层面的配置，不是 ServerFS 配置。ServerFS 不会自动修改 Docker daemon。

Daemon proxy 正常后，生产部署应固定精确 ServerFS release，而不是使用 `latest`：

```bash
VERSION=0.17.0
IMAGE="ghcr.io/ntlx/serverfs_mcp:${VERSION}"
docker pull --platform linux/arm64 "$IMAGE"
docker image inspect --format '{{.Os}}/{{.Architecture}}' "$IMAGE"
```

最后一个命令必须返回：

```text
linux/arm64
```

随后在 `.env` 中使用同一精确镜像：

```text
SERVERFS_IMAGE=ghcr.io/ntlx/serverfs_mcp:0.17.0
```

这里的版本号只是精确固定版本的示例；实际部署应使用当时已经发布的准确 release 版本。

### Tunnel proxy 是另一回事

如果镜像已经能够正常拉取，但可选 OpenAI Tunnel 访问 control plane 仍需要代理，再配置 ServerFS 的 Tunnel 专用开关与共享 proxy endpoint：

```text
SERVERFS_OPENAI_TUNNEL_USE_PROXY=true
SERVERFS_PROXY_HOST=proxy.example.internal
SERVERFS_PROXY_PORT=3128
```

该设置只影响 Tunnel 启动后的 control-plane egress，不会修改 `dockerd`、registry authentication 或镜像拉取。运行时诊断见 [Tunnel 诊断](./tunnel-diagnostics/)。

## 路径 B：目标主机无法访问 GHCR

使用一台可联网机器获取精确 ARM64 镜像，保存为归档，通过可信方式传输后再在目标主机加载。

### 1. 在联网机器显式拉取 ARM64 variant

```bash
VERSION=0.17.0
IMAGE="ghcr.io/ntlx/serverfs_mcp:${VERSION}"
docker pull --platform linux/arm64 "$IMAGE"
docker image inspect --format '{{.Os}}/{{.Architecture}}' "$IMAGE"
```

创建归档前必须确认结果为 `linux/arm64`。

### 2. 保存镜像

如果当前 Docker Engine/API 支持按 platform 保存：

```bash
docker image save --platform linux/arm64 \
  -o "serverfs-mcp-${VERSION}-linux-arm64.tar" \
  "$IMAGE"
sha256sum "serverfs-mcp-${VERSION}-linux-arm64.tar" \
  > "serverfs-mcp-${VERSION}-linux-arm64.tar.sha256"
```

如果当前 Docker API 不支持 `docker image save --platform`，先确保本地 tag 对应预期 ARM64 variant，再使用普通 `docker save`：

```bash
docker save -o "serverfs-mcp-${VERSION}-linux-arm64.tar" "$IMAGE"
```

这里应使用 `docker save` / `docker load`，**不要**使用 `docker export` / `docker import`。Save/load 会保留 Compose 所需的镜像、tag 与 layer 模型。

### 3. 传输并验证

使用可信传输方式复制 tar 与 checksum。目标主机上执行：

```bash
sha256sum -c "serverfs-mcp-${VERSION}-linux-arm64.tar.sha256"
docker load -i "serverfs-mcp-${VERSION}-linux-arm64.tar"
docker image inspect --format '{{.Os}}/{{.Architecture}}' "$IMAGE"
```

如果没有得到：

```text
linux/arm64
```

就不要继续启动服务。

### 4. 让 Compose 固定使用已加载镜像

在 `.env` 中：

```text
SERVERFS_IMAGE=ghcr.io/ntlx/serverfs_mcp:0.17.0
```

启动前先渲染：

```bash
docker compose config --quiet
docker compose up -d
```

由于精确 tag 已经存在于本地，Compose 可以直接使用已加载镜像。离线/受限网络部署不要长期使用 `latest`，否则无法明确知道实际转运的是哪个版本。

## 如果同时使用 OpenAI Tunnel

基础 Compose 部署还依赖单独固定版本的 `OPENAI_TUNNEL_IMAGE`。如果目标主机完全无法访问 registry，也必须提前准备这个镜像。可以复用同一流程：在联网机器显式拉取所需 `linux/arm64` Tunnel 镜像、核实架构、`docker save`、传输、`docker load`，并保持 `OPENAI_TUNNEL_IMAGE` 固定为 `.env.example` 中经过项目验证的版本。

如果只使用通用 Streamable HTTP（`compose.yml` + `compose.http.yml`），则不需要 OpenAI Tunnel 镜像。

## 启动前检查

受限网络 ARM64 部署启动前至少执行：

```bash
uname -m
docker image inspect --format '{{.Os}}/{{.Architecture}}' "$IMAGE"
docker compose config --quiet
```

预期架构为 `linux/arm64`。

这可以提前发现误下载或误转运 amd64 镜像的常见问题。QEMU/模拟有时会让不匹配镜像看似能够运行，但 ServerFS 文档不会把意外模拟执行视为等价于已验证的原生 ARM64 部署。
