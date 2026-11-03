#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SRAM 差分扫描：定位设置结构里的档位字段

背景：R4 标定用的 0x2000A528 窗口实际上是 UI 状态区，与按键队列重叠，
测不出档位字段。静态分析已把真正的设置结构定位到 0x2000001C（时基索引
在 +0x0D），本工具用「注键前后各扫一遍 SRAM、比对差异」的办法把其余
字段（垂直档位等）钉死。

需要 R5 固件（命令 0x08：任意 SRAM 读）。

用法
----
    # 注入「上」13 次前后各扫一遍，看哪些字节动了
    python tools/host/phosphor_scan.py -p COM13 --key 3 --count 13

    # 依次试 上/下/拨轮右/拨轮左/MODE，每键扫一遍并自动复原（MODE 除外）
    python tools/host/phosphor_scan.py -p COM13 --battery

    # 只dump一段内存存盘
    python tools/host/phosphor_scan.py -p COM13 --dump mem.bin

报告里的自动标注：
    [0..12]  两次取值都落在 0..12 且有变化 → 垂直档位候选
    [0..23]  两次取值都落在 0..23 且有变化 → 时基候选
    固定地址 0x20000029 为静态分析得出的时基索引（+0x0D），
    0x20004FCC 为 MODE 选中项。
