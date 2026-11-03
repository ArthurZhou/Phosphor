/*
 * 屏幕画面流式传输
 *
 * 为什么这样传
 *   这台设备的可用 SRAM 到 0x2000C000 为止（48KB，实测），一帧 320x240
 *   RGB565 要 153600 字节，装不下；Flash 余量也不够。RAM 里更没有整屏缓冲：
 *   每个区域按 w*h*2 临时分配、推送、释放。因此唯一的办法是在推屏的那一刻
 *   把这块区域直接送出去，不落地。
 *
 * 触发点
 *   push_band（0x08008E64）入口跳板 -> img_send_band()。函数无参数，区域
 *   参数在全局窗口结构里：0x2000A568 win_x / A56C win_y / A570 win_w /
 *   A574 win_h / A578 fb_ptr。缓冲为 w*h 个 RGB565，行优先紧密排列，
 *   与 push_band 内部的下标一一对应（它按 i 从 0 数到 w*h-1）。
 *
 * 代价
 *   链路为“一个 64 字节包在途”，实测约 1ms 一包 -> 约 64KB/s。一块
 *   320x26 的区域约 16640 字节，约 260ms 发完；整屏约 2.4s。发送期间渲染
 *   任务停在 push_band 里，画面会一块一块地往下走。这是链路带宽决定的，
 *   不是实现取舍；主机侧因此按块拼图，收到多少显示多少。
 *
 * 安全边界
 *   - 未武装时本函数第一条判断即返回，正常开机路径与原厂一致；
 *   - 每个包都有超时（约 30ms），主机不在读或 USB 掉线时立刻放弃并自动
 *     解除武装，不会把设备挂死；
 *   - 窗口参数与 fb_ptr 全部先做范围校验，越界直接丢弃该块；
 *   - 武装状态在 .bss，热复位可能保留：因此每块递减预算、每包都有超时，
 *     最坏情况是主机没在读时白耗 30ms 然后自动解除，不会形成复位循环。
 */
#include <stdint.h>

/* 由 phosphor_usb.c 提供：发一个包（阻塞到完成或超时）、判断发送空闲、
 * 取下一个帧序号。放在同一编译单元会与 USB 状态机纠缠，故做成窄接口。 */
int      usb_img_packet(const uint8_t *p, uint16_t len);
int      usb_tx_free(void);
int      usb_tx_ready(void);
uint16_t usb_seq_next(void);

/* 绘制窗口（推屏前有效，推屏后缓冲即被释放，指针失效） */
#ifdef IMG_HOST_TEST
/* 本机单元测试：把窗口结构与 RAM 边界换成可写的替身，
 * 其余逻辑（分块、描述、校验、预算）与固件完全一致。见 tools/test/。
 * 替身指针用 uintptr_t：宿主机可能是 64 位，uint32_t 会截断。 */
extern uint16_t t_win_x, t_win_y, t_win_w, t_win_h;
extern uintptr_t t_win_fb;
extern uint8_t  t_ui_mode;
void t_enq(uint32_t id);
#define WIN_X   t_win_x
#define WIN_Y   t_win_y
#define WIN_W   t_win_w
#define WIN_H   t_win_h
#define WIN_FB  t_win_fb
#else
#define WIN_X   (*(volatile uint16_t *)0x2000A568u)
#define WIN_Y   (*(volatile uint16_t *)0x2000A56Cu)
#define WIN_W   (*(volatile uint16_t *)0x2000A570u)
#define WIN_H   (*(volatile uint16_t *)0x2000A574u)
#define WIN_FB  (*(volatile uint32_t *)0x2000A578u)
#endif

#define SCR_W        320u
#define SCR_H        240u
#ifdef IMG_HOST_TEST
#define RAM_LO       ((uintptr_t)0u)
#define RAM_HI       (~(uintptr_t)0u)   /* 宿主机：不设上界（64 位地址） */
#else
#define RAM_LO       0x20000000u
#define RAM_HI       0x2000C000u    /* 实测可用上界：写 0x2000C000 即复位 */
#endif

#define DESC_LEN     8u             /* x, y, w, h 各 2 字节（小端） */
#define MAX_PAYLOAD  4096u          /* 单帧负载上限，与既有帧长度上限一致 */
#define PKT_MAX      64u            /* EP1 批量 IN 最大包长 */

#define FLAG_IMG     0x20u          /* 帧标志：屏幕画面块 */

