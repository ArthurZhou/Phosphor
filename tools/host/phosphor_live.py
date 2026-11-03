#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""直接探测内部状态（无 GUI）

读设置结构、信号发生器波形表 0x20004574、采样窗口寄存器，并在写入前后
对比，用来判断「只写字段」和「走固件命令」到底差在哪一步。

已确认的口径：
    垂直档位在 +0x01（0..8），只写这个字节**不会**切前端衰减 —— 衰减由
    0x08008754(idx) 配置（写 0x4002xxxx 的 GPIO），原厂按键链路是
    「改字段 → 调 0x08008754 → 推 UI 事件」，固件命令 0x0D 复刻的也是这条。
    0x20004574 那 301 项 u16 表是信号发生器波形（按 +0x59 的波形类型生成），
    与垂直刻度无关；+0x59 是波形类型 0..13，不是档位。
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import phosphor_proto as proto

STRUCT = proto.STRUCT_ADDR
TABLE = 0x20004574
TABLE_N = 300
WINDOW_ADDRS = ((0x20000254, "WIN_A"), (0x20000002, "WIN_B"),
                (0x20000256, "WIN_C"), (0x20000258, "WIN_D"),
                (0x2000025A, "WIN_E"))


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

    def read(self, addr, n, timeout=1.0):
        """读任意长度：命令 0x08 单次上限 0xFF，这里自动分段。"""
        out = b""
        while n > 0:
            step = min(n, 240)
            part = self._read_once(addr, step, timeout)
            if part is None:
                return None
            out += part
            addr += step
            n -= step
        return out

    def _read_once(self, addr, n, timeout=1.0):
        cmd = bytes([proto.CMD_RAMREAD]) + int(addr).to_bytes(4, "little") + \
            bytes([n])
        for _ in range(3):
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

    def set_param(self, role, target):
        self.link.write(bytes([proto.CMD_SET_PARAM, role, target & 0xFF]))
        self.link.flush()

    def apply(self, role):
        self.link.write(bytes([proto.CMD_APPLY, role]))
        self.link.flush()

    def struct(self, n=128):
        return self.read(STRUCT, n)

    def table(self):
        chunks = []
        addr = TABLE
        left = TABLE_N * 2
        step = 250
        while left > 0:
            n = min(step, left)
            b = self.read(addr, n)
            if b is None:
                return None
            chunks.append(b)
            addr += n
            left -= n
        return b"".join(chunks)

    def windows(self):
        out = {}
        for addr, name in WINDOW_ADDRS:
            b = self.read(addr, 2, 0.5)
            out[name] = None if b is None else int.from_bytes(b, "little")
        return out


def table_head(t, n=12):
    if t is None:
        return "读取失败"
    vals = [int.from_bytes(t[i * 2:i * 2 + 2], "little") for i in range(n)]
    return " ".join(str(v) for v in vals)


def table_diff(a, b, limit=10):
    if a is None or b is None:
        return ["  无法比较"]
    out = []
    for i in range(TABLE_N):
        va = int.from_bytes(a[i * 2:i * 2 + 2], "little")
        vb = int.from_bytes(b[i * 2:i * 2 + 2], "little")
        if va != vb:
            out.append("  [%3d] %d -> %d" % (i, va, vb))
            if len(out) >= limit:
                out.append("  …")
                break
    return out or ["  缩放表未变化"]


def main() -> int:
    ap_port = None
    for i, a in enumerate(sys.argv[1:]):
        if a == "--port" and i + 2 <= len(sys.argv) - 1:
            ap_port = sys.argv[2 + i]
    dev = Dev(ap_port)
    print("端口", dev.port)

    SCREEN = 0x2000425E
    SCREEN_N = 301
    RAW = 0x2000025D

    def screen():
        return dev.read(SCREEN, SCREEN_N)

    def raw_head(n=16):
        b = dev.read(RAW, n)
        return "读取失败" if b is None else " ".join(str(v) for v in b)

    st = dev.struct()
    print("结构初始: 时基=%d 垂直=%d 探头=%d 波形类型=%d"
          % (st[0x0D], st[0x01], st[0x03], st[0x59]))
    s0 = screen()
    print("屏幕缓冲前 16:", " ".join(str(v) for v in (s0 or b"")[:16]))
    print("原始样本前 16:", raw_head())

    print("\n=== 只写 +0x01（垂直档位字段），不调 0x08008754 ===")
    print("预期：字段跟着变，屏幕缓冲基本不动（前端衰减没切）")
    for val in (2, 8):
        dev.write(STRUCT + 0x01, bytes([val]))
        time.sleep(1.5)
        st1 = dev.struct()
        s1 = screen()
        same = (s0 == s1)
        print("写 +0x01=%d → 读回 %d ；屏幕缓冲%s"
              % (val, st1[0x01], "未变化" if same else "已变化"))
        if not same and s0 and s1:
            d = sum(1 for a, b in zip(s0, s1) if a != b)
            print("  屏幕缓冲 %d/%d 点不同，前 16: %s"
                  % (d, len(s0), " ".join(str(v) for v in s1[:16])))
        s0 = s1

    print("\n=== 只写 +0x0D（时基） ===")
    s0 = screen()
    for val in (8, 18):
        dev.write(STRUCT + 0x0D, bytes([val]))
        time.sleep(1.5)
        st1 = dev.struct()
        s1 = screen()
        same = (s0 == s1)
        print("写 +0x0D=%d → 读回 %d ；窗口 %s ；屏幕缓冲%s"
              % (val, st1[0x0D], dev.windows(),
                 "未变化" if same else "已变化"))
        s0 = s1

    print("\n=== 只写 +0x03（探头） ===")
    s0 = screen()
    for val in (1, 0):
        dev.write(STRUCT + 0x03, bytes([val]))
        time.sleep(1.5)
        st1 = dev.struct()
        s1 = screen()
        same = (s0 == s1)
        print("写 +0x03=%d → 读回 %d ；屏幕缓冲%s"
              % (val, st1[0x03], "未变化" if same else "已变化"))
        s0 = s1

    # 把字段写回原值，别把设备留在探测时的状态上
    for off in (0x0D, 0x01, 0x03, 0x59):
        dev.write(STRUCT + off, bytes([st[off]]))
    time.sleep(0.4)
    st = dev.struct()
    print("\n已复原: 时基=%d 垂直=%d 探头=%d 波形类型=%d"
          % (st[0x0D], st[0x01], st[0x03], st[0x59]))

    dev.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
