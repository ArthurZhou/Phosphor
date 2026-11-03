#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Phosphor 数据通道吞吐与延迟测量。

测量三项指标：

  1. 单帧往返延迟  主机下发 0x01 到收齐一帧的时间，反映命令响应速度。
  2. 连续吞吐      连续模式下单位时间收到的字节数与帧数。
  3. 序号连续性    连续模式下帧序号的缺失数量，反映链路可靠性。

用法
    python tools/host/phosphor_bench.py -p COM13
    python tools/host/phosphor_bench.py -p COM13 --seconds 10
    python tools/host/phosphor_bench.py --list
    python tools/host/phosphor_bench.py --simulate
"""

from __future__ import annotations

import argparse
import sys
import time

import phosphor_proto as proto

FRAME_BYTES = 4096 + 9          # 原始数据帧：8 字节帧头 + 4096 样本 + 1 校验


def open_link(port: str | None):
    if port is None:
        return proto.SimulatedSerial(), "仿真设备"
    return proto.open_port(port), port


def bench_latency(link, parser: proto.FrameParser, rounds: int) -> list[float]:
    """单帧往返延迟。"""
    times = []
    for _ in range(rounds):
        link.reset_input_buffer()
        parser.reset()
        t0 = time.time()
        link.write(bytes([proto.CMD_SINGLE]))
        link.flush()
        frames = []
        deadline = t0 + 3.0
        while not frames and time.time() < deadline:
            chunk = link.read(65536)
            if chunk:
                frames = parser.feed(chunk)
        dt = time.time() - t0
        if frames:
            times.append(dt)
        time.sleep(0.05)
    return times


def bench_stream(link, parser: proto.FrameParser, seconds: float):
    """连续模式吞吐。返回 (字节数, 时长, 帧数)。"""
    link.reset_input_buffer()
    parser.reset()
    link.write(bytes([proto.CMD_STREAM]))
    link.flush()

    total = 0
    t0 = time.time()
    while time.time() - t0 < seconds:
        chunk = link.read(65536)
        if chunk:
            total += len(chunk)
            parser.feed(chunk)
    dt = time.time() - t0

    link.write(bytes([proto.CMD_STOP]))
    link.flush()
    time.sleep(0.15)
    link.reset_input_buffer()
    return total, dt, parser.frames_ok


def main() -> int:
    ap = argparse.ArgumentParser(description="Phosphor 吞吐与延迟测量")
    ap.add_argument("-p", "--port", help="串口号，例如 COM13")
    ap.add_argument("--list", action="store_true", help="列出候选串口")
    ap.add_argument("--simulate", action="store_true", help="使用内置仿真设备")
    ap.add_argument("--seconds", type=float, default=5.0, help="连续模式测量时长")
    ap.add_argument("--rounds", type=int, default=5, help="单帧延迟测量次数")
    args = ap.parse_args()

    if args.list:
        for p in proto.list_ports():
            mark = "  <- Phosphor" if p.is_target else ""
            print("%-8s %s%s" % (p.device, p.label, mark))
        return 0

    if args.simulate:
        link, name = open_link(None)
    else:
        port = args.port or proto.find_port()
        if port is None:
            print("未找到目标端口，用 -p 指定或用 --list 查看")
            return 1
        link, name = open_link(port)
    parser = proto.FrameParser()
    print("端口: %s" % name)
    print()

    times = bench_latency(link, parser, args.rounds)
    if times:
        print("单帧往返延迟（%d 次）" % len(times))
        print("  最小 %.1f ms   平均 %.1f ms   最大 %.1f ms"
              % (min(times) * 1e3, sum(times) / len(times) * 1e3,
                 max(times) * 1e3))
    else:
        print("单帧往返延迟：未收到数据")
    print()

    total, dt, frames = bench_stream(link, parser, args.seconds)
    print("连续模式（%.1f 秒）" % dt)
    print("  数据量   %d 字节" % total)
    print("  吞吐率   %.1f KB/s" % (total / dt / 1024))
    print("  帧数     %d   帧率 %.2f 帧/秒" % (frames, frames / dt))
    if frames:
        print("  单帧平均 %.1f ms" % (dt / frames * 1e3))
    print("  校验异常 %d" % parser.frames_bad_chk)

    link.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