"""

from __future__ import annotations

import argparse
import sys
import time

import phosphor_proto as proto

CHUNK = 240                                # 每次读的字节数（≤255）

KNOWN = {
    proto.STRUCT_ADDR + 0x0D: "时基索引（静态分析，cmp #23 边界）",
    proto.SLOT_ADDR: "MODE 当前选中项（静态分析）",
}


def dump_range(link, parser, start: int, end: int, timeout: float = 0.8,
               quiet: bool = False):
    """逐块读 [start, end) 的 SRAM，返回 bytes。"""
    out = bytearray()
    addr = start
    while addr < end:
        n = min(CHUNK, end - addr)
        cmd = bytes([proto.CMD_RAMREAD]) + addr.to_bytes(4, "little") + \
            bytes([n])
        got = None
        for _ in range(3):                 # 失败重试
            link.write(cmd)
            link.flush()
            deadline = time.time() + timeout
            while time.time() < deadline:
                chunk = link.read(4096)
                if not chunk:
                    continue
                for frame in parser.feed(chunk):
                    if len(frame.samples) == n:
                        got = bytes(frame.samples)
                        break
                if got is not None:
                    break
            if got is not None:
                break
        if got is None:
            raise RuntimeError("读 0x%08X 失败（设备无响应或固件不支持 0x08）"
                               % addr)
        out += got
        addr += n
        if not quiet:
            print(".", end="", flush=True)
    if not quiet:
        print()
    return bytes(out)


def report(base: bytes, after: bytes, start: int, count: int | None,
           title: str):
    """打印差分报告。"""
    n = min(len(base), len(after))
    changed = []
    for i in range(n):
        if base[i] != after[i]:
            changed.append(i)
    print("\n== %s：%d 个字节变化 ==" % (title, len(changed)))
    if not changed:
        return
    for i in changed:
        addr = start + i
        old, new = base[i], after[i]
        delta = new - old
        tag = ""
        if count is not None and delta in (count, -count):
            tag = "  ← 步进 %d，强候选" % count
        if 0 <= old <= 12 and 0 <= new <= 12:
            tag += "  [值域≤12，垂直候选]"
        elif 0 <= old <= 23 and 0 <= new <= 23:
            tag += "  [值域≤23，时基候选]"
        if addr in KNOWN:
            tag += "  ◆ " + KNOWN[addr]
        print("  0x%08X: %3d -> %3d (%+d)%s" % (addr, old, new, delta, tag))


def read_slot(link, parser):
    """读 MODE 选中项字节（0x20004FCC）。失败返回 None。"""
    cmd = bytes([proto.CMD_RAMREAD]) + proto.SLOT_ADDR.to_bytes(4, "little") \
        + bytes([1])
    link.write(cmd)
    link.flush()
    deadline = time.time() + 0.6
    while time.time() < deadline:
        chunk = link.read(4096)
        if not chunk:
            continue
        for frame in parser.feed(chunk):
            if len(frame.samples) == 1:
                return frame.samples[0]
    return None


def watch(link, parser, ranges, seconds: float, poll: float = 0.3):
    """观察模式：轮询多段窗口，报告「稳定后变化、变化后确认」的字节。

    过滤规则：字节至少连续 4 轮不变后才允许报变化；新值需再连续 2 轮
    不变才确认。每轮都在动的采集噪声（DMA 环形缓冲、测量刷新）因此
    被全部剔除。
    """
    spans = [(a, b, b - a) for a, b in ranges]
    last = None                              # 上轮数据（各段拼接）
    runlen = {}                              # addr -> 连续相同轮数
    pending = {}                             # addr -> (旧值, 新值, 剩余确认轮数)
    seqs = {}                                # addr -> 取值序列（去重连续）
    t_end = time.time() + seconds
    print("观察 %d 秒（窗口较大，实际约 %.1f 秒/轮）。请在设备上操作："
          % (seconds, poll))
    print("  1) 把垂直档位（V/div）改 1 档，等 3 秒；再改 1 档，等 3 秒；")
    print("  2) 再把时基（S/div）改 1 档，等 3 秒（用于自检，必须出现 0x20000029）；")
    print("  3) 结束后把输出全文发回。")
    while time.time() < t_end:
        t0 = time.time()
        parts = []
        try:
            for a, b, _ in spans:
                parts.append(dump_range(link, parser, a, b, timeout=0.6,
                                        quiet=True))
        except RuntimeError as exc:
            print("[!] %s" % exc)
            return None
        data = b"".join(parts)
        # ---- 重建绝对地址视图 ----
        addr_view = {}
        off = 0
        for a, b, span in spans:
            for i in range(span):
                addr_view[a + i] = data[off + i]
            off += span
        if last is not None:
            for a in sorted(addr_view):
                v = addr_view[a]
                if a in pending:
                    old, new, left = pending[a]
                    if v == new:
                        left -= 1
                        if left <= 0:
                            pending.pop(a, None)
                            runlen[a] = 1
                            seqs.setdefault(a, [old]).append(new)
                            print("  %6.1fs  0x%08X: %3d -> %3d (%+d)"
                                  % (seconds - (t_end - time.time()),
                                     a, old, new, new - old))
                        else:
                            pending[a] = (old, new, left)
                    else:
                        pending.pop(a, None)   # 新值没稳住，丢弃
                elif runlen.get(a, 0) >= 4 and last.get(a) != v:
                    pending[a] = (last[a], v, 2)
                    runlen[a] = 0
                elif last.get(a) == v:
                    runlen[a] = runlen.get(a, 0) + 1
                else:
                    runlen[a] = 1
        last = addr_view
        wait = poll - (time.time() - t0)
        if wait > 0:
            time.sleep(wait)

    print("\n== 取值序列（去重连续，仅报告过的字节）==")
    for a in sorted(seqs):
        s = seqs[a]
        mark = ""
        if a == proto.STRUCT_ADDR + 0x0D:
            mark = "  ◆ 已知时基索引"
        elif a == proto.SLOT_ADDR:
            mark = "  ◆ MODE 选中项"
        elif a == proto.STRUCT_ADDR + 0x03:
            mark = "  ◆ 探头倍率（实测 0=×1 / 1=×10）"
        if len(s) > 1:
            print("  0x%08X: %s%s" % (a, " -> ".join(map(str, s)), mark))
    return seqs


def main() -> int:
    ap = argparse.ArgumentParser(description="SRAM 差分扫描")
    ap.add_argument("-p", "--port", help="串口，如 COM13")
    ap.add_argument("--range", default=None,
                    help="扫描范围 A:B 或逗号分隔多段（默认见各模式）")
    ap.add_argument("--key", type=int, help="注入的键码")
    ap.add_argument("--count", type=int, default=13, help="注入次数")
    ap.add_argument("--gap", type=float, default=0.08, help="注入间隔秒")
    ap.add_argument("--battery", action="store_true",
                    help="依次试 上/下/拨轮右/拨轮左/MODE")
    ap.add_argument("--watch", metavar="SECONDS", nargs="?", const=90,
                    type=float, help="观察模式：你在设备上手动改档位，"
                    "工具报告跟着变的字节（默认 90 秒）")
    ap.add_argument("--dump", metavar="FILE", help="仅扫描并保存原始数据")
    ap.add_argument("--simulate", action="store_true", help="使用仿真设备")
    args = ap.parse_args()

    # 默认窗口：观察模式覆盖全局变量区 + UI 状态区两段；其他模式用小窗
    if args.range is None:
        if args.watch is not None:
            args.range = "0x20000000:0x2000025C,0x20004390:0x2000A580"
        else:
            args.range = "0x20000000:0x20000260"

    ranges = []
    for part in args.range.split(","):
        try:
            a_s, b_s = part.strip().split(":")
            a_v, b_v = int(a_s, 0), int(b_s, 0)
        except ValueError:
            print("range 格式应为 A:B 或 A:B,C:D，如 0x20000000:0x20000260")
            return 2
        if not (0x20000000 <= a_v < b_v <= 0x20060000):
            print("range %s 超出 SRAM（0x20000000..0x20060000）" % part)
            return 2
        ranges.append((a_v, b_v))
    start, end = ranges[0]
    total = sum(b - a for a, b in ranges)
    if args.watch is None and total > 0x2000:
        print("范围过大（>8KB），差分报告会淹没在噪声里；请缩小范围")
        return 2
    if total > 0x10000:
        print("观察窗口过大（>64KB），轮询会太慢")
        return 2

    if args.simulate:
        link = proto.SimulatedSerial()
        port_name = "simulate"
    else:
        if not args.port:
            print("需要 -p 指定串口（--list 可用 phosphor_cli.py 查看）")
            return 2
        link = proto.open_port(args.port)
        port_name = args.port

    parser = proto.FrameParser()
    print("端口: %s   窗口: %s（共 %d 字节）"
          % (port_name, ", ".join("0x%08X..0x%08X" % (a, b) for a, b in ranges),
             total))

    def one_dump(title):
        print("%s " % title, end="", flush=True)
        return dump_range(link, parser, start, end)

    if args.dump:
        data = one_dump("扫描")
        with open(args.dump, "wb") as fh:
            fh.write(data)
        print("已保存 %s (%d 字节)" % (args.dump, len(data)))
        link.close()
        return 0

    if args.battery:
        plan = ((3, 13, 4), (4, 13, 3), (7, 13, 9), (9, 13, 7), (5, 6, None))
        for key, count, undo in plan:
            base = one_dump("[键 %d 基线]" % key)
            slot0 = read_slot(link, parser)
            print("注入键 %d × %d ..." % (key, count))
            for _ in range(count):
                link.write(bytes([proto.CMD_KEY, key]))
                link.flush()
                time.sleep(args.gap)
            after = one_dump("[键 %d 之后]" % key)
            slot1 = read_slot(link, parser)
            report(base, after, start, count, "键 %d" % key)
            if slot0 is not None and slot1 is not None and slot0 != slot1:
                print("  MODE 选中项: %d -> %d  ◆ 0x%08X"
                      % (slot0, slot1, proto.SLOT_ADDR))
            if undo is not None:
                print("复原：注入键 %d × %d ..." % (undo, count))
                for _ in range(count):
                    link.write(bytes([proto.CMD_KEY, undo]))
                    link.flush()
                    time.sleep(args.gap)
                back = one_dump("[复原后]")
                if back == base:
                    print("  已复原到基线")
                else:
                    print("  [!] 与基线不同，字段可能被夹在端点；"
                          "可注入 AUTO(6) 恢复")
            else:
                print("  （MODE 无法自动复原，选中项可能已移动）")
        link.close()
        return 0

    if args.watch is not None:
        watch(link, parser, ranges, args.watch)
        link.close()
        return 0

    if args.key is not None:
        base = one_dump("基线")
        slot0 = read_slot(link, parser)
        print("注入键 %d × %d ..." % (args.key, args.count))
        for _ in range(args.count):
            link.write(bytes([proto.CMD_KEY, args.key]))
            link.flush()
            time.sleep(args.gap)
        after = one_dump("之后")
        slot1 = read_slot(link, parser)
        report(base, after, start, args.count, "键 %d × %d"
               % (args.key, args.count))
        if slot0 is not None and slot1 is not None and slot0 != slot1:
            print("  MODE 选中项: %d -> %d  ◆ 0x%08X"
                  % (slot0, slot1, proto.SLOT_ADDR))
        link.close()
        return 0

    print("请指定 --watch、--key/--count、--battery 或 --dump 之一")
    link.close()
    return 2


if __name__ == "__main__":
    sys.exit(main())
