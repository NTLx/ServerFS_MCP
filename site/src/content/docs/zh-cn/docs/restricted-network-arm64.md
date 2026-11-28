---
title: 受限网络 ARM64 部署
description: 为网络受限或离线的 Linux ARM64 主机准备版本匹配的镜像组合。
---

本指南适用于 Linux ARM64 Docker 部署。ServerFS 的发布工作流构建 amd64 和 arm64 镜像。选择精确目标版本，当前发行版为 v0.26.0。使用经过验证的正式 ARM64 镜像与匹配源码；独立构建的本地镜像属于另一构建产物。

## 1. 区分镜像获取与运行时网络

| 网络路径 | 使用条件 |
| --- | --- |
| Docker daemon → Registry | 拉取任一选定镜像 |
| 宿主机 → 源码/依赖缓存 | 获取 checkout 或安装宿主机 Bridge |
| Connector → Provider | 使用对应 OpenAI 或 Cloudflare Connector |
| 授权服务 → 身份 Provider | 启用 OAuth |
| 文件入口 sidecar → 允许的 HTTPS 来源 | 启用文件入口 |
| 宿主机 Agent → Provider | 运行已配置的原生 Agent |

直接 HTTP 的纯文件 Core 不需要 Tunnel 或 Agent 凭据。Registry 不可达不等于运行时完全离线；一个组件的代理配置也不会配置其他组件。

在目标主机运行 `uname -m`；原生 ARM64 部署应为 `aarch64` 或 `arm64`。负责下载镜像的机器可以是不同架构。

## 2. 选择精确镜像

从目标 release 或已验证的本地构建获取版本匹配的 checkout 及可信精确镜像引用。避免使用 `latest` 或语义不明确的开发标签。示例使用 shell 变量；将占位值替换为实际经过验证、带版本标签的镜像引用，两台机器使用相同引用。

```bash
IMAGE='<exact-version-tagged-image-reference>'
ARCH=linux/arm64
docker pull --platform "$ARCH" "$IMAGE"
docker image inspect --format '{{.Os}}/{{.Architecture}} {{.Id}}' "$IMAGE"
```

若使用已经构建且可信的本地 ARM64 镜像，跳过上述 pull，直接检查该精确本地镜像。确认 `linux/arm64`，记录预期镜像 ID。依据可信发布 manifest/digest 或经过验证的本地构建核对来源；架构正确并不能证明镜像真实性。

如果目标机可以通过代理访问 GHCR，按 [Docker 官方 daemon proxy 指南](https://docs.docker.com/engine/daemon/proxy/)配置 Docker daemon，并直接在目标机拉取。Docker daemon 的代理属于宿主机配置，不由 ServerFS 管理。否则在联网机器获取镜像，再按下一节转运。

## 3. 目标机无法访问 Registry 时转运镜像

在联网机器拉取并核对 ARM64 镜像后：

```bash
ARCHIVE=serverfs-core-linux-arm64.tar
docker image save --platform "$ARCH" -o "$ARCHIVE" "$IMAGE"
```

仅在当前 Docker API 支持时使用按平台保存。否则先确保本地 tag 对应预期 ARM64 镜像，再执行 `docker save -o "$ARCHIVE" "$IMAGE"`。两种方式均应在最终归档生成后创建 checksum：

```bash
sha256sum "$ARCHIVE" > "$ARCHIVE.sha256"
docker image inspect --format '{{.Os}}/{{.Architecture}} {{.Id}}' "$IMAGE"
```

使用 save/load，不使用 export/import，以保留 Compose 使用的镜像、tag 和 layer 模型。通过可信渠道传输 tar、checksum、精确镜像引用和预期镜像 ID。归档 checksum 检查传输完整性，不能单独证明发布者身份。

在目标机重新设置相同的实际镜像引用与文件名：

```bash
IMAGE='<same-exact-version-tagged-image-reference>'
ARCHIVE=serverfs-core-linux-arm64.tar
sha256sum -c "$ARCHIVE.sha256" &&
  docker image load -i "$ARCHIVE" &&
  docker image inspect --format '{{.Os}}/{{.Architecture}} {{.Id}}' "$IMAGE"
```

checksum 失败、架构不是 `linux/arm64` 或镜像 ID 不符合可信记录时停止。不要将意外模拟执行等同于经过验证的原生 ARM64 部署。

## 4. 仅启动所选 Core，不拉取、不构建

按照[快速开始](../getting-started/)准备新的私有 env、实际 Workdir、非 root UID/GID、空闲端口、Host 白名单和调用方认证。将 `SERVERFS_IMAGE` 设为本地已加载的精确 tag。未需要 Tunnel 或 Bridge 时不要增加对应设置。在版本匹配的 checkout 根目录，以直接 HTTP 纯文件部署为例：

```bash
ENV_FILE=/absolute/private/blue.env
core=(docker compose -p serverfs-blue --env-file "$ENV_FILE"
  -f compose.yml -f compose.http.yml
  -f deployment/linux/compose.publish-http.yml
  -f deployment/linux/compose.bridge-disabled.yml)

"${core[@]}" config --quiet
"${core[@]}" up -d --no-deps --no-build --pull never serverfs-mcp
"${core[@]}" ps serverfs-mcp
```

缺少镜像或配置不合法时停止。关闭全部可选能力时，按快速开始验证认证后的 MCP initialize、tools/list（11 个工具）、list_workdirs、安全读取和只读写入拒绝。后续操作保留同一 project、env 和 overlay 组合。

## 5. 受限网络中的可选组件

只准备所选拓扑需要的组件：

- Cloudflare：单独获取选定 `CLOUDFLARED_IMAGE`，核对 ARM64 variant 后转运。运行时还需要独立公网 Tunnel 路由和 Provider 网络。
- OpenAI：单独获取经过验证且支持命名 HTTP upstream 与文件凭据选项的 `OPENAI_TUNNEL_IMAGE`，核对 ARM64 兼容性后转运。若运行时需要代理，使用 `SERVERFS_OPENAI_TUNNEL_USE_PROXY` 及文档指定的 `SERVERFS_PROXY_*` 配置；不会配置 Docker daemon 拉取或其他组件代理。见 [Tunnel 诊断](../tunnel-diagnostics/)。
- OAuth 和文件入口：其 Linux 服务使用同一个 `SERVERFS_IMAGE`；分别准备配置、授权状态及实际所需 Provider/HTTPS fetch 出站路径。本地镜像不证明运行时外联可用。
- 宿主机 Agent Bridge：在宿主机准备版本匹配的 Bridge 源码、uv 缓存和依赖。仅在所需缓存齐全时使用手册中的离线安装选项；Provider executable、凭据及出站路由留在宿主机。Repo Ops-only 不需要 Provider 执行前置验证。

精确 overlay/profile、签名接入及组件级启停见 [Linux 运维手册](https://github.com/NTLx/ServerFS_MCP/blob/v0.26.0/deployment/linux/README.md)。保持完整 Compose 参数，离线启动使用 `--no-build --pull never`，按对应手册显式启动并验证所需依赖。公网 Connector、OAuth 和原生 Agent 行为需各自实测，不能仅凭镜像转运成功推断。