/* 状态（.bss）。host 侧用 0x08 读，便于判断“到底武装没有”。 */
volatile uint32_t img_state;        /* 0 = 关，1 = 已武装 */
volatile uint32_t img_bands;        /* 已送出块数 */
volatile uint32_t img_rows;         /* 已送出行数（估算覆盖率用） */
volatile uint32_t img_budget;       /* 剩余块预算，0 = 不再发 */
volatile uint32_t img_err;          /* 0 无 / 1 超时 / 2 参数非法 / 3 发送中止 */
volatile uint32_t img_last;         /* 最近一块：x<<0 | y<<8 | w<<16 | h<<24 */
volatile uint8_t  img_busy;         /* 1 = 正在阻塞发送（命令层据此让路） */

/* 命令层在发送期间只放行 0x00，收到即置位 */
volatile uint8_t  img_abort;

/* 1 = 请主循环排一次整页重绘（见 img_serve_redraw） */
volatile uint8_t  img_need_full;

/* --------------------------------------------------------------- 整页重绘 */
/*
 * 为什么需要它
 *   UI 不是每帧重画整个屏幕：它有一个消息队列（0x2000A528），消息 id 代表
 *   "这个绘制单元脏了，去画它"；主循环每轮先 ui_drain()（0x0800B544）取
 *   一条消息，再调 draw_block(id)（0x0801741C），画完返回。
 *   波形 8 条带（id 1）每秒都在重画，随时抓都有；但顶栏（id 2）、底栏（id 3）
 *   与菜单只在状态变化时入队一次，武装太晚就永远等不到——这是 R30
 *   "只截到波形框"的原因。
 *
 * 为什么不能投 0..13
 *   这 14 个 id 不是"屏幕上的 14 个区域"，而是 14 类图元：清屏、波形带、
 *   顶栏、底栏、整宽标题栏、左列菜单、右侧数值框、角标……draw_block 无条件
 *   把该图元画上去，不判断它当前该不该可见。全投一遍等于把所有菜单和
 *   数值框都叠在同一张屏上（R32 实机现象：一坨控件互压）。
 *
 * 原厂怎么决定投哪些
 *   0x08017182 处的页面重建函数（tbb 表在 0x08017196）：读 0x20004FCC
 *   （UI 页面号），cmp #8 后
 *   tbb 进 9 个分支，每支固定投一组 id。这就是"该页面由哪些图元组成"
 *   的原厂定义，照抄即可：
 *     0 -> 9          1 -> (无)        2 -> 1,2,3,A,B   （主界面）
 *     3 -> 4,5,A      4 -> 4,7,A       5/6/7 -> (无)    8 -> D
 *   （各分支还带一个前置的 0 = 清屏，本实现剔掉，见下。）
 *
 * 只做一件减法：不投 0
 *   0 是纯清屏（0x080113D4 取 A554/A556 的屏宽高，fill 全屏为 0，直写 LCD，
 *   不经过 push_band）。投了设备屏幕会先黑一下再逐块长回来；而主机每帧本来
 *   就是空白画布，自己知道起点，不需要设备清屏。原厂投 0 是为了抹掉上一页
 *   的残留，主机画布没有残留，这一条对我们是多余的。
 *
 * 不要自作聪明往里加东西（R33 的教训）
 *   R33 曾认为"底部参数条在所有页面常驻"，于是集合里没有 3 时补投一次 id 3。
 *   实机证明是错的：菜单页的集合 {4,5,A} / {4,7,A} 前面那个 0 已经把整屏清掉
 *   了，菜单页根本没有参数条。补进去的结果是设备屏幕上凭空闪出五个示波器
 *   参数卡片（TOT / X1 / 1MS / DC / RUN），抓图里也多了这一条。
 *   结论：原厂表是完整的闭合定义——它投什么就画什么，一个不多一个不少。
 *
 * 安全性
 *   入队发生在主循环上下文（与 ui_drain 同一线程、两条指令之间），不与
 *   UI 竞争队列；队列容量 20，一次最多 5 条。draw_block 是原厂自己的
 *   代码路径，画出来的就是设备当前页面的真实内容，不猜布局。
 */
#define UI_MODELS    9u
#define UI_NONE      0xFFu

