#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证「只写字段、不调用任何函数」时设备是否自己应用

对 +0x01（垂直档位）、+0x0D（时基）、+0x03（探头倍率）分别只写字段，
然后观察设备自己维护的派生状态是否跟着变：
  +0x61 垂直增益 / +0x5C..0x5D 每格码值 / 采样窗口寄存器

已知结论（实机）：只写 +0x01 字段不会切前端衰减 —— 衰减由 0x08008754(idx)
配置，必须像原厂按键链路那样显式调用。本脚本保留下来就是为了反复确认这一点。
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import phosphor_proto as proto

STRUCT = proto.STRUCT_ADDR
WIN = ((0x20000254, "A"), (0x20000002, "B"), (0x20000256, "C"),
       (0x20000258, "D"), (0x2000025A, "E"))


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

    def _once(self, addr, n, timeout=0.8):
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
            time.sleep(0.03)
        return None

    def read(self, addr, n, timeout=0.8):
        out = b""
        while n > 0:
            step = min(n, 200)
            part = self._once(addr, step, timeout)
            if part is None:
                return None
            out += part
            addr += step
            n -= step
        return out

    def write(self, addr, payload):
        self.link.write(bytes([proto.CMD_WRITE_RAM])
                        + int(addr).to_bytes(4, "little")
                        + bytes([len(payload)]) + payload)
        self.link.flush()

    def st(self):
        return self.read(STRUCT, 128)

    def wins(self):
        out = []
        for addr, name in WIN:
            b = self._once(addr, 2, 0.4)
            out.append("%s=%s" % (name, "?" if b is None
                                  else int.from_bytes(b, "little")))
        return " ".join(out)


def report(dev, tag):
    s = dev.st()
    print("  %-14s 时基+0D=%-3d 垂直+01=%-3d +5C/5D=%-5d +60=%-3d +61=%-3d "
          "滚动+64=%-3d | %s"
          % (tag, s[0x0D], s[0x01],
             int.from_bytes(s[0x5C:0x5E], "little"),
             s[0x60], s[0x61], s[0x64], dev.wins()))


def main() -> int:
    port = None
    for i, a in enumerate(sys.argv[1:]):
        if a == "--port":
            port = sys.argv[i + 2]
    dev = Dev(port)
    print("端口", dev.port)
    st0 = dev.st()
    report(dev, "初始")

    print("\n只写 +0x01（垂直档位），不调用任何函数：")
    print("预期：+01 跟着变，但 +61/窗口/屏幕都不动 —— 前端衰减没切")
    for v in (2, 8, 5, 0):
        dev.write(STRUCT + 0x01, bytes([v]))
        time.sleep(1.0)
        report(dev, "写 +01=%d" % v)

    print("\n只写 +0x0D，不调用任何函数：")
    for v in (6, 16, 12):
        dev.write(STRUCT + 0x0D, bytes([v]))
        time.sleep(1.0)
        report(dev, "写 +0D=%d" % v)

    print("\n只写 +0x03，不调用任何函数：")
    for v in (1, 0):
        dev.write(STRUCT + 0x03, bytes([v]))
        time.sleep(1.0)
        report(dev, "写 +03=%d" % v)

    for off, val in ((0x01, st0[0x01]), (0x0D, st0[0x0D]),
                     (0x03, st0[0x03]), (0x59, st0[0x59])):
        dev.write(STRUCT + off, bytes([val]))
    time.sleep(0.4)
    print("\n复原后：")
    report(dev, "复原")

    dev.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
