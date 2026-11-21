---
title: 二进制传输
description: 启用有界整文件下载和受保护上传。
---

二进制传输为可选能力。客户端支持 MCP 二进制资源或确实需要上传原始字节时再启用；仅处理文本时无需开启。

## 启用传输

| 平台 | 配置 |
| --- | --- |
| Linux | `SERVERFS_BINARY_TRANSFER_ENABLED=true` 或 `WORKDIR_XX_BINARY_TRANSFER_ENABLED=true` |
| macOS / Windows | 原生 TOML 的 `[defaults]` 中设置 `binary_transfer_enabled = true`（作用于该 Core 全部 Workdir） |

二进制传输本身不需要 Bridge。仍执行普通 Workdir 授权：只读目录可以下载，上传需要可写目录。所有 Workdir 都未开启时，不注册两个二进制工具。

## 下载

调用 `download_binary_file`，传入 Workdir 别名和相对路径。结果包含 MCP EmbeddedResource/BlobResourceContents 中的文件字节（传输层为 Base64），以及大小、MIME type、SHA-256 和不透明 revision 等结构化元数据。重建文件应使用二进制资源而非元数据，需要确认完整性时校验哈希。

## 上传

调用 `upload_binary_file`，传入别名、目标路径及且仅一种来源：

- `data_base64`：严格 RFC 4648 Base64 编码的完整文件。
- `file`：文件入口可用时，使用 ChatGPT/OpenAI 文件参数。

`overwrite=false` 仅创建，已有路径会被拒绝。替换使用 `overwrite=true` 与 stat/download 返回的当前 `expected_revision`，原子替换并拒绝多硬链接普通文件。仅创建时不要提供 revision。显式目标路径为准，客户端元数据中的文件名或 ID 不决定文件系统路径。

## 可选 ChatGPT 文件入口

Linux 在已有部署组合中追加 `file-ingress` Compose profile，并设置 `SERVERFS_FILE_INGRESS_ENABLED=true`。通过 `SERVERFS_FILE_INGRESS_ALLOWED_HOSTS` 配置精确主机，或显式开启 `SERVERFS_FILE_INGRESS_ALLOW_OPENAI_BLOB_HOSTS=true` 允许受限的 `oaisdmntpr<后缀>.blob.core.windows.net` 家族。拒绝通用通配符和非公网 DNS 地址。隔离 sidecar 下载临时 HTTPS 文件；Core 不访问任意 URL。安全边界见[安全模型](../security/)。

启用文件入口的上传工具声明 `_meta["openai/fileParams"] = ["file"]`。不能假定原生命名 Core 存在该 sidecar：macOS 命名 HTTP 实例禁用共享文件入口，Windows 原生使用直接二进制传输。macOS 兼容部署存在独立 helper 路径。以实际工具 schema 与就绪状态为准，而不是只看 env 开关。

## 大小与上下文限制

默认原始文件大小为 8 MiB。Linux 全局 `SERVERFS_MAX_BINARY_TRANSFER_BYTES` 提供默认值，槽位的 `WORKDIR_XX_MAX_BINARY_TRANSFER_BYTES` 可替换直接上传/下载限制。文件入口执行槽位限制与 sidecar 全局上限中较小者。原生 TOML 在 `[defaults]` 中使用 `max_binary_transfer_bytes`。HTTP 请求体上限预留最大启用传输的 Base64 扩展空间。

整文件传输不支持分块或断点续传。Base64 约增加三分之一字节数，可能显著消耗客户端上下文。Agent 应优先使用客户端支持的二进制资源或文件参数，不在对话提示词中复制大段 Base64。查看文本时使用有界文本读取。传输后核对大小、SHA-256 与目标 revision。
