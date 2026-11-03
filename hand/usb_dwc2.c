/* DSO-153 补丁：AT32F435 USB OTG FS（Synopsys DWC2）设备侧最小驱动
 *
 * v6：改成**完全轮询、不用中断**。
 *
 * 为什么要这么改：前几版一直依赖 USBFS 中断（IRQ67 / 向量表 / NVIC / GINTMSK），
 * 但设备到底有没有真的进中断，从外部完全看不出来 —— v4 能让主机弹出
 * "Device Descriptor Request Failed"、v5 连设备都不出现，两次都无法区分
 * 是"中断没进"还是"EP0 逻辑写错"。
 *
 * DWC2 的所有事件标志（USBRST / ENUMDNE / RXFLVL / XFRC）都是**粘滞**的，
 * 不清就一直挂着。所以完全可以在主循环里轮询把它们处理掉，代价只是响应慢几十毫秒，
 * 而 USB 主机对首次枚举的容忍度是秒级。这样一次性消掉整条中断链路的未知数：
 *   - 不再需要向量表第 67 项正确
 *   - 不再需要 NVIC 使能
 *   - 不再需要 GINTMSK / GAHBCFG 配置正确
 *
 * 另外加了"自愈"：如果一直没被配置成功，就周期性把 D+ 断开再连上，
 * 让 Windows 重新枚举一次，而不是只赌开机那一次。
 */
#include <stdint.h>
#include "usb_ctrl.h"
#include "usb_desc.h"
#include "board_dso153.h"

#define REG(a)  (*(volatile uint32_t *)(USBFS_BASE + (a)))

/* ---- 全局寄存器 ---- */
#define GAHBCFG   0x008
#define GUSBCFG   0x00C
#define GRSTCTL   0x010
#define GINTSTS   0x014
#define GINTMSK   0x018
#define GRXSTSP   0x020
#define GRXFSIZ   0x024
#define HNPTXFSIZ 0x028
#define DIEPTXF(n) (0x104 + 4 * ((n) - 1))   /* TX FIFO 分配，n>=1 */
#define GCCFG     0x038

/* ---- 设备寄存器 ---- */
#define DCFG      0x800
#define DSTS      0x808      /* [3:1] 枚举速度, bit0 挂起 */
#define DCTL      0x804
#define DIEPMSK   0x810
#define DOEPMSK   0x814
#define DAINT     0x818
#define DAINTMSK  0x81C

#define DIEPCTL(n)  (0x900 + 0x20 * (n))
#define DIEPINT(n)  (0x908 + 0x20 * (n))
#define DIEPTSIZ(n) (0x910 + 0x20 * (n))
#define DTXFSTS(n)  (0x918 + 0x20 * (n))   /* [15:0] 该 IN 端点 FIFO 还剩多少字 */
#define DOEPCTL(n)  (0xB00 + 0x20 * (n))
#define DOEPINT(n)  (0xB08 + 0x20 * (n))
#define DOEPTSIZ(n) (0xB10 + 0x20 * (n))
#define FIFO(n)     (0x1000 + 0x1000 * (n))

#define INT_RXFLVL   (1u << 4)
#define INT_USBRST   (1u << 12)
#define INT_ENUMDNE  (1u << 13)

#define EPCTL_EPENA  (1u << 31)
#define EPCTL_EPDIS  (1u << 30)
#define EPCTL_SD0PID (1u << 28)
#define EPCTL_CNAK   (1u << 26)
#define EPCTL_SNAK   (1u << 27)
#define EPCTL_STALL  (1u << 21)
#define EPCTL_EPTYP  (2u << 18)      /* bulk */
#define EPCTL_USBAEP (1u << 15)

#define EPINT_XFRC   (1u << 0)

#define PKTSTS_OUT_DATA   2
#define PKTSTS_SETUP_DATA 6

#define EP0_IDLE   0
#define EP0_DATA   1
#define EP0_WDATA  2
#define EP0_STATUS 3

/* EP0 的 MPS 字段是编码不是字节数：0=64 / 1=32 / 2=16 / 3=8 */
#define EP0_MPS_CODE 0u

/* 重连节拍：EOPF 在总线活动时每 1ms 一次，用它当毫秒基准；
 * 总线静止（主机挂起）时 EOPF 不来，用轮询次数兜底。 */
/* 轮换间隔：直接数"主循环跑了多少圈"。以前用 EOPF 当毫秒基准，但 EOPF 在
 * 一帧里只会被读到一次，实际是"帧数"而不是毫秒 —— 4000 帧就是一分多钟，
 * 屏幕上 A 一直显示 0 就是这个原因。现在改成 300 圈一轮，几十秒内一定能看到 A 在变。 */
#define RETRY_POLL 150u

static volatile uint8_t g_ready;
static volatile uint8_t g_busy;
static volatile uint8_t g_inited;
static volatile uint8_t ep0_phase;
static const uint8_t *ep0_ptr;
static uint16_t ep0_len;
static uint16_t ep0_sent;
volatile uint32_t g_diag[20];      /* 屏幕诊断叠加用，见 hook.c */
volatile uint32_t g_send_flags;    /* EP0 IN/OUT 传输路径诊断位 */

/* ---- 请求追踪：把最近 3 条 SETUP 打包记下来，屏幕上看枚举卡在哪一步 ----
 * 打包格式：低 8 位 bRequest，中 16 位 wValue，高 8 位我们回的字节数（0xFF=回 STALL） */
