/*
 * Phosphor 固件主循环钩子
 *
 * 补丁以 B.W 指令替换 APP 主循环中 0x0801A7BC 处的调用（原目标 0x08008EE0，
 * UI 渲染任务）。本函数每轮先执行 USB 轮询，再调用原渲染任务，最后在
 * 整帧之上叠加扁平风格的分割细线（fill_rect 代价极低，整帧后置绘制
 * 保证不被本帧 UI 覆盖）。
 *
 * 约束：
 *   1. 被替换的原指令是 BL，主循环依赖其返回地址继续执行。此处以普通
 *      BL 调用原任务（编译器自动 push/pop lr），返回地址不变。
 *   2. 补丁不含启动代码，全局数据的搬迁与清零由 usb_poll() 首次调用完成。
 */
#include <stdint.h>

#define ORIG_TASK  0x08008EE0u
#define FILL_RECT  0x0800E73Cu   /* fill_rect(x1,y1,x2,y2,color) */

/* 扁平分割线配色：与未选中卡片同灰 #39424A，克制、不抢波形 */
#define RULE_C     0x29A7u

static const struct rule {
    int16_t x1, y1, x2, y2;
} rules[] = {
    {   0,  20, 319,  20 },   /* 标题栏下水平细线 */
    { 112,  21, 112, 219 },   /* 波形区 | 右面板 竖向细线 */
    { 140, 118, 305, 118 },   /* 右面板分段线 1（原斜纹条1 处） */
    { 140, 174, 305, 174 },   /* 右面板分段线 2（原斜纹条2 处） */
};

static void draw_rules(void)
{
    void (*fill_rect)(int, int, int, int, uint16_t) =
        (void (*)(int, int, int, int, uint16_t))(FILL_RECT | 1u);

    /* 页面号 0x20004FCC：3=信号发生器、4=设置——这两页的整宽标题栏
     * 高 31px（原厂渐变角板已被补丁移除，只留标题文字），分隔线画在
     * y=31；其余页面（含示波器主界面）沿用 20px 标题区的布局。 */
    uint8_t page = *(volatile const uint8_t *)0x20004FCCu;
    if (page == 3u || page == 4u) {
        fill_rect(0, 31, 319, 31, RULE_C);
        return;
    }

    for (unsigned i = 0; i < sizeof(rules) / sizeof(rules[0]); i++)
        fill_rect(rules[i].x1, rules[i].y1, rules[i].x2, rules[i].y2, RULE_C);
}

void usb_poll(void);
void phosphor_param_poll(void);
void img_serve_redraw(void);

__attribute__((section(".text.hook")))
void hook(void)
{
    usb_poll();
    /* 屏幕画面抓取武装后，按当前 UI 页面号排一次页面重绘（img.c）。
     * 必须在 ui_drain 之后、下一轮 ui_drain 之前完成——本钩子正处在这个
     * 位置：主循环 0x0801A7B4 先 drain，0x0801A7BC 才进到这里。 */
    img_serve_redraw();
    ((void (*)(void))(ORIG_TASK | 1u))();
    draw_rules();
    /* 必须放在 UI 任务之后：主循环下一轮开头就是按键任务，此时页面号 / 子
     * 选择刚被强制成目标参数的页面，注入的键码一定会作用到正确的参数上。
     * 放在 usb_poll() 里（UI 任务之前）会被本帧渲染改动页面，导致改错参数。 */
    phosphor_param_poll();
}
