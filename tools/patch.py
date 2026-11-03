#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phosphor 固件补丁工具（原厂 APP v1.6 镜像）

镜像布局：
    0x08000000 - 0x08005FFF  IAP bootloader            不修改
    0x08006000 - 0x08006FFF  设置页（版本 / 语言 / 校准）  不修改
    0x08007000 - 0x08035AFF  APP 镜像（191232 字节）      仅修改下列位置
    0x08035B00 - 0x0803FFFF  空闲 Flash，存放补丁代码

补丁动作：
    1. 0x0801A7BC 处的 bl 改为跳转到补丁入口 hook（主循环末尾，UI 渲染任务）
    2. OTGFS1 中断向量（第 67 项，0x0800714C）指向补丁的 usb_irq
    3. 保留 __user_setup_stackheap 的 stack_base 为原值 0x2000A580，不再借用
       栈空间：补丁的 .data / .bss 位于初始 SP（0x2000B580）之上的 0x2000B800
    4. 在 APP 镜像末尾追加补丁代码

用法：
    python tools/patch.py <输入.bin> -o <输出.bin> --stub <补丁.bin> --sym <符号表>
"""
import argparse
import hashlib
import struct
import sys

BASE = 0x08007000
APP_SIZE = 0x2EB00
APP_END = BASE + APP_SIZE          # 0x08035B00
STUB_ADDR = APP_END

HOOK_SITE = 0x0801A7BC
ORIG_CALL = 0x08008EE0

# push_band（0x08008E64）序言的前两条 16 位指令：push {r7, lr} + sub sp, #8，
# 共 4 字节，替换为 b.w img_hook。跳板补执行这两条后调用 img_send_band，
# 再回到 0x08008E68 继续原函数体。--no-img 可生成不含该钩子的对照版。
IMG_SITE = 0x08008E64
IMG_ORIG = (0xB580, 0xB082)

# 关于页 FNIRSI logo 位图绘制（bl 0x0800EA48，2bpp 位图 120x20）的位置，
# 替换为 bl hook_about（画 Phosphor logo 后直接返回，原位图不再绘制）。
ABOUT_SITE = 0x0800DEBE
ABOUT_ORIG = 0x0800EA48

# __user_setup_stackheap @0x08007380 返回 r0..r3：
#   r0 = [0x08007394] = 0x2000A580  stack_base
#   r1 = [0x08007398] = 0x2000B580  stack_limit（初始 SP，不修改）
#   r2 = [0x08007394]               heap_base（与 r0 共用同一字面量）
#   r3 = [0x0800739C] = 0x2000A580  heap_limit
# 原厂固件 heap_base == heap_limit == stack_base，即未使用堆，字面量保持原值
# 即为“不借栈”。补丁变量区改到 0x2000B800（初始 SP 0x2000B580 之上），见
# docs/DEVELOPMENT.md 第 4.2 节与 link.ld。
HEAP_SITES = (0x08007394, 0x0800739C)
HEAP_OLD = 0x2000A580
HEAP_NEW = 0x2000A580               # 与 HEAP_OLD 相同 = 不改动栈/堆边界

USB_IRQN = 67                        # OTG1；OTG2 为 77，可用 --irqn 覆盖
VECTOR_ADDR = BASE + 4 * (16 + USB_IRQN)

# HardFault 是异常 3，向量表第 4 项。原厂固件没有看门狗，异常会停在默认向量
# 里（b .）；改为 fault_stub 后可记录现场并软件复位。
FAULT_VECTOR_ADDR = BASE + 4 * 3


def off(addr):
    return addr - BASE


def bl(from_addr, to_addr):
    """编码一条 Thumb-2 BL 指令。"""
    return _branch(from_addr, to_addr, link=True)


def bw(from_addr, to_addr):
    """编码一条 Thumb-2 B.W 指令（不写链接寄存器）。"""
    return _branch(from_addr, to_addr, link=False)


def _branch(from_addr, to_addr, link):
    pc = from_addr + 4
    delta = to_addr - pc
    if delta % 2 or not -(1 << 24) <= delta < (1 << 24):
        raise ValueError("跳转超范围: %x -> %x" % (from_addr, to_addr))
    s = (delta >> 24) & 1
    i1 = (delta >> 23) & 1
    i2 = (delta >> 22) & 1
    imm10 = (delta >> 12) & 0x3FF
    imm11 = (delta >> 1) & 0x7FF
    j1 = ((i1 ^ s) ^ 1) & 1
    j2 = ((i2 ^ s) ^ 1) & 1
    hw2_top = 0xD000 if link else 0x9000
    return struct.pack('<HH',
                       0xF000 | (s << 10) | imm10,
                       hw2_top | (j1 << 13) | (j2 << 11) | imm11)


def read_syms(path):
    """解析 llvm-nm 输出，例如 '08035b00 T hook'。"""
    out = {}
    for line in open(path, encoding='utf-8'):
        parts = line.split()
        if len(parts) >= 3:
            try:
                out[parts[2]] = int(parts[0], 16)
            except ValueError:
                pass
    return out


def decode_bl(data, addr):
    """解析 addr 处一条 Thumb-2 BL 的目标地址，失败返回 None。"""
    hw1, hw2 = struct.unpack_from('<HH', data, off(addr))
    if (hw1 & 0xF800) != 0xF000 or (hw2 & 0xD000) != 0xD000:
        return None
    s = (hw1 >> 10) & 1
    i1 = (hw2 >> 13) & 1
    i2 = (hw2 >> 11) & 1
    imm10 = hw1 & 0x3FF
    imm11 = hw2 & 0x7FF
    j1 = (i1 ^ s) ^ 1
    j2 = (i2 ^ s) ^ 1
    delta = (s << 24) | (j1 << 23) | (j2 << 22) | (imm10 << 12) | (imm11 << 1)
    if s:
        delta -= (1 << 25)
    return addr + 4 + delta


def rebuild(src, dst, stub, syms=None, vector=True, ram=True, irqn=67,
            hook_site=HOOK_SITE, fault=True, about_site=ABOUT_SITE,
            img=True):
    global VECTOR_ADDR, HOOK_SITE, ORIG_CALL
    VECTOR_ADDR = BASE + 4 * (16 + irqn)
    HOOK_SITE = hook_site
    data = bytearray(open(src, 'rb').read())
    if len(data) != APP_SIZE:
        print('[!] 输入大小 %d，预期 %d' % (len(data), APP_SIZE))

    orig = decode_bl(data, hook_site)
    if orig is not None:
        ORIG_CALL = orig
        print('[i] 钩子点 0x%08X 原调用目标 0x%08X' % (hook_site, orig))

    hook_addr = (syms or {}).get('hook', STUB_ADDR)
    irq_addr = (syms or {}).get('usb_irq')
    irq_addr_fault = (syms or {}).get('fault_stub')
    about_addr = (syms or {}).get('hook_about')

    # 1) 主循环钩子
    data[off(hook_site):off(hook_site) + 4] = bl(hook_site, hook_addr)

    # 1b) 关于页 logo 钩子：替换版本行的 bl draw_text
    if about_site and about_addr:
        # 先核实原指令确实是 bl 0x0800EA48（FNIRSI logo 位图）
        orig_about = decode_bl(data, about_site)
        if orig_about != ABOUT_ORIG:
            print('[!] 关于页钩子点 0x%08X 原目标为 %s，预期 0x%08X（FNIRSI logo）'
                  % (about_site,
                     '0x%08X' % orig_about if orig_about else '非 BL', ABOUT_ORIG))
            print('    已跳过 logo 钩子，固件其余部分不受影响')
        else:
            data[off(about_site):off(about_site) + 4] = bl(about_site, about_addr)
            print('[i] 关于页 logo 钩子 0x%08X -> 0x%08X' % (about_site, about_addr))
    elif about_site:
        print('[i] 补丁无 hook_about 符号，跳过 logo 钩子')

    # 1b2) push_band 入口：push {r7,lr} + sub sp,#8 -> b.w img_hook
    #      画面流的数据源。钩子未启用时 img_send_band() 第一条判断就返回，
    #      对渲染路径的影响仅为一次调用开销。
    img_addr = (syms or {}).get('img_hook')
    if img and img_addr:
        got = struct.unpack_from('<HH', data, off(IMG_SITE))
        if got != IMG_ORIG:
            print('[!] push_band 入口 0x%08X 原指令为 %s，预期 %s，跳过画面流钩子'
                  % (IMG_SITE,
                     '(%s, %s)' % tuple(hex(x) for x in got),
                     '(%s, %s)' % tuple(hex(x) for x in IMG_ORIG)))
        else:
            data[off(IMG_SITE):off(IMG_SITE) + 4] = bw(IMG_SITE, img_addr)
            print('[i] push_band 0x%08X -> 0x%08X（b.w img_hook）'
                  % (IMG_SITE, img_addr))
    elif img:
        print('[i] 补丁无 img_hook 符号，跳过画面流钩子')

    # 1c) 底部卡片未选中色：0xBDF7(浅灰#BDBEBD) -> 0x3A09(暗灰#39424A)。
    # 选中卡片用调色板次级色（原厂亮黄，现为主题亮灰），未选中是写死的
    # 常量。次级色调成灰后两者几乎无法区分（R9 实机反馈），把未选中压暗。
    CARD_UNSEL_SITE = 0x0800C424        # movw r6, #0xBDF7
    CARD_UNSEL_OLD = 0xBDF7
    CARD_UNSEL_NEW = 0x29A7       # #283438，R14 起整体调暗一档
    hw1, hw2 = struct.unpack_from('<HH', data, off(CARD_UNSEL_SITE))
    rd = (hw2 >> 8) & 0xF
    imm4 = hw1 & 0xF
    i_bit = (hw1 >> 10) & 1
    imm3 = (hw2 >> 12) & 0x7
    cur = (imm4 << 12) | (i_bit << 11) | (imm3 << 8) | (hw2 & 0xFF)
    if cur != CARD_UNSEL_OLD:
        print('[!] 0x%08X 处 movw 立即数为 0x%04X，预期 0x%04X，跳过卡片色补丁'
              % (CARD_UNSEL_SITE, cur, CARD_UNSEL_OLD))
    else:
        n = CARD_UNSEL_NEW
        n_imm4, n_i, n_imm3, n_imm8 = (n >> 12) & 0xF, (n >> 11) & 1, \
            (n >> 8) & 0x7, n & 0xFF
        hw1 = (hw1 & 0xFBF0) | (n_i << 10) | n_imm4
        hw2 = (n_imm3 << 12) | (rd << 8) | n_imm8
        struct.pack_into('<HH', data, off(CARD_UNSEL_SITE), hw1, hw2)
        print('[i] 底部卡片未选中色 0x%04X -> 0x%04X' % (CARD_UNSEL_OLD,
                                                        CARD_UNSEL_NEW))

    # 1d) 扁平化 UI：NOP 掉立体/斜纹装饰的绘制调用（参照扁平设计语言：
    # 无浮雕、细线分割）。每处 4 字节 bl -> 2x nop.n，原函数其余逻辑不变。
    DECOR_NOP = [
        # 注意：0x0800D70A/D8A6 的 134x13 条纹最初被当作装饰误删——
        # 实为声光页音量/亮度滑条（与扬声器/亮度图标同一绘制函数）。
        # R14 起保留，不再 NOP。
        (0x0800C2F6, 0x0800E94C, '顶栏银色立体板 102x18'),
        (0x0800C85E, 0x0800E94C, '波形区分界竖条 p1 16x176'),
        (0x0800C8C2, 0x0800E94C, '右缘竖条 p1 13x53'),
        (0x0800D228, 0x0800E94C, '波形区分界竖条 p2 16x176'),
        (0x0800D28C, 0x0800E94C, '右缘竖条 p2 13x53'),
    ]
    for site, prim, what in DECOR_NOP:
        got = decode_bl(data, site)
        if got != prim:
            print('[!] 装饰点 0x%08X 原目标 %s 预期 0x%08X，跳过（%s）'
                  % (site, hex(got) if got else '非BL', prim, what))
            continue
        struct.pack_into('<HH', data, off(site), 0xBF00, 0xBF00)
        print('[i] 已移除装饰：%s（0x%08X）' % (what, site))

    # 1e) 底部卡片模板扁平化：4bpp 模板 @0x0802F544（54x17=459B）全 0 索引
    # = LUT[0] 恰为基色全亮 -> 选中亮灰/未选中暗灰的纯平矩形，无浮雕。
    CARD_TMPL = 0x0802F544
    CARD_LEN = 54 * 17 // 2
    if any(data[off(CARD_TMPL):off(CARD_TMPL) + CARD_LEN]):
        for i in range(CARD_LEN):
            data[off(CARD_TMPL) + i] = 0
        print('[i] 底部卡片模板已扁平化（0x%08X，%dB）' % (CARD_TMPL, CARD_LEN))
    else:
        print('[i] 卡片模板已是全 0，跳过')

    # 1e2) 声光页滑条扁平化：2bpp 条纹位图 @0x08031610（134x13，行距 34B，
    # 共 442B）全 0 索引。填充是"颜色替换"原语 0x0801131C——把填充宽度内
    # 等于灰色阶的像素换成绿色阶，花纹由位图继承；位图全 0 后轨道=纯灰
    # (LUT[0]=0x632C)、填充=纯主题绿(LUT[0]=主色全亮)，自动变纯平。
    SLIDER_BMP = 0x08031610
    SLIDER_LEN = 13 * 34
    if any(data[off(SLIDER_BMP):off(SLIDER_BMP) + SLIDER_LEN]):
        for i in range(SLIDER_LEN):
            data[off(SLIDER_BMP) + i] = 0
        print('[i] 声光滑条已扁平化（0x%08X，%dB）' % (SLIDER_BMP, SLIDER_LEN))
    else:
        print('[i] 声光滑条已是全 0，跳过')

    # 1e3) 整宽标题栏扁平化（设置/信号发生器等页面顶部的中央角板）：
    # 绘制函数 0x0800C548（经表 0x080211A8 回调）。构成：
    #   0x0800C560 bl 0x0800EB44  渐变角板 (94,0)-(225,30) 0x1082->0x632C
    #   0x0800C572 bl 0x0800E73C  底部阴影线 (94,31)-(225,31) 0x5AEB
    #   0x0800C586-604            页面标题文字（保留）
    #   0x0800C608-0x0800C763     两翼斜切阶块（全部 NOP）
    # 面板全去后标题文字直接落在黑底上，分隔线由 hook 按页面号补在 y=31。
    TB_GRAD = 0x0800C560
    TB_SHADOW = 0x0800C572
    TB_WINGS = (0x0800C608, 0x0800C764)   # [起, 止)
    if decode_bl(data, TB_GRAD) != 0x0800EB44:
        print('[!] 标题栏渐变角板 0x%08X 原目标不符，跳过整宽标题栏扁平化'
              % TB_GRAD)
    else:
        struct.pack_into('<HH', data, off(TB_GRAD), 0xBF00, 0xBF00)
        struct.pack_into('<HH', data, off(TB_SHADOW), 0xBF00, 0xBF00)
        # 翼块区关键锚点核验后再整体 NOP
        anchors = [0x0800C618, 0x0800C686, 0x0800C6C2, 0x0800C72E,
                   0x0800C750, 0x0800C760]
        ok = all(decode_bl(data, a) == 0x0800E73C for a in anchors)
        if not ok:
            print('[!] 标题栏翼块锚点不符，跳过翼块 NOP（角板/阴影已移除）')
        else:
            for a in range(TB_WINGS[0], TB_WINGS[1], 2):
                struct.pack_into('<H', data, off(a), 0xBF00)
            print('[i] 整宽标题栏已扁平化（角板+阴影+翼块 0x%08X..0x%08X）'
                  % (TB_GRAD, TB_WINGS[1] - 2))

    # 1f) 抬升 stack_base，让出补丁的 RAM 区
    if ram:
        for a in HEAP_SITES:
            cur = struct.unpack_from('<I', data, off(a))[0]
            if cur != HEAP_OLD:
                print('[!] 0x%08X 处为 0x%08X，预期 0x%08X' % (a, cur, HEAP_OLD))
            struct.pack_into('<I', data, off(a), HEAP_NEW)

    # 3) OTGFS1 中断向量
    if vector and irq_addr:
        struct.pack_into('<I', data, off(VECTOR_ADDR), irq_addr | 1)

    # 3b) HardFault 向量
    if fault and irq_addr_fault:
        struct.pack_into('<I', data, off(FAULT_VECTOR_ADDR), irq_addr_fault | 1)

    # 4) 追加补丁代码
    if stub:
        if len(data) > APP_SIZE:
            data = data[:APP_SIZE]
        data += stub

    open(dst, 'wb').write(data)
    print('[+] %s  %d 字节  代码 %d 字节 @0x%08X'
          % (dst, len(data), len(stub or b''), STUB_ADDR))
    if syms:
        print('    hook=0x%08X  usb_irq=%s  vector=0x%08X  fault=0x%08X'
              % (hook_addr, '0x%08X' % irq_addr if irq_addr else '无', VECTOR_ADDR,
                 irq_addr_fault if irq_addr_fault else 0))
    print('    sha256 %s' % hashlib.sha256(data).hexdigest())


def main():
    ap = argparse.ArgumentParser(description='Phosphor 固件补丁工具')
    ap.add_argument('src')
    ap.add_argument('-o', '--out', default='phosphor_patched.bin')
    ap.add_argument('--stub', help='补丁二进制')
    ap.add_argument('--sym', help='llvm-nm 输出，用于读取 hook / usb_irq 地址')
    ap.add_argument('--no-vector', action='store_true', help='不安装 USB 中断向量')
    ap.add_argument('--no-fault', action='store_true', help='不安装 HardFault 捕获向量')
    ap.add_argument('--no-ram', action='store_true', help='不修改 stack_base')
    ap.add_argument('--irqn', type=int, default=67, help='USB 中断号（OTG1=67，OTG2=77）')
    ap.add_argument('--hook-site', default='0x0801A7BC',
                    help='替换的 bl 位置（默认主循环 UI 渲染任务）')
    ap.add_argument('--about-site', default='0x0800DEBE',
                    help='关于页 logo 钩子点（版本行 bl draw_text）')
    ap.add_argument('--no-about', action='store_true', help='不安装关于页 logo 钩子')
    ap.add_argument('--no-img', action='store_true',
                    help='不安装 push_band 画面流钩子（对照版）')
    a = ap.parse_args()
    stub = open(a.stub, 'rb').read() if a.stub else b''
    if len(stub) % 2:
        stub += b'\x00'
    syms = read_syms(a.sym) if a.sym else None
    rebuild(a.src, a.out, stub, syms, vector=not a.no_vector, ram=not a.no_ram,
            irqn=a.irqn, hook_site=int(a.hook_site, 0), fault=not a.no_fault,
            about_site=None if a.no_about else int(a.about_site, 0),
            img=not a.no_img)


if __name__ == '__main__':
    sys.exit(main())
