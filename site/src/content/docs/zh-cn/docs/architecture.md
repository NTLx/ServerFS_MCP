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
   └── Qoder
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
- 结构化 Agent task RPC、模型发现与单次任务模型覆盖
- 可选的宿主机 Jev advisory decision

当前 **v0.15.0** 稳定版仍支持 11 / 13 / 21 / 23 个工具的四种 MCP 能力面。每种可选能力都有独立门控；v0.12 的 Tunnel / Agent / Jev 独立代理开关、v0.13 的 macOS 原生部署以及 v0.14 的 Windows 持久生命周期都不会改变公开工具数量。Jev 仍是 Agent Bridge 内部 advisor，而不是新的 MCP capability surface。