volatile uint32_t g_trace[3];
volatile uint32_t g_div, g_trdt, g_att, g_src;
static uint32_t s_polls;
static int g_attempt;
static uint8_t  g_tmp[80];

/* 写完寄存器后插一道内存屏障，确保顺序不被编译器重排 */
static inline void isb_dmb(void) { __asm volatile("dsb 0xF" ::: "memory"); }

/* ------------------------------------------------------------------ FIFO */

static void fifo_write(uint32_t ep, const uint8_t *p, uint32_t n)
{
    for (uint32_t i = 0; i < n; i += 4) {
        uint32_t w = (uint32_t)p[i];
        if (i + 1 < n) w |= (uint32_t)p[i + 1] << 8;
        if (i + 2 < n) w |= (uint32_t)p[i + 2] << 16;
        if (i + 3 < n) w |= (uint32_t)p[i + 3] << 24;
        REG(FIFO(ep)) = w;
    }
}

static void fifo_read(uint8_t *p, uint32_t n)
{
    for (uint32_t i = 0; i < n; i += 4) {
        uint32_t w = REG(FIFO(0));
        p[i] = (uint8_t)w;
        if (i + 1 < n) p[i + 1] = (uint8_t)(w >> 8);
        if (i + 2 < n) p[i + 2] = (uint8_t)(w >> 16);
        if (i + 3 < n) p[i + 3] = (uint8_t)(w >> 24);
    }
}

/* ------------------------------------------------------------------ EP0 */

/* 武装 EP0 OUT。两种用途：
 *   xf < 0   : 收 SETUP / 收状态 ZLP —— 照 ST 的写法 XFERSIZE=0、PKTCNT=1、STUPCNT=3
 *   xf >= 0  : 收一段 OUT 数据（SET_LINE_CODING），XFERSIZE=xf
 * 已经 EPENA 就不重写，避免破坏进行中的传输。 */
static void ep0_reset_ctl(void);   /* 定义在后面 */

/* ---- EP0 OUT 的四种武装方式，靠 attempt 轮换自动扫 ----
 *
 * v17D6 的屏幕数据把问题锁死了：
 *   E3      = USBRST 来过、ENUMDNE 来过
 *   DFF07   = DSTS.ENUMSPD=3 -> 全速 / 48MHz PHY，SUSPSTS=1（主机放弃了）
 *   ST0 P0  = RX FIFO 里一个包都没进过
 *
 * 也就是说：时钟、PHY、上拉、端口复位、枚举握手**全都正常**，
 * 唯一的问题是主机发下来的 SETUP 根本没被 EP0 OUT 收进 RX FIFO。
 * 到这一步就只剩"怎么武装 EP0 OUT"这一个变量了，所以把四种写法都编进去，
 * 每 4 秒换一种并让主机重新枚举一次。屏幕上 A 是当前第几种、ST 是收到几个 SETUP。
 * 哪个 A 让 ST 从 0 变 1，就锁定哪个。
 */
static void ep0_out_rearm(int v)
{
    uint32_t w;
    uint32_t c = REG(DOEPCTL(0));
    if (c & EPCTL_EPENA) {
        REG(DOEPCTL(0)) = c | EPCTL_EPENA | EPCTL_SNAK | EPCTL_EPDIS;
        w = 200000u;
        while ((REG(DOEPCTL(0)) & EPCTL_EPDIS) && w--) { }
    }
    /* EP0 的 MPS 字段是"编码"不是字节数：0=64 / 1=32 / 2=16 / 3=8。
     * 写 64 的低两位恰好是 0，等价于编码 0，所以一直是对的。 */
    REG(DOEPCTL(0)) = EPCTL_USBAEP | EPCTL_SD0PID | EP0_MPS_CODE;

    /* ---- 只保留这一种，别再和"时钟方案编号"绑在一起 ----
     *
     * v17D7~v17E2 这里用 `switch (v & 3)` 轮换四种武装方式，v 传的是 g_att。
     * 从 v17E3 起 g_att 被改成"时钟方案编号"，于是 A0 恰好命中 case 0 ——
     * 那个分支是"官方 usbd_ept_open() 之后不置 EPENA"，EP0 OUT 根本没开门。
     * 结果：A0(HICK 48MHz) 下 SETUP 收得到、数据发得出（s06 的 bit2），
     * 但状态阶段那个 OUT ZLP 收不到（s06 没有 bit3）→ 控制传输永远不结束
     * → Windows 一直"描述符请求失败"。
     *
     * 所以固定成 XFERSIZE=64 + PKTCNT=1 + STUPCNT=3：
     * SETUP 能进 RX FIFO，状态阶段的 ZLP 也能收。
     * 其它三种写法只在 v17D7 那一轮用过，A1 时确实能收到 OUT（s4e 有 bit3），
     * 现在统一用这一种，不再随方案漂移。 */
    (void)v;
    REG(DOEPTSIZ(0)) = (3u << 29) | (1u << 19) | EP0_SIZE;
    REG(DOEPCTL(0)) |= EPCTL_EPENA | EPCTL_CNAK;
}

static void ep0_arm_out(int xf)
{
    if (REG(DOEPCTL(0)) & EPCTL_EPENA) return;
    if (xf < 0) {
        ep0_out_rearm(1);          /* 固定用一种，别跟着时钟方案漂 */
        return;
    }
    REG(DOEPTSIZ(0)) = (1u << 19) | (uint32_t)xf;
    REG(DOEPCTL(0)) |= EPCTL_EPENA | EPCTL_CNAK;
}

