---
title: 二进制传输
description: 有界整文件下载与基于 revision 的受保护上传。
---

二进制传输是可选能力，默认关闭。

可通过 `SERVERFS_BINARY_TRANSFER_ENABLED=true` 全局启用，或通过 `WORKDIR_XX_BINARY_TRANSFER_ENABLED=true` 仅为指定 workdir 启用。

## 下载

`download_binary_file` 返回：

- 通过 MCP binary resource channel 返回原始二进制内容
- 精确字节数
- 尽力识别的 MIME type
- SHA-256
- 不透明 revision

结构化 metadata 通过工具的 output schema 暴露。

## 上传

`upload_binary_file` 要求且只接受一种整文件来源：

1. `data_base64`：严格 RFC 4648 Base64，保留给通用 MCP 客户端
2. `file`：启用可选文件入口后，由 ChatGPT/OpenAI 提供的文件参数

文件发布语义保持不变：

- `overwrite=false` 仅创建新文件
- `overwrite=true` 执行基于 revision 的原子替换，并要求提供当前 `expected_revision`

目标位置始终由显式 ServerFS `path` 决定。`file_name`、`file_id` 等客户端 metadata 永远不会成为文件系统路径。

### ChatGPT 文件入口

文件入口独立且默认关闭。启用时设置 `SERVERFS_FILE_INGRESS_ENABLED=true`，在 `SERVERFS_FILE_INGRESS_ALLOWED_HOSTS` 中配置经过实际测量的精确主机名，并使用 `--profile file-ingress` 启动 Compose。

启用后，`upload_binary_file` 会暴露 `_meta["openai/fileParams"] = ["file"]`。隔离的 ingress sidecar 负责获取临时 HTTPS URL；ServerFS MCP 主容器仍然没有互联网出口。Sidecar 不挂载任何 workdir、不持有 OpenAI 凭据、不发布端口，并拒绝未列入精确白名单的主机或解析到非公网地址的目标。

二进制传输不会绕过 workdir 授权。上传仍然要求目标 workdir 可写。

## 限制

`SERVERFS_MAX_BINARY_TRANSFER_BYTES` 以及每个 workdir 的覆盖值同时约束上传和下载。默认上限为 8 MiB。MCP HTTP request body 上限会根据启用 workdir 中最大的原始二进制上限计算，避免 Base64 兼容通道被 SDK 更小的默认请求体限制提前截断。
