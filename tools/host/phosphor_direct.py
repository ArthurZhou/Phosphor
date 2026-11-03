#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""直接内存操作工具（调试用）

配合 R25 以上固件的命令：

    0x0D  设定档位      角色 + 目标
    0x0E  写 SRAM       地址(4) + 长度(1) + 数据
    0x0F  重新应用      角色（按当前字段值重跑原厂刷新）

用法::

    # 看当前设置结构
    python tools/host/phosphor_direct.py --dump

    # 直接设定（写字段 + 调原厂刷新）
    python tools/host/phosphor_direct.py --set-vertical 8
    python tools/host/phosphor_direct.py --set-timebase 12
    python tools/host/phosphor_direct.py --set-probe 1

    # 手工写字段，再单独触发刷新
    python tools/host/phosphor_direct.py --write 0x01=8
    python tools/host/phosphor_direct.py --apply vertical

    # 一次改多个字段（例如底栏选中项连同菜单滚动位置）
    python tools/host/phosphor_direct.py --write 0x0E=1 --write 0x64=8 --apply vertical

角色名：timebase / vertical / probe（也接受 1 / 2 / 3）。
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import phosphor_proto as proto
import phosphor_steps as steps

ROLE_ALIAS = {
    "timebase": proto.PARAM_TIMEBASE, "tb": proto.PARAM_TIMEBASE,
    "1": proto.PARAM_TIMEBASE,
    "vertical": proto.PARAM_VOLTAGE, "voltage": proto.PARAM_VOLTAGE,
    "v": proto.PARAM_VOLTAGE, "2": proto.PARAM_VOLTAGE,
    "probe": proto.PARAM_PROBE, "pr": proto.PARAM_PROBE,
    "3": proto.PARAM_PROBE,
}

#: 与固件 0x0E 的放行范围一致
WRITE_RANGES = ((0x20000000, 0x20000300), (0x20004F80, 0x20005000))


def open_link(port: str | None, simulate: bool):
    if simulate:
        return proto.SimulatedSerial(), "仿真设备"
    if not port:
        target = next((p for p in proto.list_ports() if p.is_target), None)
        if target is None:
            raise SystemExit("未发现目标设备，请用 --port 指定串口")
        port = target.device
    return proto.open_port(port), port


def read_window(link, parser, addr: int, n: int,
                timeout: float = 1.0) -> bytes | None:
    cmd = bytes([proto.CMD_RAMREAD]) + int(addr).to_bytes(4, "little") + \
        bytes([n])
    for _ in range(3):
        link.write(cmd)
        link.flush()
        deadline = time.time() + timeout
        while time.time() < deadline:
            chunk = link.read(4096)
            if not chunk:
                continue
            for frame in parser.feed(chunk):
                if len(frame.samples) == n:
                    return bytes(frame.samples)
        time.sleep(0.05)
    return None


def dump(link, parser, window: int = 128) -> None:
    data = read_window(link, parser, proto.STRUCT_ADDR, window)
    if data is None:
        print("读取失败（需要命令 0x08 支持）")
        return
    known = {
        proto.TIMEBASE_OFFSET: "时基",
        proto.VOLTAGE_OFFSET: "垂直",
        proto.PROBE_OFFSET: "探头",
    }
    print("设置结构 0x%08X（%d 字节）" % (proto.STRUCT_ADDR, len(data)))
    for off in range(0, len(data), 8):
        row = data[off:off + 8]
        marks = []
        for i in range(len(row)):
            note = known.get(off + i)
            if note:
                marks.append("+%02X=%d %s" % (off + i, row[i], note))
        line = "  +%02X  %s" % (off, " ".join("%3d" % b for b in row))
        if marks:
            line += "   | " + ", ".join(marks)
        print(line)
    page = read_window(link, parser, proto.SLOT_ADDR, 1, 0.5)
    if page is not None:
        print("  页面 0x%08X = %d" % (proto.SLOT_ADDR, page[0]))

    for role, off, table in (
            ("timebase", proto.TIMEBASE_OFFSET, proto.TIMEBASE_STEPS),
            ("voltage", proto.VOLTAGE_OFFSET, proto.VOLTAGE_STEPS),
            ("probe", proto.PROBE_OFFSET, proto.PROBE_STEPS)):
        idx = data[off]
        name = table[idx] if 0 <= idx < len(table) else "越界"
        print("  %-9s 索引 %-3d → %s" % (role, idx, name))


def write_bytes(link, addr: int, payload: bytes) -> None:
    link.write(bytes([proto.CMD_WRITE_RAM]) + int(addr).to_bytes(4, "little")
               + bytes([len(payload)]) + payload)
    link.flush()


def main() -> int:
    ap = argparse.ArgumentParser(description="直接内存操作（调试）")
    ap.add_argument("--port")
    ap.add_argument("--simulate", action="store_true")
    ap.add_argument("--dump", action="store_true", help="打印设置结构")
    ap.add_argument("--set-timebase", type=int, metavar="N")
    ap.add_argument("--set-vertical", type=int, metavar="N")
    ap.add_argument("--set-probe", type=int, metavar="N")
    ap.add_argument("--write", action="append", default=[],
                    metavar="OFF=VAL", help="写结构内字段，可重复")
    ap.add_argument("--apply", help="按当前字段值重新应用：timebase/vertical/probe")
    args = ap.parse_args()

    link, in_use = open_link(args.port, args.simulate)
    parser = proto.FrameParser()
    print("已连接 %s" % in_use)

    try:
        if args.dump or not any((args.set_timebase is not None,
                                 args.set_vertical is not None,
                                 args.set_probe is not None,
                                 args.write, args.apply)):
            dump(link, parser)

        if args.write:
            for item in args.write:
                if "=" not in item:
                    raise SystemExit("--write 需要 OFF=VAL，例如 0x01=8")
                off_s, val_s = item.split("=", 1)
                off = int(off_s, 0)
                val = int(val_s, 0) & 0xFF
                addr = proto.STRUCT_ADDR + off
                if not any(lo <= addr < hi for lo, hi in WRITE_RANGES):
                    raise SystemExit("偏移 0x%02X 超出可写范围" % off)
                write_bytes(link, addr, bytes([val]))
                print("写入 +0x%02X = %d" % (off, val))
            time.sleep(0.1)

        for flag, role in ((args.set_timebase, "timebase"),
                           (args.set_vertical, "vertical"),
                           (args.set_probe, "probe")):
            if flag is None:
                continue
            code = ROLE_ALIAS[role]
            link.write(bytes([proto.CMD_SET_PARAM, code, flag & 0xFF]))
            link.flush()
            print("设定 %s → 索引 %d" % (role, flag))
            time.sleep(0.4)

        if args.apply:
            key = args.apply.strip().lower()
            if key not in ROLE_ALIAS:
                raise SystemExit("--apply 只接受 timebase / vertical / probe")
            link.write(bytes([proto.CMD_APPLY, ROLE_ALIAS[key]]))
            link.flush()
            print("已请求重新应用 %s" % key)
            time.sleep(0.4)

        if args.set_timebase is not None or args.set_vertical is not None \
                or args.set_probe is not None or args.apply:
            dump(link, parser)
    finally:
        link.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