/* ---- 总线复位 / 枚举完成 ----
 * 这两个事件必须在**微秒级**处理掉：主机做完端口复位后 10ms 内就会发
 * GET_DESCRIPTOR，如果那时 EP0 OUT 还没武装好，SETUP 直接被 NAK，
 * 主机重试几次就判失败（Windows: Device Descriptor Request Failed）。
 * 之前全靠主循环轮询，反应要 10~30ms，所以怎么武装都没用 —— 根本没赶上。 */
static void ep0_bus_reset(void)
{
    g_diag[17]++;                 /* 主机复位总线的次数：SET_ADDRESS 之后如果一直在涨，
                                   * 说明主机的请求打不到我们身上，它超时后就复位重来 */
    g_ready = 0;
    g_busy = 0;
    ep0_phase = EP0_IDLE;
    ep0_len = 0;
    ep0_sent = 0;
    REG(DIEPINT(0)) = 0xFF;
    REG(DOEPINT(0)) = 0xFF;
    ep0_reset_ctl();
    REG(DCFG) = 3u;
    REG(DAINTMSK) = (1u << 0) | (1u << 16);
    ep0_arm_out(-1);
}

static void ep0_enum_done(void)
{
    REG(DCFG) = (REG(DCFG) & ~3u) | 3u;
    REG(DIEPINT(0)) = 0xFF;
    REG(DOEPINT(0)) = 0xFF;
    ep0_reset_ctl();
    ep0_phase = EP0_IDLE;
    ep0_arm_out(-1);
}

/* USBFS 中断（IRQ67）。只处理"必须马上做"的两件事：端口复位、枚举完成。
 * 收包（RXFLVL）仍旧留给主循环轮询 —— 那个慢几毫秒没影响，
 * 而且放中断里会和主循环的 usb_service() 抢 RX FIFO，容易出重入问题。 */
__attribute__((section(".text.usb_irq")))
void usb_irq(void)
{
    uint32_t sts = REG(GINTSTS);

    g_diag[2] |= 8u;                       /* 中断确实进来了 */

    if (sts & INT_USBRST) {
        REG(GINTSTS) = INT_USBRST;
        g_diag[2] |= 0x10u;
        ep0_bus_reset();
    }
    if (REG(GINTSTS) & INT_ENUMDNE) {
        REG(GINTSTS) = INT_ENUMDNE;
        g_diag[2] |= 0x20u;
        ep0_enum_done();
    }
}

/* 把 EP0 IN 停成官方库 usbd_ept_in_check_fifo() 的样子：
 *   置 SNAK + EPDIS -> 等 EPTDIS 落地 -> 刷掉 EP0 的 TX FIFO
 * 官方就是靠"先 SNAK 再 EPDIS"，不然端点会卡在使能态、之后的 EPENA 全被忽略。 */
static void ep0_in_stop(void)
{
    uint32_t c = REG(DIEPCTL(0));
    if (!(c & EPCTL_EPENA)) return;

    REG(DIEPCTL(0)) = c | EPCTL_EPENA | EPCTL_SNAK | EPCTL_EPDIS;
    uint32_t w = 200000u;
    while ((REG(DIEPCTL(0)) & EPCTL_EPDIS) && w--) { }
    if (REG(DIEPCTL(0)) & EPCTL_EPDIS) { g_send_flags |= 16u; return; }

    REG(GRSTCTL) = (REG(GRSTCTL) & ~(0x1Fu << 6)) | (1u << 5);   /* txfnum=0 */
    w = 200000u;
    while ((REG(GRSTCTL) & (1u << 5)) && w--) { }
    g_send_flags |= 1u;
}

/* EP0 IN 发送。诊断位见 g_send_flags：
 *   bit0 = 端点原来是使能的（需要先停）
 *   bit1 = 本次真的武装了
 *   bit2 = 见过 IN XFRC
 *   bit3 = 见过 OUT XFRC
 *   bit4 = 停端点超时
 *   bit5 = FIFO 空间不够，这块没喂进去
 *   bit6 = 做过一次长断开重连
 *
 * v17D4 起改用 Artery 官方库的顺序（usbd_ept_send → usbd_inept_handler）：
 *   DIEPTSIZ -> CNAK -> EPENA -> 再往 FIFO 塞数据
 * 之前一直是"先写 FIFO 再 EPENA"，那是 ST HAL 的写法。在这颗核上两者语义不同：
 * 端点未使能时写进去的字，官方流程里根本就不存在，改回官方顺序至少消掉一个变量。 */
