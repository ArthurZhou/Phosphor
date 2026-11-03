#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""垂直档位实测：确认「改档位」真的改了前端增益，而不只是改了一格数字

只用命令 0x0D（设定）与 0x08（读内存）。

判据
    垂直档位是**前端增益**：0x08008754 按索引切衰减/增益（写 0x4002xxxx），
    所以同一路输入在不同 V/div 下，屏幕列缓冲 0x2000425E 的峰峰值按档位成
    反比。取 k = 峰峰 × mV/div，未超屏的档 k 应当一致。

    别用「峰峰随档位单调递减」当判据：输入幅度不变时，高灵敏档（10mV..500mV）
    全都会撞到屏幕上下边界而被压平，那一段峰峰是往上走的（越灵敏越接近满屏），
    只看单调性会误报。这里用「k 明显偏小 → 超屏裁剪，剔除后再比比例」。

    实机标定（200Hz 3.8Vpp 方波，R36）：5V≈18px、2V≈44px、1V≈84px，
    500mV 及以下全部贴边。

字段
    档位在 +0x01（0..8 = 10mV..5V/div），底栏选中项在 +0x0E（1 起）。
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import phosphor_proto as proto

STRUCT = proto.STRUCT_ADDR
SCREEN = 0x2000425E
SCREEN_N = 301
PB_FIELD = proto.VOLTAGE_OFFSET    # 垂直档位（+0x01，0..8）
SEL_FIELD = proto.CURSOR_OFFSET    # 底栏选中项（+0x0E，从 1 起）


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

    def set_param(self, role, target):
        self.link.write(bytes([proto.CMD_SET_PARAM, role, target & 0xFF]))
        self.link.flush()

    def st(self):
        return self.read(STRUCT, 128)

    def screen_pp(self):
        """返回 (峰峰, 最小值, 最大值)。"""
        d = self.read(SCREEN, SCREEN_N)
        return None if d is None else (max(d) - min(d), min(d), max(d))


def main() -> int:
    port = None
    for i, a in enumerate(sys.argv[1:]):
        if a == "--port":
            port = sys.argv[i + 2]
    dev = Dev(port)
    print("端口", dev.port)

    s = dev.st()
    if s is None:
        print("读不到设置结构")
        return 1
    print("起始：档位 +0x01=%d（%s）  探头 +0x03=%d  时基 +0x0D=%d  选中项 +0x0E=%d"
          % (s[PB_FIELD], proto.VOLTAGE_STEPS[s[PB_FIELD]] if s[PB_FIELD] < len(proto.VOLTAGE_STEPS) else "?",
             s[0x03], s[0x0D], s[SEL_FIELD]))

    rows = []                      # (idx, pb, pp)
    for idx in range(0, 9):
        dev.set_param(proto.PARAM_VOLTAGE, idx)
        time.sleep(0.7)
        s = dev.st()
        m = dev.screen_pp()
        if s is None or m is None:
            print("档位 %d 读取失败" % idx)
            continue
        pp = m[0]
        rows.append((idx, s[PB_FIELD], pp))

    bad = [r[0] for r in rows if r[1] != r[0]]
    live = [r for r in rows if r[1] == r[0] and r[2]]   # 命中且有信号

    # 同一路输入下 k = 峰峰 × mV/div 应当恒定；被屏幕上下边界裁掉的档 k 会明显
    # 偏小。基准取最大的 k（最不灵敏档最不可能超屏）——列缓冲里有非波形的
    # 空白值，不能用「贴 min/max 边界」来判裁剪。
    mv = proto.VOLTAGE_MV_PER_DIV
    ks = sorted(r[2] * mv[r[0]] for r in live)
    ref = ks[-1] if ks else 1
    clipped = {r[0] for r in live if r[2] * mv[r[0]] < 0.8 * ref}
    usable = sorted(r for r in live if r[0] not in clipped)

    print("\n%-4s %-8s %-6s %-8s %-9s %s"
          % ("档位", "V/div", "+0x01", "屏幕峰峰", "k=pp*mV", "状态"))
    for idx, pb, pp in rows:
        note = ""
        if pb != idx:
            note = "字段未落到目标"
        elif idx in clipped:
            note = "超屏裁剪"
        print("%-4d %-8s %-6d %-8d %-9d %s"
              % (idx, proto.VOLTAGE_STEPS[idx], pb, pp,
                 pp * mv[idx], note))

    def finish(ok, msg):
        print(msg)
        dev.set_param(proto.PARAM_VOLTAGE, 7)
        time.sleep(0.4)
        dev.close()
        return 0 if ok else 1

    print()
    if bad:
        return finish(False, "档位未落到目标：%s。检查固件是否 R36 及以上。" % bad)
    if not live or len(set(r[2] for r in live)) <= 1:
        return finish(False, "字段落到了目标，但屏幕峰峰始终不变 —— 前端增益没切。")

    # 同一路输入下 k = 峰峰 × mV/div 应当恒定；被屏幕上下边界裁掉的档 k 会明显
    # 偏小，用中位 k 当基准把它们挑出来。
    if len(usable) < 2:
        return finish(False, "只有 %d 档没被裁剪（信号太大）。把输入降到约 %d mVpp"
                             " 以下再跑。" % (len(usable),
                                             4 * mv[max(clipped or [0])]))

    # 未裁剪段：档位变大，峰峰必须变小，且比例要跟 mV/div 对得上
    drops = [(usable[i][0], usable[i + 1][0],
              usable[i][2] / usable[i + 1][2],
              mv[usable[i + 1][0]] / mv[usable[i][0]])
             for i in range(len(usable) - 1)
             if usable[i + 1][0] == usable[i][0] + 1]
    worst = max((abs(a - e) / e for _i, _j, a, e in drops), default=0.0)
    mono = all(a > b for a, b in
               zip((r[2] for r in usable), (r[2] for r in usable[1:])))

    if not mono or worst > 0.20:
        return finish(False,
                      "档位命中，但未裁剪档位的峰峰比例与 mV/div 差 %.0f%%"
                      "（单调=%s），前端口径不一致，需人工看波形。"
                      % (worst * 100, "是" if mono else "否"))

    print("档位 0..8 全部命中；未裁剪档位 %s 的峰峰随档位递减，比例与 mV/div "
          "偏差 ≤ %.0f%%（阈值 20%%）—— 前端增益按档位等比生效。"
          % ([r[0] for r in usable], worst * 100))
    if clipped:
        print("超屏档位 %s 未参与判定；要覆盖它们，把输入降到约 %d mVpp 以下再跑。"
              % (sorted(clipped), 4 * mv[min(clipped)]))
    return finish(True, "")


if __name__ == "__main__":
    sys.exit(main())
