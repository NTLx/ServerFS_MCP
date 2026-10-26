---
title: 架构
description: ServerFS 的数据路径与信任边界。
---

## MCP Core 与入口路径

```text
MCP 宿主
   │
   ├── stdio（Windows/macOS 原生）
   ├── Streamable HTTP（Linux；默认 loopback/私有）
   └── OpenAI Secure MCP Tunnel（可选 ChatGPT 入口）
               │
               ▼
          ServerFS MCP Core
               │  限定策略
               ▼
          明确配置的 workdir
```

MCP Core 与工具契约不依赖特定宿主。Linux 的通用 HTTP 发布为显式 opt-in，默认只绑定 loopback；对公网开放时必须显式配置 Host 策略，并在明确的入口边界终止 TLS 与认证。OpenAI Tunnel 继续作为可选、经过认证、仅出站的 ChatGPT 适配器，并使用独立控制面网络。MCP 主容器仍然没有通用互联网出口。

## 可选 ChatGPT 文件入口路径

```text
ChatGPT 文件参数
   │
   │ 临时 HTTPS URL
   ▼
ServerFS MCP
   │ 仅专用内部网络
   ▼
serverfs-file-ingress
   │ 仅允许经策略校验的 HTTPS/443 出站
   ▼
临时文件主机
```

该 sidecar 为显式可选能力。它不挂载 workdir、不持有 OpenAI/Tunnel 凭据，也不发布端口。MCP 主容器始终不获得互联网出口，只把临时 URL 和字节上限发送给 sidecar。主机授权可以来自管理员配置的精确主机名，也可以来自单独开启、根据真实 ChatGPT 文件参数实测得到的受限 OpenAI Azure Blob 账户家族；通用通配符会被拒绝。随后 Sidecar 继续校验 DNS 结果必须全部可公网路由，将连接固定到已验证 IP 并按原始主机名校验 TLS，对每次重定向重新校验，并执行超时和字节上限后才返回原始字节。

## 可选静态 API Key 路径

```text
MCP client
   │ Authorization: Bearer <key>
   │ 或 /mcp?key=<key>
   ▼
ServerFS 静态 API Key ASGI 边界
   ▼
Streamable HTTP MCP app
   ▼
现有 ServerFS workdir 策略
```

v0.17 的 API Key 路径只是 SDK Streamable HTTP app 外的一层小型 HTTP ingress wrapper，不引入 Authorization Server、数据库、用户身份、scope 模型或 provider egress。API Key 与内置 OAuth 互斥。为了兼容一行 MCP URL 支持 query key，但该模式会关闭 ServerFS 自身默认的 Uvicorn access log，避免由 ServerFS 自己打印带 secret 的 URL。详见[静态 API Key](../api-key/)。

## 可选 OAuth 路径

```text
MCP client
   │ OAuth bearer
   ▼
ServerFS MCP Resource Server
   │ 仅私有 introspection
   ▼
serverfs-auth
   │
   ├── GitHub
   ├── 钉钉
   ├── 飞书 / Lark
   └── 微信开放平台
```

`serverfs-auth` 是独立部署的非 root 服务，拥有自己的可写 SQLite state 与 OAuth-only egress network。Provider 凭据不会进入 `serverfs-mcp`；MCP 容器也不会加入 OAuth 出站网络。Resource Server 只接受 ServerFS 自己签发、绑定到配置 MCP resource 的不透明 Token。准入策略精确且默认拒绝，身份准入后仍使用原有 workdir 策略。

OAuth 属于 Linux remote-HTTP 部署形态，不属于 Windows/macOS 原生 stdio。公网 TLS/反向代理形态、provider callback、client 注册与 Tunnel 兼容边界详见 [OAuth 2.1](../oauth/)。

## 可选 Deterministic Repo Ops 路径（v0.23.0）

```text
ServerFS MCP 容器
   │  结构化 repo.* RPC
   ▼
已认证的宿主机 Bridge transport
   │
   ├── 固定语义的 Git inspection / commit / push
   └── 管理员定义、integrity-pinned 的 named actions
```

