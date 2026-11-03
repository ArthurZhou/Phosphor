#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phosphor 数据源抓取：原始采样 / 屏幕波形 / 屏幕画面，三选一

设备侧用命令 0x10 切换数据源，0x01 取一帧（画面则是一次扫屏），0x02 连续。
本工具把三种来源统一成一条命令，输出也统一：文本摘要 + 可选的 CSV / PNG。

用法:
    python tools/host/phosphor_capture.py --src image -o shot.png
    python tools/host/phosphor_capture.py --src image -o shot.png --key 5
    python tools/host/phosphor_capture.py --src raw   -o wave.csv -n 3
    python tools/host/phosphor_capture.py --src screen -o screen.csv

屏幕画面为什么是“拼”出来的
    设备没有整屏帧缓冲（可用 SRAM 只有 48KB，一帧要 153600 字节），画面是在
    每次推屏时把那一块直接发出来的。因此主机收到的是一块块矩形，按 x/y/w/h
    贴到 320×240 的画布上。

为什么现在能截到完整界面
    设备平时只重画“变脏”的区域：波形 8 条带每秒都在画，但标题栏、菜单、
    数值框只在状态变化时画一次。武装抓图后，固件按当前 UI 页面号
    （0x20004FCC）查出这一页由哪几个绘制单元组成，把它们依次排进 UI 消息队列，
    于是整页被重画一遍。这张“页面 → 单元”的表是原厂自己的定义（0x08017196），
    所以截到的是设备此刻真实的那一页，不会把别的页面的菜单叠上来。
    表里投什么就画什么，不多不少：菜单页没有底部参数条，抓图里也不会多出来。
    换页（按键）之后再抓，得到的就是新页面。
    --key 仍然可用，但一般不再需要。

退出码:
    0  成功
    1  没收到数据
    2  环境或参数错误
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import phosphor_proto as proto

SCR_W, SCR_H = 320, 240
DEFAULT_ARM_BANDS = 64          # 与固件 IMG_SWEEP_BANDS 一致


# ------------------------------------------------------------------ 画布
def rgb565_to_rgb888(v: int):
    r = (v >> 11) & 0x1F
    g = (v >> 5) & 0x3F
    b = v & 0x1F
    return (r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)


class Canvas:
    """320×240 RGB565 画布，按块贴图。"""

    def __init__(self):
        self.px = bytearray(SCR_W * SCR_H * 2)
        self.fill = [0] * SCR_H        # 每行已写入的像素数（封顶 SCR_W）
        self.bands = 0
        self.pixels = 0

    def put(self, x: int, y: int, w: int, h: int, data: bytes) -> bool:
        if w <= 0 or h <= 0 or x < 0 or y < 0:
            return False
        if x + w > SCR_W or y + h > SCR_H:
            return False
        row_bytes = w * 2
        for j in range(h):
            src = j * row_bytes
            dst = ((y + j) * SCR_W + x) * 2
            self.px[dst:dst + row_bytes] = data[src:src + row_bytes]
            self.fill[y + j] = min(SCR_W, self.fill[y + j] + w)
        self.bands += 1
        self.pixels += w * h
        return True

    # ---- 覆盖率 ----
    def covered_rows(self) -> int:
        return sum(1 for n in self.fill if n >= SCR_W)

    def gap_rows(self) -> list[int]:
        return [i for i, n in enumerate(self.fill) if n == 0]

    def partial_rows(self) -> list[int]:
        return [i for i, n in enumerate(self.fill) if 0 < n < SCR_W]

    # ---- 输出 ----
    def to_rgb(self) -> bytes:
        out = bytearray(SCR_W * SCR_H * 3)
        px = self.px
        for i in range(SCR_W * SCR_H):
            v = px[i * 2] | (px[i * 2 + 1] << 8)
            r, g, b = rgb565_to_rgb888(v)
            o = i * 3
            out[o] = r
            out[o + 1] = g
            out[o + 2] = b
        return bytes(out)

    def save(self, path: str) -> None:
        ext = os.path.splitext(path)[1].lower()
        rgb = self.to_rgb()
        if ext == ".png":
            try:
                from PIL import Image
            except ImportError:
                print("  没有 Pillow，改写同名 BMP（PNG 需要 pip install pillow）")
                path = os.path.splitext(path)[0] + ".bmp"
                self.save(path)
                return
            Image.frombytes("RGB", (SCR_W, SCR_H), rgb).save(path)
        elif ext == ".bmp":
            stride = (SCR_W * 3 + 3) & ~3
            pad = stride - SCR_W * 3
            body = bytearray()
            for y in range(SCR_H - 1, -1, -1):      # BMP 自下而上
                row = rgb[y * SCR_W * 3:(y + 1) * SCR_W * 3]
                bgr = bytearray(len(row))           # BMP 为 BGR 顺序
                bgr[0::3] = row[2::3]
                bgr[1::3] = row[1::3]
                bgr[2::3] = row[0::3]
                body += bytes(bgr)
                body += b"\x00" * pad
            hdr = b"BM" + struct.pack("<IHHI", 54 + len(body), 0, 0, 54)
            info = struct.pack("<IiiHHIIiiII", 40, SCR_W, SCR_H, 1, 24, 0,
                               len(body), 2835, 2835, 0, 0)
            open(path, "wb").write(hdr + info + bytes(body))
        else:
            raise SystemExit("输出格式只支持 .png / .bmp")
        print("  已写入 %s" % path)


