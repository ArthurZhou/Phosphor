#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DSO-153 (APP v1.6) 固件补丁工具

镜像布局（字符串指针交叉验证过）：
    0x08000000 - 0x08005FFF  IAP bootloader          不动
    0x08006000 - 0x08006FFF  设置页（版本/语言/校准）  不动
    0x08007000 - 0x08035AFF  APP 镜像（191232 字节）   只改下面几处
    0x08035B00 - ...         空闲 Flash，补丁代码放这里

补丁内容：
  1) 0x0801A7B0  bl 0x08009500  ->  bl <hook>          主循环钩子
  2) 0x08007394 / 0x0800739C  栈底 0x2000A580 -> 0x2000A600
     （只让出 128 字节给补丁的 .bss；v1 让了 1KB，把栈压到 3KB，风险过大）
  3) 0x0800714C  向量表第 67 号（USBFS）-> <usb_irq>|1
  4) 文件末尾追加补丁代码（.text）
"""
import argparse
import hashlib
import struct
import sys

BASE = 0x08007000
APP_SIZE = 0x2EB00
APP_END = BASE + APP_SIZE          # 0x08035B00
STUB_ADDR = APP_END

# 主循环四个任务调用：0x0801A7B0 / 0x0801A7B4 / 0x0801A7B8 / 0x0801A7BC
# 想"画在 UI 之上"就钩最后一个（0x0801A7BC -> 0x08008EE0，UI 渲染任务）。
HOOK_SITE = 0x0801A7B0
ORIG_CALL = 0x08009500

# __user_setup_stackheap @0x08007380 返回 r0..r3：
#   r0=[0x08007394]=0x2000A580  stack_base
#   r1=[0x08007398]=0x2000B580  stack_limit（初始 SP，不动）
#   r2=[0x08007394]             heap_base（与 r0 共用同一字面量）
#   r3=[0x0800739C]=0x2000A580  heap_limit
# 原固件 heap_base == heap_limit == 栈底，说明它根本不用堆（malloc 大小为 0）。
# 所以把 stack_base 抬 128 字节，等于把栈从 4KB 缩到 3.875KB —— 影响可忽略，
# 而这 128 字节就是补丁的 .bss。（v1 曾抬了 1KB，栈只剩 3KB，风险大得多。）
HEAP_SITES = (0x08007394, 0x0800739C)   # stack_base 与 heap_limit 所在的字面量
HEAP_OLD = 0x2000A580
HEAP_NEW = 0x2000A800                   # hand 线 .bss 只用 640B (0x2000A580..0x2000A7FF)，栈留 3456B

USB_IRQN = 67                            # 默认 OTG1；OTG2 用 77（--irqn 覆盖）
VECTOR_ADDR = BASE + 4 * (16 + USB_IRQN)   # 0x0800714C

# 推屏函数 0x08008E64：把当前矩形区域的 16bpp 临时缓冲逐像素推给 LCD。
# 全固件 13 处调用都走它，是"画完 / 还没推走"的唯一瞬间 —— 往屏幕上叠字只能插在这。
BAND_SITE = 0x08008E64


def off(addr):
    return addr - BASE


def bl(from_addr, to_addr):
    pc = from_addr + 4
    delta = to_addr - pc
    if delta % 2 or not -(1 << 24) <= delta < (1 << 24):
        raise ValueError("BL 超范围: %x -> %x" % (from_addr, to_addr))
    s = (delta >> 24) & 1
    i1 = (delta >> 23) & 1
    i2 = (delta >> 22) & 1
    imm10 = (delta >> 12) & 0x3FF
    imm11 = (delta >> 1) & 0x7FF
    j1 = ((i1 ^ s) ^ 1) & 1
    j2 = ((i2 ^ s) ^ 1) & 1
    return struct.pack('<HH',
                       0xF000 | (s << 10) | imm10,
                       0xD000 | (j1 << 13) | (j2 << 11) | imm11)


def bw(from_addr, to_addr):
    """32 位无条件 B.W（T4）。和 BL 只差第二个半字的两个位：
    BL   = 11 J1 1 J2 imm11   (基值 0xD000)
    B.W  = 10 J1 1 J2 imm11   (基值 0x9000)
    用 B.W 而不是 BL，是为了不动 lr —— 被替换的那条 push 会把 lr 存进栈，
    原函数结尾要靠它返回，BL 改掉 lr 就等于让它跳回自己、无限循环。"""
    pc = from_addr + 4
    delta = to_addr - pc
    if delta % 2 or not -(1 << 24) <= delta < (1 << 24):
        raise ValueError("B.W 超范围: %x -> %x" % (from_addr, to_addr))
    s = (delta >> 24) & 1
    i1 = (delta >> 23) & 1
    i2 = (delta >> 22) & 1
    imm10 = (delta >> 12) & 0x3FF
    imm11 = (delta >> 1) & 0x7FF
    j1 = ((i1 ^ s) ^ 1) & 1
    j2 = ((i2 ^ s) ^ 1) & 1
    return struct.pack('<HH',
                       0xF000 | (s << 10) | imm10,
                       0x9000 | (j1 << 13) | (j2 << 11) | imm11)


def read_syms(path):
    """解析 llvm-nm 输出：'08035b00 T hook' """
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
    """解出 addr 处一条 Thumb BL 的目标地址（失败返回 None）"""
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


def rebuild(src, dst, stub, syms=None, vector=True, ram=True, irqn=67, hook_site=HOOK_SITE):
    global VECTOR_ADDR, HOOK_SITE, ORIG_CALL
    VECTOR_ADDR = BASE + 4 * (16 + irqn)
    HOOK_SITE = hook_site
    data = bytearray(open(src, 'rb').read())
    if len(data) != APP_SIZE:
        print('[!] 输入大小 %d，预期 %d' % (len(data), APP_SIZE))

    orig = decode_bl(data, hook_site)
    if orig is not None:
        ORIG_CALL = orig
        print('[i] 钩子点 0x%08X 原来的调用目标是 0x%08X' % (hook_site, orig))

    hook_addr = (syms or {}).get('hook', STUB_ADDR)
    irq_addr = (syms or {}).get('usb_irq')

    # 1) 主循环钩子
    data[off(hook_site):off(hook_site) + 4] = bl(hook_site, hook_addr)

    # 1b) 推屏钩子（屏幕上实时叠字）
    band_addr = (syms or {}).get('band_hook')
    if band_addr:
        old = bytes(data[off(BAND_SITE):off(BAND_SITE) + 4])
        data[off(BAND_SITE):off(BAND_SITE) + 4] = bw(BAND_SITE, band_addr)
        print('[i] 推屏点 0x%08X 原来 %s -> b.w 0x%08X'
              % (BAND_SITE, old.hex(), band_addr))

    # 2) 栈底上移，把 0x2000A580..0x2000A7FF 让给补丁的 .bss
    #    --no-ram 时不改（给不占任何 RAM 的最小验证版用）
    if ram:
        for a in HEAP_SITES:
            cur = struct.unpack_from('<I', data, off(a))[0]
            if cur != HEAP_OLD:
                print('[!] 0x%08X 处不是 0x%08X 而是 0x%08X' % (a, HEAP_OLD, cur))
            struct.pack_into('<I', data, off(a), HEAP_NEW)

    # 3) USB 中断向量
    if vector and irq_addr:
        struct.pack_into('<I', data, off(VECTOR_ADDR), irq_addr | 1)

    # 4) 追加代码
    if stub:
        if len(data) > APP_SIZE:
            data = data[:APP_SIZE]
        data += stub

    open(dst, 'wb').write(data)
    print('[+] %s  %d 字节  代码 %d 字节 @0x%08X'
          % (dst, len(data), len(stub or b''), STUB_ADDR))
    if syms:
        print('    hook=0x%08X  usb_irq=%s  vector=0x%08X'
              % (hook_addr, '0x%08X' % irq_addr if irq_addr else '无', VECTOR_ADDR))
    print('    sha256 %s' % hashlib.sha256(data).hexdigest())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('src')
    ap.add_argument('-o', '--out', default='APP_DSO153_patched.bin')
    ap.add_argument('--stub')
    ap.add_argument('--sym', help='llvm-nm 输出，用于取 hook/usb_irq 地址')
    ap.add_argument('--no-vector', action='store_true', help='不装 USB 中断向量')
    ap.add_argument('--no-ram', action='store_true', help='不改栈底（补丁不占 RAM 时用）')
    ap.add_argument('--irqn', type=int, default=67, help='USB 中断号（OTG1=67, OTG2=77）')
    ap.add_argument('--hook-site', default='0x0801A7B0',
                    help='被替换的 bl 位置（0x0801A7B0=第1个任务，0x0801A7BC=最后一个/UI任务）')
    a = ap.parse_args()
    stub = open(a.stub, 'rb').read() if a.stub else b''
    if len(stub) % 2:
        stub += b'\x00'
    syms = read_syms(a.sym) if a.sym else None
    rebuild(a.src, a.out, stub, syms, vector=not a.no_vector, ram=not a.no_ram,
            irqn=a.irqn, hook_site=int(a.hook_site, 0))


if __name__ == '__main__':
    sys.exit(main())
