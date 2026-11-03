#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校验 img.c 实际发出的字节流（宿主机原生编译跑出来的真代码）。

流程：tools/test/run.sh 把 img_test_main.c 与 img.c 同编成原生可执行文件，
      运行后字节流落盘 stream.bin
      本脚本用生产用的解析器/画布还原 -> 与源画面逐像素比对

检查项：
  1. 未武装时一个字节都不发
  2. 块描述（x/y/w/h）、负载长度、校验、序号全部成立
  3. 逐像素与原画面一致（宽块与半宽块混排，含覆盖写）
  4. 预算递减：arm(2) 只发 2 块
  5. 非法窗口（w=0）被拒绝，img_err = 2
  6. 画布能输出 BMP/PNG
"""
import json
import os
import struct
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "tools", "host"))

import phosphor_proto as proto

W, H = 320, 240
EXE = os.path.join(HERE, "img_test.exe" if os.name == "nt" else "img_test")
STREAM = os.path.join(HERE, "stream.bin")


def pat(x, y):
    return ((x & 0x1F) << 11) | ((y & 0x3F) << 5) | ((x + y) & 0x1F)


def run_harness():
    out = subprocess.run(
        [EXE, STREAM],
        capture_output=True, text=True)
    if out.returncode != 0:
        print("测试驱动执行失败：\n%s%s" % (out.stdout, out.stderr))
        sys.exit(2)
    return json.loads(out.stdout.strip().splitlines()[-1])


def main() -> int:
    if not os.path.exists(EXE):
        print("先运行 tools/test/run.sh 编译测试驱动")
        return 2
    info = run_harness()
    print("设备侧统计:", json.dumps(info, ensure_ascii=False))

    data = open(STREAM, "rb").read()
    parser = proto.FrameParser()
    frames = parser.feed(data)

    errs = []
    if info["idle_bytes"] != 0:
        errs.append("未武装却发出了 %d 字节" % info["idle_bytes"])
    if info["bad_bytes"] != 0:
        errs.append("非法窗口（w=0）仍发出了 %d 字节" % info["bad_bytes"])
    if info["bad_err"] != 2:
        errs.append("非法窗口未置 img_err=2（实际 %d）" % info["bad_err"])
    if info["budget_bytes"] != 2 * 15428:
        errs.append("预算 2 块却发了 %d 字节（应为 30856）" % info["budget_bytes"])
    if parser.bytes_discarded:
        errs.append("%d 字节被丢弃（帧头不同步）" % parser.bytes_discarded)
    if parser.frames_bad_chk:
        errs.append("%d 帧校验不符" % parser.frames_bad_chk)

    # 页面重绘：img_serve_redraw 按 UI 页面号（0x20004FCC）查表投绘制单元。
    # 期望值就是原厂 0x08017196 那张 tbb 表的镜像：id 0（清屏）必须剔除，
    # 其余一个不多一个不少（R33 的“补底栏”已证明是错的，见 docs/RELEASES.md）。
    expect_pages = {
        0: [9],
        1: [],                         # 原厂不重建 -> 什么都不投
        2: [1, 2, 3, 10, 11],          # 主界面
        3: [4, 5, 10],
        4: [4, 7, 10],
        5: [],
        6: [],
        7: [],
        8: [13],
        9: [1, 2, 3, 10, 11],          # 越界页面号 -> 落到主界面
    }
    for m, want in expect_pages.items():
        got = info["pages"].get(str(m))
        if got != want:
            errs.append("页面 %d 投递 %s，应为 %s" % (m, got, want))
        if got and 0 in got:
            errs.append("页面 %d 投了 id 0（清屏），设备屏会闪黑" % m)

    canvas = [[None] * W for _ in range(H)]
    bands = 0
    max_payload = 0
    for f in frames:
        if not f.is_image:
            errs.append("seq=%d 不是画面块（flags=0x%02X）" % (f.seq, f.flags))
            continue
        d = proto.decode_band(f.samples)
        if d is None:
            errs.append("seq=%d 描述字段非法（负载 %d 字节）" % (f.seq, len(f.samples)))
            continue
        x, y, w, h, px = d
        max_payload = max(max_payload, len(f.samples))
        if x + w > W or y + h > H:
            errs.append("seq=%d 越界 x=%d y=%d w=%d h=%d" % (f.seq, x, y, w, h))
            continue
        bands += 1
        for j in range(h):
            row = struct.unpack_from("<%dH" % w, px, j * w * 2)
            for i, v in enumerate(row):
                canvas[y + j][x + i] = v

    if max_payload > 4096:
        errs.append("单块负载 %d 字节，超过 4096 上限" % max_payload)

    seqs = [f.seq for f in frames]
    for i in range(1, len(seqs)):
        if (seqs[i] - seqs[i - 1]) & 0xFFFF != 1:
            errs.append("序号不连续：%d -> %d" % (seqs[i - 1], seqs[i]))
            break

    diff = 0
    first = None
    for y in range(H):
        for x in range(W):
            if canvas[y][x] != pat(x, y):
                diff += 1
                if first is None:
                    first = (x, y, pat(x, y), canvas[y][x])
    if diff:
        errs.append("%d 像素不符，首个 (x=%d,y=%d) 期望 0x%04X 实际 %s"
                    % (diff, first[0], first[1], first[2],
                       "0x%04X" % first[3] if first[3] is not None else "无数据"))

    # 画布输出（顺带验证 BMP/PNG 写入路径）
    from phosphor_capture import Canvas
    cv = Canvas()
    for f in frames:
        d = proto.decode_band(f.samples)
        if d:
            cv.put(*d[:4], d[4])
    bmp = os.path.join(HERE, "canvas.bmp")
    cv.save(bmp)
    if os.path.getsize(bmp) <= 54:
        errs.append("BMP 输出为空")

    print("块 %d  帧 %d  最大负载 %d  覆盖整行 %d/240"
          % (bands, len(frames), max_payload, cv.covered_rows()))
    if errs:
        print("\n[XX]")
        for e in errs:
            print("   - " + e)
        return 1
    print("\n[OK] 分块 / 描述 / 校验 / 序号 / 预算 / 越界 / 像素 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
