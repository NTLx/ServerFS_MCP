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

`upload_binary_file` 支持：

1. 当 `overwrite=false` 时仅创建新文件
2. 当 `overwrite=true` 时执行基于 revision 的原子替换

替换操作要求调用方提供当前 `expected_revision`。

二进制传输不会绕过 workdir 授权。上传仍然要求目标 workdir 可写。

## 限制

`SERVERFS_MAX_BINARY_TRANSFER_BYTES` 以及每个 workdir 的覆盖值同时约束上传和下载。默认上限为 8 MiB。
