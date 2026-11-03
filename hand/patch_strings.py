#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把固件里"设置页三行文字"的字符串指针改指向补丁的 RAM 缓冲区。

固件设置页（0x0800d4f0）这样取文字：
    L = *(uint8_t *)(0x2000001C + 0x67)          # 语言 1..8
    version = *(char **)(0x080215FC + 8*L)
    model   = *(char **)(0x08021600 + 8*L)
    caution = *(char **)(0x08021640 + 4*L)

把这 24 个指针换成补丁里的 g_s1 / g_s2 / g_s3，那三行就显示我们的内容，
走的是原厂渲染路径，不需要调用任何绘制函数。

用法:
    python tools/patch_strings.py <in.bin> --sym tools/stub/dso_patch.sym -o <out.bin>
"""
import argparse
import hashlib
import struct
import sys

BASE = 0x08007000
FLASH_LO = 0x08000000
FLASH_HI = 0x08040000

VER_BASE = 0x080215FC
MOD_BASE = 0x08021600
CAU_BASE = 0x08021640


def read_syms(path):
    out = {}
    for line in open(path, encoding="utf-8"):
        p = line.split()
        if len(p) >= 3:
            try:
                out[p[2]] = int(p[0], 16)
            except ValueError:
                pass
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("--sym", required=True)
    ap.add_argument("-o", "--out", default="patched.bin")
    a = ap.parse_args()

    sym = read_syms(a.sym)
    for k in ("g_s1", "g_s2", "g_s3"):
        if k not in sym:
            print("符号表里没有 %s" % k)
            return 2
    ram = {k: sym[k] for k in ("g_s1", "g_s2", "g_s3")}
    for k, v in ram.items():
        if not (0x20000000 <= v < 0x2000B000):
            print("%s = 0x%08X 不在 RAM 区" % (k, v))
            return 2

    d = bytearray(open(a.src, "rb").read())
    changed = []

    def put(addr, val, tag):
        off = addr - BASE
        old = struct.unpack_from("<I", d, off)[0]
        if not (FLASH_LO <= old < FLASH_HI):
            print("[!] 0x%08X 原值 0x%08X 不像 flash 指针，跳过" % (addr, old))
            return False
        struct.pack_into("<I", d, off, val)
        changed.append((addr, old, val, tag))
        return True

    for L in range(1, 9):
        put(VER_BASE + 8 * L, ram["g_s1"], "version L%d" % L)
        put(MOD_BASE + 8 * L, ram["g_s2"], "model   L%d" % L)
        put(CAU_BASE + 4 * L, ram["g_s3"], "caution L%d" % L)

    open(a.out, "wb").write(d)
    print("改了 %d 个指针：" % len(changed))
    for addr, old, new, tag in changed:
        print("  0x%08X  %s  0x%08X -> 0x%08X" % (addr, tag, old, new))
    print("\n输出 %s  %d 字节" % (a.out, len(d)))
    print("sha256 %s" % hashlib.sha256(d).hexdigest())
    return 0


if __name__ == "__main__":
    sys.exit(main())