static void ep0_send_chunk(uint32_t n)
{
    ep0_in_stop();

    /* 顺序必须按 DWC2 手册：**先往 TX FIFO 填数据**，再写传输长度，最后才置 EPENA。
     * 反过来的话（先 EPENA 再填 FIFO），核可能在 FIFO 还空着的时候就去发包，
     * 主机收到的就是空包/残包 —— 表现正是"IN XFRC 置位了但描述符读不出来"。 */
    /* ---- 顺序：TSIZ -> EPENA|CNAK -> 再写 FIFO ----
     * 这条是实测出来的，不是照手册抄的。两种顺序的效果：
     *   EPENA 在前 -> S06，有 bit2（IN XFRC），主机确实收到了数据包
     *   FIFO  在前 -> S02，没有 bit2，主机什么都没收到
     * 说明这颗核的 DFIFO 写入要在端点使能之后才会被真正组包；
     * 先写 FIFO 的话数据进了缓冲区但从不发送，XFERSIZE 永远等不满，XFRC 也不会来。
     *
     * 另外**不要**用 DTXFSTS0(0x918) 判断"FIFO 还有没有空间"——
     * 这颗核上它恒读 0，v17E7 因此把每一包都判成"放不下"直接扔掉。
     * EP0 的 IN FIFO 是非周期 TX FIFO（64 字），一包最多 16 字，不会满。 */
    g_diag[18] = REG(DTXFSTS(0)) & 0xFFFFu;

    REG(DIEPTSIZ(0)) = (1u << 19) | n;
    REG(DIEPCTL(0)) |= EPCTL_CNAK | EPCTL_EPENA;
    g_send_flags |= 2u;

    if (n) fifo_write(0, ep0_ptr + ep0_sent, n);

    ep0_sent = (uint16_t)(ep0_sent + n);
}

/* DWC2 规定：端点使能时不能直接改写 EPCTL，必须先 EPENA|EPDIS 停掉。
 * 直接写 0 会让端点卡在"半使能"状态，之后 EPENA 再也置不上 —— 数据永远发不出去。 */
static void ept_disable(uint32_t off)
{
    uint32_t c = REG(off);
    if (!(c & EPCTL_EPENA)) return;
    REG(off) = c | EPCTL_EPENA | EPCTL_EPDIS;
    uint32_t w = 200000u;
    while ((REG(off) & EPCTL_EPENA) && w--) { }
}

static void ep0_reset_ctl(void)
{
    /* 官方 usb_ept_open() 对 EP0 也会置 usbacept(bit15) 和 setd0pid(bit28)。 */
    ept_disable(DIEPCTL(0));
    ept_disable(DOEPCTL(0));
    REG(DIEPCTL(0)) = EPCTL_USBAEP | EP0_MPS_CODE;
    REG(DOEPCTL(0)) = EPCTL_USBAEP | EPCTL_SD0PID | EP0_MPS_CODE;
}

static void ep0_stall(void)
{
    REG(DIEPCTL(0)) |= EPCTL_STALL;
    REG(DOEPCTL(0)) |= EPCTL_STALL;
}

static void ep0_unstall(void)
{
    REG(DIEPCTL(0)) &= ~EPCTL_STALL;
    REG(DOEPCTL(0)) &= ~EPCTL_STALL;
}

static void apply_address(void)
{
    uint8_t a = usb_ctrl_address() & 0x7F;
    if (a != (uint8_t)((REG(DCFG) >> 4) & 0x7F))
        REG(DCFG) = (REG(DCFG) & ~(0x7Fu << 4)) | ((uint32_t)a << 4);
}

/* ------------------------------------------------------------ 事件处理 */

