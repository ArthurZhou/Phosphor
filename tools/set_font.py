#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""替换 DSO-153 固件的中文点阵字形（不动字库结构、不增删字符）。

字库布局（三档，均为「3 字节 UTF-8 + 点阵」的定长记录）：

    字号   基址         步长   条目数   点阵          记录布局
    12px   0x080263B5    27    154     12x12 1bpp    3 + 12*2 = 27
    16px   0x080273F3    35    357     16x16 1bpp    3 + 16*2 = 35
    20px   0x0802A4C2    63    284     20x20 1bpp    3 + 20*3 = 63

点阵为行主序、每行 MSB 在左，行宽 = ceil(字号/8) 字节，字形在行内左对齐。
因为结构固定，字形可以整体替换：只要字符集与顺序不变，绘制代码无需改动。

用法：
    python tools/set_font.py in.bin --list
    python tools/set_font.py in.bin --font C:/Windows/Fonts/Deng.ttf -o out.bin
    python tools/set_font.py in.bin --font ... --size 16 --preview 关于
    python tools/set_font.py in.bin --font ... --sheet tools/build/tmp/sheet.png
"""

import argparse
import os
import struct
import sys

BASE = 0x08007000

# 字号 -> (基址, 步长, 条目数)
FONTS = {
    12: (0x080263B5, 27, 154),
    16: (0x080273F3, 35, 357),
    20: (0x0802A4C2, 63, 284),
}

# ASCII 半角字库（draw_char @0x080109A4 内 tbb 分派）：
#   字号   基址         字符间隔   行数   行宽
#   12px   0x08022985   12 字节    12     1B（8 位用左 6）
#   16px   0x08022DF9   16 字节    16     1B（8 位）
#   20px   0x080233E9   48 字节    20     2B（16 位用左 10）
ASCII_FONTS = {
    12: (0x08022985, 12, 12, 1),
    16: (0x08022DF9, 16, 16, 1),
    20: (0x080233E9, 48, 20, 2),
}
ASCII_COUNT = 95          # 0x20..0x7E

# 12px ASCII 手工兜底字形（6 宽 x 12 高，与渲染管线同坐标系：小写 x 高度
# 3..9，大写 1..8）。6px 单元格里 Sarasa 的 m 中竖恰好落在像素缝隙上
# （读成 n）、v/V 斜线过渡行被阈值截断（中段断裂）——超采样与调阈值均
# 无法两全，此三字用手工点阵，其余 92 字符用 4x 超采样渲染。
OVERRIDE_12 = {
    'm': [
        "......", "......", "......",
        "######",
        "#.##.#",
        "#.##.#",
        "#.##.#",
        "#.##.#",
        "#.##.#",
        "#.##.#",
        "......", "......",
    ],
    'v': [
        "......", "......", "......",
        "#....#",
        "#....#",
        "#....#",
        ".#..#.",
        ".#..#.",
        "..##..",
        "..##..",
        "......", "......",
    ],
    'V': [
        "......",
        "#....#",
        "#....#",
        "#....#",
        ".#..#.",
        ".#..#.",
        ".#..#.",
        "..##..",
        "..##..",
        "......", "......", "......",
    ],
}

# 这些是"图形符号字"：触发沿图标、单位上标、复选/警告标志等 UI 直接拿
# 字形当图标用。Sarasa 渲染出来的形状与原厂差异大（R6 实测触发沿图标
# 变黑块、底部 "~V" 单位碎裂），一律保留原厂字形。
SYMBOLS_KEEP = set('Ω℃←∞⏻⚠✓✖✗❏❒︴﹁﹂')


def off(addr):
    return addr - BASE


def metrics(size):
    """返回 (行数, 每行字节数)。"""
    return size, (size + 7) // 8


def entries(data, size):
    """读出字库里的字符列表（按索引顺序）。"""
    base, stride, count = FONTS[size]
    o = off(base)
    out = []
    for i in range(count):
        b = data[o + i * stride: o + i * stride + 3]
        try:
            out.append(b.decode('utf-8'))
        except UnicodeDecodeError:
            out.append('\uFFFD')
    return out


def bitmap_bytes(data, size, idx):
    base, stride, _ = FONTS[size]
    rows, wb = metrics(size)
    o = off(base) + idx * stride + 3
    return bytes(data[o:o + rows * wb])


def unpack(bits, size):
    """点阵字节 -> 行列表，每行是 0/1 列表。"""
    rows, wb = metrics(size)
    out = []
    for y in range(rows):
        line = []
        for xb in range(wb):
            byte = bits[y * wb + xb]
            for k in range(8):
                line.append((byte >> (7 - k)) & 1)
        out.append(line[:size])
    return out


def pack(grid, size):
    rows, wb = metrics(size)
    out = bytearray(rows * wb)
    for y in range(rows):
        for x in range(size):
            if grid[y][x]:
                out[y * wb + (x >> 3)] |= 0x80 >> (x & 7)
    return bytes(out)


def render_char(font, ch, size, threshold, dx=0, dy=0):
    """把单字渲染成 size x size 的黑白点阵（居中，可按像素微调）。"""
    from PIL import Image, ImageDraw
    img = Image.new('L', (size, size), 0)
    d = ImageDraw.Draw(img)
    try:
        bbox = d.textbbox((0, 0), ch, font=font)
    except Exception:
        bbox = (0, 0, size, size)
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    ox = (size - w) // 2 - bbox[0] + dx
    oy = (size - h) // 2 - bbox[1] + dy
    d.text((ox, oy), ch, font=font, fill=255)
    px = img.load()
    return [[1 if px[x, y] >= threshold else 0 for x in range(size)]
            for y in range(size)]


def ascii_baseline_shift(font, size, threshold):
    """渲染 '0' 量出 ink 底边，与原厂基线位置（≈0.79*size）求差。

    原厂三档数字底边分别在 9/13/16（≈0.79*size），TTF 按 ascent 比例
    换算的基线普遍偏高，不校准的话数字会浮起来。
    """
    grid = render_ascii_char(font, '0', size, threshold)
    ref = {12: 9, 16: 13, 20: 16}[size]
    bottom = 0
    for y, row in enumerate(grid):
        if any(row):
            bottom = y
    return ref - bottom


def render_ascii_char(font, ch, size, threshold, dx=0, dy=0):
    """半角字符渲染到 (size/2) x size 格子。

    与 CJK 不同，ASCII 必须按固定基线画：数字、单位、冒号若各自按 ink
    框居中会高低不齐。因此以 TTF 的 ascent/descent 比例换算基线位置，
    再用 anchor='ls'（左-基线）绘制。
    """
    from PIL import Image, ImageDraw
    W, H = size // 2, size
    if size <= 12:
        # 小字号直渲会丢笔画/断裂，4x 超采样后降采样再阈值化。
        # 注意画布放大 4x 后必须用放大 4x 的字体对象，否则字形只有
        # 画布的 1/4 大小，降采样后消失。
        scale = 4
        try:
            import PIL.ImageFont as _IF
            fbig = _IF.truetype(font.path, size * scale)
        except Exception:
            fbig = font
        img = Image.new('L', (W * scale, H * scale), 0)
        d = ImageDraw.Draw(img)
        asc, desc = fbig.getmetrics()
        y_base = round(asc * H * scale / max(1, asc + desc)) + dy * scale
        d.text((dx * scale, y_base), ch, font=fbig, fill=255, anchor='ls')
        img = img.resize((W, H), Image.LANCZOS)
    else:
        img = Image.new('L', (W, H), 0)
        d = ImageDraw.Draw(img)
        asc, desc = font.getmetrics()
        y_base = round(asc * H / max(1, asc + desc)) + dy
        d.text((dx, y_base), ch, font=font, fill=255, anchor='ls')
    px = img.load()
    return [[1 if px[x, y] >= threshold else 0 for x in range(W)]
            for y in range(H)]


def ascii_pack(grid, size):
    """半角点阵 -> 字库字节（行内 MSB 在前，每行 rowbytes 字节）。"""
    base, stride, rows, rowbytes = ASCII_FONTS[size]
    out = bytearray()
    for y in range(rows):
        line = grid[y] if y < len(grid) else [0] * (size // 2)
        for b in range(rowbytes):
            byte = 0
            for k in range(8):
                x = b * 8 + k
                if x < len(line) and line[x]:
                    byte |= 0x80 >> k
            out.append(byte)
    return bytes(out)


def ascii_art(grid, label=''):
    lines = []
    if label:
        lines.append('  ' + label)
    for row in grid:
        lines.append('  ' + ''.join('#' if v else '.' for v in row))
    return '\n'.join(lines)


def load_font(path, size):
    from PIL import ImageFont
    # ttc 可带序号：msyh.ttc 里 0 = 常规
    return ImageFont.truetype(path, size)


def main():
    ap = argparse.ArgumentParser(description='DSO-153 中文字形替换')
    ap.add_argument('src')
    ap.add_argument('-o', '--out', help='输出文件（省略则就地修改）')
    ap.add_argument('--list', action='store_true', help='列出三档字库信息')
    ap.add_argument('--dump', metavar='TEXT', help='打印指定文字在现有字库中的点阵')
    ap.add_argument('--font', help='TTF/TTC 字体文件路径')
    ap.add_argument('--size', type=int, choices=sorted(FONTS),
                    help='只改一档字号（省略则三档全改）')
    ap.add_argument('--no-ascii-12', action='store_true',
                    help='保留原厂 12px ASCII（默认替换，m/v/V 用手工兜底）')
    ap.add_argument('--cjk-12', action='store_true',
                    help='连 12px CJK 一起替换（默认保留原厂：12x12 里笔画密的'
                         '字经 TTF 渲染必然断裂糊字，实机 R36 证实期/脉/值不可读）')
    ap.add_argument('--threshold', type=int, default=128,
                    help='二值化阈值 0-255，默认 128；调低笔画更粗')
    ap.add_argument('--dx', type=int, default=0, help='字形水平微调像素')
    ap.add_argument('--dy', type=int, default=0, help='字形垂直微调像素')
    ap.add_argument('--preview', metavar='TEXT', help='改完后打印这些字的点阵')
    ap.add_argument('--sheet', metavar='PNG', help='输出三档字号对照图')
    ap.add_argument('--dry-run', action='store_true', help='只渲染不写文件')
    a = ap.parse_args()

    data = bytearray(open(a.src, 'rb').read())

    if a.list or (not a.font and not a.dump):
        print('%s 字库：' % a.src)
        for size in sorted(FONTS):
            base, stride, count = FONTS[size]
            rows, wb = metrics(size)
            chars = entries(data, size)
            uniq = len(set(chars))
            print('  %2dpx  0x%08X  步长 %d  %d 条（唯一 %d）  点阵 %dx%d = %d 字节'
                  % (size, base, stride, count, uniq, size, size, rows * wb))
        allc = set()
        for size in FONTS:
            allc |= set(entries(data, size))
        print('\n  合起来共 %d 个字：' % len(allc))
        t = ''.join(sorted(allc))
        for i in range(0, len(t), 40):
            print('    ' + t[i:i + 40])
        if a.list:
            return 0

    if a.dump:
        for size in sorted(FONTS):
            chars = entries(data, size)
            for ch in a.dump:
                if ch in chars:
                    idx = chars.index(ch)
                    grid = unpack(bitmap_bytes(data, size, idx), size)
                    print('--- %dpx  %r  索引 %d' % (size, ch, idx))
                    print(ascii_art(grid))
                else:
                    print('--- %dpx  %r  字库中没有' % (size, ch))
        return 0

    sizes = [a.size] if a.size else sorted(FONTS)
    fontpath = a.font
    if not (os.path.isfile(fontpath) and fontpath.lower().endswith(('.ttf', '.ttc', '.otf'))):
        print('[XX] 找不到字体文件：%s' % fontpath)
        return 1

    total = 0
    report = []
    kept = []
    # 12px CJK 默认冻结：12x12 单元格内 TTF 渲染的密集笔画会断裂/粘连
    # （同 R9 的 12px ASCII 教训，但汉字笔画密度更高，超采样也救不回）。
    # 原厂 12px CJK 是手工点阵，是此尺寸的最优解。--cjk-12 强制替换。
    cjk_sizes = [s for s in sizes if s != 12 or a.cjk_12]
    for size in cjk_sizes:
        base, stride, _ = FONTS[size]
        chars = entries(data, size)
        font = load_font(fontpath, size)
        miss = 0
        for i, ch in enumerate(chars):
            if ch == '\uFFFD' or ch in SYMBOLS_KEEP:
                continue
            grid = render_char(font, ch, size, a.threshold, a.dx, a.dy)
            # 字体缺这个字时渲染结果全空。此时保留原厂字形，否则会变成白块。
            if not any(any(r) for r in grid):
                miss += 1
                if miss <= 12:
                    kept.append((size, ch))
                continue
            bits = pack(grid, size)
            o = off(base) + i * stride + 3
            if data[o:o + len(bits)] != bits:
                data[o:o + len(bits)] = bits
                total += 1
        report.append((size, len(chars), miss))

    print('字体 %s' % os.path.basename(fontpath))
    cjk_skip = sorted(set(sizes) - set(cjk_sizes))
    if cjk_skip:
        print('  CJK    %s 档保留原厂点阵（小字号 TTF 渲染不可读）'
              % '/'.join(str(s) for s in cjk_skip))
    for size, n, miss in report:
        print('  %2dpx  %d 字，%d 个渲染为空（保留原厂字形）%s'
              % (size, n, miss, '  [!] 字体覆盖不全' if miss else ''))
    if kept:
        sample = ''.join(ch for _, ch in kept[:40])
        print('    缺字示例：%s%s' % (sample, ' …' if len(kept) > 40 else ''))

    # ASCII 半角：三档全部是固定集（0x20..0x7E），字体缺字按空格处理。
    # 12px 不换：6px 宽的单元格里 TTF 渲染 'm' 三竖笔必然糊成一团、'V' 斜线
    # 过渡行会被阈值截断（实机 R8 证实 mV 显示成 ~V），原厂 12px ASCII 是
    # 手工点阵，清晰且与任意 CJK 混排无违和。--ascii-12 可强制替换。
    total_a = 0
    ascii_sizes = list(sizes) if not a.no_ascii_12 else \
        [s for s in sizes if s != 12]
    for size in ascii_sizes:
        base, stride, rows, rowbytes = ASCII_FONTS[size]
        font = load_font(fontpath, size)
        # 自动基线校准：以 '0' 的底边对齐原厂基线
        shift = ascii_baseline_shift(font, size, a.threshold)
        for i in range(ASCII_COUNT):
            ch = chr(0x20 + i)
            if size == 12 and ch in OVERRIDE_12:
                grid = [[1 if c == '#' else 0 for c in row]
                        for row in OVERRIDE_12[ch]]
            else:
                grid = render_ascii_char(font, ch, size, a.threshold,
                                         a.dx, a.dy + shift)
            bits = ascii_pack(grid, size)
            o = off(base) + i * stride
            if data[o:o + len(bits)] != bits:
                data[o:o + len(bits)] = bits
                total_a += 1
        print('  ASCII  %dpx 基线偏移 %+d px' % (size, shift))
    skipped = sorted(set(sizes) - set(ascii_sizes))
    if skipped:
        print('  ASCII  %s 档保留原厂点阵（小字号 TTF 渲染不可读）'
              % '/'.join(str(s) for s in skipped))
    print('  ASCII  95 字符 × %d 档已更新' % len(ascii_sizes))

    if not a.dry_run:
        dst = a.out or a.src
        open(dst, 'wb').write(data)
        print('已写入 %s（CJK %d + ASCII %d 个字形更新）' % (dst, total, total_a))

    if a.preview:
        print('\n改后点阵：')
        for size in sizes:
            chars = entries(data, size)
            font = load_font(fontpath, size)
            for ch in a.preview:
                if ch in chars:
                    if size not in cjk_sizes:
                        # 该档 CJK 冻结原厂：显示原厂点阵而非新字体渲染
                        grid = unpack(bitmap_bytes(data, size, chars.index(ch)), size)
                        tag = ' [原厂]'
                    else:
                        grid = render_char(font, ch, size, a.threshold, a.dx, a.dy)
                        tag = ''
                    print('--- %dpx  %r%s' % (size, ch, tag))
                    print(ascii_art(grid))
                elif 0x20 <= ord(ch) <= 0x7E:
                    if size == 12 and ch in OVERRIDE_12:
                        grid = [[1 if c == '#' else 0 for c in row]
                                for row in OVERRIDE_12[ch]]
                    else:
                        shift = ascii_baseline_shift(font, size, a.threshold)
                        grid = render_ascii_char(font, ch, size, a.threshold,
                                                 a.dx, a.dy + shift)
                    print('--- %dpx  %r (ASCII)%s'
                          % (size, ch,
                             ' [兜底]' if size == 12 and ch in OVERRIDE_12
                             else ''))
                    print(ascii_art(grid))

    if a.sheet:
        make_sheet(data, fontpath, a.threshold, a.dx, a.dy, a.sheet)
        print('对照图已写入 %s' % a.sheet)

    return 0


def make_sheet(data, fontpath, threshold, dx, dy, out_png, text=None):
    """三档字号 × 若干字符：左侧原厂字形，右侧新字体，拼成一张对照图。"""
    from PIL import Image, ImageDraw

    if not text:
        text = '关于版本型号设置语言'

    sizes = [12, 16, 20]
    pad = 8
    gap = 14                 # 原厂块与新字体块之间的间距
    zoom = 4                 # 位图放大倍数
    cell = max(sizes) + 4    # 单字格边长（不含放大）

    cols = len(text)
    block_w = cols * cell
    width = pad * 3 + block_w * 2 + gap
    height = pad * 2 + len(sizes) * (cell + pad)

    img = Image.new('RGB', (width * zoom, height * zoom), (16, 20, 24))
    d = ImageDraw.Draw(img)

    # 块间分隔线
    sep = (pad + block_w + gap // 2) * zoom
    d.line([(sep, 0), (sep, height * zoom)], fill=(60, 70, 78), width=1)

    for ri, size in enumerate(sizes):
        chars = entries(data, size)
        font = load_font(fontpath, size)
        y0 = pad + ri * (cell + pad)
        for ci, ch in enumerate(text):
            # 原厂（青绿）
            if ch in chars:
                grid = unpack(bitmap_bytes(data, size, chars.index(ch)), size)
                draw_grid(d, grid, (pad + ci * cell) * zoom, y0 * zoom, zoom, (0, 229, 160))
            # 新字体（琥珀），画在同一列右侧块
            grid = render_char(font, ch, size, threshold, dx, dy)
            if any(any(r) for r in grid):
                draw_grid(d, grid,
                          (pad * 2 + block_w + gap // 2 + ci * cell) * zoom,
                          y0 * zoom, zoom, (255, 176, 32))

    img.save(out_png)
    return img


def draw_grid(d, grid, x0, y0, scale, color):
    for y, row in enumerate(grid):
        for x, v in enumerate(row):
            if v:
                d.rectangle([x0 + x * scale, y0 + y * scale,
                             x0 + x * scale + scale - 1, y0 + y * scale + scale - 1],
                            fill=color)


if __name__ == '__main__':
    sys.exit(main())