Repo Ops 会复用宿主机 Bridge transport、宿主机 Workdir 路径、当前用户 Git 凭据上下文以及共享 writer lease，但它**不会**进入 Agent 执行路径。不会选择模型/Runtime，不会创建 Agent task/session，也不会调用 Provider adapter。Repo Ops 与 Agent delegation 按 Workdir 独立授权。v0.23.0 发布范围仅声明 Linux。详见 [Deterministic Repo Ops](../repo-ops/)。

## 可选 Agent 路径

```text
ServerFS MCP 容器
   │
   │ 只读 Unix socket 挂载
   ▼
宿主机 Agent Bridge
   │
   ├── Codex
   ├── Claude Code
   ├── Qoder
   └── ACP v1 profiles（运维配置）
```

Bridge 以用户级宿主机服务运行，并在仓库 checkout 之外维护自己的 runtime socket、锁、已安装 release、配置与状态。

### 可选 Jev advisory 路径

```text
Agent task / 模型建议请求 / provider approval
   │
   ▼
宿主机 Agent Bridge
   │  最小化结构化 state
   ▼
TypeSafe Jev API
   │  probabilities / confidence / Noul
   ▼
仅作为 advisory result
```

Jev 从宿主机 Bridge 发起调用，不运行在 MCP 容器中。Task Preflight 与 Runtime Router 共用一次任务提交请求；Model Advisor 仅在原生模型发现之后、任务提交之前按需运行；Approval Advisor 只在 provider 真正创建 approval request 后运行。这些输出不会获得授权权力，也不会自动选择 runtime 或模型。

## 原生持久生命周期

原生部署把持久化放在 MCP 协议面之外，并严格限定在当前用户的操作系统生命周期中：

```text
Windows v0.14.0+
Task Scheduler 2.0（InteractiveToken · LeastPrivilege）
   │  登录触发 + 1 分钟 TimeTrigger heartbeat
   ▼
无窗口 watchdog
   │  Job Object containment + 有界重启
   ▼
managed runner
   │  有界 Tunnel generation 重启
   ▼
tunnel-client -> supervisor -> ServerFS / Agent Bridge

macOS v0.13.2
SMAppService + 当前用户 LaunchAgent
   ▼
持久 Tunnel / Agent Bridge
```

Windows v0.14 生命周期刻意**不使用** SCM/LocalSystem Windows Service：安装、运行、维护与卸载全过程都保持当前用户、无需提权。全新任务注册后默认 disabled；`service start` 才启用，`service stop` 会先禁用任务再结束当前 Action，因此 heartbeat 不会把主动停止误认为故障并重新拉起。`IgnoreNew` 与原生 ownership mutex 共同保证只有一个受管 Windows Tunnel 生命周期。

## 能力边界

ServerFS 提供窄能力操作，而不是通用执行原语：

- 文件系统读取工具
- 受保护的文件写入
- 有界整文件二进制传输
- 可选且隔离的 ChatGPT 文件入口
- Linux remote HTTP 可选单一共享密钥静态 API Key 边界
- Linux remote HTTP 可选 OAuth 2.1 Resource Server + 隔离身份 broker
- 结构化 Agent task RPC、模型发现与单次任务模型覆盖
- v0.23.0 Linux Deterministic Repo Ops：有界 Git inspection、固定 named action、显式文件 commit、精确当前 upstream push，以及面向已验证 `package.json` 发布形态的固定 npm 发布动作
- 可选的宿主机 Jev advisory decision

稳定版 **v0.23.0** 保留既有 11 / 13 / 21 / 23 工具组合，并可在 Linux 上独立增加 4 个 Repo Ops 工具，因此 Repo Ops-only 为 15 个、二进制 + Repo Ops 为 17 个、Agent + Repo Ops 为 25 个，三者全部启用为 27 个。Repo Ops 与 Agent delegation 分别门控，且 Repo Ops 不创建 Agent task、不接收通用 shell / argv / env / remote / ref / force。Linux bootstrap / 内部 E2E、公共 ChatGPT MCP E2E、刷新后的 root/Agent Bridge CI、MCP conformance、Windows/macOS 回归以及 documentation freeze 后的站点 build/deploy 均在发布前通过。完整契约与证据见 [Deterministic Repo Ops](../repo-ops/)。Jev 仍是 Agent Bridge 内部 advisor，而不是 MCP capability surface。
