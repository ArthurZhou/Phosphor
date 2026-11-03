#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按键机制诊断（只读分析，用于确认原厂改垂直档位时写入哪些字节）

依次尝试若干「选中项 + 按键」组合，注入键码后对比设置结构 128 字节，
打印真正变化的偏移。仅用于逆向确认，不用于产品功能。

用法: python tools/host/phosphor_keys.py [--port COM13]
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import phosphor_proto as proto

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

    def read(self, addr, n, timeout=0.8):
        out = b""
        while n > 0:
            step = min(n, 200)
            part = self._read_once(addr, step, timeout)
            if part is None:
                return None
            out += part
            addr += step
            n -= step
        return out

    def _read_once(self, addr, n, timeout):
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

    def write(self, addr, payload):
        self.link.write(bytes([proto.CMD_WRITE_RAM])
                        + int(addr).to_bytes(4, "little")
                        + bytes([len(payload)]) + payload)
        self.link.flush()

    def key(self, code):
        self.link.write(bytes([proto.CMD_KEY, code]))
        self.link.flush()

    def st(self):
        return self.read(STRUCT, 128)

    def page(self):
        b = self.read(PAGE, 1, 0.4)
        return None if b is None else b[0]


def diff(a, b):
    out = []
    for i in range(min(len(a), len(b))):
        if a[i] != b[i]:
            out.append("+%02X:%d→%d" % (i, a[i], b[i]))
    return out


def run(dev, label, setup, key, wait=0.9):
    before = dev.st()
    pg0 = dev.page()
    for addr, val in setup:
        dev.write(addr, bytes([val]))
    dev.key(key)
    time.sleep(wait)
    after = dev.st()
    pg1 = dev.page()
    d = diff(before, after)
    print("%-28s 页 %s→%s  变化: %s"
          % (label, pg0, pg1, " ".join(d) if d else "无"))
    return d


def main() -> int:
    port = None
    for i, a in enumerate(sys.argv[1:]):
        if a == "--port":
            port = sys.argv[i + 2]
    dev = Dev(port)
    print("端口", dev.port, "  当前页", dev.page())
    st = dev.st()
    print("初始: 时基=%d 垂直=%d 探头=%d 选中项=%d"
          % (st[0x0D], st[0x01], st[0x03], st[0x0E]))

    print("\n=== 探测 垂直档位的页面与按键 ===")
    for page in (2, 3, 4):
        for sub_off in (0x66,):
            for sub_val in (0, 1, 2):
                for key, kname in ((9, "拨轮左"), (7, "拨轮右")):
                    run(dev, "页%d/%02X=%d %s" % (page, sub_off, sub_val,
                                                 kname),
                        [(PAGE, page), (STRUCT + sub_off, sub_val)], key)

    print("\n=== 探测 上/下 在页 2/页 3 的作用 ===")
    for page in (2, 3):
        for sub in (0, 1, 2, 3):
            for key, kname in ((3, "上"), (4, "下")):
                run(dev, "页%d/0E=%d %s" % (page, sub, kname),
                    [(PAGE, page), (STRUCT + 0x0E, sub)], key)

    print("\n=== MODE 与 OK 的页面作用 ===")
    run(dev, "MODE", [], 5)
    run(dev, "OK", [], 8)
    run(dev, "OK×2", [], 8)

    print("\n恢复：页面写回 2")
    dev.write(PAGE, bytes([2]))
    dev.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
