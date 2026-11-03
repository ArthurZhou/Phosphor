#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phosphor 命令行采集工具

用于验证数据通道、批量取数与导出样本。图形界面见 phosphor_host.py。

用法:
    python tools/host/phosphor_cli.py --list
    python tools/host/phosphor_cli.py                      # 单帧原始数据
    python tools/host/phosphor_cli.py --port COM13 --frames 5
    python tools/host/phosphor_cli.py --screen             # 屏幕波形 301 点
    python tools/host/phosphor_cli.py --stream --frames 20 # 连续模式
    python tools/host/phosphor_cli.py --csv wave.csv
    python tools/host/phosphor_cli.py --simulate --frames 2
    python tools/host/phosphor_cli.py --diag           # 读取设备栈用量
    python tools/host/phosphor_cli.py --fault          # 读取上次死机的现场

退出码:
    0  成功
    1  未收到数据
    2  参数或环境错误
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import phosphor_proto as proto


def render_ascii(samples: bytes, columns: int = 96, height: int = 12) -> list[str]:
    """以字符画形式输出波形，每列显示该区间的最小值与最大值。"""
    n = len(samples)
    if n == 0:
        return []
    lo, hi = min(samples), max(samples)
    if hi == lo:
        hi = lo + 1
    grid = [[" "] * columns for _ in range(height)]
    step = max(1, n // columns)
    for c in range(columns):
        seg = samples[c * step:(c + 1) * step]
        if not seg:
            seg = samples[-1:]
        top = (height - 1) - (max(seg) - lo) * (height - 1) // (hi - lo)
        bottom = (height - 1) - (min(seg) - lo) * (height - 1) // (hi - lo)
        for r in range(min(top, bottom), max(top, bottom) + 1):
            grid[r][c] = "#"
    return ["".join(row) for row in grid]


def collect(link, parser, count: int, timeout: float, command: int | None) -> list:
    """采集指定帧数。

    command 非空时按帧重复下发命令（单帧模式）；为空时用于连续模式，
    仅在启动时下发过一次命令。
    """
    frames = []
    while len(frames) < count:
        if command is not None:
            link.write(bytes([command]))
            link.flush()
        before = len(frames)
        deadline = time.time() + timeout
        while len(frames) < count and time.time() < deadline:
            chunk = link.read(8192)
            if chunk:
                frames.extend(parser.feed(chunk))
        if len(frames) == before:
            break
    return frames[:count]


def main() -> int:
    ap = argparse.ArgumentParser(description="Phosphor 命令行采集工具")
    ap.add_argument("-p", "--port", help="串口名，缺省自动查找 2E3C:5740")
    ap.add_argument("-n", "--frames", type=int, default=1, help="采集帧数")
    ap.add_argument("--screen", action="store_true", help="采集屏幕波形（301 点）")
    ap.add_argument("--stream", action="store_true", help="以连续模式采集")
    ap.add_argument("--csv", help="将样本写入 CSV 文件")
    ap.add_argument("--simulate", action="store_true", help="使用内置仿真设备")
    ap.add_argument("--strict", action="store_true",
                    help="丢弃校验异常且非参照值的帧")
    ap.add_argument("--no-plot", action="store_true", help="不输出字符波形")
    ap.add_argument("--timeout", type=float, default=3.0, help="单帧等待秒数")
    ap.add_argument("--list", action="store_true", help="列出串口")
    ap.add_argument("--diag", action="store_true",
                    help="读取设备诊断信息（栈峰值等，命令 0x04）")
    ap.add_argument("--fault", action="store_true",
                    help="读取上次 HardFault 现场（命令 0x05）")
    ap.add_argument("--key", type=int, metavar="N",
                    help="注入键码 N（1..14，命令 0x06），等效于按下对应按键")
    ap.add_argument("--repeat", type=int, default=1, metavar="N",
                    help="与 --key 配合，重复注入 N 次（1..10，默认 1）")
    ap.add_argument("--keys", action="store_true", help="列出键码含义")
    ap.add_argument("--settings", action="store_true",
                    help="读取设置结构 0x2000A528 的 64 字节快照（命令 0x07）")
    args = ap.parse_args()

    if args.keys:
        print("键码  含义")
        for code in sorted(proto.KEY_NAMES):
            print("  %2d   %s" % (code, proto.KEY_NAMES[code]))
        print("\n上/下与拨轮改哪一项取决于设备上的 MODE 选中项。")
        return 0

    if args.list:
        ports = proto.list_ports()
        if not ports:
            print("未发现串口。")
            return 0
        for p in ports:
            print("%-8s %-42s %s" % (p.device,
                                     p.description or "-",
                                     "2E3C:5740" if p.is_target else "-"))
        return 0

    if args.simulate:
        link = proto.SimulatedSerial()
        port_name = "仿真设备"
    else:
        port = args.port or proto.find_port()
        if not port:
            print("未找到 Phosphor 设备（2E3C:5740）。可用 --list 查看串口，"
                  "或用 --simulate 进行离线验证。")
            return 2
        link = proto.open_port(port)
        port_name = port

    parser = proto.FrameParser()

    if args.key:
        n = max(1, min(10, args.repeat))
        for i in range(n):
            link.write(bytes([proto.CMD_KEY, args.key & 0xFF]))
            link.flush()
            if n > 1 and i + 1 < n:
                time.sleep(0.06)              # 按键队列 20 项，别一次灌满
        print("端口: %s   已注入 %s（键码 %d）× %d"
              % (port_name, proto.KEY_NAMES.get(args.key & 0xFF, "未标定"),
                 args.key & 0xFF, n))
        if not args.simulate:
            link.close()
        return 0

    if args.diag:
        cmd = proto.CMD_DIAG
    elif args.fault:
        cmd = proto.CMD_FAULT
    elif args.settings:
        cmd = proto.CMD_SETTINGS
    else:
        cmd = proto.CMD_SCREEN if args.screen else (
            proto.CMD_STREAM if args.stream else proto.CMD_SINGLE)
    if args.stream:
        link.write(bytes([cmd]))
        link.flush()
        repeat = None
    else:
        repeat = cmd

    print("端口: %s   命令: 0x%02X   期望帧数: %d" % (port_name, cmd, args.frames))

    frames = collect(link, parser, args.frames, args.timeout, repeat)
    if args.strict:
        frames = [f for f in frames if f.checksum_ok or f.checksum_advisory]

    if args.stream:
        link.write(bytes([proto.CMD_STOP]))
    if not args.simulate:
        link.close()

    if not frames:
        print("未收到完整帧。请确认设备已枚举为串口，且固件版本支持本协议。")
        return 1

    if args.diag:
        info = proto.decode_diag(frames[-1].samples)
        if info is None:
            print("  诊断帧格式不符（%d 字节，首 4 字节 %r）。固件可能不支持命令 0x04。"
                  % (len(frames[-1].samples), bytes(frames[-1].samples[:4])))
            return 1
        print("  栈区间  0x%08X .. 0x%08X   共 %d 字节"
              % (info["stack_base"], info["stack_top"], info["stack_total"]))
        print("  栈峰值  %d 字节（最深到 0x%08X）" % (info["stack_peak"], info["min_sp"]))
        print("  剩余    %d 字节" % info["stack_free"])
        print("  水印    %s" % ("已铺设" if info["painted"] else "未铺设，数值无效"))
        print("  发送失败 %d 次" % info["tx_errors"])
        if info["stack_peak"] >= info["stack_total"]:
            print("  [!!] 栈已被用尽，必然溢出到补丁变量区")
        if info["stack_free"] < 512:
            print("  [!!] 余量不足 512 字节，AUTO 等深调用链仍有溢出风险")
        return 0

    if args.settings:
        s = bytes(frames[-1].samples)
        print("  设置结构 0x2000A528，%d 字节" % len(s))
        for off in range(0, len(s), 16):
            row = s[off:off + 16]
            print("  +%02X  %s  %s" % (
                off,
                " ".join("%02X" % b for b in row),
                "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in row)))
        return 0

    if args.fault:
        info = proto.decode_fault(frames[-1].samples)
        if info is None:
            print("  本次上电没有记录到 HardFault（收到 %d 字节，魔数不符）。"
                  % len(frames[-1].samples))
            return 0
        print("  故障次数 %d（同一上电周期内，超过 3 次不再自动复位）" % info["count"])
        print("  PC=0x%08X  LR=0x%08X  xPSR=0x%08X" % (info["pc"], info["lr"], info["xpsr"]))
        print("  R0=0x%08X R1=0x%08X R2=0x%08X R3=0x%08X R12=0x%08X"
              % (info["r0"], info["r1"], info["r2"], info["r3"], info["r12"]))
        print("  CFSR=0x%08X  HFSR=0x%08X  MMFAR=0x%08X  BFAR=0x%08X"
              % (info["cfsr"], info["hfsr"], info["mmfar"], info["bfar"]))
        for line in proto.fault_explain(info):
            print("  → " + line)
        print("  PC 落在补丁区（0x08035B00 起）则先看补丁，否则查原厂 APP")
        return 0

    started = time.time()
    for i, f in enumerate(frames, 1):
        st = proto.SignalStats.from_samples(f.samples)
        print("  帧 %2d  seq=%-5d 点数=%4d 标志=0x%02X 校验=%s"
              % (i, f.seq, f.count, f.flags, f.checksum_text))
        print("        最小=%3d 最大=%3d 峰峰=%3d 均值=%6.2f RMS=%6.2f"
              % (st.minimum, st.maximum, st.peak_to_peak, st.mean, st.rms))

    total_bytes = sum(f.count for f in frames)
    print("  合计 %d 帧 / %d 字节" % (len(frames), total_bytes))
    if parser.bytes_discarded:
        print("  丢弃无效字节 %d（重新同步）" % parser.bytes_discarded)
    advisory = sum(1 for f in frames
                   if not f.checksum_ok and f.checksum_advisory)
    invalid = sum(1 for f in frames
                  if not f.checksum_ok and not f.checksum_advisory)
    if advisory:
        print("  校验为参照值 %d 帧（实时缓冲在发送期间被覆盖，见 docs/PROTOCOL.md）"
              % advisory)
    if invalid:
        print("  校验异常 %d 帧（传输或同步错误）" % invalid)

    if not args.no_plot:
        print()
        for line in render_ascii(frames[-1].samples):
            print("  |" + line)

    if args.csv:
        with open(args.csv, "w", encoding="utf-8", newline="") as fh:
            fh.write("frame,index,code\n")
            for fi, f in enumerate(frames):
                for i, v in enumerate(f.samples):
                    fh.write("%d,%d,%d\n" % (fi, i, v))
        print("样本已写入 %s（%d 行）" % (args.csv, total_bytes))

    del started
    return 0


if __name__ == "__main__":
    sys.exit(main())
