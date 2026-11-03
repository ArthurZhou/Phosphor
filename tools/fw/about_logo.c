/*
 * 关于页 Phosphor logo（位平面由 phosphor-logo-horizontal-balanced.svg
 * 直接光栅化生成，管线 tools/build/tmp/gen_about_logo_bal.py）。
 *
 * 挂接方式：patch.py 把关于页 FNIRSI 位图绘制指令（0x0800DEBE 处的
 * bl 0x0800EA48，2bpp 位图 @0x080317CA 120x20）替换为 bl hook_about。
 * hook_about（hook_about.S）直接画我们的 logo 后返回 —— 原 FNIRSI
 * 位图不再绘制，关于页其余内容（边框、版本号、型号、公司信息）不受影响。
 *
 * 尺寸：168x54，居中于关于页右栏（栏区约 x=133..306）。
 * 配色：P 标志 + wordmark = 白（SVG 深灰在深色屏上的转译），
 *       绿点 = 品牌绿 #0EF395 的 RGB565 (0x0F92)。
 */
#include <stdint.h>

#include "logo_wide.h"

#define LOGO_X 135
#define LOGO_Y 50

#define MAIN_COLOR 0xFFFFu          /* P 标志 + wordmark：白 */
#define DOT_COLOR  0x0F92u          /* 绿点：#0EF395 */

#define PUT_PIXEL_ADDR 0x0800E680u
typedef void (*put_pixel_t)(int16_t x, int16_t y, uint16_t c);
#define put_pixel ((put_pixel_t)(PUT_PIXEL_ADDR | 1u))

/* blit_logo 不能是 static：hook_about.S 以符号引用 */
__attribute__((noinline))
void blit_logo(void)
{
    for (int y = 0; y < LOGO_H; y++) {
        const uint8_t *mr = logo_wide + y * LOGO_WB;       /* P+wordmark */
        const uint8_t *dr = logo_wide_dot + y * LOGO_WB;   /* 绿点 */
        int16_t py = LOGO_Y + y;
        for (int x = 0; x < LOGO_W; x++) {
            uint8_t bit = (uint8_t)(0x80u >> (x & 7));
            if (dr[x >> 3] & bit)
                put_pixel(LOGO_X + x, py, DOT_COLOR);
            else if (mr[x >> 3] & bit)
                put_pixel(LOGO_X + x, py, MAIN_COLOR);
        }
    }
}

/* 调用约定 thunk 在 hook_about.S：blit_logo 后直接返回（跳过原位图） */
