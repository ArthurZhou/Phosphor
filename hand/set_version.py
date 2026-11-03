#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
改写固件里"关于"页显示的版本号（7 种语言一次全改）。

为什么要动冒号后的空格：
  原串是  "Version: V1.6<00><00>"，V1.6 后面只有 2 个 0 的空间。
  直接写 "V1.6-1"（6 字节）会把结尾的 0 吃掉，界面会连读下一串
  （"V1.6-1 Model : DSO-153"）。
  所以把 ": V1.6" 换成 ":V1.6-1"，从冒号起算正好 8 字节，与原来
  ": V1.6<00><00>" 的 8 字节等长，且保留结尾 0。

用法:
    python tools/set_version.py <输入.bin> <新版本号> [-o 输出.bin]
例:
    python tools/set_version.py APP_DSO153_v2_usb.bin V1.6-1
"""
import argparse
import hashlib
import struct
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
        # 从 V1.6 往前找冒号，最多回退 21 字节（最长的是俄语 "Версия : "）
        q = i - 1
        while q >= 0 and i - q <= 21 and d[q] != 0x3A:
            q -= 1
        if d[q] != 0x3A:
            print("  [!] 0x%08X 处的 V1.6 找不到前面的冒号，跳过" % (BASE + i))
            i += 4
            continue
        span = i + 6 - q                    # 含冒号、空格、V1.6、两个 0
        new = b":" + ver + b"\x00"
        if len(new) > span:
            print("  [X] 0x%08X 处空间不够：需要 %d 字节，只有 %d"
                  % (BASE + i, len(new), span))
            return None, hits
        d[q:i + 6] = new + b"\x00" * (span - len(new))
        hits.append((BASE + q, bytes(d[q:q + span])))
        i += 6
    return d, hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("ver")
    ap.add_argument("-o", "--out", default=None)
    a = ap.parse_args()

    data = bytearray(open(a.src, "rb").read())
    out, hits = patch(data, a.ver)
    if out is None:
        return 1
    dst = a.out or a.src.replace(".bin", "") + "_%s.bin" % a.ver.replace(".", "").replace("-", "d")
    open(dst, "wb").write(out)
    print("改了 %d 处：" % len(hits))
    for addr, seg in hits:
        print("  0x%08X  ->  %r" % (addr, seg))
    print("\n输出: %s  (%d 字节)" % (dst, len(out)))
    print("sha256 %s" % hashlib.sha256(bytes(out)).hexdigest())
    print("\n刷入后在 设置 -> 关于 里应看到 版本号 : %s" % a.ver)
    return 0


if __name__ == "__main__":
    sys.exit(main())
