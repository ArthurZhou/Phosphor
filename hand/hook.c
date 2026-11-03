/* DSO-153 补丁钩子：把诊断数据通过"字符串指针劫持"显示在设置页上
 *
 * 为什么不再直接调用绘制函数：
 *   v8（D0/D1）调用固件的文字绘制 0x080102F0，屏幕上什么都没出现。
 *   原因是字形先写进显存、再由 UI 任务统一推屏，而推屏发生在我们钩子**之前**，
 *   所以每次画完立刻被下一帧覆盖，永远上不了屏。
 *
 * 现在改走 UI 自己那条路：设置页那三行文字是从一张指针表里取字符串再画的：
 *       language = *(uint8_t *)(0x2000001C + 0x67)      // 1..8
 *       版本 = *(char **)(0x08021600 + 8*L)
 *       型号 = *(char **)(0x08021604 + 8*L)
 *       警告 = *(char **)(0x08021640 + 4*L)
 *   把这三组指针（共 24 个）改指向本文件里的 g_s1/g_s2/g_s3，
 *   设置页就会显示我们写进去的内容 —— 走的是原厂渲染路径，必定能上屏。
 *
 * 三行 × 两页轮播，每 64 帧换一页（页面若持续重绘就能看到轮播；不重绘就只看得到第 0 页）。
 */
#include <stdint.h>
#include "board_dso153.h"

#ifndef ORIG_TASK
#define ORIG_TASK 0x08008EE0u
#endif
#define ORIG()  (((void (*)(void))(ORIG_TASK | 1u))())

void usb_poll(void);

/* 被 patch_strings.py 接进指针表的三个缓冲区（.bss） */
char g_s1[28];
char g_s2[28];
char g_s3[28];

/* usb_dwc2.c 维护的诊断快照 */
extern volatile uint32_t g_diag[20];
extern volatile uint32_t g_div, g_trdt, g_att, g_send_flags;
extern volatile uint32_t g_trace[3];

/* s_frame 已不用（不再翻页） */

/* ------------------------------------------------------------ 小工具 */

static char *s_put(char *p, const char *s)
{
    while (*s) *p++ = *s++;
    return p;
}

static char *hx(char *p, uint32_t v, int nibbles)
{
    for (int i = (nibbles - 1) * 4; i >= 0; i -= 4) {
        int d = (int)((v >> i) & 0xFu);
        *p++ = (char)(d < 10 ? '0' + d : 'A' + d - 10);
    }
    return p;
}

static char *dec(char *p, uint32_t v)
{
    char t[12];
    int n = 0;
    if (v == 0) { *p++ = '0'; return p; }
    while (v && n < 12) { t[n++] = (char)('0' + v % 10u); v /= 10u; }
    while (n) *p++ = t[--n];
    return p;
}

/* ------------------------------------------------------------ 组织文字 */
/*
 * 为什么不再分三页轮播：
 *   设置页/关于页是"进页面时画一次"的静态画面 —— 固件只在进入该页时调用一次
 *   绘制，之后不管 RAM 里的字符串怎么变都不会重画。所以 (s_frame/96)%3 这种
 *   翻页永远看不到效果，用户每次都得退出再进。
 *
 *   既然一屏就是一次快照，那就干脆把最要紧的东西全塞进同三行，进一次页面
 *   就能看全，不再有"翻到第几页"的问题。
 *
 *   L1  ST<SETUP 累计个数> PK<最后收到的包类型> X<DIEPINT>
 *   L2  RQ <最后一条 SETUP 的 bmRequestType|bRequest|wValue>
 *   L3  A<attempt> D<usbdiv> T<trdt> S<g_send_flags>
 */

/* 一条请求排成一行：<bRequest:2h> <wValue:4h> <回给主机的字节数:2h>（FF=STALL） */
static char *trace_line(char *p, uint32_t rec)
{
    p = hx(p, rec & 0xFFu, 2);                 /* bRequest */
    p = s_put(p, " ");
    p = hx(p, (rec >> 8) & 0xFFFFu, 4);        /* wValue */
    p = s_put(p, " ");
    p = hx(p, (rec >> 24) & 0xFFu, 2);         /* 应答长度 */
    return p;
}

