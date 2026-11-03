#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全 SRAM 稳定差分：找出原厂改「垂直档位」时真正写入的地址

做法：
  1. 注入按键前连读 3 遍 0x20000000..0x2000BFFF，只保留「三遍完全相同」的
     地址（排除波形缓冲这类每帧都在变的区域）。
  2. 注入按键，等待。
  3. 再连读 3 遍，同样只保留稳定地址。
  4. 报告「前后都稳定、但值不同」的地址。

这样得到的就是原厂改档时真正写入的设置所在，不靠反汇编猜测。

用法: python tools/host/phosphor_sramdiff.py [--port COM13]
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import phosphor_proto as proto

LO = 0x20000000
HI = 0x2000C000
CHUNK = 200
STRUCT = proto.STRUCT_ADDR
PAGE = proto.SLOT_ADDR


class Dev:
    def __init__(self, port=None):
        if port is None:
            t = next((p for p in proto.list_ports() if p.is_target), None)
            if t is None:
                raise SystemExit("未发现目标设备")
            port = t.device
        self.port = port
        self.link = proto.open_port(port)
        self.parser = proto.FrameParser()

    def close(self):
        self.link.close()

    def _once(self, addr, n, timeout=0.5):
        cmd = bytes([proto.CMD_RAMREAD]) + int(addr).to_bytes(4, "little") + \
            bytes([n])
        for _ in range(2):
            self.link.write(cmd)
            self.link.flush()
            deadline = time.time() + timeout
            while time.time() < deadline:
                chunk = self.link.read(4096)
                if not chunk:
                    continue
                for f in self.parser.feed(chunk):
                    if len(f.samples) == n:
                        return bytes(f.samples)
            time.sleep(0.02)
        return None

    def snapshot(self) -> bytearray | None:
        buf = bytearray(HI - LO)
        addr = LO
        while addr < HI:
            n = min(CHUNK, HI - addr)
            part = self._once(addr, n)
            if part is None:
                return None
            buf[addr - LO:addr - LO + n] = part
            addr += n
        return buf

    def stable(self, rounds=3) -> bytearray | None:
        """连读 rounds 遍，返回「每遍都相同」的掩码与最后一遍数据。"""
        base = self.snapshot()
        if base is None:
            return None
        same = bytearray(b"\x01") * (HI - LO)
        for _ in range(rounds - 1):
            cur = self.snapshot()
            if cur is None:
                return None
            for i in range(HI - LO):
                if cur[i] != base[i]:
                    same[i] = 0
            base = cur
        return base, same

    def write(self, addr, payload):
        self.link.write(bytes([proto.CMD_WRITE_RAM])
                        + int(addr).to_bytes(4, "little")
                        + bytes([len(payload)]) + payload)
        self.link.flush()

    def key(self, code):
        self.link.write(bytes([proto.CMD_KEY, code]))
        self.link.flush()

    def st(self):
        return self._once(STRUCT, 128, 0.6)


def diff_stable(a, amask, b, bmask):
    out = []
    for i in range(HI - LO):
        if not (amask[i] and bmask[i]):
            continue
        if a[i] != b[i]:
            out.append((LO + i, a[i], b[i]))
    return out


def main() -> int:
    port = None
    for i, a in enumerate(sys.argv[1:]):
        if a == "--port":
            port = sys.argv[i + 2]
    dev = Dev(port)
    print("端口", dev.port)
    print("读取 3 遍全 SRAM 建立稳定基线…")
    r = dev.stable(3)
    if r is None:
        print("读取失败")
        return 1
    base, base_mask = r
    s = dev.st()
    print("当前 时基=%d 垂直=%d 探头=%d" % (s[0x0D], s[0x01], s[0x03]))

    # 先把页面与子选择强制到垂直页，再按拨轮，确保按键作用到垂直档位
    dev.write(PAGE, bytes([3]))
    dev.write(STRUCT + 0x66, bytes([0]))
    time.sleep(0.6)

    print("\n注入「拨轮右」（+1 档）…")
    dev.key(7)
    time.sleep(1.2)

    print("再读 3 遍…")
    r2 = dev.stable(3)
    if r2 is None:
        print("读取失败")
        return 1
    after, after_mask = r2
    s2 = dev.st()
    print("之后 时基=%d 垂直=%d 探头=%d" % (s2[0x0D], s2[0x01], s2[0x03]))

    d = diff_stable(base, base_mask, after, after_mask)
    print("\n前后都稳定的地址中，值发生变化的：%d 处" % len(d))
    for addr, old, new in d:
        rel = ""
        if STRUCT <= addr < STRUCT + 128:
            rel = "  (结构 +0x%02X)" % (addr - STRUCT)
        print("  0x%08X  %3d -> %-3d%s" % (addr, old, new, rel))

    if not d:
        print("  （除了波形缓冲这类不稳定区，没有稳定的变化）")

    dev.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
