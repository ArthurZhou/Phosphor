# 上位机参考

上位机代码都在 `tools/host/`，负责串口连接、命令收发、帧解析、波形呈现与参数
换算。图形界面和命令行共用同一个协议实现模块，`tools/host/phosphor_proto.py`。
操作流程层面的内容见 [USAGE.md](USAGE.md)，本页是各工具的参数与输出判读手册。

## 1. 工具一览

| 文件 | 用途 |
|---|---|
| `phosphor_proto.py` | 协议常量、端口枚举、帧解析器、诊断/故障解码、仿真设备 |
| `phosphor_host.py` / `phosphor_host.bat` | 图形界面（tkinter）；`.bat` 自动挑选带 tkinter 的解释器 |
| `phosphor_cli.py` | 命令行采集、遥控、诊断、CSV 导出 |
| `phosphor_capture.py` | 三源抓取：原始采样 / 屏幕波形 / 屏幕画面（PNG） |
| `phosphor_diag.py` | 只读诊断：复位原因、故障现场、栈、地址探测 |
| `phosphor_bench.py` | 连续模式吞吐与单帧延迟 |
| `phosphor_steps.py` | 档位直选实现库（GUI 调用） |
| `phosphor_direct.py` | 调试：手动调 `0x0D` / `0x0E` / `0x0F` |
| `phosphor_vtest.py` | 实测验证档位切换确实改变了前端增益 |
| `check_iap.py` | 刷写前只读确认 IAP 卷 |
| `usb_devices.py` | USB 设备与错误码快照，支持插拔监视 |
| `build_exe.sh` / `build_exe.bat` | PyInstaller 打包 |
| `phosphor_scan.py`、`phosphor_probe.py`、`phosphor_sramdiff.py`、`phosphor_keys.py`、`phosphor_live.py`、`phosphor_writeonly.py`、`phosphor_calib.py` | 逆向标定工具（见 [ANALYSIS.md](ANALYSIS.md) 第 12 节；`phosphor_calib.py` 基于已被推翻的假设，仅存档） |

运行环境：Python 3.10+，`pyserial`；PNG 输出与字形渲染需要 Pillow；GUI 需要
tkinter。`--simulate` 参数在多数工具上可用，内置仿真设备按协议生成正弦叠加
数据，不需要硬件。

## 2. 图形界面

```bash
python tools/host/phosphor_host.py [--port COM13] [--simulate] [--autostart]
```

| 参数 | 说明 |
|---|---|
| `-p`, `--port` | 指定串口；缺省自动找 `2E3C:5740` 设备 |
| `--simulate` | 仿真设备，无硬件演示与回归 |
| `--autostart` | 启动后立即开始连续采集 |

| 区域 | 内容 |
|---|---|
| 工具栏 | 端口、刷新、连接/断开、模式、采集、停止、屏幕波形、CSV、触发对齐、自动缩放 |
| 档位面板 | 时基 / 垂直 / 探头下拉直选、微调、回读显示 |
| 按键栏 | 常用键 + 可折叠的完整键区 |
| 波形区 | 横轴样本序号，纵轴 8 位码值；按列画最小/最大值，抽点不丢峰值 |
| 信息面板 | 帧信息、幅度统计、会话统计、设置结构快照 |
| 状态栏 | 连接状态与操作反馈 |

采集模式：屏幕（301 点，约 120ms 周期重复请求，默认）、单帧（4096 点）、连续
（4096 点直到停止）。

档位直选的实现要点：主机发 `0x0D`（角色 + 目标索引）后立即返回——固件在 USB
轮询里写字段、调用原厂前端配置函数、推 UI 事件，设备侧毫秒级生效，主机不做
轮询确认，面板先按目标值乐观更新，后台回读仅作纠偏（只查 5 秒内的命令）。
回读（`0x08`）由采集线程分发：读方向的全部数据走同一条解析流，应答按帧长度
分发给等待者，采集不再为命令交互停顿。注意实机确认：连续流期间固件不发出
`0x08` 应答（憋到停流），因此流模式下回读超时是常态，靠乐观更新保证界面。

波形区"触发对齐"裁掉触发点之前的头部，不做循环拼接；"自动缩放"按当前帧极值
加 12% 余量，关闭后固定 0–255。

## 3. 命令行 phosphor_cli.py

```bash
python tools/host/phosphor_cli.py --list
python tools/host/phosphor_cli.py -p COM13 -n 3
python tools/host/phosphor_cli.py -p COM13 --screen
python tools/host/phosphor_cli.py -p COM13 --stream -n 20 --csv wave.csv
python tools/host/phosphor_cli.py -p COM13 --diag
python tools/host/phosphor_cli.py -p COM13 --fault
python tools/host/phosphor_cli.py -p COM13 --key 6 --repeat 5
python tools/host/phosphor_cli.py --simulate -n 2
```

| 参数 | 说明 |
|---|---|
| `-p`, `--port` | 串口名，缺省自动查找 |
| `-n`, `--frames` | 帧数，默认 1 |
| `--screen` | 采 301 点屏幕波形 |
| `--stream` | 连续模式 |
| `--csv PATH` | 写 CSV |
| `--diag` / `--fault` | 读诊断 / 故障现场 |
| `--key N` / `--repeat N` | 注入键码 1–14 / 重复 1–10 次 |
| `--keys` | 列出键码含义 |
| `--settings` | 读 `0x2000A528` 起 64 字节窗口（UI 状态区，兼容用途） |
| `--strict` | 丢弃校验不符的帧（默认不丢 CHK_ADV 帧） |
| `--no-plot` | 不打字符波形 |
| `--timeout` | 单帧等待秒数，默认 3 |
| `--list` | 列串口并标记目标设备 |

