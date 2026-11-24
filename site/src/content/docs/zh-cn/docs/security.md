---
title: 安全模型
description: 独立的调用授权、文件系统、入口网络与宿主机执行边界。
---

ServerFS 提供受限能力，而非直接开放宿主机 Shell。各层独立执行策略，启用 Connector、Bridge 或某项能力不会自动授权其他操作。

## 1. 信任边界

| 边界 | 执行机制 |
| --- | --- |
| 远程调用方 → Core | 公网入口 TLS、精确 Host/Origin 准入、API Key 或 OAuth |
| Core → Workdir | 显式别名、默认只读、操作限制、凭据拒绝规则及系统权限 |
| Core → Bridge | 实例身份、私有原生 IPC、受限策略与 grant |
| Bridge → Git / Agent | 固定 Repo Ops action 或已授权 Provider runtime、writer lease、原生审批 |
| 可选外部下载/建议 | 独立文件入口或 Jev 建议，不扩大 Workdir 权限 |

Tunnel 密钥证明 Connector 身份，不代表调用方身份。不同实例应使用独立凭据和私有状态。同一 UID/SID 不构成授权；同用户组件隔离不是针对恶意同用户进程的安全沙箱。

## 2. 文件系统与变更

Core 使用 POSIX 基于文件描述符或 Windows 原生基于 HANDLE 的路径遍历，拒绝 symlink 和不安全的 Windows 重定向路径，仍受操作系统权限约束。仅暴露配置的 Workdir 别名和相对路径。

- 默认只读，变更需要显式可写策略。
- 文本编辑、删除及二进制覆盖使用不透明 revision 防护。没有通用覆盖/force 开关或递归删除目录。
- 隐藏文件与凭据拒绝规则相互独立。Linux 额外 deny 规则取并集，不建议关闭内建 deny；原生 TOML 保留内建拒绝规则。
- 读、搜索、写入均有大小或数量限制。结构化操作日志不记录文件内容；不要将凭据写入提示词或日志。

Linux 额外使用非 root 容器、只读根文件系统、drop capabilities 与 no-new-privileges。原生部署依赖当前用户权限、私有目录/ACL 与系统原生生命周期，不是容器隔离。不要将实例状态或 Provider 凭据根目录暴露为 Workdir。

## 3. HTTP 准入

Streamable HTTP 保留 DNS rebinding 防护，以配置的 Host 白名单为准。Linux 留空时派生内部服务 authority，命名原生 Core 使用配置的 hostname 与 loopback authority。未获准的 Host/Origin 被拒绝。使用精确条目，而不是为排查代理故障放宽策略。

[API Key](../api-key/) 在分发请求前以常量时间比较校验共享密钥。优先使用 bearer header，URL 参数有日志泄露风险。Key 不区分个人身份，也不改变 Workdir 策略。

[OAuth](../oauth/) 使用独立授权服务签发的、绑定 resource 的不透明 Token，通过私有 introspection 校验。Provider 凭据留在授权服务；Provider 原生 token 不能直接用于 MCP。稳定 principal 默认拒绝、精确授权，code/state 单次使用，refresh token 轮换，原始 bearer 值不持久化。OAuth scope 不能替代 Workdir 策略。API Key 与 OAuth 互斥；命名 OpenAI HTTP 入口要求静态 bearer 准入。

## 4. 文件入口与网络隔离

Linux 仅内部网络模式不发布 Core 宿主机端口；直接 HTTP 发布增加的网络可能允许普通容器出站。两者的隔离级别不同。

可选 Linux 文件入口使用独立出站 sidecar，不挂载 Workdir、不持有 OpenAI 凭据、不发布端口。Core 调用固定内部端点，不直接访问传入的临时 URL。获取文件要求 HTTPS:443，主机名必须精确授权或属于显式启用的受限 OpenAI Blob 家族。Sidecar 拒绝非公网 DNS 结果，固定到已验证 IP，按原主机名校验 TLS，并重新检查每次重定向。字节数和超时均受限制。见[二进制传输](../binary-transfer/)。

## 5. 宿主机执行与组件身份

Bridge 可选。Linux 签名接入与 Windows 命名接入使用受限 grant；macOS owned 使用专属原生 socket 和 manifest/策略校验，Shared Core 使用签名 v2 grant。共享 owner 自己管理授权和生命周期；未授权方法、撤销或过期 grant、跨 Core 任务访问必须安全拒绝。独立 Bridge owner 不得占用重叠可写根目录。

[Repo Ops](../repo-ops/) 执行确定性 Git 操作和管理员固定的 named action，不创建 Agent task。调用方不能提供任意 argv、environment、remote、ref、hooks 或 force。Commit 只接受显式路径且拒绝已存在的暂存修改；push 使用当前配置的 upstream。Named action 执行前校验 SHA-256 pin，即使标为不变更也必须获取独占 writer lease。释放 lease 前须证明子进程树已清理；Windows 使用 Job 约束并拒绝不安全 reparse 路径。

[Agent 委托](../agent-bridge/) 通过宿主机 IPC 使用已配置的 runtime 与 Workdir profile，而非通用 MCP 命令接口。Provider 的原生权限、沙箱和审批仍然约束 Agent。Linux Agent 代理只允许无凭据端点；代理模式 Codex 使用 Bridge 独立 app-server。不要为消除故障而直接放行审批或跨实例共享凭据。v0.26.0 候选版在脱敏前为原始审批请求生成与任务绑定的私有带密钥标识，并保存操作及处理结果元数据；识别出的 Workdir 外写入请求不得远程批准，应拒绝或取消。公开脱敏标签不是资源身份，也不能替代原生权限检查。

[Jev](../jev-advisors/) 仅提供建议，不能授权工具、决定权限或响应原生审批。私有密钥留在 Bridge；审批建议使用最小化、脱敏输入。建议失败不改变权限。

平台限制见[架构](../architecture/)，策略见[配置](../configuration/)。实际授权及拒绝行为必须独立于进程健康状态验证。
