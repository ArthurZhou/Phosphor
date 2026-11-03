/*
 * push_band 入口跳板
 *
 * push_band（0x08008E64）的序言是两条 16 位指令：
 *     0x08008E64  push {r7, lr}      0xB580
 *     0x08008E66  sub  sp, #8        0xB082
 * 共 4 字节，正好放一条 b.w。补丁把这两条替换为 b.w img_hook，跳板先补执行
 * 被替换的序言（保持栈布局，函数体末尾的 add sp,#8 / pop {r7,pc} 才配得上），
 * 再调用 img_send_band 把当前区域送出，最后回到函数体 0x08008E68 继续。
 *
 * 为什么可以不管 r0-r3：函数体第一条指令就是 movw r0,#0xa568，调用者传入的
 * 寄存器在这里已经作废。
 *
 * push_body 由链接脚本赋值（汇编期地址未知，写死立即数会越界）。
 */
    .syntax unified
    .cpu cortex-m4
    .thumb

    .section .text.img_hook, "ax", %progbits
    .global img_hook
    .type   img_hook, %function
    .thumb_func
img_hook:
    push    {r7, lr}
    sub     sp, #8
    bl      img_send_band
    b.w     push_body
    .size   img_hook, . - img_hook