/* 把当前挂起的所有 USB 事件处理掉。不依赖任何中断。 */
static void usb_service(void)
{
    uint32_t sts = REG(GINTSTS);
    g_diag[0] = sts;

    if (sts & INT_USBRST) {
        REG(GINTSTS) = INT_USBRST;
        g_diag[2] |= 1u;
        ep0_bus_reset();
    }

    if (REG(GINTSTS) & INT_ENUMDNE) {
        REG(GINTSTS) = INT_ENUMDNE;
        g_diag[2] |= 2u;
        ep0_enum_done();
    }

    /* ---- RX FIFO：SETUP 与 OUT 数据都从这里取 ---- */
    uint32_t guard = 64;
    while ((REG(GINTSTS) & INT_RXFLVL) && guard--) {
        uint32_t grx = REG(GRXSTSP);
        uint32_t ep  = grx & 0xF;
        uint32_t cnt = (grx >> 4) & 0x7FF;
        uint32_t pkt = (grx >> 17) & 0xF;
        g_diag[2] |= 4u;
        g_diag[10] = pkt | (ep << 8);          /* 最后一个 RX 包的 pktsts / 端点号 */
        g_diag[14]++;                          /* RX 包累计个数 */

        if (pkt == PKTSTS_SETUP_DATA) {
            usb_setup_t s;
            usb_ctrl_result_t r;
            fifo_read((uint8_t *)&s, 8);
            g_diag[1]++;
            g_diag[6] = ((uint32_t)s.bmRequestType << 24)
                      | ((uint32_t)s.bRequest << 16) | (uint32_t)s.wValue;
            g_diag[8] = s.wLength;
            s_polls = 0;             /* 主机正在枚举，别在这个节骨眼上重连 */
            usb_ctrl_handle(&s, &r);
            ep0_unstall();
            g_diag[9] = (uint32_t)(r.stall ? 1u : 0u)
                      | (uint32_t)(r.out_data ? 2u : 0u)
                      | ((uint32_t)r.len << 8);

            /* 最上面是最新的，方便看"最后一步是什么" */
            g_trace[0] = g_trace[1];
            g_trace[1] = g_trace[2];
            g_trace[2] = ((uint32_t)(r.stall ? 0xFFu : (r.len > 0xFEu ? 0xFEu : r.len)) << 24)
                       | ((uint32_t)s.wValue << 8)
                       | (uint32_t)s.bRequest;

            if (r.stall) {
                ep0_phase = EP0_IDLE;
                ep0_stall();
                continue;
            }
            if (r.out_data) {
                ep0_phase = EP0_WDATA;
                ep0_arm_out(64);
            } else {
                ep0_ptr  = r.data;
                ep0_len  = r.len;
                ep0_sent = 0;

                /* ---- SET_ADDRESS：官方是"先写 DCFG 地址，再回状态 ZLP" ----
                 * 我们原来等状态阶段那个 ZLP 的 XFRC 之后才写地址，
                 * 于是地址永远没落到 DCFG 里 —— 主机切到新地址后根本打不到我们，
                 * 超时 -> 复位总线 -> 从头再来，屏幕上就永远停在 `05 00xx`。
                 * DWC2 的硬件会把地址切换延后到当前控制传输结束，所以先写是安全的。
                 * 参考：usbd_sdr.c 的 usbd_set_address()。 */
                if (s.bRequest == 0x05 && (s.bmRequestType & 0x7Fu) == 0x00)
                    apply_address();

                if (r.len) {
                    ep0_phase = EP0_DATA;
                    ep0_send_chunk(r.len > EP0_SIZE ? EP0_SIZE : r.len);
                } else {
                    ep0_phase = EP0_STATUS;
                    ep0_send_chunk(0);
                }
                ep0_arm_out(-1);
            }
        } else if (pkt == PKTSTS_OUT_DATA && cnt) {
            uint32_t n = cnt <= sizeof(g_tmp) ? cnt : sizeof(g_tmp);
            fifo_read(g_tmp, n);
            if (ep == 0) usb_ctrl_line_coding_write(g_tmp);
            else         usb_cdc_rx_push(g_tmp, (uint16_t)n);
        } else if (cnt) {
            uint32_t n = cnt <= sizeof(g_tmp) ? cnt : sizeof(g_tmp);
            fifo_read(g_tmp, n);
        }
    }

    uint32_t daint = REG(DAINT) & REG(DAINTMSK);

    /* ---- EP0 IN 完成 ---- */
    if (daint & (1u << 0)) {
        uint32_t f = REG(DIEPINT(0));
        g_diag[11] = f;
        REG(DIEPINT(0)) = 0xFF;
        if (f & EPINT_XFRC) {
            g_send_flags |= 4u;
            if (ep0_phase == EP0_DATA && ep0_sent < ep0_len) {
                uint16_t left = (uint16_t)(ep0_len - ep0_sent);
                ep0_send_chunk(left > EP0_SIZE ? EP0_SIZE : left);
            } else if (ep0_phase == EP0_STATUS) {
                ep0_phase = EP0_IDLE;
                apply_address();
                ep0_arm_out(-1);
            } else if (ep0_phase == EP0_DATA) {
                ep0_phase = EP0_IDLE;
                ep0_arm_out(-1);
            }
        }
    }

    /* ---- EP1 IN 完成 ---- */
    if (daint & (1u << 1)) {
        REG(DIEPINT(1)) = 0xFF;
        g_busy = 0;
    }

    /* ---- EP0 OUT 完成 ---- */
    if (daint & (1u << 16)) {
        uint32_t f = REG(DOEPINT(0));
        REG(DOEPINT(0)) = 0xFF;
        if (f & EPINT_XFRC) g_send_flags |= 8u;
        if ((f & EPINT_XFRC) && ep0_phase == EP0_WDATA) {
            ep0_phase = EP0_STATUS;
            ep0_ptr = 0;
            ep0_len = 0;
            ep0_sent = 0;
            ep0_send_chunk(0);
        }
        if (ep0_phase == EP0_IDLE) ep0_arm_out(-1);
    }

    /* ---- EP2 OUT 完成 -> 重新武装 ---- */
    if (daint & (1u << 18)) {
        REG(DOEPINT(2)) = 0xFF;
        if (g_ready) {
            REG(DOEPTSIZ(2)) = (1u << 19) | EP2_SIZE;
            REG(DOEPCTL(2)) |= EPCTL_CNAK | EPCTL_EPENA;
        }
    }

    /* ---- 兜底：EP0 OUT 一旦空闲就保持武装 ----
     * DWC2 只有在 EP0 OUT 处于使能状态时才会接收 SETUP。
     * 只要有一次漏了重新武装（例如某个分支 continue 掉了），
     * 后续所有控制请求都会被 STALL，表现为"第一个描述符之后就彻底没了"。 */
    {
        uint32_t ctl = REG(DOEPCTL(0));
        if (!(ctl & EPCTL_EPENA) && !(ctl & EPCTL_STALL) && ep0_phase == EP0_IDLE)
            ep0_arm_out(-1);
    }
}

/* ------------------------------------------------------------------ 数据 */

int usb_cdc_write(const uint8_t *data, int len)
{
    int sent = 0;
    if (!g_ready) return 0;
    while (sent < len) {
        int chunk = len - sent;
        if (chunk > EP1_SIZE) chunk = EP1_SIZE;
        uint32_t guard = 400000;
        while (g_busy && guard--) usb_service();   /* 轮询版必须自己推进状态机 */
        if (g_busy) break;
        g_busy = 1;
        REG(DIEPTSIZ(1)) = (1u << 19) | (uint32_t)chunk;
        REG(DIEPCTL(1)) |= EPCTL_CNAK | EPCTL_EPENA;
        fifo_write(1, data + sent, (uint32_t)chunk);
        sent += chunk;
    }
    return sent;
}

int usb_cdc_connected(void) { return g_ready; }

/* ------------------------------------------------------------ 初始化/重连 */

