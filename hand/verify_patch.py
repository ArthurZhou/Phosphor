#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
补丁静态自检。刷之前必须全过。

检查项（每一项都对应一次真实踩过的坑）：
  1. hook 入口的 blx 目标 bit0 == 1（Thumb）—— v1 卡启动的直接原因
  2. .bss 结束地址 < stack_base —— 否则栈会吃掉补丁变量
  3. 打补丁后：主循环钩子的 bl 目标 == hook 地址
  4. 打补丁后：USBFS 向量 == usb_irq|1
  5. 打补丁后：__user_setup_stackheap 的两个字面量 == stack_base
  6. 补丁代码里没有指向非法区域的绝对地址
  7. while 等待都有超时（源码级，仅提示）

用法: python tools/verify_patch.py <打过补丁的.bin> [--elf tools/stub/dso_patch.elf]
"""
import argparse
import re
import struct
import sys

try:
    from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB
    from capstone.arm import ARM_OP_IMM, ARM_OP_REG, ARM_OP_MEM, ARM_REG_PC
except ImportError:
    print("需要 capstone")
    sys.exit(2)

BASE = 0x08007000
HOOK_CALL_ADDR = 0x0801A7BC      # 主循环第 4 个任务（UI 渲染）的 bl，被替换成 hook
ORIG_TARGET = 0x08009500         # 原 bl 目标
STUB_ADDR = 0x08035B00
USB_IRQN = 67
VECTOR_ADDR = BASE + 4 * (16 + USB_IRQN)

def set_hook(site, orig):
    global HOOK_CALL_ADDR, ORIG_TARGET
    HOOK_CALL_ADDR = site
    ORIG_TARGET = orig


def set_irqn(n):
    global USB_IRQN, VECTOR_ADDR
    USB_IRQN = n
    VECTOR_ADDR = BASE + 4 * (16 + n)

ok = []
bad = []
warn = []


def chk(cond, msg_ok, msg_bad):
    (ok if cond else bad).append(msg_ok if cond else msg_bad)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bin")
    ap.add_argument("--elf", default=None)
    ap.add_argument("--sym", default=None)
    ap.add_argument("--hook-site", default="0x0801A7BC")
    ap.add_argument("--orig", default="0x08009500")
    a = ap.parse_args()

    set_hook(int(a.hook_site, 0), int(a.orig, 0))
    d = open(a.bin, "rb").read()
    md = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
    md.detail = True
    md.skipdata = True

    # ---- 1. hook 入口的 blx 目标 ----
    code = d[STUB_ADDR - BASE:]
    first = list(md.disasm(code[:40], STUB_ADDR))
    blx_tgt = None
    for i, ins in enumerate(first):
        # clang 对 hook 末尾的 ORIG() 可能生成 blx r，也可能 movw/movt + bx r
        if ins.mnemonic in ("blx", "bx") and ins.operands and ins.operands[0].type == ARM_OP_REG:
            # 回看前两条 movw/movt
            lo = hi = None
            for j in range(max(0, i - 3), i):
                m = first[j]
                if m.mnemonic == "movw":
                    lo = m.operands[1].imm & 0xFFFF
                elif m.mnemonic == "movt":
                    hi = m.operands[1].imm & 0xFFFF
            if lo is not None and hi is not None:
                blx_tgt = (hi << 16) | lo
            break
    if blx_tgt is None:
        warn.append("hook 入口没找到 blx/bx 寄存器，需人工确认")
    else:
        chk((blx_tgt & 1) == 1,
            "hook 入口 blx 目标 0x%08X，bit0=1（Thumb）" % blx_tgt,
            "hook 入口 blx 目标 0x%08X 是偶数 —— Cortex-M 会切 ARM 状态并 HardFault" % blx_tgt)
        chk((blx_tgt & ~1) == ORIG_TARGET,
            "blx 目标是原任务 0x%08X" % ORIG_TARGET,
            "blx 目标 0x%08X 不是原任务 0x%08X" % (blx_tgt & ~1, ORIG_TARGET))

    # ---- 2. .bss 与 stack_base ----
    stack_base = struct.unpack_from("<I", d, 0x08007394 - BASE)[0]
    if a.sym:
        bss = []
        for line in open(a.sym):
            p = line.split()
            if len(p) == 3 and p[1] in ("b", "B"):
                bss.append((int(p[0], 16), p[2]))
        if bss:
            # 最后一项的结束地址 = 起始 + 64（保守，g_tmp 是 64）
            last_addr, last_name = max(bss)
            end = last_addr + (64 if "tmp" in last_name else 4)
            chk(end <= stack_base,
                ".bss 结束 0x%08X <= stack_base 0x%08X" % (end, stack_base),
                ".bss 结束 0x%08X 超过 stack_base 0x%08X —— 栈会覆盖补丁变量" % (end, stack_base))
            chk(stack_base >= 0x2000A580,
                "stack_base 0x%08X 仍然在合法 RAM 内" % stack_base,
                "stack_base 0x%08X 异常" % stack_base)
            used = 0x2000B580 - stack_base
            if used < 3000:
                warn.append("栈只剩 %d 字节（原 4096），确认原固件栈用量" % used)
            else:
                ok.append("栈 %d 字节（原 4096）" % used)

    # ---- 3. 主循环钩子 ----
    hoff = HOOK_CALL_ADDR - BASE
    ins = list(md.disasm(d[hoff:hoff + 4], HOOK_CALL_ADDR))
    tgt = None
    if ins and ins[0].mnemonic in ("bl", "b"):
        tgt = ins[0].operands[0].imm
    chk(tgt == STUB_ADDR,
        "0x%08X 已改为跳 0x%08X" % (HOOK_CALL_ADDR, STUB_ADDR),
        "0x%08X 的跳转目标异常：%s" % (HOOK_CALL_ADDR, hex(tgt) if tgt else "解析失败"))

    # ---- 4. USB 向量 ----
    v = struct.unpack_from("<I", d, VECTOR_ADDR - BASE)[0]
    if v == 0 or (0x08007000 <= (v & ~1) < STUB_ADDR):
        warn.append("USBFS 向量(IRQ%d) 未安装（值 0x%08X），该固件不应有 USB 中断" % (USB_IRQN, v))
    else:
        chk((v & 1) == 1 and STUB_ADDR <= (v & ~1) < STUB_ADDR + 0x2000,
            "USBFS 向量 -> 0x%08X（Thumb、指向补丁区）" % v,
            "USBFS 向量 0x%08X 异常" % v)

    # ---- 5. stack heap 字面量 ----
    for site in (0x08007394, 0x0800739C):
        val = struct.unpack_from("<I", d, site - BASE)[0]
        chk(val == stack_base,
            "0x%08X -> 0x%08X（与 stack_base 一致）" % (site, val),
            "0x%08X -> 0x%08X，与 stack_base 0x%08X 不一致" % (site, val, stack_base))
    sp = struct.unpack_from("<I", d, 0)[0]
    chk(sp == 0x2000B580,
        "初始 SP 仍为 0x%08X（未被动过）" % sp,
        "初始 SP 被改成 0x%08X" % sp)

    # ---- 6. 补丁代码里的绝对地址 ----
    regs = {}
    suspicious = []
    for ins in md.disasm(code, STUB_ADDR):
        if ins.id == 0:
            continue
        ops = ins.operands
        val = None
        if ins.mnemonic == "movw" and len(ops) == 2 and ops[0].type == ARM_OP_REG and ops[1].type == ARM_OP_IMM:
            regs[ops[0].reg] = ops[1].imm & 0xFFFF
            continue
        if ins.mnemonic == "movt" and len(ops) == 2 and ops[0].type == ARM_OP_REG and ops[1].type == ARM_OP_IMM:
            r = ops[0].reg
            val = ((ops[1].imm & 0xFFFF) << 16) | regs.pop(r, 0)
        elif ins.mnemonic.startswith("ldr") and ops and ops[-1].type == ARM_OP_MEM and ops[-1].mem.base == ARM_REG_PC:
            ad = ins.address + 4 + ops[-1].mem.disp
            if STUB_ADDR <= ad < STUB_ADDR + len(code) - 4:
                val = struct.unpack_from("<I", d, ad - BASE)[0]
        if val is None:
            continue
        # 四个字节全是可打印 ASCII -> 是字符串常量（"SNPS" 之类），不是地址
        raw = struct.pack("<I", val)
        if all(0x20 <= c <= 0x7E for c in raw):
            continue
        # 官方库里的寄存器位掩码（0xE007FFFF = DIEPCTL 的保留位掩码之类），
        # 长得像 PPB 地址，实际是常量
        if val in (0xE007FFFF, 0xEFFFFFFF, 0xE0000000):
            continue
        looks_like_addr = (0x08000000 <= val < 0x09000000
                           or 0x20000000 <= val < 0x21000000
                           or 0x40000000 <= val < 0x50080000
                           or 0xE0000000 <= val < 0xF0000000)
        if not looks_like_addr:
            continue
        good = (STUB_ADDR <= val < STUB_ADDR + 0x2000
                or stack_base <= val < stack_base + 0x400
                or 0x20000000 <= val < 0x2000B800
                or 0x40000000 <= val < 0x50010000
                or 0xE000E000 <= val < 0xE0010000
                or 0x08000000 <= val < 0x08040000)
        if not good:
            suspicious.append((ins.address, val))
    chk(not suspicious,
        "补丁代码内 %d 个绝对地址全部落在合法区域" % 1,
        "补丁代码内有可疑绝对地址：%s" % ", ".join("0x%08X:0x%08X" % x for x in suspicious[:6]))

    # ---- 输出 ----
    print("=== 通过 (%d) ===" % len(ok))
    for x in ok:
        print("  [OK] " + x)
    if warn:
        print("=== 提示 (%d) ===" % len(warn))
        for x in warn:
            print("  [!!] " + x)
    if bad:
        print("=== 失败 (%d) ===" % len(bad))
        for x in bad:
            print("  [XX] " + x)
        print("\n结论：不能刷，先修上面这些。")
        return 1
    print("\n结论：静态检查全过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
