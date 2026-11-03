#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""看看 DSO-153 的 IAP 盘出来没有（只读，不写任何字节）

用法:
    python tools/host/check_iap.py          # 列出所有可移动盘，标出 AT32 Msc iap
    python tools/host/check_iap.py -v       # 顺便把 U 盘前 16 字节读出来

为什么只读就够：这个盘是 bootloader 造出来的 **虚拟 FAT**（8MB 卷、实际只放一个
固件文件），既不是 flash 映射，也拿不到 bootloader。所以这里只做"盘有没有出现、
容量多少、里面有什么"的确认，不再做 dump 分析。
"""
import argparse
import ctypes
import os
import sys

TARGET_LABEL = "AT32"
FILE_ATTR_READONLY = 0x01


def volumes():
    """返回 [(盘符, 卷标, 文件系统, 序列号)]"""
    k32 = ctypes.windll.kernel32
    mask = k32.GetLogicalDrives()
    out = []
    for i in range(26):
        if not (mask >> i) & 1:
            continue
        root = "%s:\\" % chr(ord("A") + i)
        # 只关心可移动盘（DRIVE_REMOVABLE = 2）
        if k32.GetDriveTypeW(ctypes.c_wchar_p(root)) != 2:
            continue
        name = ctypes.create_unicode_buffer(256)
        fs = ctypes.create_unicode_buffer(256)
        serial = ctypes.c_ulong()
        ok = k32.GetVolumeInformationW(
            ctypes.c_wchar_p(root), name, 256, ctypes.byref(serial),
            None, None, fs, 256)
        if ok:
            out.append((root[:2], name.value, fs.value, serial.value))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    vols = volumes()
    if not vols:
        print("没有任何可移动盘 —— IAP 盘没出来。")
        print("排查顺序：换线（九成在这）→ 换 USB 口（别用 Hub）→ 换按键组合 →")
        print("确认是关机不是待机（长按电源直到屏幕全黑）。")
        return 1

    hit = None
    print("可移动盘：")
    for dev, label, fs, _serial in vols:
        mark = ""
        if TARGET_LABEL.lower() in (label or "").lower():
            mark = "   <== IAP 盘"
            hit = dev
        print("  %-3s 卷标=%-16s 文件系统=%-6s%s" % (dev, label or "(无)", fs or "?", mark))

    if not hit:
        print("\n没找到卷标含 %r 的盘。设备可能没进 IAP 模式。" % TARGET_LABEL)
        return 1

    try:
        files = os.listdir(hit + "\\")
    except OSError as e:
        print("\n%s 打不开：%s" % (hit, e))
        return 1
    print("\n%s 根目录：" % hit)
    for f in files:
        p = os.path.join(hit + "\\", f)
        try:
            sz = os.path.getsize(p)
        except OSError:
            sz = -1
        print("  %-40s %10d 字节" % (f, sz))

    if a.verbose:
        try:
            with open(hit + "\\", "rb") as fh:
                head = fh.read(16)
            print("\n卷首 16 字节: %s" % head.hex(" "))
        except OSError as e:
            print("\n读卷首失败（正常，有的卷不允许直接读根）: %s" % e)

    print("\n把这个盘里的固件换成 firmware/APP_DSO153_diag_v18b.bin 即可升级。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
