---
title: 配置
description: 配置 Core、Workdir 与可选能力。
---

每个 Core 拥有独立配置。Workdir 策略在启动时确定。不同平台使用各自的配置格式，统一安全结果，而非强制统一语法。

## 1. 选择配置格式

| 平台 | Core 策略 | 持久 HTTP 与组件 |
| --- | --- | --- |
| Linux | 私有 env + Compose，最多 16 个槽位 | 命名 project、所选暴露及 Bridge overlay、可选 Connector profile |
| macOS / Windows | 原生 TOML，最多 64 个 Workdir | 私有 HTTP 准入输入、命名服务 manifest、独立 Bridge/Connector 配置 |

Linux 参考版本匹配的 [.env.example](https://github.com/NTLx/ServerFS_MCP/blob/main/.env.example)，原生平台参考 [serverfs.toml.example](https://github.com/NTLx/ServerFS_MCP/blob/main/serverfs.toml.example)。使用前替换所有生效的示例路径，私有输入不得位于暴露的 Workdir 中。

## 2. 定义最小必要 Workdir

Linux env：

```dotenv
WORKDIR_01_ALIAS=docs
WORKDIR_01_PATH=/srv/serverfs-blue/docs
WORKDIR_01_READ_ONLY=true
```

原生 TOML（macOS 示例，Windows 使用实际 Windows 路径）：

```toml
[[workdirs]]
alias = "docs"
path = "/Users/operator/ServerFS-data/docs"
read_only = true
```

别名唯一，以字母开头，最多 32 个字母、数字、下划线或连字符。路径必须是实际存在且已授权的目录。不要为了方便暴露私有服务根目录、凭据目录或整个用户主目录。可写 Workdir 同时需要显式策略和操作系统写权限。Linux 的 `READ_ONLY` 还控制 bind mount。

## 3. 理解策略继承

| 设置 | Linux env | 原生 TOML |
| --- | --- | --- |
| 隐藏文件、读写限制、二进制传输 | 全局默认值与逐槽覆盖 | 全部 Workdir 共用 `[defaults]` |
| 额外 deny glob / 内建 deny 覆盖 | 支持；额外规则取全局与槽位并集 | 保留内建 deny，不接受这些覆盖字段 |
| 只读、Agent mode/runtimes、Repo Ops | 逐槽配置 | 逐 `[[workdirs]]` 配置 |

Linux 空标量继承默认值，显式值优先；Repo Ops 逐槽默认关闭。显式禁用 Agent 且 runtime 列表留空时，清空继承的 runtime。原生 TOML 拒绝未知字段，不要直接照搬 Linux 槽位字段。全局 Agent 默认保持禁用，仅对目标 Workdir 开启。

默认限制包括文本每页读取 512 KiB / 500 行、单次文本变更 1 MiB、整文件二进制传输 8 MiB。Linux 二进制全局值是默认值，不是不可突破的上限；直接传输使用显式槽位覆盖值。文件入口另外执行其全局上限。见[二进制传输](../binary-transfer/)。

## 4. 独立配置 HTTP 准入

Linux 按[运维手册](https://github.com/NTLx/ServerFS_MCP/blob/main/deployment/linux/README.md)选择仅内部 HTTP 或直接端口发布。Core 监听端口与宿主机发布端口分离。Linux Host 白名单留空时派生 `serverfs-mcp:<HTTP_PORT>`，只允许实际需要的 authority。Origin 白名单留空拒绝非空 Origin。保留 DNS rebinding 防护，公网访问使用 HTTPS。

命名原生 HTTP 准入要求精确配置 hostname 和 loopback authority，使用 [macOS](../macos-native/) 或 [Windows](../windows-native/) 手册，而非 Linux env 模板。

选择一种调用方认证方式：

- [静态 API Key](../api-key/)：优先使用 Authorization Header，URL 参数可能被日志记录。
- [OAuth](../oauth/)：使用独立配置的授权服务与私有 introspection。

Linux 两者默认关闭，但不得发布未认证的 Core。原生 HTTP 必须配置一种认证方式。本地 stdio 不使用 HTTP 认证。Connector 凭据独立；命名 OpenAI HTTP Connector 要求静态 bearer 认证。

## 5. 按需启用能力

| 能力 | 策略与前提 |
| --- | --- |
| [二进制传输](../binary-transfer/) | 显式开启；上传要求可写；文件入口另行配置 |
| [Repo Ops](../repo-ops/) | 逐 Workdir 启用，需已授权宿主机 Bridge，无需 Agent runtime |
| [Agent 委托](../agent-bridge/) / [ACP](../acp/) | 需授权 Bridge 与 runtime 白名单；运行要求 workspace-write 和可写 Workdir |
| [Jev](../jev-advisors/) | Bridge 侧可选建议，不授予权限 |

Linux 命名签名接入仅支持一个 Agent/Repo Ops 槽位（01）。macOS Shared Core 只支持一个绑定的 Agent Workdir，不支持 Repo Ops。Windows 初始共享配置需要精确策略子集与独立 grant。按照平台手册配置；开启能力的开关不能替代有效绑定。

Provider 可执行文件与凭据保留在宿主机。各组件的出站代理独立配置。Linux 兼容渲染器为 Tunnel、Agent 与 Jev 复用代理端点，但分别启用；Agent 代理必须无凭据。命名 Bridge owner 有自己的显式策略，不能假定修改 Core env 就会改变 owner。

## 6. 验证生效策略

修改前读取实际实例配置，确认平台、身份和 Workdir 映射。验证所选部署，必要时仅重启已授权组件，再检查认证后的 MCP、预期工具、安全读取及拒绝行为。配置文件正确或 task 正在运行都不等于权限已经验证。见[安全模型](../security/)。
