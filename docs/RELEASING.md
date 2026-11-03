# 发布流程

本文回答四个问题：仓库里放什么不放什么、构建输入从哪来、怎么发一个版本、发布
前查什么。责任与版权边界一并在末尾声明。

## 1. 仓库边界

| 内容 | 位置 | 入库 |
|---|---|---|
| 补丁源码与上位机 | `tools/`、`hand/` | 是 |
| 文档 | `README.md`、`docs/` | 是 |
| 原厂固件镜像 | `firmware/vendor/` | **否**，版权归 FNIRSI |
| 官方 USB 库 | `tools/build/at32lib/` | 否，`fetch.py` 获取 |
| 构建产物 | `firmware/*.bin` | 否，作为 Release 附件分发 |
| 原厂说明书等第三方资料 | `docs/reference/` | 否，版权归各自权利人 |
| Ghidra 工程 | `ghidra_proj/` | 否，可由镜像重建 |

## 2. 依赖获取

| 依赖 | 来源 |
|---|---|
| AT32F435_437 官方 USB 库 | GitHub `ArteryTek/AT32F435_437_Firmware_Library`，`python tools/build/at32lib/fetch.py` |
| LLVM（clang、ld.lld、llvm-nm、llvm-objcopy、llvm-size） | 需支持 `arm-none-eabi` 目标 |
| Python 3.10+ / `capstone` / `pyserial` / `Pillow` | python.org 与 PyPI |
| SarasaTermSC-Regular | Sarasa Gothic 项目；放 `SARASA_FONT` 指定的路径或下载目录 |

`fetch.py` 经 GitHub contents 接口逐文件下载并校验最小体积，防止截断文件充当
有效输入。

## 3. 原厂镜像的获取与核验

构建输入固定为 `firmware/vendor/APP_DSO153_V1.6_20240427.bin`，191232 字节，
对应 Flash `0x08007000..0x08035AFF`。获取途径：厂商官方固件包，或从自己持有的
设备提取。注意 IAP 模式的 `AT32 Msc iap` 磁盘是引导程序构造的虚拟 FAT，读不出
APP 分区，不能当提取途径。

拿到文件后核验三件事（伪造或截断的镜像会导致构建出废片）：

1. 长度恰为 191232 字节；
2. 首 4 字节是初始 SP `0x2000B580`；
3. 随后 4 字节是复位向量 `0x08007365`（Thumb 位已置）。

## 4. 发布步骤

1. 确认改动完成，当前源码树能重建出行为一致的产物；
2. 递增修订号构建（功能变化才递增；同一源码换 `PHOS_VER` 重建不算新修订，单独
   注明即可）：

   ```bash
   PHOS_VER=V1.6S<N> bash tools/build.sh R<N>
   ```

3. 确认构建末行输出"静态检查全部通过"；
4. 记录尺寸与哈希：

   ```bash
   sha256sum firmware/PHOS_V1.6R<N>.bin
   stat -c %s firmware/PHOS_V1.6R<N>.bin
   ```

5. 走完第 5 节检查清单；
6. 创建 GitHub Release：标签与修订号一致（如 `v1.6R36`），上传镜像为附件，Release
   说明中写明 SHA-256、尺寸、本版变更与已知问题（版本历史记录在 Release 说明里，
   不设独立文档）；
7. 提交文档更新，核对四份主文档（README / USAGE / DEVELOPMENT / ANALYSIS）中的
   当前版本号已同步。

## 5. 产物检查清单

| 检查项 | 方法 |
|---|---|
| 镜像长度 = 191232 + 补丁镜像长度 | `stat -c %s` |
| 补丁区未越界 | 构建阶段 4 |
| SRAM 段布局合法 | 构建阶段 3 |
| 静态自检通过（项数以构建输出为准） | 构建阶段 6 |
| 主题与字形已套用 | 构建阶段 7 输出，或与基准镜像逐字节比对 |
| 版本串正确 | 实机"设置 → 关于" |
| 数据通路可用 | `phosphor_cli.py --diag` |
| 断电重启后枚举正常 | 实机操作 |
| IAP 恢复可行 | 保留原厂镜像副本，演练一次刷回 |

## 6. 责任与版权

本项目修改设备固件。刷写后行为异常可能令设备无法正常使用；恢复手段是 IAP 模式
刷回原厂镜像，因此发布物应始终提醒用户保留原厂镜像副本。刷写风险由操作者承担。

原厂固件、说明书、官方库与第三方应用笔记版权归各自权利人。本仓库只提供与这些
内容交互的工具与文档，不分发上述文件，也不提供其下载渠道。

本项目代码采用 MIT 许可证（见仓库根 [LICENSE](../LICENSE)）。

## 相关文档

- [DEVELOPMENT.md](DEVELOPMENT.md)（构建与自检的细节）
- [FLASHING.md](FLASHING.md)（产物如何刷写与恢复）
- [../README.md](../README.md)（仓库入口）
