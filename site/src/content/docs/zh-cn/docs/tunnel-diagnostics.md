---
title: Tunnel 诊断
description: 区分 OpenAI Tunnel 的存活、就绪、MCP 观测、control-plane polling 与响应投递状态。
---

OpenAI Secure MCP Tunnel 是可选的 ChatGPT 入口适配器。容器正在运行只代表第一层证据：`docker compose ps` 显示 `Up` **并不能**单独证明 Tunnel 当前能够正常轮询 control plane 或投递 MCP 响应。

## 健康状态模型

建议按以下层级依次判断：

| 层级 | 证据 | 含义 |
| --- | --- | --- |
| Container | `docker compose ps` | 容器进程存在。 |
| Liveness | `/healthz` | tunnel-client 进程存活。 |
| Readiness | `/readyz` 或 `tunnel-client health` | 启动就绪门禁已经通过。 |
| MCP | `/health/mcp` | Tunnel 实际观测到的目标 MCP 状态。 |
| Control plane | `/health/control-plane` | poll 尝试、成功、等待、重试/backoff 与受限失败状态。 |
| Response delivery | `/health/response-delivery` | 已完成的 MCP 结果是否成功被上游接收。 |
| Local execution | `/health/queue` 与 `/health/dispatcher` | 本地积压、背压与活动操作。 |

`/healthz` 的语义刻意弱于 `/readyz`。详细 `/health/...` component route 是诊断快照：即使某个 component 的 JSON 状态为 degraded，其 HTTP 响应仍可能是 200，因此不能把 component HTTP 状态直接当作 readiness gate。

## Compose healthcheck

ServerFS v0.20.0 为内置 `openai-tunnel` 服务配置 tunnel-client 自带探针：

```text
/usr/bin/tunnel-client health --port <configured-port> --require-control-plane-poll
```

Compose healthcheck 会从 `HEALTH_LISTEN_ADDR` 推导端口。tunnel-client 探针检查 liveness 与 readiness；加上 `--require-control-plane-poll` 后，还要求至少存在一次成功的 control-plane poll，才会报告 healthy。这样可以区分“进程仅仅在运行/本地已 ready”和“启动后确实成功到达过 control plane”。

Health listener 仍只存在于容器/内部网络中，ServerFS 默认不会把 8080 端口发布到宿主机。

查看当前状态：

```bash
docker compose ps openai-tunnel
docker compose exec openai-tunnel /usr/bin/tunnel-client health --port 8080
docker compose exec openai-tunnel \
  /usr/bin/tunnel-client health --port 8080 --require-control-plane-poll --json
```

Control-plane requirement 证明的是本次 daemon 启动后至少有一次 poll 成功；它仍然属于历史启动证据，而不是远端 control plane 永久连续可用的保证。如果此前成功过、之后又出现间歇性故障，仍需继续检查 component 状态与日志。

## 读取 component 诊断

内置 Tunnel 镜像基于 Alpine，因此可以使用 BusyBox `wget` 从 Tunnel 容器内部访问 loopback-only health listener：

```bash
docker compose exec openai-tunnel \
  wget -qO- 'http://127.0.0.1:8080/health?details=true'

docker compose exec openai-tunnel \
  wget -qO- 'http://127.0.0.1:8080/health/mcp'

docker compose exec openai-tunnel \
  wget -qO- 'http://127.0.0.1:8080/health/control-plane'

docker compose exec openai-tunnel \
  wget -qO- 'http://127.0.0.1:8080/health/response-delivery'
```

较旧的 tunnel-client 版本可能没有全部详细 component route。ServerFS v0.20.0 默认使用仓库实测基线 v0.0.17；这是测试默认值，不是兼容性锁定，除非显式覆盖 `OPENAI_TUNNEL_IMAGE`。

## 排障顺序

### 1. 容器没有运行

先看：

```bash
docker compose ps openai-tunnel
docker compose logs --tail=200 openai-tunnel
```

先解决启动或配置错误，再调查远端连接问题。

### 2. 容器运行但 unhealthy

执行：

```bash
docker compose exec openai-tunnel /usr/bin/tunnel-client health --port 8080 --json
```

随后检查详细 aggregate。常见 readiness 失败包括 MCP 启动可达性、启用 OAuth 时的 discovery，以及 control-plane connectivity。

### 3. MCP 没有被观测到

检查 `/health/mcp`。根据 transport 与当前流量，即使 Tunnel 已 ready，也可能暂时显示尚未观测 MCP。应结合 component 的 observation timestamp 与 state 判断，而不是把“没有历史证据”直接等同于 MCP server 不存在。

同时独立检查 ServerFS 容器：

```bash
docker compose ps serverfs-mcp
docker compose logs --tail=200 serverfs-mcp
```

### 4. Control-plane poll 失败

检查 `/health/control-plane` 与 Tunnel 日志：

```bash
docker compose logs --tail=200 openai-tunnel
```

持续出现已完成的 poll failure 或 retry/backoff，比容器 `Up` 更能说明远端网络或认证存在问题。正在进行的 long poll 本身是正常状态，不应单独视为故障。

如果启用了 `SERVERFS_OPENAI_TUNNEL_USE_PROXY=true`，还需要检查 HTTP proxy 路径。Tunnel proxy 只影响 Tunnel control-plane 出站，不会配置 Docker 拉取镜像。详见[受限网络 ARM64 部署](../restricted-network-arm64/)。

### 5. Poll 成功但 ChatGPT 收不到结果

检查 `/health/response-delivery`。轮询命令和上传执行结果是两个独立阶段，可以分别失败。

如果本地任务看起来卡住，再查看 `/health/queue` 与 `/health/dispatcher`，用于区分背压与仍在执行中的操作。

## `doctor` 与运行时健康状态的区别

直接运行 tunnel-client 或在受控环境中复现配置时，`tunnel-client doctor --explain` 很适合作为 preflight。它验证本地配置与可达性检查，但不能替代当前运行中 Tunnel 的 component 证据，也不能证明未来每一次 control-plane poll 都会成功。

对于 ServerFS Compose 部署，优先按以下顺序：

1. `docker compose ps`；
2. 在运行中的容器内执行 `tunnel-client health --port 8080 --json`；
3. 查看 `/health?details=true` 与相关 component route；
4. 查看有界的近期 Tunnel 日志。

这样可以确保诊断针对的就是 ChatGPT 实际正在使用的那个进程与配置。

## 安全注意事项

- 不要为了排障把 Tunnel health/admin 端口直接发布到不可信网络。
- 不要把 `.env`、proxy credential、control-plane key 或未经限制的完整日志直接发送到支持渠道。
- 优先使用有界 component 快照和近期、已脱敏日志。
- Health observation 是带时间戳的运维证据，不是持续可用性的永久保证。