#ifdef IMG_HOST_TEST
#define UI_MODE      t_ui_mode
#define UI_ENQ(id)   t_enq((uint32_t)(id))
#else
#define UI_QUEUE     0x2000A528u
#define UI_ENQUEUE   0x080188E8u    /* enqueue(queue, msg) */
#define UI_MODE      (*(volatile uint8_t *)0x20004FCCu)  /* UI 页面号 */
#define UI_ENQ(id)   ((void (*)(void *, uint32_t))(UI_ENQUEUE | 1u)) \
                         ((void *)(uintptr_t)UI_QUEUE, (uint32_t)(id))
#endif

/* 页面号 -> 绘制单元集合（原厂 0x08017196 的 tbb 表，id 0 已剔除）。
 * 空集合 = 原厂在该页面号上不重建（1/5/6/7），此时不投递任何东西。 */
static const uint8_t page_ids[UI_MODELS][5] = {
    { 9u,                            UI_NONE, UI_NONE, UI_NONE, UI_NONE },
    {                                UI_NONE, UI_NONE, UI_NONE, UI_NONE },
    { 1u, 2u, 3u, 0x0Au, 0x0Bu },   /* 主界面：波形 + 顶栏 + 底栏 + 角标 */
    { 4u, 5u, 0x0Au, UI_NONE, UI_NONE },
    { 4u, 7u, 0x0Au, UI_NONE, UI_NONE },
    {                                UI_NONE, UI_NONE, UI_NONE, UI_NONE },
    {                                UI_NONE, UI_NONE, UI_NONE, UI_NONE },
    {                                UI_NONE, UI_NONE, UI_NONE, UI_NONE },
    { 0x0Du,                         UI_NONE, UI_NONE, UI_NONE, UI_NONE }
};

void img_serve_redraw(void)
{
    uint32_t i, m;
    uint8_t n = 0u;
    const uint8_t *set;

    if (img_need_full == 0u)
    {
        return;
    }
    img_need_full = 0u;
    if (img_state == 0u)
    {
        return;                     /* 已解除，别再重绘 */
    }

    m = (uint32_t)(UI_MODE & 0x7Fu);   /* bit7 是标志位，见 draw_block id 10 */
    if (m >= UI_MODELS)
    {
        m = 2u;
    }
    set = page_ids[m];

    for (i = 0u; i < 5u && set[i] != UI_NONE; i++)
    {
        UI_ENQ(set[i]);
        n++;
    }
    if (n == 0u)
    {
        /* 原厂在这几个页面号上不重建任何东西（1 / 5 / 6 / 7，多为过渡态）。
         * 不猜布局、也不兜底画主界面：真机此时屏幕上就是上一页的残留，
         * 主机画布是空的，画一份假主界面只会误导。主机侧会报"0 块"并
         * 带上页面号，据此判断是页面号的问题还是别的问题。 */
        return;
    }
}

void img_arm(uint32_t bands)
{
    img_abort = 0u;
    img_err   = 0u;
    img_bands = 0u;
    img_rows  = 0u;
    img_budget = (bands == 0u) ? 1u : bands;
    img_state = 1u;
    img_need_full = 1u;             /* 钩子会按当前页面排一次重绘 */
}

void img_disarm(void)
{
    img_state  = 0u;
    img_budget = 0u;
}

