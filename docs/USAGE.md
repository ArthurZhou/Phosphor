# 使用手册

本手册面向设备操作者，覆盖从准备工作到日常采集的完整流程：先判断自己是否适用，
再装依赖、刷写、连上设备，最后是各种采集与排障手段。刷写的逐步操作单独写在
[FLASHING.md](FLASHING.md)，上位机每个工具的参数细节写在 [HOST.md](HOST.md)，
协议本身见 [PROTOCOL.md](PROTOCOL.md)。

适用条件：FNIRSI DSO-153，原厂 APP v1.6 基线（版本号形如 `V1.6`），当前固件修订
R36。其他基线版本未经测试。

## 1. 准备

### 1.1 硬件

| 项目 | 要求 |
|---|---|
| 设备 | FNIRSI DSO-153 |
| 数据线 | 带数据芯线的 USB Type-C 线；纯充电线无法枚举 |
| 主机 | Windows 10 及以上（Linux 依赖 CDC-ACM 与大容量存储的标准行为，未逐项实测，TODO(需验证)） |
| 电量 | 50% 以上；刷写期间绝不能断电 |

### 1.2 软件

| 组件 | 用途 | 安装 |
|---|---|---|
| Python 3.10+ | 全部脚本 | python.org |
| `pyserial` | 串口通信 | `python -m pip install pyserial` |
| `capstone` | 仅构建时静态自检用 | `python -m pip install capstone` |
| Pillow | 可选：抓屏输出 PNG | 缺失时截图退回 BMP |
| tkinter | 可选：图形界面 | Windows 官方安装包默认带 |
| Sarasa Term SC 字体 | 可选：构建时的中文字形替换 | 缺失时只影响字形，不影响功能 |

若只需要刷写官方发布的产物、不做构建，`capstone` 等构建依赖可以不装。

## 2. 获取固件产物

两条路：

1. **直接下载**：从本项目 Release 页取 `PHOS_V1.6R36.bin`（209924 字节），核对
   页面给出的 SHA-256；
2. **自己构建**：见 [DEVELOPMENT.md](DEVELOPMENT.md) 第 5 节，产物位于
   `firmware/PHOS_V1.6R<N>.bin`。

无论哪条路，产物必须是通过了静态自检的版本（自建时构建脚本会强制自检，失败不出
片）。

## 3. 刷写

核心流程一句话：设备**完全关机**后，按住 `AUTO`，再按住电源键，插入 Type-C，主机
出现名为 `AT32 Msc iap` 的磁盘，把固件复制进去，设备写完自动重启。

关键注意：

- 只向卷标为 `AT32 Msc iap` 的磁盘写入，这个磁盘不放别的文件；
- 写入期间（约 10–30 秒）保持按键、不断电、不拔线；
- 先留好原厂镜像副本，随时可刷回恢复。

逐步操作、每一步的预期结果、失败对照表和恢复流程，全部在
[FLASHING.md](FLASHING.md)。

## 4. 首次连接

刷写完成并重启后：

```bash
python tools/host/usb_devices.py          # 应出现 0x2E3C:0x5740 的 USB 串行设备
python tools/host/phosphor_cli.py --list  # 标记目标设备的串口
```

确认版本：设备开机进入"设置 → 关于"，应显示本次构建的版本串（R36 对应
`V1.6SI`）。文件名里的修订号与显示串不一一对应是正常的，对应关系见
[RELEASING.md](RELEASING.md)。

跑通第一条数据：

```bash
python tools/host/phosphor_cli.py -p COM13 -n 1
```

预期输出一帧序号、4096 个样本和统计值。若校验一栏显示"参照值"，属于正常现象
（原因见 [PROTOCOL.md](PROTOCOL.md) 第 6 节），不是传输错误。

Windows 记住了旧驱动名称（设备曾用名 `AT32 Virtual Com Port`）时，串口名可能与
描述符不一致：在设备管理器卸载该设备后重新插拔即可。想固定 COM 口号，在端口
属性 → 端口设置 → 高级里指定。

## 5. 采集

### 5.1 图形界面

```bash
tools/host/phosphor_host.bat                 # Windows 推荐，自动挑带 tkinter 的解释器
python tools/host/phosphor_host.py --port COM13
```

操作顺序：刷新端口 → 连接 → 选采集模式（屏幕 / 单帧 / 连续）→ 采集 → 停止。
"导出 CSV"保存当前帧。

界面上方是档位直选：时基（24 档）、垂直（13 档，10mV–100V）、探头倍率，可用
`◀` `▶` 微调；右侧显示设备回读的当前值。改档走固件命令 `0x0D`，主机不碰设备
内存，设备侧完成换档、重绘与保存。探头倍率务必与探头手柄的 1X/10X 开关一致，
否则电压读数差十倍。

按键栏放常用键（AUTO、MODE、RUN/停止、上、下），其余键（拨轮、确定、返回、详细
信息、触发边沿、自动校正、关机）收在"更多按键"里。所有按键都通过命令 `0x06`
注入，与手按等效。关机（键码 1）与自动校正（键码 13）有二次确认——自动校正会
改写设备内部校准值，校正前应按说明书断开或短接探头。

### 5.2 命令行

```bash
python tools/host/phosphor_cli.py --list                       # 列串口
python tools/host/phosphor_cli.py -p COM13 -n 3                # 原始采样 3 帧
python tools/host/phosphor_cli.py -p COM13 --screen            # 屏幕波形
python tools/host/phosphor_cli.py -p COM13 --stream -n 20 --csv wave.csv
python tools/host/phosphor_cli.py -p COM13 --key 6             # 注入 AUTO 键
python tools/host/phosphor_cli.py --simulate -n 2              # 无硬件仿真
```

