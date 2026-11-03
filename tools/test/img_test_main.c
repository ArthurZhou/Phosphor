/*
 * img.c 的宿主机测试驱动（纯 C，无 wasm / node）
 *
 * 与 tools/fw/img.c 同编成本机可执行文件，跑的是真实代码，不是模型：
 *   设窗口 -> img_send_band() -> 桩函数把字节攒进 outbuf -> main 落盘
 * 再由 tools/test/img_check.py 用生产用的解析器拼回画面逐像素比对。
 *
 * 向 stdout 打印一行 JSON 统计（供 img_check.py 断言），字节流写到
 * argv[1]（默认 tools/test/stream.bin）。测试场景：
 *   A. 未武装推屏 -> 不产生字节
 *   B. 武装 20 块，整屏 10 条 24 行（自下而上）+ 一条半宽（208）右面板
 *   C. 预算 2 块 -> 推到第 3 次不再有数据
 *   D. 非法窗口（w=0）-> 拒绝且 img_err = 2
 *   E. 页面号 0..9 -> img_serve_redraw 按原厂查表投递的绘制单元序列
 */
#include <stdio.h>
#include <stdint.h>

typedef uint32_t u32;
typedef uint16_t u16;
typedef uint8_t  u8;

u16 t_win_x, t_win_y, t_win_w, t_win_h;
uintptr_t t_win_fb;

#define OUT_CAP (200u * 1024u)
static u8  outbuf[OUT_CAP];
static u32 out_len;

/* ---- USB 桩 ---- */
static u32 seq;

int usb_tx_free(void) { return 1; }
int usb_tx_ready(void) { return 1; }
u16 usb_seq_next(void) { return (u16)seq++; }

int usb_img_packet(const u8 *p, u16 len)
{
    u32 i;
    if (out_len + len > OUT_CAP) return 0;
    for (i = 0u; i < len; i++) outbuf[out_len++] = p[i];
    return 1;
}

/* ---- img.c 接口 ---- */
void img_arm(u32 bands);
void img_disarm(void);
void img_send_band(void);
void img_serve_redraw(void);
extern volatile u32 img_state, img_bands, img_rows, img_budget, img_err;

/* ---- 页面重绘：队列桩 ----
 * img.c 在 IMG_HOST_TEST 下把 enqueue 换成 t_enq，页面号换成 t_ui_mode，
 * 因此这里跑的是固件里同一份查表逻辑。 */
u8 t_ui_mode;

static u8  qbuf[16];
static u32 qlen;

void t_enq(u32 id)
{
    if (qlen < sizeof(qbuf)) qbuf[qlen++] = (u8)id;
}

static void redraw(u32 mode)
{
    t_ui_mode = (u8)mode;
    qlen = 0u;
    img_arm(8u);            /* 武装会置 img_need_full */
    img_serve_redraw();
}

/* ---- 测试数据 ---- */
static u16 screen[240][320];
static u16 band[320 * 32];

static void setup(void)
{
    int x, y;
    for (y = 0; y < 240; y++)
        for (x = 0; x < 320; x++)
            screen[y][x] = (u16)(((x & 31) << 11) | ((y & 63) << 5) | ((x + y) & 31));
}

static void push_rect(int x, int y, int w, int h)
{
    int i, j;
    for (j = 0; j < h; j++)
        for (i = 0; i < w; i++)
            band[j * w + i] = screen[y + j][x + i];
    t_win_x = (u16)x;
    t_win_y = (u16)y;
    t_win_w = (u16)w;
    t_win_h = (u16)h;
    t_win_fb = (uintptr_t)band;
    img_send_band();
}

int main(int argc, char **argv)
{
    const char *out_path = (argc > 1) ? argv[1] : "tools/test/stream.bin";
    FILE *f;
    u32 idle, main_len, budget_len, bad_len, err_before, bad_err;
    int y, i, m;
    u32 k;

    setup();

    /* A. 未武装：推屏不应产生任何字节 */
    push_rect(0, 100, 320, 24);
    idle = out_len;

    /* B. 武装 20 块，整屏 10 条 24 行（自下而上，与真机 UI 绘制顺序一致），
     *    再加一条只有右面板的半行 */
    img_arm(20u);
    for (y = 216; y >= 0; y -= 24) push_rect(0, y, 320, 24);
    push_rect(112, 0, 208, 24);
    main_len = out_len;

    f = fopen(out_path, "wb");
    if (f == NULL)
    {
        fprintf(stderr, "写 %s 失败\n", out_path);
        return 2;
    }
    fwrite(outbuf, 1u, main_len, f);
    fclose(f);

    /* C. 预算 2 块：推到第 3 次就不该再有数据 */
    out_len = 0u;
    img_arm(2u);
    for (i = 0; i < 5; i++) push_rect(0, i * 24, 320, 24);
    budget_len = out_len;

    /* D. 非法窗口：宽度为 0，应被拒绝且不产生字节 */
    out_len = 0u;
    img_arm(4u);
    err_before = img_err;
    push_rect(0, 0, 0, 24);
    bad_len = out_len;
    bad_err = img_err;      /* 后面 redraw 会重新 arm，务必在此处取走 */

    /* E. 页面重绘：固件按 UI 页面号查表决定投哪些绘制单元。
     *    这里跑的是 img.c 里同一份查表逻辑（t_enq 只是把入队记下来）。 */
    printf("{\"idle_bytes\":%u,\"main_bytes\":%u,\"budget_bytes\":%u,"
           "\"bad_bytes\":%u,\"bad_err\":%u,\"err_before_bad\":%u,"
           "\"bands\":%u,\"pages\":{",
           idle, main_len, budget_len, bad_len, bad_err, err_before, img_bands);
    for (m = 0; m <= 9; m++)
    {
        redraw((u32)m);
        printf("%s\"%d\":[", m ? "," : "", m);
        for (k = 0u; k < qlen; k++)
            printf("%s%u", k ? "," : "", (u32)qbuf[k]);
        printf("]");
    }
    printf("}}\n");
    return 0;
}
