---
title: 快速开始
description: 部署并验证最小 Core，再按需接入其他组件。
---

## 1. 选择部署形态

先阅读[架构](../architecture/)。macOS 或 Windows 上的本地 MCP 客户端可以使用原生 stdio，不需要持久 HTTP 服务。面向持久远程 MCP 时，从纯文件 HTTP Core 开始，再按需增加组件。

| 平台 | 部署入口 |
| --- | --- |
| Linux amd64 / arm64 | Docker Compose；使用下方直接 HTTP 示例 |
| Apple Silicon macOS 27、APFS | [macOS 原生部署](../macos-native/) |
| Windows 11 x64、NTFS | [Windows 原生部署](../windows-native/) |

使用版本匹配的安装产物和源码，不将尚未发布的构建当成稳定版，也不隐式迁移已有实例。

## 2. Linux：直接 HTTP 的纯文件 Core

安装 Docker 与 Compose v2.24+。获取目标版本经过验证的镜像（或使用独立 scratch tag 在本机构建）。使用真实存在的目录，并选择能够读取该目录的非 root 容器 UID/GID。在 Git 和全部暴露目录之外创建权限为 0600 的私有 env 文件，参考下列配置；替换占位值，并在本地生成独立高熵 bearer key。

```text
SERVERFS_IMAGE=<verified-version-matched-image>
SERVERFS_UID=1000
SERVERFS_GID=1000
SERVERFS_HTTP_HOST=0.0.0.0
SERVERFS_HTTP_PORT=8000
SERVERFS_HTTP_PATH=/mcp
SERVERFS_HTTP_PUBLISH_HOST=127.0.0.1
SERVERFS_HTTP_PUBLISH_PORT=18080
SERVERFS_HTTP_ALLOWED_HOSTS=serverfs-mcp:8000,127.0.0.1:18080
SERVERFS_HTTP_ALLOWED_ORIGINS=
SERVERFS_HTTP_API_KEY=<generate-a-unique-secret-locally>
SERVERFS_OAUTH_ENABLED=false
WORKDIR_01_ALIAS=docs
WORKDIR_01_PATH=/srv/serverfs-blue/docs
WORKDIR_01_READ_ONLY=true
SERVERFS_AGENT_BRIDGE_ENABLED=false
SERVERFS_AGENT_MODE=disabled
SERVERFS_AGENT_RUNTIMES=
WORKDIR_01_AGENT_MODE=disabled
WORKDIR_01_REPO_OPS_ENABLED=false
CONTROL_PLANE_API_KEY=
CONTROL_PLANE_TUNNEL_ID=
```

不要自动开放示例目录或使用示例凭据。未使用的 Workdir slot 保持禁用、只读占位状态。示例将容器 8000 端口发布到宿主机 loopback 的 18080 端口，两者分别配置。显式 Host 白名单同时包含容器内部和宿主机发布的 authority。

在版本匹配的 checkout 根目录执行 Bash：

```bash
ENV_FILE=/absolute/private/blue.env
core=(docker compose -p serverfs-blue --env-file "$ENV_FILE"
  -f compose.yml -f compose.http.yml
  -f deployment/linux/compose.publish-http.yml
  -f deployment/linux/compose.bridge-disabled.yml)

"${core[@]}" config --quiet
# 仅在需要获取已发布且本地尚无的镜像时执行：
"${core[@]}" pull serverfs-mcp
"${core[@]}" up -d --no-deps --no-build serverfs-mcp
"${core[@]}" ps serverfs-mcp
```

若使用已在本机构建的 scratch 镜像，跳过 pull。任一命令失败时停止。后续操作保持同一个 project、私有 env 和 overlay 组合，不输出展开后的 Compose 配置。

## 3. 连接并验证

在 MCP 客户端中选择 Streamable HTTP，地址为 `http://127.0.0.1:18080/mcp`，并在客户端私有认证 header 字段中配置 bearer 凭据。该 loopback 地址只适用于客户端和 Core 位于同一宿主机的场景，除非另行配置访问路径。

容器运行或 TCP 健康检查成功并不代表 MCP 验收通过。验证 initialize、tools/list、list_workdirs、目标目录的安全读取、只读目录的写入拒绝，以及缺少或错误凭据时的拒绝。关闭全部可选能力时，工具集合包含 11 个工具。

## 4. 按需扩展

- 直接远程 HTTP：配置精确 Host/Origin 准入及独立管理的 HTTPS 反向代理，公网访问需要调用方认证。认证模式见 [API Key](../api-key/)和 [OAuth](../oauth/)。
- 仅 Tunnel 或内部网络：用 internal-only overlay 替代端口发布 overlay。按对应 Provider 增加 [Cloudflare](../cloudflare-tunnel/) 或 OpenAI overlay/profile 与独立凭据；直接 HTTP 不要求任何 Tunnel。
- Agent 委托或确定性 [Repo Ops](../repo-ops/)：部署已授权的[宿主机 Bridge](../agent-bridge/)，明确 Core 身份和 Workdir/方法范围。不要仅为启用 Repo Ops 而启动 Agent Provider。
- [二进制传输](../binary-transfer/)：显式启用能力；可选文件入口采用独立 sidecar。

完整配置、Connector 独立生命周期、签名 Bridge provision、回滚和退役步骤见 [Linux 运维手册](https://github.com/NTLx/ServerFS_MCP/blob/v0.26.0/deployment/linux/README.md)。其他平台使用各自原生手册，不直接翻译 Docker 命令。