/* USB 时钟配置。
 *
 * 关键：不能用 HICK 这条路。官方 crm_usb_clock_source_select(HICK) 内部会调用
 * crm_hick_sclk_frequency_select(CRM_HICK_SCLK_48MHZ)，那会把 hick_to_sclk(bit14)
 * 和 hickdiv(bit12) 一起置起来 —— HICK 同时也是 CPU 主频的来源，改它等于改主频，
 * 会把 ADC 采样时序和 UI 全带歪。
 *
 * 所以只走 PLL：CRM_MISC1.bit13=0 选 PLL，分频在 CRM_MISC2[15:12]=usbdiv。
 * PLL 频率不用猜 —— 固件自己算过一份 SystemCoreClock，就放在 RAM 里。 */
/* 每个 attempt 换一组 (usbdiv, usbtrdtim)。
 *   attempt 0 -> 用固件自己算出的主频推 div，trdt = 9（8 个 PHY 时钟）
 *   attempt 1 -> 同上但 trdt = 5（16 个 PHY 时钟，Artery 官方 USB_TRDTIM_16）
 *   attempt >=2 -> div 强制 1..15，trdt 交替
 * 之所以要扫 trdt：ST 的 FS 核用 9，AT32 官方库用 5，两者差了整整一倍，
 * 周转时间不对会表现为"复位/枚举完成都正常，但数据阶段全是坏包"。 */
#define MAX_ATT 6

/* ---------------------------------------------------------------- 时钟方案
 *
 * v17E3 的读数定了案：`ST8 P16 C150` —— USB 链路通了，但 C150 说明核心跑 150MHz。
 * 150 用整数分频凑不出 48：
 *   /3 = 50.00MHz（+4.17%）  /4 = 37.5   /2 = 75
 * 4% 的偏差能让"收"勉强工作（PHY 的 DPLL 会锁到主机发来的信号上），
 * 但"发"是按本地时钟分频出来的，12Mbps 会变成 12.5Mbps，主机很可能判失败。
 * 所以必须换时钟源：AT32F435 的 HICK 内部 RC 标称就是 48MHz，
 * 官方 crm_usb_clock_source_select(CRM_USB_CLOCK_SOURCE_HICK) 就是干这个的。
 *
 * 方案顺序（每次只换一组，收到包就停）：
 *   0) HICK /1        -> 48.00MHz 精确
 *   1) PLL  /d0       -> 就近分频（v17E3 已经证明能收包）
 *   2..5) PLL 其它分频，兜底用
 */
#define SRC_PLL  0
#define SRC_HICK 1

static uint8_t s_seq_src[MAX_ATT];
static uint8_t s_seq_div[MAX_ATT];
static uint8_t s_seq_ok;

/* HICK 是否正好 48MHz 的整数分频能凑出来 —— 能就用 PLL，不能就上 HICK */
static void build_seq(void)
{
    static const uint8_t all_div[6] = { 1, 2, 3, 4, 5, 6 };
    uint32_t core = *(volatile uint32_t *)0x20000244u;
    int d0, exact, n, i;

    if (core >= 1000u && core < 1000000u) core *= 1000u;

    d0 = (int)((core / 1000000u + 24u) / 48u);
    if (d0 < 1) d0 = 1;
    if (d0 > 6) d0 = 6;
    exact = ((uint32_t)(d0 * 48) == core / 1000000u);

    n = 0;
    if (exact) { s_seq_src[n] = SRC_PLL;  s_seq_div[n] = (uint8_t)d0; n++; }
    else       { s_seq_src[n] = SRC_HICK; s_seq_div[n] = 1;           n++; }

    /* 第二种：exact 时给 HICK 做对照，否则回落到 PLL 就近分频 */
    s_seq_src[n] = exact ? SRC_HICK : SRC_PLL;
    s_seq_div[n] = exact ? 1        : (uint8_t)d0;
    n++;

    for (i = 0; i < 6 && n < MAX_ATT; i++) {
        if (all_div[i] == d0) continue;
        s_seq_src[n] = SRC_PLL; s_seq_div[n] = all_div[i]; n++;
    }
    while (n < MAX_ATT) { s_seq_src[n] = SRC_PLL; s_seq_div[n] = 1; n++; }

    s_seq_ok = 1;
}

static void attempt_params(int a, int *pcode, int *ptr, int *psrc)
{
    uint32_t core = *(volatile uint32_t *)0x20000244u;
    uint32_t pllcfg, ns, ms, fr, rcs_mhz, pll_mhz;
    int d, code;

    if (!s_seq_ok) build_seq();
    a %= MAX_ATT;

    d    = s_seq_div[a];
    code = (d == 7) ? 12 : (2 * d - 1);

    *pcode = code;
    *ptr   = 5;                       /* Artery 官方 16 个 PHY 时钟 */
    *psrc  = s_seq_src[a];

    g_send_flags = 0;
    g_div  = (uint32_t)d;
    g_trdt = 5;
    g_att  = (uint32_t)a;
    g_src  = (uint32_t)s_seq_src[a];

    /* 顺手把 PLL 频率从 CRM_PLLCFG 独立算一遍，跟 SystemCoreClock 交叉验证。
     * 注意：fr = 1 << ((pllcfg>>16) & 7)，上一版 << 和 & 优先级写反了，所以永远显示 0。 */
    if (core >= 1000u && core < 1000000u) core *= 1000u;
    pllcfg  = *(volatile uint32_t *)0x40023804u;
    ms      = pllcfg & 0xFu;
    ns      = (pllcfg >> 6) & 0x1FFu;
    fr      = 1u << ((pllcfg >> 16) & 7u);
    rcs_mhz = ((pllcfg >> 22) & 1u) ? 48u : 8u;     /* HICK 48M / HEXT 按 8M */
    pll_mhz = (ms && fr) ? (rcs_mhz * ns) / (ms * fr) : 0u;
    if (pll_mhz > 400u || pll_mhz < 24u) pll_mhz = 0u;
    (void)pll_mhz;

    g_diag[18] = core / 1000000u;
    g_diag[19] = pllcfg;        /* 直接给原值，屏幕上级出十六进制，比猜 MHz 靠谱 */
}

