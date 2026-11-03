#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phosphor 诊断：复位原因 / 故障现场 / 栈用量 / 内存结构。

用法:
    python tools/host/phosphor_diag.py            # 只读：复位原因 + 故障现场 + SRAM 说明
    python tools/host/phosphor_diag.py --stack    # 附 0x04 栈用量
    python tools/host/phosphor_diag.py --ram      # 附分配器基址表 / UI 窗口结构
    python tools/host/phosphor_diag.py --probe 0x2000A568:32
    python tools/host/phosphor_diag.py --raw      # 打印 56 字节原始故障负载

SRAM 可用范围（已定论，不需要再测）
    原厂初始 SP = 0x2000B580，栈区 0x2000A580..0x2000B580；补丁变量区
    0x2000B800..0x2000C000。写 0x2000C000 与 0x2003A800 都是"写下去即关机"，
    写 0x2000B800..0x2000BFFF 则一切正常 —— 三者夹出的可用上界就是
    **0x2000C000（48 KB）**，往上没有 SRAM。

    因此"把一帧 320x240 RGB565（153600 字节）存进 SRAM"这条路不可行：整块
    可用 RAM 只有 48 KB，且已被原厂数据、栈与补丁占用；Flash 余量也只有
    52 KB。截屏相关代码在 R21 中已整体移除，本工具不再有 --fb/--dry/--test。

跨复位为什么读得到
    0x2000BF00 起的故障现场、0x2000BF40 的复位原因、0x2000BF60 的故障摘要
    都在冷启动清零范围之外，只要复位没伴随掉电就还在。
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import phosphor_proto as proto

EOPB0_ADDR = 0x1FFFC010
CRM_RST_ADDR = 0x40023874          # CRM 复位标志，bit27 POR / bit28 SW ...
BOOT_MARK_ADDR = 0x2000BF40        # __fault_rec + 64：复位原因记录
FAULT_SUM_ADDR = 0x2000BF60        # 故障摘要（冷启动不清）：魔数 + CFSR/HFSR/BFAR/PC/LR

# 实测可用 SRAM 范围（见模块说明），0x08 只放行这一段，避免诊断把设备读崩
RAM_LO, RAM_HI = 0x20000000, 0x2000C000

RST_BITS = (
    (31, "LOWPOWER"),
    (30, "WWDT"),
    (29, "WDT"),
    (28, "SW 软件复位"),
    (27, "POR 上电"),
    (26, "NRST 引脚"),
)


def read_n(ser, addr: int, n: int, timeout: float = 1.2):
    """RAMREAD（0x08）。失败返回 None。"""
    parser = proto.FrameParser()
    try:
        ser.write(bytes((0x08,)) + struct.pack("<I B", addr, n))
    except Exception:
        return None
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            chunk = ser.read(256)
        except Exception:
            return None
        if chunk:
            for fr in parser.feed(chunk):
                if not (fr.flags & (proto.FLAG_SCREEN | proto.FLAG_SETTINGS)) \
                        and len(fr.samples) == n and fr.checksum_ok:
                    return bytes(fr.samples)
    return None


def read_words(ser, addr: int, count: int, timeout: float = 1.2):
    raw = read_n(ser, addr, count * 4, timeout=timeout)
    if raw is None:
        return None
    return list(struct.unpack("<%dI" % count, raw))


def read_cmd(ser, cmd: int, want: int, timeout: float = 1.5):
    parser = proto.FrameParser()
    try:
        ser.write(bytes((cmd,)))
    except Exception:
        return None
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            chunk = ser.read(256)
        except Exception:
            return None
        if chunk:
            for fr in parser.feed(chunk):
                if len(fr.samples) == want:
                    return bytes(fr.samples)
    return None


