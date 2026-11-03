#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""改写「关于」页的品牌信息与型号行（8 种语言）

固件里「关于」页（设置 -> 关于）只画两行 16px 文本，取串方式是：

    L = *(uint8_t *)0x20001C67              # 语言号，1..8
    ver   = *(char **)(0x080215FC + 8 * L)
    model = *(char **)(0x08021600 + 8 * L)
    draw_text(213, 110, ver,   16, ...)     # 第一行
    draw_text(213, 135, model, 16, ...)     # 第二行

语言号 1..8 = 中文 / English / Русский / 日本語 / Español /
Português / Deutsch / 한국어。

两种改写方式：

  inplace（默认）
      就地覆盖原字符串。可用字节数 = 原串长度 + 1（原串后有一枚冗余
      0x00），因此型号值最长 8 个 ASCII 字符（"DSO-153\0" -> "PHOS-153\0"）。
      不占用额外 Flash，不改变任何指针。

  alloc（--alloc）
      新串写到空闲 Flash（默认 0x0803FA00，补丁区末尾之后），只改 16 个
      指针。长度不受限，但要留出空间并注意别超出 112px 的显示宽度。

16px 字宽 = 字号/2（ASCII 8px，中文 16px），两行可用宽度约 104~112px。

用法：
    python tools/set_about.py firmware/PHOS_V1.6R4.bin --list
    python tools/set_about.py in.bin --value PHOS-153 -o out.bin
    python tools/set_about.py in.bin --line2 "型号 : PHOS-153" --lang 1 -o out.bin
    python tools/set_about.py in.bin --line2 "Phosphor" --alloc -o out.bin
