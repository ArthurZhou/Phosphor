@ hook_about —— 关于页 logo 钩子（汇编 thunk）
@
@ patch.py 把关于页 FNIRSI 位图绘制（0x0800DEBE 处 bl 0x0800EA48）
@ 替换为 bl hook_about。本 thunk 画我们的 logo 后直接返回：
@ 原位图不再绘制，关于页后续绘制流程不受影响。

.syntax unified
.thumb

.section .text.hook_about, "ax", %progbits
.balign 4
.global hook_about
.type hook_about, %function
hook_about:
    push {r4, lr}
    bl   blit_logo
    pop  {r4, pc}
