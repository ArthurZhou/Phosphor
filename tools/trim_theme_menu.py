#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从设置菜单中移除「主题设置」项（两套主题已被 Phosphor 统一，该设置无意义）。

原理（不改绘制代码，只动数据 + 三个边界常量）：
  1. 菜单字符串指针表 @0x08021B74：8 语言组 × 7 槽。把每组内
     「主题设置」(槽 3) 移到组尾 (槽 6)，其余项前移。
  2. 按键分派表（tbh）@0x0800D532：7 个半字与槽序一一对应，同步重排，
     主题设置的分支挪到末尾。
  3. 滚动上限 @0x0800A9B0：cmp #1 -> cmp #0。窗口只显示前 6 槽，
     主题设置（槽 6）永不出现在屏幕上。
  4. 回车分派边界 @0x0800D52A：cmp #6 -> cmp #5。即使高亮被移到槽 6，
     按确认也无响应。

用法：python tools/trim_theme_menu.py <in.bin> -o <out.bin>
"""

import argparse
import struct
import sys

BASE = 0x08007000

MENU_TABLE = 0x08021B74     # 8 组 × 7 槽 × 4B
GROUPS = 8
SLOTS = 7
THEME_SLOT = 3              # 主题设置原槽位

TBH_TABLE = 0x0800D532      # 7 个 16 位分支偏移
TBH_COUNT = 7

# (地址, 旧立即数, 新立即数)  Thumb16 CMP r0, #imm8
CMP_PATCHES = [
    (0x0800D52A, 6, 5),     # 回车分派允许 0..5
    (0x0800A9B0, 1, 0),     # 滚动上限 0
]


def off(addr):
    return addr - BASE


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('src')
    ap.add_argument('-o', '--out', required=True)
    a = ap.parse_args()

    data = bytearray(open(a.src, 'rb').read())

    # ---- 1. 菜单表：每组槽 3 与槽 4..6 依次前移，主题设置落槽 6 ----
    changed = 0
    for g in range(GROUPS):
        base = off(MENU_TABLE) + g * SLOTS * 4
        slots = [struct.unpack_from('<I', data, base + i * 4)[0] for i in range(SLOTS)]
        theme = slots[THEME_SLOT]
        rest = slots[:THEME_SLOT] + slots[THEME_SLOT + 1:]
        new = rest + [theme]
        if new != slots:
            for i, v in enumerate(new):
                struct.pack_into('<I', data, base + i * 4, v)
            changed += 1

    # ---- 2. tbh 分派表：半字 3 移到末尾 ----
    tb = list(struct.unpack_from('<%dH' % TBH_COUNT, data, off(TBH_TABLE)))
    theme_br = tb[THEME_SLOT]
    tb_new = tb[:THEME_SLOT] + tb[THEME_SLOT + 1:] + [theme_br]
    if tb_new != tb:
        struct.pack_into('<%dH' % TBH_COUNT, data, off(TBH_TABLE), *tb_new)

    # ---- 3. 边界常量 ----
    for addr, old, new in CMP_PATCHES:
        pos = off(addr)
        hw = struct.unpack_from('<H', data, pos)[0]
        expect = 0x2800 | old          # cmp r0, #imm8
        if hw != expect:
            print('[XX] 0x%08X 处为 0x%04X，预期 0x%04X（cmp r0, #%d）——镜像不符，未改动'
                  % (addr, hw, expect, old))
            return 1
        struct.pack_into('<H', data, pos, 0x2800 | new)

    open(a.out, 'wb').write(data)
    print('[+] 菜单表重排 %d 组，tbh 分派同步，边界 cmp 收紧' % changed)
    print('[+] 「主题设置」已移出可见窗口且确认无效')
    return 0


if __name__ == '__main__':
    sys.exit(main())
