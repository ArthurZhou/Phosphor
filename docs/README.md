# 文档索引

本目录按读者分成三类。每类有一篇主文档负责该读者的完整链路，其余文档承载具体
专题的细节，主文档与专题文档互相链接，同一事实在所有文档中保持一致。

## 使用（设备操作者）

| 文档 | 内容 |
|---|---|
| [USAGE.md](USAGE.md) | 主文档：环境、刷写、连接、GUI/CLI 操作、采集、诊断、排障 |
| [FLASHING.md](FLASHING.md) | IAP 模式进入、写入、验证、恢复原厂的完整步骤 |
| [HOST.md](HOST.md) | 上位机各工具的参数、输出判读、CSV 格式、仿真模式 |

建议顺序：USAGE → FLASHING（刷写前）→ USAGE 后半 → HOST 按需查。

## 二次开发（开发者 / 维护者）

| 文档 | 内容 |
|---|---|
| [DEVELOPMENT.md](DEVELOPMENT.md) | 主文档：架构、目录、构建、自检、补丁机制、扩展流程 |
| [PROTOCOL.md](PROTOCOL.md) | 数据通道协议：命令、帧格式、标志位、校验语义、扩展规则 |
| [USB_STACK.md](USB_STACK.md) | USB 栈：官方库来源与改造、时钟、端点、中断、发送状态机 |
| [UI_PATCH.md](UI_PATCH.md) | 界面改写：调色板、字形、关于页、菜单裁剪、扁平化 |
| [RELEASING.md](RELEASING.md) | 仓库边界、依赖获取、发布步骤、产物检查清单 |

建议顺序：DEVELOPMENT → PROTOCOL → USB_STACK → UI_PATCH → RELEASING。

## 逆向与分析（审计者 / 逆向开发者）

| 文档 | 内容 |
|---|---|
| [ANALYSIS.md](ANALYSIS.md) | 主文档：基线、方法、存储器布局、启动链、各子系统、补丁点、证据表、未解问题 |

补丁代码中出现的所有硬编码地址，其来源与置信度都在 ANALYSIS.md 中；修改补丁前
应先核对该文档的标注。

## 参考资料与约定

`reference/` 存放第三方资料，仅供分析对照，不随仓库分发：

| 文件 | 内容 |
|---|---|
| `DSO-153-_Manual.pdf`、`manual.txt` | 原厂说明书及其文本提取 |
| `strings_all.txt`、`strings_utf16.txt` | 固件字符串提取结果 |
| `at32_otgfs_an.pdf`、`at32_otgfs_an.txt` | 雅特力 OTGFS 应用笔记 |

写作约定：

- 术语保留英文原文（hook、trampoline、vector table、ring buffer、bulk endpoint、
  FIFO、CFSR、EOPB0 等），首次出现时给中文解释；
- 地址写成 `0x` 前缀的定长大写十六进制（如 `0x0801A7BC`）；
- 数值与单位之间不加空格（`12Mbps`、`200ms`）；
- 逆向结论标注证据强度：**已验证**（静态分析与实机行为一致）、**静态**（仅由反
  汇编或数据扫描得出）、**推断**（由间接证据推得）；推断项不得在实现中当作事实；
- 文档与源码冲突时以源码为准，并应同步修正文档。

## 相关文档

- [../README.md](../README.md)（仓库入口）
