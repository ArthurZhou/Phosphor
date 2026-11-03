#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""改写固件中的版本号字符串（7 种语言一并处理）

版本号位于设置页的 "Version: V1.6" 字符串中，后接两个 0 字节。
可写字段自冒号起共 8 字节（": V1.6\\0\\0"）。为保证写入后仍以 0 结尾且不
与相邻字符串连读，改写时去掉冒号后的空格：

    ": V1.6\\0\\0"  ->  ":V1.6R1\\0"      共 8 字节

因此版本号正文最长 6 个字符，Phosphor 的命名规则 PHOS_V1.6R1 对应的
版本号字符串 "V1.6R1" 恰好为 6 个字符。

用法:
    python tools/set_version.py <输入.bin> <版本号> [-o 输出.bin]
"""
import argparse
import hashlib
import sys

BASE = 0x08007000
OLD = b"V1.6\x00\x00"


def patch(d, newver):
    ver = newver.encode("ascii")
    hits = []
    i = 0
    while True:
        i = d.find(OLD, i)
        if i < 0:
            break
        # 自 "V1.6" 向前查找冒号，最多回退 21 字节（最长的语言前缀为俄语）
        q = i - 1
        while q >= 0 and i - q <= 21 and d[q] != 0x3A:
            q -= 1
        if d[q] != 0x3A:
            print("  [!] 0x%08X 处的版本串前未找到冒号，跳过" % (BASE + i))
            i += 4
            continue
        span = i + 6 - q                    # 冒号 + 版本号 + 两个 0 字节
        new = b":" + ver + b"\x00"
        if len(new) > span:
            print("  [X] 0x%08X 处空间不足：需要 %d 字节，可用 %d"
                  % (BASE + i, len(new), span))
            return None, hits
        d[q:i + 6] = new + b"\x00" * (span - len(new))
        hits.append((BASE + q, bytes(d[q:q + span])))
        i += 6
    return d, hits


def main():
    ap = argparse.ArgumentParser(description="改写固件版本号字符串")
    ap.add_argument("src")
    ap.add_argument("ver")
    ap.add_argument("-o", "--out", default=None)
    a = ap.parse_args()

    data = bytearray(open(a.src, "rb").read())
    out, hits = patch(data, a.ver)
    if out is None:
        return 1
    dst = a.out or a.src.replace(".bin", "") + "_%s.bin" % a.ver.replace(".", "")
    open(dst, "wb").write(out)
    print("改写 %d 处：" % len(hits))
    for addr, seg in hits:
        print("  0x%08X  ->  %r" % (addr, seg))
    print("输出: %s  (%d 字节)" % (dst, len(out)))
    print("sha256 %s" % hashlib.sha256(bytes(out)).hexdigest())
    return 0


if __name__ == "__main__":
    sys.exit(main())