static void build(void)
{
    char *p;

    /* 第 0 行：SETUP 数 / RX 包数 / 发送诊断位
     *   S 的位：1=已武装 2=IN XFRC（主机 ACK 了数据） 4=?
     *          8=OUT XFRC（状态阶段收到） 0x20=FIFO 满 0x40=重连过 */
    p = g_s1; p = s_put(p, "ST"); p = dec(p, g_diag[1]);
    p = s_put(p, " P"); p = dec(p, g_diag[14]);
    p = s_put(p, " S"); p = hx(p, g_send_flags & 0xFFu, 2); *p = 0;

    /* 第 1 行：DCFG 实值 + 主机复位总线的次数
     *   DCFG 正常应为 0x1B3 —— 低 2 位 3 = 全速，[10:4] = 0x1B = 主机分配的地址 27
     *   R 一直涨 = 主机在 SET_ADDRESS 之后等不到回应，超时就复位重来 */
    p = g_s2; p = s_put(p, "DF"); p = hx(p, g_diag[16] & 0xFFFFu, 3);
    p = s_put(p, " R"); p = dec(p, g_diag[17]); *p = 0;

    /* 第 2 行：最新一条请求 */
    p = g_s3; p = trace_line(p, g_trace[2]); *p = 0;
}

/* ---------------------------------------------- 示波器界面上的实时叠加 */
/*
 * 固件的显示模型（逆向 0x0801745E 那个循环 + 0x08008E64 得到的）：
 *
 *   win_x/win_y/win_w/win_h = 0x2000A568 / A56C / A570 / A574
 *   fb_ptr                  = 0x2000A578
 *
 *   每块矩形区域：malloc(w*h*2) -> 画内容 -> 0x08008E64() 逐像素推屏 -> free()
 *   画点 0x0800E680(x,y,colour)，画字 0x080102F0(x,y,str,size)。
 *
 * 所以在别处画也没用（fb 已经 free 或已推走），只能插在 0x08008E64 推屏前一刻。
 */

#define TEXT_DRAW 0x080102F0u
typedef void (*text_fn_t)(int x, int y, const char *s, int size);

#define SCR_WIN_Y (*(volatile uint16_t *)0x2000A56Cu)
#define SCR_WIN_W (*(volatile uint16_t *)0x2000A570u)
#define SCR_WIN_H (*(volatile uint16_t *)0x2000A574u)

volatile uint32_t g_band_hits;
volatile uint32_t g_band_drawn;

/* 由 band_hook 调用：此刻这块区域已经画完、正等推屏 */
void band_overlay(void)
{
    uint32_t y = SCR_WIN_Y, w = SCR_WIN_W, h = SCR_WIN_H;
    uint32_t row;

    g_band_hits++;

    /* 只认"整屏宽、26 像素高"的文本行（示波器测量值就是 8 行 y = 20 + 25i）。
     * 别的区域（小图标、按键框、波形块）一律不碰。 */
    if (w != 0x140u || h != 0x1Au) return;

    /* 只叠头三行；用 %3 会让 3/4/5 行再重复一遍 */
    row = y / 0x19u;
    if (row > 2u) return;

    build();
    g_band_drawn++;
    ((text_fn_t)(TEXT_DRAW | 1u))(190, (int)y + 5,
                                  row == 0u ? g_s1 : (row == 1u ? g_s2 : g_s3),
                                  0x10);
}

__attribute__((section(".text.band"), naked))
void band_hook(void)
{
    __asm volatile(
        "push {r7, lr}\n\t"
        "sub  sp, #8\n\t"
        "bl   band_overlay\n\t"
        "b.w  orig_band_continue\n\t");
}

/* ------------------------------------------------------------------ 钩子 */

__attribute__((section(".text.hook")))
void hook(void)
{
    /* .bss 没有启动代码去清零，所以不依赖任何初值，每帧都重建一遍 */
    usb_poll();
    build();                      /* 三条线一次写全（关于页用） */
    ORIG();                       /* 原 UI 任务（它读的正是这三个缓冲区） */
}