static void hw_init(int attempt)
{
    uint32_t g;
    int code, trdt, src;
    g_diag[15]++;                 /* 初始化过几次：屏幕上用来看轮换有没有在跑 */
    attempt_params(attempt, &code, &trdt, &src);

    OTG_CLK_REG |= (1u << OTG_CLK_BIT);

    /* 先断开，改时钟和复位期间别让主机看到半死的设备 */
    REG(DCTL) |= (1u << 1);

    /* USB 时钟源：MISC1.bit13 = 0 选 PLL，= 1 选 HICK（HICK 标称 48MHz）。
     * 选 HICK 时官方还会把 hickdiv(bit12) 清 0，让 HICK 不受 /6 影响。 */
    if (src == SRC_HICK) {
        /* MISC1[13:12] 必须是 0b11：
         *   bit13 hick_to_usb = 1  选 HICK 给 USB
         *   bit12 hickdiv     = 1  -> CRM_HICK48_NODIV，HICK 输出 48MHz
         * 注意 hickdiv=0 对应的是 CRM_HICK48_DIV6，也就是固定 8MHz。
         * v17E4 把 bit12 清 0 了，等于给 USB 核喂 8MHz，所以 ST0 P0 一个包都没有。 */
        CRM_MISC1 |= (1u << 12) | (1u << 13);
        /* HICK 是内部 RC，标称 48MHz 但实际有 ±1% 左右漂移，USB 要求 ±0.25%。
         * auto_step_en 让它拿主机的 SOF 当基准自动微调 hicktrim，正好是官方给
         * "HICK 做 USB 时钟"准备的功能（crm_auto_step_mode_enable）。 */
        CRM_MISC2 |= (3u << 4);
        for (volatile int i = 0; i < 20000; i++) { }   /* 等 HICK 切到 48MHz 稳定 */
    } else {
        CRM_MISC1 &= ~(1u << 13);
    }
    CRM_MISC2 = (CRM_MISC2 & ~(0xFu << 12)) | ((uint32_t)code << 12);

    /* HICK 是 PLL 的参考源，确保它在跑 */
    CRM_CTRL |= 1u;
    g = 400000u;
    while (!(CRM_CTRL & 2u) && g--) { }

    for (volatile int i = 0; i < 2000; i++) { }

    /* 核心软复位：先等 AHB idle，再 csftrst */
    g = 400000u;
    while (!(REG(GRSTCTL) & (1u << 31)) && g--) { }
    REG(GRSTCTL) |= (1u << 0);
    g = 400000u;
    while ((REG(GRSTCTL) & 1u) && g--) { }

    /* PHY 上电：GCCFG bit16=1 是「退出掉电」（官方 usb_global_init 的写法） */
    REG(GCCFG) |= (1u << 16);
    REG(GCCFG) &= ~(1u << 17);
    REG(GCCFG) |= (1u << 21);                 /* 忽略 VBUS 检测 */
    REG(GCCFG) &= ~(1u << 20);
    for (volatile int i = 0; i < 20000; i++) { }

    /* 设备模式 + 周转时间（trdt 由 attempt 决定，见 attempt_params） */
    REG(GUSBCFG) = (REG(GUSBCFG) & ~((1u << 29) | (0xFu << 10)))
                 | (1u << 30) | ((uint32_t)trdt << 10);

    /* 刷 FIFO：txfnum=16 全部，再刷 RX */
    REG(GRSTCTL) = (REG(GRSTCTL) & ~(0x1Fu << 6)) | (16u << 6) | (1u << 5);
    g = 400000u;
    while ((REG(GRSTCTL) & (1u << 5)) && g--) { }
    REG(GRSTCTL) |= (1u << 4);
    g = 400000u;
    while ((REG(GRSTCTL) & (1u << 4)) && g--) { }

    /* FIFO 分配：RX 128 字 -> EP0TX 64 -> EP1TX 64（共 256/320 字） */
    REG(GRXFSIZ)    = 128;
    REG(HNPTXFSIZ)  = (64u << 16) | 128;
    REG(DIEPTXF(1)) = (64u << 16) | (128 + 64);

    REG(DCFG) = 3u;
    REG(DCTL) |= (1u << 1);                  /* sftdiscon=1：先断开 */
    ep0_reset_ctl();

    REG(DIEPMSK)  = EPINT_XFRC;
    REG(DOEPMSK)  = EPINT_XFRC | (1u << 3);  /* bit3 = setupmsk */
    REG(DAINTMSK) = (1u << 0) | (1u << 16);
    REG(GINTSTS)  = 0xBFFFFFFFu;
    /* 只开"必须立刻处理"的两个中断；收包仍旧轮询 */
    REG(GINTMSK)  = INT_USBRST | INT_ENUMDNE;
    REG(GAHBCFG)  = 1u;                      /* GLBLINTRMSK */
    /* NVIC: IRQ67 -> ISER[2] bit3，优先级给个中间值 */
    *(volatile uint32_t *)0xE000E108u |= (1u << 3);
    *(volatile uint8_t  *)(0xE000E400u + 67u) = 0x40u;

    isb_dmb();

    usb_ctrl_init();
    ep0_phase = EP0_IDLE;
    ep0_len = 0;
    ep0_sent = 0;

    /* 接上总线 */
    REG(DCTL) &= ~(1u << 1);
    for (volatile int i = 0; i < 30000; i++) { }
    ep0_arm_out(-1);
}

