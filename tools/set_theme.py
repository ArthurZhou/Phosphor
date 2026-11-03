#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""改写 DSO-153 固件的主题调色板（主色 / 次级色）。

调色板位于 0x08035850，每主题 4 字节，两个 16 位 RGB565 颜色：

    +0  次级色（网格、辅助线等，全固件 4 处读取）
    +2  主色  （按钮、曲线、高亮，全固件 35 处读取）

读取方式（固件内统一）：

    idx = *(uint8_t *)0x20001C6A          // 主题号
    c   = *(uint16_t *)(0x08035850 + idx*4 + 2)

原厂两套主题：
    theme 0   次级 0xFEA0(黄)  主色 0x055F(蓝)
    theme 1   次级 0x055F(蓝)  主色 0xFEA0(黄)

用法：
    python tools/set_theme.py in.bin --list
    python tools/set_theme.py in.bin --primary 00E5A0 -o out.bin
    python tools/set_theme.py in.bin --preset phosphor -o out.bin
    python tools/set_theme.py in.bin --preset phosphor --accent 2A3B36 -o out.bin
"""

import argparse
import struct
import sys

BASE = 0x08007000
PALETTE = 0x08035850
THEMES = 2

# 主题名标签：设置→主题设置 里两个选项的名字。原厂为 蓝色/黄色，
# 指针表 0x080215BC + lang*8 + slot*4（lang 1..8，slot 0=主题0 / 1=主题1），
# 即 0x080215C4..0x08021600 共 16 项。菜单用 16px 绘制（0x0800DDD4）。
# 中文字库没有 绿/白 字形（封闭点阵字库，绿 白 三档全缺），按用户决定
# 全语言用英文 Green/White（ASCII 字库三档全覆盖）。
LABEL_TABLE = 0x080215C4
LABEL_COUNT = 16
LABELS_ADDR = 0x0803F800     # 空闲 Flash，存放新标签字符串
LABEL_MAX = 12               # 标签最长字符数（16px ASCII 每字 8px，勿超）


def off(addr):
    return addr - BASE


def rgb565(hex_rgb):
    """'#RRGGBB' / 'RRGGBB' -> RGB565 半字。"""
    s = hex_rgb.lstrip('#')
    if len(s) != 6:
        raise ValueError('颜色需为 6 位十六进制：%r' % hex_rgb)
    r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    return ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)


def from565(v):
    """RGB565 -> '#RRGGBB'，用于回显。"""
    r = (v >> 11) & 0x1F
    g = (v >> 5) & 0x3F
    b = v & 0x1F
    return '#%02X%02X%02X' % (r << 3 | r >> 2, g << 2 | g >> 4, b << 3 | b >> 2)


# 预设：每个主题一对 (次级色, 主色)。字符串为 #RRGGBB，整数为已编码的 RGB565。
PRESETS = {
    # 绿(当前) + 白(黑白灰方案)：theme0 青绿主色配暗灰次级；
    # theme1 纯白主色配浅灰次级，即参考设计的黑白灰方案
    'phosphor2': [('#78838D', '#00E5A0'), ('#BDBEBD', '#FFFFFF')],
    # Phosphor 品牌：石墨黑打底，荧光青绿作主色；次级色压暗，避免抢波形
    'phosphor': [('#2A3B36', '#00E5A0'), ('#2A3B36', '#6BFFC4')],
    # 荧光青绿 + 琥珀对照，适合强光下读数
    'amber':    [('#3A2E1A', '#00E5A0'), ('#3A2E1A', '#FFB020')],
    # 冷白高对比，接近原厂观感但去掉了纯黄/纯蓝
    'ice':      [('#24303A', '#7FD8FF'), ('#24303A', '#E8F6FF')],
    # 原厂值，用于还原
    'stock':    [(0xFEA0, 0x055F), (0x055F, 0xFEA0)],
}

# 预设对应的主题名标签（英文，ASCII 字库限制）。stock 预设恢复原厂蓝/黄，
# 但原厂中文名是 蓝色/黄色（CJK），此处统一英文 Blue/Yellow 亦可。
PRESET_LABELS = {
    'phosphor2': ('Green', 'White'),
    'phosphor':  ('Green', 'Green2'),
    'amber':     ('Green', 'Amber'),
    'ice':       ('Ice', 'Ice2'),
    'stock':     ('Blue', 'Yellow'),
}


def as565(v):
    """预设里的颜色既可能是 '#RRGGBB' 也可能是已编码半字。"""
    return rgb565(v) if isinstance(v, str) else v


def read_themes(data):
    out = []
    for i in range(THEMES):
        a = struct.unpack_from('<H', data, off(PALETTE) + i * 4)[0]
        b = struct.unpack_from('<H', data, off(PALETTE) + i * 4 + 2)[0]
        out.append((a, b))
    return out


def show(data, label=''):
    themes = read_themes(data)
    if label:
        print('%s' % label)
    for i, (a, b) in enumerate(themes):
        print('  theme %d   次级 0x%04X %-9s   主色 0x%04X %-9s'
              % (i, a, from565(a), b, from565(b)))


def main():
    ap = argparse.ArgumentParser(description='DSO-153 主题调色板改写')
    ap.add_argument('src')
    ap.add_argument('-o', '--out', help='输出文件（省略则就地修改）')
    ap.add_argument('--list', action='store_true', help='只列出当前调色板')
    ap.add_argument('--preset', choices=sorted(PRESETS), help='套用预设配色')
    ap.add_argument('--primary', help='theme 0 主色，如 00E5A0')
    ap.add_argument('--primary2', help='theme 1 主色')
    ap.add_argument('--accent', help='theme 0 次级色')
    ap.add_argument('--accent2', help='theme 1 次级色')
    ap.add_argument('--label0', help='主题 0 的菜单名（ASCII，默认随预设）')
    ap.add_argument('--label1', help='主题 1 的菜单名（ASCII，默认随预设）')
    a = ap.parse_args()

    data = bytearray(open(a.src, 'rb').read())
    if len(data) < off(PALETTE) + 8:
        print('[XX] 文件太小，不是 DSO-153 APP 镜像')
        return 1

    if a.list or not any([a.preset, a.primary, a.primary2, a.accent, a.accent2]):
        show(data, '%s:' % a.src)
        if a.list:
            return 0
        print('\n未指定改动。用 --preset phosphor 或 --primary RRGGBB。')
        return 0

    before = read_themes(data)
    after = [list(t) for t in before]

    if a.preset:
        for i, (acc, pri) in enumerate(PRESETS[a.preset]):
            if acc is not None:
                after[i][0] = as565(acc)
            if pri is not None:
                after[i][1] = as565(pri)

    if a.accent:
        after[0][0] = rgb565(a.accent)
    if a.accent2:
        after[1][0] = rgb565(a.accent2)
    if a.primary:
        after[0][1] = rgb565(a.primary)
    if a.primary2:
        after[1][1] = rgb565(a.primary2)

    changed = 0
    for i, (old, new) in enumerate(zip(before, after)):
        for k in range(2):
            if old[k] != new[k]:
                struct.pack_into('<H', data, off(PALETTE) + i * 4 + k * 2, new[k])
                changed += 1

    # ---- 主题名标签：写新字符串到空闲 Flash 并重指 16 个指针 ----
    labels = None
    if a.label0 or a.label1:
        labels = (a.label0 or 'Green', a.label1 or 'White')
    elif a.preset in PRESET_LABELS:
        labels = PRESET_LABELS[a.preset]

    labels_written = 0
    if labels:
        for name in labels:
            if not name or len(name) > LABEL_MAX or \
                    any(not (0x20 <= ord(c) <= 0x7E) for c in name):
                print('[XX] 标签 %r 非法：需 1..%d 个 ASCII 字符'
                      % (name, LABEL_MAX))
                return 1
        s0 = labels[0].encode('ascii') + b'\x00'
        s1 = labels[1].encode('ascii') + b'\x00'
        a1 = LABELS_ADDR + len(s0)
        # 镜像文件可能短于写入点（空闲 Flash 在 APP 镜像末尾之后），
        # 必须先补 0xFF 到目标偏移，否则 Python 切片赋值会把字符串
        # 追加到文件末尾而不是目标地址（指针将指向空白 Flash）。
        end = off(a1) + len(s1)
        if len(data) < end:
            data.extend(b'\xff' * (end - len(data)))
        data[off(LABELS_ADDR):off(LABELS_ADDR) + len(s0)] = s0
        data[off(a1):off(a1) + len(s1)] = s1
        ptrs = [LABELS_ADDR, a1]
        for lang in range(8):
            for slot in range(2):
                ea = LABEL_TABLE + (lang * 2 + slot) * 4
                struct.pack_into('<I', data, off(ea), ptrs[slot])
                labels_written += 1
        print('主题名标签已重写：theme0=%r theme1=%r（16 个指针 -> 0x%08X）'
              % (labels[0], labels[1], LABELS_ADDR))

    if not changed and not labels_written:
        print('配色未变化。')
        return 0

    dst = a.out or a.src
    open(dst, 'wb').write(data)
    print('改动 %d 个颜色：' % changed)
    for i, (old, new) in enumerate(zip(before, after)):
        if old != new:
            print('  theme %d  次级 0x%04X %s -> 0x%04X %s   主色 0x%04X %s -> 0x%04X %s'
                  % (i, old[0], from565(old[0]), new[0], from565(new[0]),
                     old[1], from565(old[1]), new[1], from565(new[1])))
    if a.out:
        print('已写入 %s' % dst)
    else:
        print('已就地写入 %s' % dst)
    return 0


if __name__ == '__main__':
    sys.exit(main())
