# USB 协议栈实现

本文说明数据通道在固件侧如何搭起来：协议栈从哪来、怎么初始化、USB 时钟怎么选、
端点怎么分、数据怎么调度。对外的协议行为见 [PROTOCOL.md](PROTOCOL.md)。

## 1. 组成：官方库 + 补丁胶水

协议栈主体是 Artery AT32F435_437 官方 USB device 库，由
`tools/build/at32lib/fetch.py` 获取到 `tools/build/at32lib/`（不入库），
源文件不改写：

| 库文件 | 职责 |
|---|---|
| `usb_core.c` | OTG 控制器底层、FIFO 分配 |
| `usbd_core.c` | 设备状态机、端点管理、标准请求 |
| `usbd_int.c` | 中断分发 |
| `usbd_sdr.c` | 描述符发送 |
| `cdc_class.c` / `cdc_desc.c` | CDC 类与描述符表 |
| `at32f435_437_usb.c` 等 | 外设寄存器与时钟/GPIO/NVIC/ACC |

时钟树、GPIO 复用、FIFO 划分、SETUP 解析、地址分配、TRDT/GCCFG 全部交给官方库，
补丁不做寄存器级干预。

补丁侧的 USB 相关代码：

| 文件 | 职责 |
|---|---|
| `tools/fw/hook.c` | 主循环入口：冷启动 → `usb_poll()` → 原渲染任务 → 绘制修饰 |
| `tools/fw/usb/phosphor_usb.c` | 初始化、时钟选择、组帧发送、命令解析、故障捕获 |
| `tools/fw/usb/usb_conf.h` | OTG1 分支、FIFO 尺寸、端点表规模 |
| `tools/fw/usb/at32f435_437_conf.h` | 外设配置裁剪 |
| `tools/fw/usb/stdio.h` | 空壳头，供官方头文件包含 |

### 1.1 官方库的局部修改

`tools/patch_lib.py` 用幂等字符串替换施加，每次构建前重跑：

| 项 | 原值 | 改后 |
|---|---|---|
| 厂商字符串 | `Artery` | `Phosphor` |
| 产品字符串 | `AT32 Virtual Com Port` | `Phosphor DSO-153` |
| 配置/接口字符串 | `CDC` | `Phosphor CDC Config` / `Phosphor CDC Data` |
| 线路编码波特率 | 115200 | 921600 |
| VID:PID | `2E3C:5740` | 不变 |

## 2. 初始化序列

`usb_poll()` 每轮主循环被调用，首次调用时执行 `usb_bringup()`：

1. 复制官方 `cdc_class_handler` 结构，保存原 IN/OUT 回调后替换为补丁实现；
2. 配置 PA11=DM、PA12=DP（复用功能 10，推挽，无上下拉）；
3. 使能 OTG1 外设时钟；
4. 选择 48MHz USB 时钟（第 3 节）；
5. 使能 OTGFS1 中断（优先级 2）；
6. 复位发送状态机（`tx_active/tx_sent/stream_on/stream_tick` 清零）——**不可省
   略**：热复位后 RAM 保留上次的半帧状态，不复位会卡死首帧；
7. `usbd_init()` 完成控制器与端点初始化，置 `g_usb_state = 1`。

补丁不经过芯片复位流程。`usb_poll()` 入口先跑 `phosphor_cold_start()`（搬运
`.data`、清 `.bss` 与故障记录、铺栈水印，双字魔数判定只跑一次）。

后续每轮 `usb_poll()`：控制器有效性检查（`g_usb_state` 非零且 DWC2 的 `GINTMSK`
非零，无效则重新 bringup——覆盖冷启动与 IAP 跳转重入两类情况）→ 更新连接状态
→ 若有待注入键码则入队 → 扫描栈水印。

## 3. USB 时钟：为什么用 HICK

全速 USB 要求 48MHz ±0.25%。本机 PLL 输出 300MHz（8MHz × 75 ÷ 2），USB 分频器
输入取 PLL 输出：

| 分频 | 结果 | 偏差 |
|---|---|---|
| /6 | 50.00MHz | +4.17% |
| /6.5 | 46.15MHz | −3.85% |

都不合格。因此走内部 HICK + ACC（自动时钟校准）路线：

1. `crm_usb_clock_source_select(CRM_USB_CLOCK_SOURCE_HICK)`；
2. 使能 ACC 时钟，写修剪窗口 `acc_write_c1(7980)` / `c2(8000)` / `c3(8020)`；
3. `acc_sof_select(ACC_SOF_OTG1)`，以主机发来的 SOF 帧为校准基准；
4. `acc_calibration_mode_enable(ACC_CAL_HICKTRIM, TRUE)`。

代码的判定规则是通用的：若 SYSCLK 来自 PLL、PLL 频率在 48–400MHz、且最接近的
0.5 步进分频能把 USB 时钟带进 47.5–48.5MHz，就用 PLL 分频（例如 PLL=288MHz 时
/6=48MHz 自动切回），否则用 HICK。