static void usb_next_attempt(void);

/* 手动换下一组参数（按键触发用） */
void usb_advance(void)
{
    g_ready = 0;
    g_busy = 0;
    ep0_phase = EP0_IDLE;
    usb_next_attempt();
}

void usb_init(void)
{
    g_ready = 0;
    g_busy = 0;
    g_attempt = 0;
    hw_init(0);
}

/* 换一组 USB 时钟参数，整块重新初始化，再逼主机重新枚举。
 * 每次尝试换一个 usbdiv，16 次刚好把 1..15 扫一遍。 */
static void usb_next_attempt(void)
{
    g_attempt++;
    if (g_attempt >= MAX_ATT) g_attempt = 0;
    g_ready = 0;
    g_busy = 0;
    ep0_phase = EP0_IDLE;

    /* ---- 关键：真的把 D+ 拉低够久 ----
     * v17D3 及以前这里只在 hw_init 里瞬间置/清 sftdiscon，断开时间只有几微秒。
     * USB 规范要求设备至少呈现 2.5us 的 SE0 才算"拔出"，但 Windows 的
     * hub 驱动要连续采样到足够长的断开才会认为设备被拔掉、愿意重新枚举。
     * 结果是：开机后第一次枚举失败 -> 之后每次"重连"主机根本没看见 ->
     * 屏幕永远是 ST0 PK0 RQ 00000000（一个 SETUP 都没收到），跟死了一样。
     * 这里改成保持 ~150ms，让主机真的看到一次插拔。 */
    REG(DCTL) |= (1u << 1);
    for (volatile uint32_t i = 0; i < 6000000u; i++) { }
    REG(DCTL) &= ~(1u << 1);
    for (volatile uint32_t i = 0; i < 400000u; i++) { }

    hw_init(g_attempt);
    /* hw_init 里的 attempt_params() 会把 g_send_flags 清零，所以标记位要在它之后置 */
    g_send_flags |= 64u;
}

/* ------------------------------------------------------------------ 轮询 */

void usb_poll(void)
{
    if (!g_inited) {
        g_inited = 1;
        usb_init();
        return;
    }

    usb_service();

    if (!g_ready && usb_ctrl_config()) {
        g_ready = 1;
        REG(DIEPCTL(1)) = EPCTL_USBAEP | EPCTL_EPTYP | EPCTL_SD0PID | EP1_SIZE;
        REG(DAINTMSK) |= (1u << 1);

        REG(DOEPCTL(2)) = EPCTL_USBAEP | EPCTL_EPTYP | EPCTL_SD0PID | EP2_SIZE;
        REG(DOEPTSIZ(2)) = (1u << 19) | EP2_SIZE;
        REG(DOEPCTL(2)) |= EPCTL_CNAK | EPCTL_EPENA;
        REG(DAINTMSK) |= (1u << 18);
    }

    /* 自愈：只要还没收到过任何 SETUP/RX 包，就每隔 RETRY_POLL 圈换一种
     * EP0 OUT 武装方式 + 断开重连一次，逼主机重新枚举、顺带让新武装方式生效。
     * 一旦 ST 或 P 不再是 0（主机的东西终于进来了），立刻停止扯线。 */
    s_polls++;

    /* 自动扫时钟方案，**停止条件是"真的枚举成功了"**（主机发过 SET_CONFIGURATION，
     * 也就是 usb_ctrl_config() != 0），而不是"收到包"。
     *
     * 现在 v17E5 已经证明 A0(HICK 48MHz) 能收包（ST8 P16），所以只要收到包就钉住
     * 这一档、不再重连 —— 重连会打断主机正在进行的枚举。 */
    if (!g_ready && g_diag[1] == 0 && g_diag[14] == 0) {
        uint32_t thr = (g_diag[15] < 2u) ? 600u : 400u;
        if (s_polls > thr) {
            s_polls = 0;
            usb_next_attempt();
        }
    } else {
        s_polls = 0;
    }

    g_diag[3] = ep0_phase;
    g_diag[4] = ep0_sent;
    g_diag[5] = ep0_len;
    g_diag[7] = g_ready;
    g_diag[12] = REG(DSTS);     /* 核到底有没有被主机"枚举完成" */
    g_diag[13] = REG(DCFG);
    g_diag[16] = REG(DCFG);          /* DAD 在 [10:4]，应读成 0x1B0|3 = 0x1B3 */
    g_diag[17] = REG(DOEPTSIZ(0));
    /* 注意：这里以前会把 g_diag[11] 覆盖成 SystemCoreClock，导致屏幕上的 X
     * 永远显示时钟而不是 DIEPINT。已去掉，g_diag[11] 只留给 DIEPINT。 */
}