退出码：`0` 成功，`1` 没收到数据，`2` 参数或环境错误。全部参数见 [HOST.md](HOST.md)。

### 5.3 三种数据怎么选

| 数据 | 命令 | 内容 | 适用 |
|---|---|---|---|
| 屏幕波形 | `0x03` 或源 1 | 301 点，与屏幕显示逐点一致 | 稳定观察、与屏比对 |
| 原始采样 | 源 0 默认 | 4096 点 ADC 码值，发送期间仍在被新样本覆盖 | 高分辨率分析、采样率校准 |
| 屏幕画面 | 源 2 | 推屏时逐块送出的 RGB565 块 | 抓图、远程看屏 |

抓屏：

```bash
python tools/host/phosphor_capture.py --src image -o shot.png
python tools/host/phosphor_capture.py --src image -o shot.png --key 5   # 先按一次 MODE 再抓
```

画面没有整屏缓冲可用（SRAM 装不下），设备在每次刷新屏幕时把那一块直接发出来，
主机拼成 320×240。抓图时固件会按当前页面主动请求一次完整重绘，所以一次抓图就是
当前页面的完整画面；换页后再抓就是新页面。带宽限制下整页要几秒，期间设备屏幕会
逐块重画，属正常现象。工具会报告收到的区域与空行；一块都没收到且页面号为
1/5/6/7 时，那是原厂不重建的过渡页面，换一页再抓即可。

### 5.4 测量换算

- 电压：固件每格 25 个码值，mV/格按 1-2-5 序列；`电压 = (码值 − 零点) ×
  (mV/格 ÷ 25)`，零点默认 128，可用"校准零点"（探头短接采一帧取均值）实测；
- 时间/频率：固件不上报采样率，用设备自带的 1kHz 校准方波在**单帧模式**下实测
  一次，主机侧记住该值；
- 档位随设备回读自动更新，V/div 与 S/div 不需要手工维护，当前值保存在
  `tools/host/phosphor_gear.json`。

## 6. 诊断

```bash
python tools/host/phosphor_cli.py -p COM13 --diag    # 栈峰值、水印状态、端点错误计数
python tools/host/phosphor_cli.py -p COM13 --fault   # 故障现场（寄存器级）
python tools/host/phosphor_diag.py                   # 复位原因 + 故障摘要（只读）
python tools/host/phosphor_diag.py --probe 0x2000A568:32   # 探测某地址是否可读
python tools/host/phosphor_bench.py -p COM13 --seconds 10  # 吞吐测量
```

判读要点：

- 水印未铺设（`painted=0`）时栈数据不可信；余量低于 512 字节工具会告警；
- 故障现场里 PC 落在 `0x08035B00` 及以上 → 补丁代码；落在
  `0x08007000..0x08035AFF` → 原厂 APP 或传参问题；
- 故障记录跨软件复位保留，但断电即清，崩溃后应尽快读；
- 同一次上电内连续 3 次故障会自动复位，第 4 次设备停住不再复位。

## 7. 故障排查

| 现象 | 可能原因 | 处理 |
|---|---|---|
| 串口列表为空 | 充电线 / 未枚举 / 驱动问题 | 换数据线；查设备管理器；必要时卸载重插 |
| 连接被拒绝 | 串口被别的程序占用 | 关掉串口监视工具 |
| 采集无数据 | 未连接、超时太短 | 先 `--list` 确认；加大 `--timeout` |
| 校验显示"参照值" | 原始帧的正常标志 | 不用处理，见协议第 6 节 |
| 帧序号跳号 | 主机读得不及时 | 降低采集频率、减少并发 |
| 图形界面起不来 | 缺 tkinter | 用 `phosphor_host.bat` 或装完整 Python |
| 连按几下只生效一下 | 键码注入间隔太密 | 相邻键码间隔 ≥240ms，工具已内置 |
| 改了档位波形没变 | 固件低于 R23 不支持 `0x0D` | 升级固件 |
| 写高地址 RAM 后掉线 | 超出实测上界 | 只访问 `0x20000000..0x2000BFFF` |
| 设备根本没有串口 | 还在跑原厂固件 | 原厂固件无此通道，先刷 Phosphor |
| 刷写后无显示 / 起不来 | 产物损坏或中断电 | 重新进 IAP 刷回原厂镜像，见 FLASHING.md 第 6 节 |

## 8. 安全注意

- 刷写改写 Flash：失败可能导致设备无法启动，恢复手段是 IAP 模式刷回原厂镜像，
  所以原厂镜像副本必须保留；
- 键码 13（自动校正）会覆盖设备校准数据，操作前按说明书处理探头；
- 键码 1（关机）立即断电，远程操作时留意；
- 协议的内存读取只放行实测安全的区域，但自写脚本时不要向设备发送越界地址——
  越界访问会触发复位；
- 刷写风险由操作者承担。

## 相关文档

- [FLASHING.md](FLASHING.md)（刷写与恢复逐步操作）
- [HOST.md](HOST.md)（上位机参数与输出判读）
- [PROTOCOL.md](PROTOCOL.md)（协议细节）
- [DEVELOPMENT.md](DEVELOPMENT.md)（自行构建）
- [../README.md](../README.md)（仓库入口）
