# Phosphor

Phosphor是为FNIRSI DSO-153示波器开发的第三方固件，在不修改原有功能和硬件的情况下添加了上位机支持。用USB连接到主机，即可读回采样、波形、
屏幕画面等必要数据，并支持遥控按键与档位。本项目使用FNIRSI原厂固件修改。

## 能力

- 三种数据源（命令 `0x10` 切换）：4096 点原始采样、301 点屏幕波形、屏幕画面
- 按键遥控（命令 `0x06`）：注入键码，等同于按下 AUTO、MODE、方向键等
- 档位直选（命令 `0x0D`）：时基、垂直档位、探头倍率直接设定，复用原厂的前端
  配置链路，主机回读确认
- 运行期诊断：栈峰值水印、HardFault 现场记录（跨软件复位保留）、复位原因
- 图形界面与命令行工具共用同一套协议代码，均内置仿真设备，无硬件也能跑通
- 界面定制（调色板、字形、关于页）
- 构建末尾自动执行静态自检（补丁入口、向量、段布局、绝对地址）

## 快速开始

```bash
# 1. 拉取官方 USB 库（构建依赖，不入库）
python tools/build/at32lib/fetch.py

# 2. 把原厂镜像放到 firmware/vendor/APP_DSO153_V1.6_20240427.bin
#    （自行从持有的设备或厂商固件包获取，版权归 FNIRSI，仓库不提供）

# 3. 构建（需要 LLVM 工具链与 Python 3.10+）
PHOS_VER=V1.6SI bash tools/build.sh R36

# 4. 刷写：关机 → 按住 AUTO → 加按电源 → 插 Type-C，
#    把产物复制到出现的 "AT32 Msc iap" 磁盘

# 5. 采集
python -m pip install pyserial
python tools/host/phosphor_cli.py --list
python tools/host/phosphor_cli.py -p COM13 -n 3
python tools/host/phosphor_capture.py --src image -o shot.png
```

只想先看看界面和数据长什么样，不需要设备：

```bash
python tools/host/phosphor_host.py --simulate --autostart
```

## 刷写与恢复

刷写会改写设备 Flash，操作前请确认：

- 产物通过了构建末尾的静态自检（自检失败时构建返回非零退出码）；
- 已单独保存一份原厂镜像 `APP_DSO153_V1.6_20240427.bin`，任何时候都可经IAP
  模式刷回恢复；
- 刷写过程中不得断电、拔线

刷写风险由操作者自行承担。

## 文档

| 想做点什么？ | 从这里开始 |
|---|---|
| 刷机并使用上位机 | [docs/USAGE.md](docs/USAGE.md)（刷写细节见 [docs/FLASHING.md](docs/FLASHING.md)，上位机参数见 [docs/HOST.md](docs/HOST.md)） |
| 改代码、加命令、发版本 | [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)（协议见 [docs/PROTOCOL.md](docs/PROTOCOL.md)，USB 实现见 [docs/USB_STACK.md](docs/USB_STACK.md)，界面改写见 [docs/UI_PATCH.md](docs/UI_PATCH.md)，发布流程见 [docs/RELEASING.md](docs/RELEASING.md)） |
| 了解固件怎么被逆向 | [docs/ANALYSIS.md](docs/ANALYSIS.md) |
| 找某篇文档 | [docs/README.md](docs/README.md) |

## 许可证与版权

本项目代码采用MIT许可

原厂固件镜像、原厂说明书、雅特力官方库与第三方应用笔记的版权归各自权利人所有，
本仓库不分发这些文件；仓库中对其仅做必要的技术性描述。