"""
import argparse
import hashlib
import struct
import sys

BASE = 0x08007000
VER_BASE = 0x080215FC          # 0x080215FC + 8*L -> 版本行指针
MOD_BASE = 0x08021600          # 0x08021600 + 8*L -> 型号行指针
LANG_COUNT = 8
DEFAULT_AREA = 0x0803FA00      # 空闲 Flash，放 alloc 模式的新串

# 16px 中文字库：0x080273F3，每条 3 字节 UTF-8 + 32 字节点阵，共 357 条
FONT16_BASE = 0x080273F3
FONT16_STRIDE = 35
FONT16_COUNT = 357

LANG_NAME = {
    1: "中文", 2: "English", 3: "Русский", 4: "日本語",
    5: "Español", 6: "Português", 7: "Deutsch", 8: "한국어",
}


def rd32(d, addr):
    return struct.unpack_from("<I", d, addr - BASE)[0]


def cstr(d, addr):
    t = addr - BASE
    e = d.find(b"\x00", t)
    return d[t:e].decode("utf-8", "replace")


def span(d, addr):
    """原串占用的字节数，含结尾 0x00 与其后那枚冗余 0x00。"""
    t = addr - BASE
    e = d.find(b"\x00", t)
    if e < 0:
        return len(d) - t
    n = e - t + 1
    while t + n < len(d) and d[t + n] == 0x00:
        n += 1
    return n


def load_font16(d):
    """返回 16px 字库里已有汉字的集合。"""
    o = FONT16_BASE - BASE
    out = set()
    for i in range(FONT16_COUNT):
        b = d[o + i * FONT16_STRIDE: o + i * FONT16_STRIDE + 3]
        try:
            out.add(b.decode("utf-8"))
        except UnicodeDecodeError:
            pass
    return out


def missing_glyphs(text, font):
    return sorted({c for c in text if ord(c) > 0x2E80 and c not in font})


def read_entry(d, lang):
    vp = VER_BASE + 8 * lang
    mp = MOD_BASE + 8 * lang
    va, ma = rd32(d, vp), rd32(d, mp)
    return {
        "lang": lang,
        "ver_ptr": vp, "mod_ptr": mp,
        "ver_addr": va, "mod_addr": ma,
        "ver": cstr(d, va), "mod": cstr(d, ma),
        "ver_span": span(d, va), "mod_span": span(d, ma),
    }


def list_about(d):
    print("=== 「关于」页当前内容 ===")
    for L in range(1, LANG_COUNT + 1):
        e = read_entry(d, L)
        print("  %d %-9s  行1 %-20r (0x%08X, %2d 字节)  行2 %-22r (0x%08X, %2d 字节)"
              % (L, LANG_NAME[L], e["ver"], e["ver_addr"], e["ver_span"],
                 e["mod"], e["mod_addr"], e["mod_span"]))
    addrs = {}
    for L in range(1, LANG_COUNT + 1):
        e = read_entry(d, L)
        addrs.setdefault(e["ver_addr"], []).append(("行1", L))
        addrs.setdefault(e["mod_addr"], []).append(("行2", L))
    shared = {a: v for a, v in addrs.items() if len(v) > 1}
    if shared:
        print("\n  共用同一字符串的语言组（就地改写会一起变）：")
        for a, v in sorted(shared.items()):
            print("    0x%08X  %s" % (a, ", ".join("%s/L%d" % x for x in v)))


def pixels(text, size=16):
    """估算像素宽度：ASCII 取 size/2，CJK 取 size。"""
    w = 0
    for c in text:
        w += size if ord(c) > 0x2E80 else size // 2
    return w


def apply_inplace(d, key, text, langs, font):
    """就地覆盖。key 为 'ver' 或 'mod'。返回改动列表。"""
    raw = text.encode("utf-8")
    groups = {}
    for L in langs:
        e = read_entry(d, L)
        groups.setdefault(e[key + "_addr"], []).append((L, e))
    changes = []
    for addr, items in sorted(groups.items()):
        e = items[0][1]
        avail = e[key + "_span"] - 1          # 留一枚 0x00 作终止符
        if len(raw) > avail:
            print("  [X] 0x%08X 空间不足：需 %d 字节，可用 %d（%s）"
                  % (addr, len(raw), avail, ", ".join("L%d" % x[0] for x in items)))
            return None
        off = addr - BASE
        if len(raw) != e[key + "_span"]:
            d[off:off + e[key + "_span"]] = b"\x00" * e[key + "_span"]
        d[off:off + len(raw)] = raw
        d[off + len(raw)] = 0x00
        miss = missing_glyphs(text, font)
        changes.append((addr, e[key], text, miss,
                        ", ".join("L%d" % x[0] for x in items)))
    return changes


def apply_alloc(d, key, text, langs, font, area):
    """写到空闲 Flash 并改指针。返回 (改动列表, 下一个空闲地址)。"""
    raw = text.encode("utf-8") + b"\x00"
    groups = {}
    for L in langs:
        e = read_entry(d, L)
        groups.setdefault(e[key + "_addr"], []).append((L, e))
    if len(groups) > 1 and len(langs) > 1:
        print("  [X] 多种语言共用字符串时，--alloc 需按语言分别指定（加 --lang）")
        return None, area
    at = area
    if at + len(raw) - BASE > len(d):
        d += b"\xff" * (at + len(raw) - BASE - len(d))
    d[at - BASE: at - BASE + len(raw)] = raw
    changes = []
    for L in langs:
        e = read_entry(d, L)
        struct.pack_into("<I", d, e[key + "_ptr"] - BASE, at)
        miss = missing_glyphs(text, font)
        changes.append((at, e[key], text, miss, "L%d" % L))
    return changes, at + len(raw)


def main():
    ap = argparse.ArgumentParser(description="改写「关于」页品牌信息")
    ap.add_argument("src")
    ap.add_argument("-o", "--out")
    ap.add_argument("--list", action="store_true", help="只打印当前内容")
    ap.add_argument("--line1", help="整行替换第一行（版本行）")
    ap.add_argument("--line2", help="整行替换第二行（型号行）")
    ap.add_argument("--value", help="只替换型号行冒号后的值，保留各语言前缀")
    ap.add_argument("--lang", type=int, help="只改指定语言（1..8），缺省改全部")
    ap.add_argument("--alloc", action="store_true", help="新串写入空闲 Flash 并改指针")
    ap.add_argument("--area", default=hex(DEFAULT_AREA), help="alloc 模式的写入地址")
    ap.add_argument("--no-font-check", action="store_true", help="跳过字库覆盖检查")
    a = ap.parse_args()

    d = bytearray(open(a.src, "rb").read())
    font = load_font16(d) if not a.no_font_check else set()

    if a.list:
        list_about(d)
        return 0

    langs = [a.lang] if a.lang else list(range(1, LANG_COUNT + 1))
    if any(L < 1 or L > LANG_COUNT for L in langs):
        print("语言号应在 1..8")
        return 2

    area = int(a.area, 0)
    all_changes = []
    for key, text in (("ver", a.line1), ("mod", a.line2)):
        if text is None:
            continue
        if a.alloc:
            ch, area = apply_alloc(d, key, text, langs, font, area)
        else:
            ch = apply_inplace(d, key, text, langs, font)
        if ch is None:
            return 1
        all_changes += ch

    if a.value is not None:
        key = "mod"
        per = {}
        for L in langs:
            e = read_entry(d, L)
            old = e["mod"]
            i = old.rfind(":")
            if i < 0:
                print("  [X] L%d 型号行 %r 中没有冒号，跳过" % (L, old))
                continue
            per.setdefault(old[:i + 2], []).append(L)
        for prefix, ls in sorted(per.items()):
            text = prefix + a.value
            if a.alloc:
                ch, area = apply_alloc(d, key, text, ls, font, area)
            else:
                ch = apply_inplace(d, key, text, ls, font)
            if ch is None:
                return 1
            all_changes += ch

    if not all_changes:
        print("没有改动（用 --list 查看当前内容）")
        return 0

    print("改动 %d 处：" % len(all_changes))
    for addr, old, new, miss, who in all_changes:
        flag = ""
        if pixels(new) > pixels(old):
            flag = "  [!] 比原串宽 %dpx，可能出界" % (pixels(new) - pixels(old))
        print("  0x%08X  %-6s %-22r -> %-22r  %dpx%s"
              % (addr, who, old, new, pixels(new), flag))
        if miss:
            print("      [!] 16px 字库缺字：%s（会显示成空白或方框）"
                  % "".join(miss))

    dst = a.out or a.src.replace(".bin", "") + "_about.bin"
    open(dst, "wb").write(d)
    print("\n输出: %s  (%d 字节)" % (dst, len(d)))
    print("sha256 %s" % hashlib.sha256(bytes(d)).hexdigest())
    return 0


if __name__ == "__main__":
    sys.exit(main())