/* push_band 入口跳板调用。运行在渲染任务上下文（非中断）。 */
void img_send_band(void)
{
    uint32_t x, y, w, h, off, rows, per, i, len, n, sum;
    uintptr_t fb;               /* 固件上就是 32 位；宿主机 64 位也不截断 */
    const uint8_t *src;
    uint8_t frame[16];

    if (img_state == 0u)
    {
        return;                     /* 未武装：不碰任何东西 */
    }
    if (img_budget == 0u || img_abort != 0u)
    {
        img_disarm();
        return;
    }
    if (img_busy != 0u)
    {
        return;
    }
    /* 中断上下文里不能阻塞发包：USB 完成中断再也进不来，会一直等到超时。
     * 13 处 push_band 调用都在渲染任务里（线程模式），这条只是兜底。 */
#ifndef IMG_HOST_TEST
    {
        uint32_t ipsr;
        __asm__ volatile("mrs %0, ipsr" : "=r"(ipsr));
        if (ipsr != 0u)
        {
            return;
        }
    }
#endif
    fb = WIN_FB;
    x  = WIN_X;
    y  = WIN_Y;
    w  = WIN_W;
    h  = WIN_H;
    /* 指针必须落在片内 SRAM 的可用区；缓冲随后被分配器回收，不能留用 */
    if (fb < RAM_LO || fb >= RAM_HI || w == 0u || h == 0u)
    {
        img_err = 2u;
        return;
    }
    if (x >= SCR_W || y >= SCR_H || w > SCR_W || h > SCR_H)
    {
        img_err = 2u;
        return;
    }
    if (w > SCR_W - x)
    {
        w = SCR_W - x;
    }
    if (h > SCR_H - y)
    {
        h = SCR_H - y;
    }
    if (w == 0u || h == 0u)
    {
        img_err = 2u;
        return;
    }
    /* 缓冲长度 w*h*2 必须整段落在可用 RAM 内 */
    if (fb + w * h * 2u > RAM_HI)
    {
        img_err = 2u;
        return;
    }

    per = (MAX_PAYLOAD - DESC_LEN) / (w * 2u);
    if (per == 0u)
    {
        img_err = 2u;
        return;
    }

    /* 先占位、再查端点：置位之后命令层就不再调用 tx_start()，因此不会出现
     * "这边逐包发着画面块、中断里又起一帧 0x01"的交错。反过来先查后占位，
     * 中间那条窗口正好能塞进一次 OUT 中断。 */
    img_busy = 1u;
    if (!usb_tx_free())
    {
        img_busy = 0u;
        return;                     /* 别的帧在途（0x01/0x02/0x08），丢这一块 */
    }

    img_last = (x & 0xFFu) | ((y & 0xFFu) << 8) |
               ((w & 0xFFu) << 16) | ((h & 0xFFu) << 24);

    src = (const uint8_t *)(uintptr_t)fb;

    for (off = 0u; off < h; )
    {
        rows = h - off;
        if (rows > per)
        {
            rows = per;
        }
        len = rows * w * 2u;        /* 本块像素字节数 */

        /* 帧头 8 + 描述 8，凑成一个整包发出去 */
        frame[0] = 0xA5u;
        frame[1] = 0x5Au;
        n = DESC_LEN + len;
        frame[2] = (uint8_t)(n & 0xFFu);
        frame[3] = (uint8_t)(n >> 8);
        {
            uint16_t seq = usb_seq_next();
            frame[4] = (uint8_t)(seq & 0xFFu);
            frame[5] = (uint8_t)(seq >> 8);
        }
        frame[6] = FLAG_IMG;
        frame[7] = 0u;
        frame[8]  = (uint8_t)(x & 0xFFu);
        frame[9]  = (uint8_t)(x >> 8);
        frame[10] = (uint8_t)((y + off) & 0xFFu);
        frame[11] = (uint8_t)((y + off) >> 8);
        frame[12] = (uint8_t)(w & 0xFFu);
        frame[13] = (uint8_t)(w >> 8);
        frame[14] = (uint8_t)(rows & 0xFFu);
        frame[15] = (uint8_t)(rows >> 8);

        /* 校验：帧头 + 描述 + 像素，与既有帧一致（低 8 位） */
        sum = 0u;
        for (i = 0u; i < 16u; i++)
        {
            sum += frame[i];
        }
        {
            const uint8_t *p = src + off * w * 2u;
            for (i = 0u; i < len; i++)
            {
                sum += p[i];
            }
        }
        sum &= 0xFFu;

        if (!usb_img_packet(frame, 16u))
        {
            goto fail;
        }
        {
            const uint8_t *p = src + off * w * 2u;
            for (i = 0u; i < len; i += PKT_MAX)
            {
                uint16_t l = (uint16_t)((len - i > PKT_MAX) ? PKT_MAX : (len - i));
                if (!usb_img_packet(p + i, l))
                {
                    goto fail;
                }
            }
        }
        {
            uint8_t c = (uint8_t)sum;
            if (!usb_img_packet(&c, 1u))
            {
                goto fail;
            }
        }

        off += rows;
    }

    /* 最后一包可能还在总线上。等端点真正空闲再放开命令层：此时若来一条
     * 0x04 之类，tx_start() 会因端点忙返回失败，整帧就卡在 tx_active=1 里
     * 再也推不动（既有的 tx_pump 只在成功时推进）。守卫计数只是兜底。 */
    {
        uint32_t guard = 20000u;
        while (!usb_tx_ready() && guard != 0u)
        {
            guard--;
        }
    }

    img_busy = 0u;
    img_bands++;
    img_rows += h;
    img_budget--;
    if (img_budget == 0u)
    {
        img_state = 0u;             /* 预算用完自动解除，避免无限循环 */
    }
    return;

fail:
    img_busy = 0u;
    img_err   = (img_abort != 0u) ? 3u : 1u;
    img_disarm();
}
