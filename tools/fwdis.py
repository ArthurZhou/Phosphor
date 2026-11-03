"""厂商固件反汇编辅助：按地址反查引用、按常量反查使用者。

用法：
    python tools/fwdis.py dis 0x080164E8 0x08016560     # 反汇编区间
    python tools/fwdis.py pool 0x20000075               # 谁的字面量池引用了该地址
    python tools/fwdis.py callers 0x080164E8            # 谁 bl 到该地址
    python tools/fwdis.py mem 0x59                      # 谁以 #0x59 访问内存
    python tools/fwdis.py table 0x08021198 16           # 打印指针表

说明：线性扫描 + skipdata，整片覆盖。字面量池里的数据可能被当成指令解码，
因此 callers/pool 的结论要用 dis 复核一遍再下判断。
"""

import os
import pickle
import struct
import sys

from capstone import CS_ARCH_ARM, CS_MODE_THUMB, CS_OP_IMM, CS_OP_MEM, Cs
from capstone.arm import ARM_REG_PC

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BIN = os.path.join(ROOT, 'firmware', 'vendor', 'APP_DSO153_V1.6_20240427.bin')
BASE = 0x08007000
CACHE = os.path.join(HERE, 'build', 'fwdis.pkl')

_md = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
_md.detail = True
_md.skipdata = True


def _load():
    with open(BIN, 'rb') as f:
        data = f.read()
    if os.path.exists(CACHE):
        try:
            with open(CACHE, 'rb') as f:
                ins = pickle.load(f)
            if ins and ins[-1].address > BASE + len(data) - 0x400:
                return data, ins
        except Exception:
            pass
    ins = list(_md.disasm(data, BASE))
    try:
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        with open(CACHE, 'wb') as f:
            pickle.dump(ins, f)
    except Exception:
        pass
    return data, ins


DATA, INS = _load()
# skipdata 产生的 .byte 伪指令不能访问 operands（capstone 会抛 CS_ERR_SKIPDATA）
INS = [i for i in INS if i.id != 0]
BY_ADDR = {i.address: i for i in INS}


def dis(start, end):
    for i in INS:
        if i.address < start:
            continue
        if i.address >= end:
            return
        print('%08X  %-8s %s' % (i.address, i.mnemonic, i.op_str))


def pool(target):
    """字面量池里存着 target 的位置，以及 ldr 它的指令。"""
    out = []
    for i in INS:
        if not i.mnemonic.startswith('ldr') or len(i.operands) != 2:
            continue
        o = i.operands[1]
        if o.type != CS_OP_MEM or o.mem.base != ARM_REG_PC:
            continue
        addr = ((i.address + 4) & ~3) + o.mem.disp
        off = addr - BASE
        if 0 <= off <= len(DATA) - 4 and struct.unpack_from('<I', DATA, off)[0] == target:
            out.append(i)
    return out


def callers(target):
    out = []
    for i in INS:
        if i.mnemonic not in ('bl', 'blx', 'b.w', 'b'):
            continue
        if not i.operands:
            continue
        o = i.operands[0]
        if o.type == CS_OP_IMM and (o.imm & ~1) == (target & ~1):
            out.append(i)
    return out


def mem(off, bits=None):
    """以立即数偏移访问内存的指令。off 给 0x59 这类偏移，bits 限定 ldr/str/ldrb/strb。"""
    out = []
    for i in INS:
        if len(i.operands) < 2:
            continue
        o = i.operands[1]
        if o.type != CS_OP_MEM or o.mem.base == ARM_REG_PC or o.mem.disp != off:
            continue
        if bits and not i.mnemonic.startswith(bits):
            continue
        out.append(i)
    return out


def abs_users(target):
    """movw/movt 拼出 target 的指令对，以及紧随其后的第一条访存指令。"""
    out = []
    lo = target & 0xFFFF
    for i in INS:
        if not i.mnemonic.startswith('movw') or len(i.operands) != 2:
            continue
        o = i.operands[1]
        if o.type != CS_OP_IMM or o.imm != lo:
            continue
        reg = i.operands[0].reg
        j = INS.index(i) if False else None
        nxt = [x for x in INS if x.address == i.address + i.size]
        if not nxt:
            continue
        k = nxt[0]
        if not k.mnemonic.startswith('movt') or len(k.operands) != 2:
            continue
        if k.operands[0].reg != reg or k.operands[1].type != CS_OP_IMM:
            continue
        if k.operands[1].imm != (target >> 16):
            continue
        out.append((i, k))
    return out


def table(addr, count):
    for k in range(count):
        off = addr - BASE + 4 * k
        if not (0 <= off <= len(DATA) - 4):
            break
        v = struct.unpack_from('<I', DATA, off)[0]
        print('%08X  [%2d] %08X' % (addr + 4 * k, k, v))


if __name__ == '__main__':
    cmd = sys.argv[1]
    if cmd == 'dis':
        dis(int(sys.argv[2], 16), int(sys.argv[3], 16))
    elif cmd == 'pool':
        for i in pool(int(sys.argv[2], 16)):
            print('%08X  %s %s' % (i.address, i.mnemonic, i.op_str))
    elif cmd == 'callers':
        for i in callers(int(sys.argv[2], 16)):
            print('%08X  %s %s' % (i.address, i.mnemonic, i.op_str))
    elif cmd == 'mem':
        bits = sys.argv[3] if len(sys.argv) > 3 else None
        for i in mem(int(sys.argv[2], 16), bits):
            print('%08X  %-8s %s' % (i.address, i.mnemonic, i.op_str))
    elif cmd == 'abs':
        for a, b in abs_users(int(sys.argv[2], 16)):
            print('%08X  %s %s / %08X %s %s'
                  % (a.address, a.mnemonic, a.op_str, b.address, b.mnemonic, b.op_str))
    elif cmd == 'table':
        table(int(sys.argv[2], 16), int(sys.argv[3]))
    else:
        print(__doc__)