def report_reset(ser) -> None:
    print("\n-- 复位原因 --")
    w = read_words(ser, CRM_RST_ADDR, 1)
    if w is None:
        print("   CRM 复位标志 读取失败")
    else:
        v = w[0]
        hits = [name for bit, name in RST_BITS if v & (1 << bit)]
        print("   CRM 复位标志 0x40023874 = 0x%08X  %s"
              % (v, " / ".join(hits) if hits else "（无标志位，可能已被主程序清除）"))

    w = read_words(ser, BOOT_MARK_ADDR, 4)
    if w is None:
        print("   启动标记 读取失败")
        return
    mark, snap, warm, por = w
    if mark != 0x52535421:
        print("   启动标记 0x%08X 处无标识（0x%08X）：上次上电时这块 RAM 被清"
              "过，说明上一次复位伴随掉电或整块 RAM 复位" % (BOOT_MARK_ADDR, mark))
        return
    hits = [name for bit, name in RST_BITS if snap & (1 << bit)]
    print("   冷启动快照 复位标志 = 0x%08X  %s"
          % (snap, " / ".join(hits) if hits else "（无）"))
    print("   热复位 %d 次，上电 %d 次" % (warm & 0xFFFF, por & 0xFFFF))


def report_eopb0(ser) -> None:
    w = read_words(ser, EOPB0_ADDR, 1)
    print("\n-- SRAM 配置（USD EOPB0，仅供参考） --")
    if w is None:
        print("   EOPB0 读取失败")
    else:
        word = w[0]
        lo, hi = word & 0xFF, (word >> 8) & 0xFF
        kb = {0: 512, 1: 448, 2: 384, 3: 384}.get(lo & 0x3, 384)
        print("   0x1FFFC010 = 0x%08X（EOPB0=0x%02X nEOPB0=0x%02X）→ 按表 %d KB"
              % (word, lo, hi, kb))
        if (lo ^ 0xFF) != hi:
            print("   [!!] nEOPB0 不是 EOPB0 的反码：这条地址不是可靠的内存映射读"
                  "（USD 需经 Flash 控制器读取），本项读数不能当证据。")
    print("   实测可用上界 0x%08X（%d KB）：写 0x2000C000 / 0x2003A800 均当场关机，"
          "写 0x2000B800 正常" % (RAM_HI, (RAM_HI - RAM_LO) // 1024))


def report_fault_summary(ser) -> None:
    """上次故障的摘要（0x2000BF60，冷启动不清）。"""
    w = read_words(ser, FAULT_SUM_ADDR, 6)
    if w is None or w[0] != 0x464C5432:
        return
    print("   上次故障摘要（0x2000BF60，跨复位保留）：")
    print("     CFSR=0x%08X HFSR=0x%08X BFAR=0x%08X PC=0x%08X LR=0x%08X"
          % (w[1], w[2], w[3], w[4], w[5]))
    if w[1] & (1 << 15):
        print("     → BFARVALID：总线错误地址 0x%08X（写到了不存在的存储器）"
              % w[3])
    elif w[1] & (1 << 7):
        print("     → MMARVALID：访问被 MPU/总线拒绝")


def report_fault(ser, raw_dump: bool = False) -> dict | None:
    payload = read_cmd(ser, proto.CMD_FAULT, 56)
    print("\n-- HardFault 现场 0x2000BF00 --")
    if payload is None:
        print("   读取失败（设备未响应 0x05）")
        return None
    if raw_dump:
        print("   原始：" + " ".join("%02X" % b for b in payload))
    info = proto.decode_fault(payload)
    if info is None:
        print("   本次上电没有记录到 HardFault（魔数被冷启动清掉）")
        report_fault_summary(ser)
        print("   （没有摘要行 = 这次上电后确实没进过故障异常）")
        return None
    print("   第 %d 次  PC=0x%08X  LR=0x%08X" % (info["count"], info["pc"], info["lr"]))
    print("   CFSR=0x%08X HFSR=0x%08X MMFAR=0x%08X BFAR=0x%08X"
          % (info["cfsr"], info["hfsr"], info["mmfar"], info["bfar"]))
    print("   R0=0x%08X R1=0x%08X R2=0x%08X R3=0x%08X"
          % (info["r0"], info["r1"], info["r2"], info["r3"]))
    for line in proto.fault_explain(info):
        print("   → " + line)
    return info


def report_stack(ser) -> None:
    payload = read_cmd(ser, 0x04, 20)
    print("\n-- 栈用量（0x04，STK1） --")
    if payload is None:
        print("   读取失败")
        return
    if payload[:4] != b"STK1":
        print("   标记不符：" + " ".join("%02X" % b for b in payload))
        return
    lo = struct.unpack_from("<I", payload, 4)[0]
    peak = struct.unpack_from("<H", payload, 8)[0]
    painted = payload[10]
    base = struct.unpack_from("<I", payload, 12)[0]
    top = struct.unpack_from("<I", payload, 16)[0]
    print("   栈区 0x%08X..0x%08X（%d 字节）  水印已铺设=%d"
          % (base, top, top - base, painted))
    if painted:
        print("   已用峰值 %d 字节，最低到 0x%08X，余量 %d 字节"
              % (peak, lo, lo - base))
        if peak > (top - base) * 3 // 4:
            print("   [!!] 余量不足 25%%")


def dump_ram(ser) -> None:
    raw = read_n(ser, 0x20000000, 0x40)
    print("\n-- 分配器基址表 0x20000000..0x2000003F --")
    if raw is None:
        print("   读取失败")
    else:
        for i in range(0, 0x40, 4):
            print("   0x%08X = 0x%08X" % (0x20000000 + i,
                                          struct.unpack_from("<I", raw, i)[0]))

    raw = read_n(ser, 0x2000A560, 0x20)
    print("\n-- UI 窗口结构 0x2000A568 --")
    if raw is None:
        print("   读取失败")
    else:
        x, y = struct.unpack_from("<HH", raw, 0x08)
        w, h = struct.unpack_from("<HH", raw, 0x10)
        fb = struct.unpack_from("<I", raw, 0x18)[0]
        print("   win_x=%d win_y=%d win_w=%d win_h=%d fb_ptr=0x%08X  (%d 字节)"
              % (x, y, w, h, fb, w * h * 2))


def probe_addr(ser, addr: int, n: int = 16) -> bool:
    print("\n-- 映射探测 0x%08X（读 %d 字节） --" % (addr, n))
    if addr >= RAM_HI and RAM_LO <= addr:
        print("   注意：超出实测可用上界 0x%08X，读访问可能直接复位设备"
              % RAM_HI)
    raw = read_n(ser, addr, n, timeout=1.5)
    if raw is None:
        print("   无响应：该地址大概率未映射（读访问触发总线错误，设备已复位）")
        return False
    print("   可读：" + " ".join("%02X" % b for b in raw))
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Phosphor 诊断：复位原因 / 故障现场 / 内核内存")
    ap.add_argument("--port", help="串口名，缺省自动识别")
    ap.add_argument("--raw", action="store_true", help="打印故障现场原始字节")
    ap.add_argument("--ram", action="store_true", help="附分配器基址表 / 窗口结构")
    ap.add_argument("--stack", action="store_true", help="读 0x04 栈用量")
    ap.add_argument("--probe", metavar="ADDR[:LEN]", help="读指定地址测试映射")
    a = ap.parse_args()

    port = a.port or proto.find_port()
    if not port:
        print("未发现串口。设备可能：①未连接；②停在故障里未复位（拔插 USB）；"
              "③未刷入补丁固件。")
        return 2
    try:
        ser = proto.open_port(port, timeout=0.2)
    except Exception:
        print("打开 %s 失败" % port)
        return 2
    print("端口: %s" % port)

    report_reset(ser)
    report_fault(ser, a.raw)
    if a.stack:
        report_stack(ser)
    report_eopb0(ser)
    if a.ram:
        dump_ram(ser)
    if a.probe:
        spec = a.probe.split(":")
        probe_addr(ser, int(spec[0], 0), int(spec[1], 0) if len(spec) > 1 else 16)

    ser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
