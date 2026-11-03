#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""设置结构差分探测器

用你在设备上的真实操作做标定：每一步先读一遍设置结构 0x2000001C 起的
128 字节，等你按提示操作完，再读一遍并打印变化。这样得到的是「哪个字节
真的跟着某个参数走」，不依赖反汇编推断。

用法::

    python tools/host/phosphor_probe.py                 # 自动找端口
    python tools/host/phosphor_probe.py --port COM13
    python tools/host/phosphor_probe.py --watch         # 只监视变化，不提示

每步之间按回车继续。结果同时写入 phosphor_probe_report.txt。
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import phosphor_proto as proto

STRUCT_ADDR = proto.STRUCT_ADDR
WINDOW = 128

# 已知 / 待确认的字段，仅用于打印注释
NOTES = {
    0x00: "未知(0/1)",
    0x01: "垂直档位 0..8",
    0x03: "探头倍率 0/1",
    0x04: "未知",
    0x0D: "时基 0..23",
    0x0E: "底栏选中项 1..7",
    0x59: "信号发生器波形类型 0..13",
    0x60: "垂直偏移/位置?",
    0x61: "垂直增益?",
    0x64: "菜单滚动位置",
    0x65: "未知",
    0x66: "设置页行选择",
}


class Reader:
    """直接读设置结构的一个字节窗口。"""

    def __init__(self, link, parser):
        self.link = link
        self.parser = parser

    def read_at(self, addr: int, n: int, timeout: float = 1.0) -> bytes | None:
        cmd = (bytes([proto.CMD_RAMREAD])
               + int(addr).to_bytes(4, "little") + bytes([n]))
        for _ in range(2):
            self.link.write(cmd)
            self.link.flush()
            deadline = time.time() + timeout
            while time.time() < deadline:
                chunk = self.link.read(4096)
                if not chunk:
                    continue
                for frame in self.parser.feed(chunk):
                    if len(frame.samples) == n:
                        return bytes(frame.samples)
            time.sleep(0.05)
        return None

    def read(self, n: int = WINDOW, timeout: float = 1.0) -> bytes | None:
        return self.read_at(STRUCT_ADDR, n, timeout)

    def read_page(self) -> int | None:
        b = self.read_at(proto.SLOT_ADDR, 1, 0.5)
        return None if b is None else b[0]


def hexdump(data: bytes) -> list[str]:
    out = []
    for off in range(0, len(data), 8):
        row = data[off:off + 8]
        out.append("  +%02X  %s" % (off, " ".join("%02X" % b for b in row)))
    return out


def diff_text(old: bytes, new: bytes) -> list[str]:
    out = []
    for i in range(min(len(old), len(new))):
        if old[i] == new[i]:
            continue
        note = NOTES.get(i)
        out.append("  +0x%02X  %3d -> %-3d  (差 %+d)%s"
                   % (i, old[i], new[i], new[i] - old[i],
                      ("   ← " + note) if note else ""))
    return out


STEPS = [
    ("在设备上把「垂直档位」调高 1 档（轮盘右键 / 上键，视当前选中项而定）", "vertical+"),
    ("再把「垂直档位」调低 1 档（回到原档）", "vertical-"),
    ("在设备上切换「探头倍率」为另一档（×1 ↔ ×10）", "probe"),
    ("在设备上把「时基」调高 1 档", "timebase+"),
    ("在设备上把「时基」调低 1 档（回到原档）", "timebase-"),
    ("在设备上切换「AC / DC 耦合」", "coupling"),
    ("在设备上把「触发电平」调高一点", "trigger"),
]


def main() -> int:
    ap = argparse.ArgumentParser(description="设置结构差分探测")
    ap.add_argument("--port", help="串口名，如 COM13")
    ap.add_argument("--window", type=int, default=WINDOW)
    ap.add_argument("--watch", action="store_true",
                    help="只监视变化，不逐步提示")
    ap.add_argument("--simulate", action="store_true",
                    help="用内置仿真设备演示（无硬件）")
    ap.add_argument("--out", default="phosphor_probe_report.txt")
    args = ap.parse_args()

    port = args.port
    if args.simulate:
        print("仿真模式：无硬件演示，按键效果不会自己产生。")
        link = proto.SimulatedSerial()
    else:
        if not port:
            ports = proto.list_ports()
            target = next((p for p in ports if p.is_target), None)
            if target is None:
                print("未发现目标设备（%04X:%04X），请先连接并指定 --port"
                      % (proto.VID, proto.PID))
                for p in ports:
                    print("  " + p.label())
                return 2
            port = target.device
            print("自动选中端口 %s" % target.label())

        link = proto.open_port(port)
    rd = Reader(link, proto.FrameParser())
    report: list[str] = ["Phosphor 设置结构差分报告  端口 %s" % port]

    base = rd.read(args.window)
    if base is None:
        print("读取失败：设备无响应或固件不支持命令 0x08")
        link.close()
        return 1
    print("基址 0x%08X，读取 %d 字节成功。" % (STRUCT_ADDR, len(base)))

    if args.watch:
        print("监视模式：在设备上操作，Ctrl+C 结束。\n")
        prev = base
        try:
            while True:
                cur = rd.read(args.window)
                if cur is not None:
                    d = diff_text(prev, cur)
                    if d:
                        print("%s 变化:" % time.strftime("%H:%M:%S"))
                        print("\n".join(d))
                        prev = cur
                time.sleep(0.15)
        except KeyboardInterrupt:
            pass
        report.append("监视模式，已结束。")
    else:
        try:
            for text, tag in STEPS:
                print("\n" + "-" * 66)
                print("[%s] %s" % (tag, text))
                input("    操作完成后按回车…")
                time.sleep(0.35)
                cur = rd.read(args.window)
                if cur is None:
                    line = "[%s] 读取失败" % tag
                else:
                    d = diff_text(base, cur)
                    line = "[%s] %s" % (tag, text)
                    report.append("")
                    report.append(line)
                    if d:
                        print("\n".join(d))
                        report.extend(d)
                    else:
                        print("  无变化")
                        report.append("  无变化")
                    pg = rd.read_page()
                    meta = "  页面 0x20004FCC=%s" % (
                        "读失败" if pg is None else pg)
                    print(meta)
                    report.append(meta)
                    # 以本次为新的基线，下一步只看相对变化
                    base = cur
        except KeyboardInterrupt:
            print("\n已中断。")

    link.close()
    try:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write("\n".join(report) + "\n")
        print("\n报告已写入 %s" % os.path.abspath(args.out))
    except OSError as exc:
        print("报告写入失败：%s" % exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