# ------------------------------------------------------------------ 抓取
def open_link(port: str | None):
    port = port or proto.find_port()
    if not port:
        print("未发现串口。设备连接后重试，或用 -p 指定。")
        return None, None
    link = proto.open_port(port, timeout=0.2)
    print("端口: %s" % port)
    return link, port


def read_frames(link, parser, seconds: float, stop_when=None):
    """读一段时间的帧；stop_when(frames) 返回真则提前结束。"""
    got = []
    deadline = time.time() + seconds
    idle = time.time()
    while time.time() < deadline:
        chunk = link.read(8192)
        if chunk:
            idle = time.time()
            got.extend(parser.feed(chunk))
            if stop_when and stop_when(got):
                break
        elif time.time() - idle > 1.2:
            break                      # 静默 1.2 秒视为发完
    return got


def capture_image(link, parser, args) -> int:
    print("\n== 屏幕画面 ==")
    link.write(bytes([proto.CMD_SRC, proto.SRC_IMAGE]))
    link.flush()
    time.sleep(0.1)

    # UI 页面号（0x20004FCC）：固件按它查原厂表，决定这一页重绘哪些绘制单元。
    # 打出来是为了排障——画面不对时，先看当时的页面号是否符合预期。
    page = None
    try:
        from phosphor_diag import read_n
        raw = read_n(link, 0x20004FCC, 1, timeout=0.6)
    except Exception:
        raw = None
    if raw:
        page = raw[0]
        print("  UI 页面号 %d" % page)

    if args.key is not None:
        print("  先注入键码 %d（可选，固件本来就会按页面重绘）" % args.key)
        link.write(bytes([proto.CMD_KEY, args.key]))
        link.flush()
        time.sleep(0.9)

    link.write(bytes([proto.CMD_SINGLE]))       # 武装一次扫屏
    link.flush()

    band_frames = []
    print("  已武装，等待页面重绘（菜单页通常 3~6 块、1 秒内；主界面约 5 秒）…",
          flush=True)

    t0 = time.time()
    last = t0
    reported = 0
    while time.time() - t0 < args.timeout:
        frames = read_frames(link, parser, 1.0)
        new = [f for f in frames if f.is_image]
        if new:
            band_frames.extend(new)
            last = time.time()
            if len(band_frames) - reported >= 16:
                reported = len(band_frames)
                print("  已收 %2d 块 …" % len(band_frames), flush=True)
        if len(band_frames) >= args.bands:
            break
        if band_frames and (time.time() - last) > 2.0:
            break                       # 静默 2 秒：整页已发完
        if not band_frames and (time.time() - t0) > 3.0:
            break                       # 3 秒一块都没收到，别干等

    # 收尾：先停（发送期间设备只受理 0x00），隔一会儿再把源切回原始采样，
    # 否则这条 0x0D 会被当时仍在发块的命令层丢掉。
    link.write(bytes([proto.CMD_STOP]))
    link.flush()
    time.sleep(0.3)
    link.write(bytes([proto.CMD_SRC, proto.SRC_RAW]))
    link.flush()

    canvas = Canvas()
    bad = 0
    latest = {}
    for f in band_frames:
        d = proto.decode_band(f.samples)
        if d is None:
            bad += 1
            continue
        x, y, w, h, px = d
        # 同一块可能被重复推送（刷新），后到的覆盖先到的
        latest[(x, y, w, h)] = px

    for (x, y, w, h), px in sorted(latest.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        canvas.put(x, y, w, h, px)

    dur = time.time() - t0
    print("  收到块 %d（去重后 %d），异常 %d，用时 %.1fs"
          % (len(band_frames), len(latest), bad, dur))
    print("  覆盖：整行 %d/240 行，全空 %d 行，半行 %d 行"
          % (canvas.covered_rows(), len(canvas.gap_rows()),
             len(canvas.partial_rows())))
    for line in describe_regions(latest):
        print("  %s" % line)
    gaps = canvas.gap_rows()
    if gaps:
        print("  完全没数据的行为 %s" % _ranges(gaps))

    # 波形 8 条带是常态刷新（(0,20+25i,320,26)），只有它们说明页面重绘没生效。
    waveish = {(0, 20 + 25 * i, 320, 26) for i in range(8)}
    n_wave = sum(1 for b in latest if b in waveish)
    n_other = len(latest) - n_wave
    if not latest:
        if page in (1, 5, 6, 7):
            print("  → 页面号 %d 在原厂固件里不重建任何绘制单元（过渡态），"
                  "换一页再抓。" % page)
        else:
            print("  → 一块都没收到：确认固件是 R34 及以上、且设备未在复位。")
        return 1
    print("  波形带 %d/8，其他块 %d" % (n_wave, n_other))
    if n_other == 0:
        print("  → 只有波形带：页面重绘没有生效（确认固件 R34 及以上）。")

    if bad:
        print("  有 %d 块长度不符（固件与主机版本不一致？）" % bad)

    if args.out:
        canvas.save(args.out)
    return 0 if latest else 1


def describe_regions(blocks) -> list[str]:
    """把逐行切开的块并回成“区域”，便于一眼看出这一页画了哪几块。

    固件按 6 行一段推送，所以一个 320x32 的标题栏会变成 6 个块。这里按
    (x, w) 分组、y 相邻的合并，输出 2~5 行几何描述。"""
    groups = {}
    for (x, y, w, h) in blocks:
        groups.setdefault((x, w), []).append((y, h))
    merged = []
    for (x, w), ys in groups.items():
        ys.sort()
        cy, ch, n = ys[0][0], ys[0][1], 1
        for y, h in ys[1:]:
            if y <= cy + ch + 2:
                cy_end = max(cy + ch, y + h)
                ch = cy_end - cy
                n += 1
            else:
                merged.append((cy, x, w, ch, n))
                cy, ch, n = y, h, 1
        merged.append((cy, x, w, ch, n))
    merged.sort()

    out = []
    for (y, x, w, h, n) in merged:
        tag = ""
        if w >= 300 and y <= 2:
            tag = "  ← 整宽标题栏"
        elif y + h >= 220:
            tag = "  ← 贴着屏幕底部"
        out.append("区域 x=%d y=%d %dx%d（%d 块）%s" % (x, y, w, h, n, tag))
    return out


def _ranges(rows: list[int]) -> str:
    if not rows:
        return "-"
    out = []
    start = prev = rows[0]
    for r in rows[1:]:
        if r == prev + 1:
            prev = r
            continue
        out.append("%d" % start if start == prev else "%d-%d" % (start, prev))
        start = prev = r
    out.append("%d" % start if start == prev else "%d-%d" % (start, prev))
    return ", ".join(out)


def capture_points(link, parser, args, src: int, name: str) -> int:
    print("\n== %s ==" % name)
    link.write(bytes([proto.CMD_SRC, src]))
    link.flush()
    time.sleep(0.1)

    frames = []
    for _ in range(args.count):
        link.write(bytes([proto.CMD_SINGLE]))
        link.flush()
        got = read_frames(link, parser, args.timeout,
                          lambda fs: len(fs) >= 1)
        frames.extend(got[:1])
        if not got:
            break

    link.write(bytes([proto.CMD_SRC, proto.SRC_RAW]))
    link.flush()

    if not frames:
        print("  没收到数据")
        return 1

    for i, f in enumerate(frames):
        s = f.samples
        print("  帧 %d  seq=%d  %d 点  校验=%s  最小 %d 最大 %d 均值 %.1f"
              % (i, f.seq, len(s), f.checksum_text, min(s), max(s),
                 sum(s) / len(s)))
    if args.out:
        path = args.out
        if args.count > 1:
            base, ext = os.path.splitext(path)
            path = "%s_%d%s" % (base, 0, ext)
        with open(path, "w", encoding="utf-8") as fp:
            fp.write("index,%s\n" % name)
            for i, v in enumerate(frames[0].samples):
                fp.write("%d,%d\n" % (i, v))
        print("  已写入 %s（第一帧）" % path)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="按数据源抓取：原始采样 / 屏幕波形 / 屏幕画面")
    ap.add_argument("-p", "--port", help="串口名，缺省自动识别 2E3C:5740")
    ap.add_argument("--src", choices=("raw", "screen", "image"), default="image",
                    help="数据源（默认 image）")
    ap.add_argument("-o", "--out", help="输出文件：image→png/bmp，其余→csv")
    ap.add_argument("-n", "--count", type=int, default=1, help="帧数（raw/screen）")
    ap.add_argument("--bands", type=int, default=DEFAULT_ARM_BANDS,
                    help="画面块预算（默认 %d；主界面约 40 块，菜单页 3~6 块）" % DEFAULT_ARM_BANDS)
    ap.add_argument("--key", type=int, metavar="N",
                    help="抓画面前先注入键码 N（一般不需要，固件会自动请求页面重绘）")
    ap.add_argument("--timeout", type=float, default=25.0, help="总等待秒数")
    args = ap.parse_args()

    link, _ = open_link(args.port)
    if link is None:
        return 2
    parser = proto.FrameParser()

    try:
        if args.src == "image":
            return capture_image(link, parser, args)
        if args.src == "screen":
            return capture_points(link, parser, args, proto.SRC_SCREEN, "screen_y")
        return capture_points(link, parser, args, proto.SRC_RAW, "adc")
    finally:
        try:
            link.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
