#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
固件补丁静态自检。刷写前必须全部通过。

检查项：
  1. 补丁入口的跳转目标 bit0 == 1，符合 Thumb 状态要求
  2. .bss 结束地址不高于 stack_base，避免栈覆盖补丁变量
  3. 主循环钩子处的跳转目标等于补丁入口
  4. OTGFS1 向量指向补丁区且置 Thumb 位
  5. __user_setup_stackheap 的两个字面量与 stack_base 一致
  6. 补丁代码内不存在指向非法区域的绝对地址
  7. 栈剩余空间提示

用法: python tools/verify_patch.py <已打补丁的.bin> --sym <符号表>
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
HOOK_CALL_ADDR = 0x0801A7BC      # 主循环末尾（UI 渲染任务）的 bl，被替换为补丁入口
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

    # ---- 2. 补丁变量区与原厂栈是否重叠 ----
    stack_base = struct.unpack_from("<I", d, 0x08007394 - BASE)[0]
    text_end = None                      # .text 结束（__data_lma），限定第 6 项扫描范围
    if a.sym:
        ram = []
        for line in open(a.sym):
            p = line.split()
            if len(p) == 3:
                if p[2] == "__data_lma":
                    text_end = int(p[0], 16)
                if p[1] in ("b", "B", "d", "D") \
                        and not p[2].startswith("__fault_rec"):
                    ram.append((int(p[0], 16), p[2]))
        if ram:
            lo = min(ram)[0]
            hi_addr, hi_name = max(ram)
            # 末符号的结束地址无法从符号表得知，保守取 +64（数据缓冲量级）
            hi = hi_addr + (64 if ("tmp" in hi_name or "buf" in hi_name) else 4)
            chk(hi <= stack_base or lo >= 0x2000B580,
                "补丁变量区 0x%08X..0x%08X 与栈 0x%08X..0x2000B580 不重叠"
                % (lo, hi, stack_base),
                "补丁变量区 0x%08X..0x%08X 与栈 0x%08X..0x2000B580 重叠 —— 栈会覆盖补丁变量"
                % (lo, hi, stack_base))
            chk(hi <= 0x2000C000,
                "补丁变量区上界 0x%08X 未越界" % hi,
                "补丁变量区上界 0x%08X 越界" % hi)
            chk(stack_base >= 0x2000A580,
                "stack_base 0x%08X 仍然在合法 RAM 内" % stack_base,
                "stack_base 0x%08X 异常" % stack_base)
            used = 0x2000B580 - stack_base
            if used < 3000:
                warn.append("栈剩余 %d 字节（原 4096），需确认原固件实际栈用量" % used)
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

    # ---- 3b. push_band 画面流钩子 ----
    img_addr = None
    if a.sym:
        for line in open(a.sym):
            p = line.split()
            if len(p) >= 3 and p[2] == "img_hook":
                img_addr = int(p[0], 16)
    if img_addr:
        soff = 0x08008E64 - BASE
        ii = list(md.disasm(d[soff:soff + 4], 0x08008E64))
        stgt = ii[0].operands[0].imm if ii and ii[0].mnemonic in ("b", "b.w") else None
        if stgt == img_addr:
            chk(True, "push_band 0x08008E64 -> b.w img_hook (0x%08X)" % img_addr, "")
            # 跳板必须补执行被替换的两条序言，并回到函数体 0x08008E68
            scode = list(md.disasm(d[img_addr - BASE:img_addr - BASE + 16], img_addr))
            seq = [i.mnemonic for i in scode[:4]]
            chk(len(scode) >= 4 and seq[:3] == ["push", "sub", "bl"]
                and scode[3].mnemonic in ("b", "b.w")
                and scode[3].operands[0].imm == 0x08008E68,
                "img_hook 序言 push/sub/bl + b.w 0x08008E68",
                "img_hook 序言异常：%s" % " ".join(seq))
        elif struct.unpack_from("<HH", d, soff) == (0xB580, 0xB082):
            warn.append("push_band 未装画面流钩子（对照版 --no-img）")
        else:
            chk(False, "", "push_band 0x08008E64 跳转目标异常：%s"
                % (hex(stgt) if stgt else "解析失败"))
    elif struct.unpack_from("<HH", d, 0x08008E64 - BASE) != (0xB580, 0xB082):
        warn.append("push_band 序言 0x08008E64 已改动，但补丁无 img_hook 符号")

    # ---- 4. USB 向量 ----
    v = struct.unpack_from("<I", d, VECTOR_ADDR - BASE)[0]
    if v == 0 or (0x08007000 <= (v & ~1) < STUB_ADDR):
        warn.append("OTGFS1 向量(IRQ%d) 未安装（值 0x%08X），该固件不含 USB 中断" % (USB_IRQN, v))
    else:
        chk((v & 1) == 1 and STUB_ADDR <= (v & ~1) < STUB_ADDR + 0x2000,
            "OTGFS1 向量 -> 0x%08X（Thumb、指向补丁区）" % v,
            "OTGFS1 向量 0x%08X 异常" % v)

    # ---- 4b. HardFault 捕获向量 ----
    hv = struct.unpack_from("<I", d, 0x0800700C - BASE)[0]
    if hv == 0 or (0x08007000 <= (hv & ~1) < STUB_ADDR):
        warn.append("HardFault 向量未安装（值 0x%08X）：异常仍会停在原厂默认向量里" % hv)
    else:
        chk((hv & 1) == 1 and STUB_ADDR <= (hv & ~1) < STUB_ADDR + 0x2000,
            "HardFault 向量 -> 0x%08X（Thumb、指向补丁区）" % hv,
            "HardFault 向量 0x%08X 异常" % hv)

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
    # 只扫 .text：.rodata（USB 描述符、logo 掩码等）里的字被当指令解码
    # 会产生伪 "可疑地址"。
    if text_end:
        code = d[STUB_ADDR - BASE: text_end - BASE]
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
            # Thumb 的 PC 相对寻址以 Align(PC,4) 为基址：PC = 当前指令地址 + 4，
            # 再向下取整到 4 字节。少了对齐会读到错位的指令字节，
            # 把 0xE000ED2C 这类 PPB 常量误读成 0xED2CE000。
            ad = ((ins.address + 4) & ~3) + ops[-1].mem.disp
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
                # 片内 SRAM 全域：0x08 的命令放行到 0x2005FFFF，SRAM 上界探测
                # （命令 0x0B）也会把这段区间里的地址折成常量，如 0x2000FFFC
                or 0x20000000 <= val < 0x20060000
                or val == 0x20060000                    # 0x08 的 SRAM 边界常量
                or 0x40000000 <= val < 0x50010000
                or 0xE000E000 <= val < 0xE0010000
                or 0x08000000 <= val < 0x08040000)
        if not good:
            suspicious.append((ins.address, val))
    chk(not suspicious,
        "补丁代码内的绝对地址均落在合法区域",
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
        print("\n结论：自检未通过，停止刷写。")
        return 1
    print("\n结论：静态检查全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
