---
title: 配置
description: 定义受控 workdir，并继承安全默认值。
---

ServerFS 在启动时为每个已启用 workdir 解析出一份不可变的最终生效策略。

## Workdir 槽位

最多可配置 16 个 workdir：

```text
WORKDIR_01_ALIAS=projects
WORKDIR_01_PATH=/srv/projects
WORKDIR_01_DESCRIPTION="Projects"
WORKDIR_01_READ_ONLY=true
```

另一个 workdir 可以显式开放写入：

```text
WORKDIR_02_ALIAS=scratch
WORKDIR_02_PATH=/srv/scratch
WORKDIR_02_DESCRIPTION="Agent scratch space"
WORKDIR_02_READ_ONLY=false
```

## 策略继承

全局 `SERVERFS_*` 设置充当默认值。某个槽位中的 `WORKDIR_XX_*` 标量配置会覆盖全局值；留空则继承全局默认值。

这一模型适用于：

- 隐藏文件策略
- 读写限制
- 二进制传输
- Agent 策略

额外 deny glob 更严格：全局规则与 workdir 规则会**取并集**，因此 workdir 可以增加限制，但不能移除全局 deny 下限。

## 重要默认值

- Workdir 默认只读。
- 二进制传输默认关闭。
- ChatGPT 文件入口默认关闭，并且需要同时设置 `SERVERFS_FILE_INGRESS_ENABLED=true` 与启用 `file-ingress` Compose profile。
- 受限 OpenAI Blob 主机家族策略也独立默认关闭；只有确实需要 ChatGPT 文件参数时才设置 `SERVERFS_FILE_INGRESS_ALLOW_OPENAI_BLOB_HOSTS=true`。额外精确主机名通过 `SERVERFS_FILE_INGRESS_ALLOWED_HOSTS` 配置；通用通配符会被拒绝。
- Agent 策略只有在显式配置后才启用。
- MCP 工具结果不会暴露宿主机路径。
- Workdir 路径拼写错误会明确失败，而不是静默创建目录。

完整环境变量参考请查看仓库 [README](https://github.com/NTLx/ServerFS_MCP#workdir-configuration)。