退出码：`0` 成功，`1` 未收到数据，`2` 参数或环境错误。单帧模式逐帧下发 `0x01`；
连续模式只发一次 `0x02`，结束时发 `0x00`。

## 4. 抓屏 phosphor_capture.py

```bash
python tools/host/phosphor_capture.py --src image  -o shot.png [--key N]
python tools/host/phosphor_capture.py --src raw    -o wave.csv -n 3
python tools/host/phosphor_capture.py --src screen -o screen.csv
```

| 参数 | 说明 |
|---|---|
| `--src` | `raw` 原始采样 / `screen` 屏幕波形 / `image` 屏幕画面 |
| `-o` | 输出文件（画面可 PNG 或 BMP，取决于 Pillow 是否可用） |
| `--key N` | 抓图前先注入一个键码（用于换页） |
| `--bands` | 画面块预算，默认 64（主界面约 40 块，菜单页 3–6 块） |

`--src image` 的行为：设备按当前 UI 页面号自动请求一次完整重绘，推屏的每一块
直接发出，主机贴到 320×240 画布。输出会汇总打印收到的区域（如
`区域 x=0 y=0 320x32（6 块）`）、整行覆盖/空行/半行统计、波形带与其他块的块数
比例（只有波形带说明页面重绘没生效，固件低于 R35）。一块都没收到时按页面号
解释：页面 1/5/6/7 是原厂不重建的过渡态，换页再抓。

发送期间设备只受理停止命令 `0x00`，这是刻意设计：端点被画面占用时再塞别的命令
会让整帧卡死。

## 5. 诊断 phosphor_diag.py

```bash
python tools/host/phosphor_diag.py              # 复位原因 + 故障现场 + 摘要
python tools/host/phosphor_diag.py --stack      # 栈水印判读
python tools/host/phosphor_diag.py --ram        # 分配器基址、UI 窗口结构（只读）
python tools/host/phosphor_diag.py --probe ADDR[:LEN]
python tools/host/phosphor_diag.py --raw        # 附加打印 56 字节原始故障负载
```

排错数据在设备侧分三块存放，读取都用命令 `0x05` / `0x08`：

| 地址 | 内容 | 跨软件复位 |
|---|---|---|
| `0x2000BF00` | HardFault 完整现场（56 字节） | 否（下次冷启动清除） |
| `0x2000BF40` | 复位原因（CRM 标志快照） | 是 |
| `0x2000BF60` | 故障摘要 `FLT2`：CFSR/HFSR/BFAR/PC/LR | 是 |

复位原因判读：

| 读数 | 含义 |
|---|---|
| SW 软件复位 + 有 `FLT2` 摘要 | 补丁故障桩处理过的真 HardFault |
| SW 软件复位 + 无摘要 | 复位来自别处（原厂代码软复位，或故障桩没跑到） |
| WDT / WWDT | 看门狗（本机型原厂未启用，出现即异常） |
| POR | 掉电复位，RAM 全清，故障现场必为空 |
| 摘要中 CFSR bit15 置位 | BFAR 字段是有效的出错地址 |

`--probe` 的语义：读取成功说明地址已映射；设备随即掉线复位说明未映射——探测
请逐地址进行，复位后先读故障现场再继续。

## 6. 吞吐 phosphor_bench.py

```bash
python tools/host/phosphor_bench.py --list
python tools/host/phosphor_bench.py -p COM13 --seconds 10
python tools/host/phosphor_bench.py --simulate
```

`--seconds` 连续模式测量时长（默认 5），`--rounds` 单帧延迟测量次数（默认 5）。

## 7. 数据格式

CSV（命令行）：

```
frame,index,code
0,0,128
0,1,131
```

`frame` 为会话内帧序，`index` 为帧内样本序号，`code` 为 8 位 ADC 码值。GUI 导出
只有当前帧，列为 `index,code`。

## 8. 二次开发接口

`phosphor_proto.py` 可直接 import：

```python
import phosphor_proto as proto

port = proto.find_port()
ser = proto.open_port(port)
ser.write(bytes([proto.CMD_SINGLE]))

parser = proto.FrameParser()          # 可长期复用，reset() 清缓冲
for frame in parser.feed(ser.read(8192)):
    stats = proto.SignalStats.from_samples(frame.samples)
    print(frame.seq, stats.peak_to_peak)

info = proto.decode_diag(frame.samples)   # 栈区间/峰值/余量
info = proto.decode_fault(frame.samples)  # CFSR/HFSR/PC/LR/寄存器
proto.fault_explain(info)                 # 故障状态寄存器逐位解读
```

关键常量：`VID = 0x2E3C`、`PID = 0x5740`、`DEFAULT_BAUD = 921600`、
`FRAME_SAMPLES = 4096`、`SCREEN_SAMPLES = 301`、`CODES_PER_DIV = 25`。
帧组帧函数 `encode_frame()` 供测试与仿真使用。

串口速率说明：USB CDC 是虚拟串口，主机侧波特率（默认 921600）不参与传输控制，
实际速率由全速批量端点决定（64 字节包，单包在途）。

## 9. 已知限制

- 协议不上报时基/垂直档位/采样率，换算依赖主机侧记录与 1kHz 方波校准；
- 原始帧校验是发送起始快照，不能用于完整性判定；
- 档位直选需要 R23 以上固件；数据源切换需要 R35 以上固件；
- 屏幕画面受带宽限制，整页秒级；
- 设备不感知主机断开重连，主机重开串口无需重新枚举。

## 相关文档

- [USAGE.md](USAGE.md)（操作流程）
- [PROTOCOL.md](PROTOCOL.md)（协议规范）
- [ANALYSIS.md](ANALYSIS.md)（标定工具的方法与局限）
- [../README.md](../README.md)（仓库入口）