实现细节：`crm_usb_div_type` 的编码不是分频值——整数分频 `d` 编码 `2d−1`，
半档 `d+0.5` 编码 `2d−2`；计算按 0.5 步进、全程 32 位整数运算（`8MHz × pllns
≤ 1G` 不会溢出），避免引入 64 位除法辅助函数。

## 4. 端点与 FIFO

| 端点 | 方向 | 类型 | 包长 | FIFO |
|---|---|---|---|---|
| EP0 | 双向 | 控制 | 64B | 24 字 |
| EP1 | IN | 批量 | 64B | 20 字 |
| EP1 | OUT | 批量 | 64B | 共享 RX 128 字 |
| EP2 | IN | 中断 | 64B | 20 字 |

端点表 8 项（官方默认 `USB_EPT_MAX_NUM = 8`）。注意 `usbd_core.c` 的 FIFO 分配
有按该常量的分支，改端点数会连带改变 FIFO 划分。

## 5. 传输调度

### 5.1 回调替换

官方 CDC 回调只做标志置位，补丁复制结构体后替换 IN/OUT 两个回调：

- IN 回调：先调原实现，再 `tx_pump()` 推进当前帧；帧发完且连续模式开启时直接
  起下一帧——连续帧率因此不受主循环约束。
- OUT 回调：先调原实现，再 `usb_vcp_get_rxdata()` 取出命令字节交给
  `cmd_exec()`。

端点号比较要屏蔽方向位（`& 0x7F`）：官方库传入的端点号带方向编码，直接比较会
匹配失败。

### 5.2 发送状态机

变量：`tx_hdr[8]`、`tx_src`、`tx_n`、`tx_sent`、`tx_sum`、`tx_active`、`tx_seq`。

`tx_start()` 组帧头、对头与负载累加校验、**当场**提交首包（首包必须同步发出：
IN 完成中断只在传输完成后才有，等中断推首包就永远发不出）。`tx_pump()` 按
`tx_sent` 分段：

| 区间 | 内容 |
|---|---|
| `< 8` | 帧头剩余 |
| `8 .. 8+tx_n` | 负载，按 64B 分包 |
| `== 8+tx_n` | 校验字节 |
| 之后 | 清 `tx_active`，`g_usb_tx++` |

`tx_active` 置位时 `tx_start()` 直接返回：同一时刻只有一帧在途。两个调用点
（命令解析、连续续帧）都在 USB 中断上下文，无需关中断。

### 5.3 中断与 HardFault

OTGFS1 中断号 67，向量表项 `0x0800714C`（表基址 `0x08007000`）补丁改指
`usb_irq()`：计数后转发 `usbd_irq_handler()`。该函数无 C 引用，靠
`KEEP(*(.text.usb_irq))` 保留，向量值必须置 Thumb 位。

HardFault 向量 `0x0800700C` 改指 `fault_stub`：`naked` 函数，不用栈，按
`EXC_RETURN` bit2 选 MSP/PSP 栈帧，记录 CFSR/HFSR/MMFAR/BFAR 与 R0–R3/R12/LR/
PC/xPSR 到 `0x2000BF00`，摘要到 `0x2000BF60`，计数 ≤3 时写 `SYSRESETREQ` 软复位，
否则原地停住。原厂默认向量是 `b .`（死机不打招呼），这是补上诊断能力的关键。

## 6. 资源占用（R36）

| 项目 | 值 |
|---|---|
| 补丁镜像 | 18692 字节（`0x08035B00` 起） |
| Flash 余量 | 约 23.5KB（补丁区共 42240 字节，上界 `0x08040000`） |
| `.data` + `.bss` | 约 1.7KB（`0x2000B800` 起） |
| `.fault_rec` | 256 字节（`0x2000BF00`，`.bss` 之外） |
| 补丁 SRAM 总占用 | 2KB，全部位于初始 SP `0x2000B580` 之上 |

原厂 4KB 栈保持完整；补丁变量区借用的是原厂链接器声明范围之外的高段 SRAM。

## 7. 限制

- 官方库是"单包在途"模型：`usb_vcp_send_data()` 以 `g_tx_completed` 为闸门，
  每包完成即清 TX FIFO，无法多包排队——加大 EP1 FIFO 不提升吞吐；
- 全速批量端点包长上限 64B，4096 点单帧拆 65 包；
- HICK 初始精度约 ±1%，枚举依赖 ACC 按 SOF 校准完成；
- 未实现 USB 挂起/恢复；
- 设备不感知主机断开重连（主机重开串口无需重新枚举）；
- 控制器有效性判定依赖 `GINTMSK`：若外设时钟被外部关掉而该位残留非零，判定
  失效；
- 发送失败只累加 `g_usb_err`，不重试当前包。

## 相关文档

- [PROTOCOL.md](PROTOCOL.md)（协议行为）
- [DEVELOPMENT.md](DEVELOPMENT.md)（构建、补丁机制、不变量）
- [ANALYSIS.md](ANALYSIS.md)（地址与时钟结论的证据）
- [../README.md](../README.md)（仓库入口）
