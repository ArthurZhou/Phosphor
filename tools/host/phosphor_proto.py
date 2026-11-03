#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phosphor 数据通道协议实现

本模块实现主机侧与 Phosphor 固件之间的帧协议、端口枚举与统计学计算，
不依赖图形界面，供命令行工具与图形上位机共用。

帧格式
    +--------+--------+--------+--------+--------+--------+--------+
    |  A5 5A |  N(2)  | seq(2) | flags  |  rsv   |  N 字节样本  | chk |
    +--------+--------+--------+--------+--------+--------+--------+
    N      样本数，小端
    seq    帧序号，小端，逐帧递增
    flags  见 FLAG_* 常量
    rsv    保留，恒为 0
    chk    帧头 8 字节与样本逐字节累加之和的低 8 位

标志位
    FLAG_LIVE       0x01  数据取自实时环形缓冲
    FLAG_CHK_ADV    0x02  校验值为发送起始时刻的快照，仅作参照
    FLAG_SCREEN     0x04  屏幕波形数据（301 点）
    FLAG_SETTINGS   0x08  设置结构快照（0x2000A528）
    FLAG_IMG        0x20  屏幕画面块，负载 = 8 字节区域描述 + RGB565 像素

命令
    CMD_STOP   0x00  停止连续发送
    CMD_SINGLE 0x01  发送一帧原始数据（4096 点）
    CMD_STREAM 0x02  启动连续发送
    CMD_SCREEN 0x03  发送一帧屏幕波形（301 点）
    CMD_DIAG   0x04  发送 20 字节诊断信息（栈用量等）
    CMD_FAULT  0x05  发送 56 字节 HardFault 现场（PC / CFSR 等）
    CMD_KEY    0x06  注入键码，后随 1 字节键码（1..14）
    CMD_SETTINGS 0x07  发送 64 字节设置结构快照
    CMD_SET_PARAM 0x0D  直接设定时基/垂直档位，后随角色 1 字节 + 目标 1 字节
    CMD_WRITE_RAM 0x0E  写 SRAM，后随地址 4 字节 + 长度 1 字节 + 数据
    CMD_APPLY   0x0F  按当前字段值重新应用，后随角色 1 字节
    CMD_SRC     0x10  选择数据源，后随 1 字节（0 原始 / 1 屏幕波形 / 2 屏幕画面）
"""

from __future__ import annotations

import math
import struct
import time
from dataclasses import dataclass

VID = 0x2E3C
PID = 0x5740

CMD_STOP = 0x00
CMD_SINGLE = 0x01
CMD_STREAM = 0x02
CMD_SCREEN = 0x03
CMD_DIAG = 0x04
CMD_FAULT = 0x05
CMD_KEY = 0x06           # 注入键码，后随 1 字节键码（1..14，0xFF 无效）
CMD_SETTINGS = 0x07      # 读设置结构快照（0x2000A528 起 64 字节，旧窗口）
CMD_RAMREAD = 0x08       # 读 SRAM，后随地址 4 字节（小端）+ 长度 1 字节
CMD_SET_PARAM = 0x0D     # 直接设定档位，后随角色 1 字节 + 目标 1 字节
CMD_WRITE_RAM = 0x0E     # 写 SRAM，后随地址 4 字节 + 长度 1 字节 + 数据
CMD_APPLY = 0x0F         # 按当前字段值重新应用，后随角色 1 字节
CMD_SRC = 0x10           # 选择数据源，后随 1 字节（0 raw / 1 screen / 2 image）

# 直接设定命令的角色编号（对应固件 command 0x0D 的参数）
PARAM_TIMEBASE = 1
PARAM_VOLTAGE = 2
PARAM_PROBE = 3

# 设置结构 `0x2000001C` 内已确认的档位字段（反汇编 + 实机双向核对）
TIMEBASE_OFFSET = 0x0D       # 时基索引，0..23
VOLTAGE_OFFSET = 0x01        # 垂直档位索引，0..8（10mV..5V/div）
PROBE_OFFSET = 0x03          # 探头倍率，0 = ×1，1 = ×10
CURSOR_OFFSET = 0x0E         # 底栏选中项，**从 1 起**：1 档位 / 2 探头 / 3 时基 / 4 耦合…
# 各字段的可达上界。垂直档位的上限不是表长减一：原厂键 3 在 +0x01 上加到 8
# （5V/div）就停住，9..12 那几档在设备上走不到；前端衰减由 0x08008754(idx) 按
# 同一个索引配置，越界索引会落进未定义的 tbb 分支。
VOLTAGE_MAX = 8
PROBE_MAX = 1

PROBE_STEPS = ("×1", "×10")

SETTINGS_ADDR = 0x2000A528   # 旧快照窗口（实际是 UI 状态区，与按键队列重叠）
SETTINGS_LEN = 64            # 命令 0x07 上报的字节数
STRUCT_ADDR = 0x2000001C     # 真正的设置结构基址（静态分析：被「上」处理函数
                             # 引用 52 次；时基索引在 +0xD，边界 cmp #23）
SLOT_ADDR = 0x20004FCC       # 当前 UI 页面号（0..8，2 = 主界面），不是选中项

# 键码含义，由实测标定（逐个注入后观察设备屏幕与设置结构变化得出）。
# 上/下与拨轮的作用取决于 MODE 当前选中的项目，与在设备上操作等价。
KEY_NAMES = {
    1: "关机",
    2: "RUN/停止",
    3: "上",
    4: "下",
    5: "MODE",
    6: "AUTO",
    7: "拨轮 ▶",
    8: "确定",
    9: "◀ 拨轮",
    10: "未标定",
    11: "详细信息",
    12: "触发边沿",
    13: "长按 AUTO（自动校正）",
    14: "返回",
}

# 档位表，取自固件字符串池：
#   垂直 0x080227E0 起 13 项（10mV..100V，单位 V/Div），
#   紧随其后 X1 / X10 为探头倍率，AC / DC 为耦合，STOP / RUN 为运行状态；
#   时基 0x0802281C 起 24 项（500nS..20S，单位 s/Div）。
# 顺序即设备上「上」键递增的方向。索引由键 3 / 键 4 在 +0x01 上增减，
# 原厂加档上限为 8（5V/div），所以 0..8 可达；9 起走不到，仅列作对照。
VOLTAGE_STEPS = ("10mV", "20mV", "50mV", "100mV", "200mV", "500mV",
                 "1V", "2V", "5V", "10V", "20V", "50V", "100V")
TIMEBASE_STEPS = ("500nS", "1uS", "2.5uS", "5uS", "10uS", "20uS", "50uS",
                  "100uS", "200uS", "500uS", "1mS", "2mS", "5mS", "10mS",
                  "20mS", "50mS", "100mS", "200mS", "500mS", "1S", "2S",
                  "5S", "10S", "20S")
TIMEBASE_MAX = len(TIMEBASE_STEPS) - 1

FLAG_LIVE = 0x01
FLAG_CHK_ADV = 0x02
FLAG_SCREEN = 0x04
FLAG_SETTINGS = 0x08
FLAG_IMG = 0x20              # 屏幕画面块：负载 = 8 字节描述 + RGB565 像素

# 数据源（命令 0x10）：决定 0x01 / 0x02 送什么
SRC_RAW = 0
SRC_SCREEN = 1
SRC_IMAGE = 2
SRC_NAMES = {SRC_RAW: "原始采样", SRC_SCREEN: "屏幕波形", SRC_IMAGE: "屏幕画面"}

BAND_DESC_LEN = 8            # x, y, w, h（各 2 字节，小端）

FRAME_SAMPLES = 4096
SCREEN_SAMPLES = 301
HEADER_LEN = 8
MAX_SAMPLES = 16384

# ------------------------------------------------ 真实单位换算（固件常数）
# mV/格表 @0x0801F3E8（u16，10 项 10..10000）；后 3 档（20V/50V/100V）为
# ×10 衰减档，mV/格 同为 1-2-5 序列延续。0x0801F3D4 的伴表 = mV/格÷25
# （单位 0.1mV/码值），由此得每格 25 个码值。
VOLTAGE_MV_PER_DIV = (10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000,
                      20000, 50000, 100000)
CODES_PER_DIV = 25
CAL_1KHZ = 1000.0                    # 设备校准方波频率，用于采样率标定


def volts_per_code(v_index: int | None) -> float | None:
    """垂直档位 → 伏/码值。"""
    if v_index is None or not (0 <= v_index < len(VOLTAGE_MV_PER_DIV)):
        return None
    return VOLTAGE_MV_PER_DIV[v_index] / 1000.0 / CODES_PER_DIV


def fmt_volts(v: float) -> str:
    a = abs(v)
    if a >= 1.0:
        return "%.3f V" % v
    if a >= 1e-3:
        return "%.2f mV" % (v * 1e3)
    return "%.1f uV" % (v * 1e6)


def fmt_seconds(s: float) -> str:
    a = abs(s)
    if a >= 1.0:
        return "%.3f S" % s
    if a >= 1e-3:
        return "%.3f mS" % (s * 1e3)
    if a >= 1e-6:
        return "%.3f uS" % (s * 1e6)
    return "%.1f nS" % (s * 1e9)


def measure_wave(samples, zero_code: float = 128.0):
    """对一帧 8 位样本做波形测量（码值域）。

    返回 dict：min/max/pp/mean/rms（码值）、period（平均周期，样本数，
    滞回过零法）、duty（0..1）、cycles（观测到的完整周期数）。
    样本过少或找不到完整边沿时 period/duty 为 None。
    """
    n = len(samples)
    if n < 8:
        return {"min": 0, "max": 0, "pp": 0, "mean": 0, "rms": 0,
                "period": None, "duty": None, "cycles": 0}
    mx = max(samples)
    mn = min(samples)
    mean = sum(samples) / n
    rms = (sum((v - mean) ** 2 for v in samples) / n) ** 0.5
    out = {"min": mn, "max": mx, "pp": mx - mn,
           "mean": mean, "rms": rms, "period": None, "duty": None,
           "cycles": 0}
    mid = (mx + mn) / 2.0
    hys = max(1.0, (mx - mn) * 0.1)
    # 边沿检测：下穿 mid-hys 后武装，首次上穿 mid+hys 记为上升沿
    rising = []
    armed = False
    for i in range(1, n):
        if samples[i - 1] <= mid - hys:
            armed = True
        if armed and samples[i] > mid + hys:
            rising.append(i)
            armed = False
    if len(rising) >= 2:
        gaps = [b - a for a, b in zip(rising, rising[1:])]
        out["period"] = sum(gaps) / len(gaps)
        out["cycles"] = len(rising) - 1
        if len(rising) >= 3:
            # 占空比：第一个完整周期内高于 mid 的比例
            a, b = rising[0], rising[1]
            seg = samples[a:b]
            if seg:
                out["duty"] = sum(1 for v in seg if v > mid) / len(seg)
    return out


class ProtocolError(Exception):
    """帧格式错误。"""


@dataclass
class Frame:
    seq: int
    flags: int
    samples: bytes
    checksum_ok: bool
    checksum: int

    @property
    def count(self) -> int:
        return len(self.samples)

    @property
    def is_screen(self) -> bool:
        return bool(self.flags & FLAG_SCREEN)

    @property
    def is_image(self) -> bool:
        return bool(self.flags & FLAG_IMG)

    @property
    def checksum_advisory(self) -> bool:
        """校验值是否为参照值。

        实时缓冲在发送期间被覆盖，校验只描述发送起始时刻的内容。
        以 CHK_ADV 显式标注；对仅置 LIVE 的早期固件同样按参照值处理。
        """
        if self.flags & FLAG_CHK_ADV:
            return True
        return bool(self.flags & FLAG_LIVE) and not self.is_screen

    @property
    def checksum_text(self) -> str:
        if self.checksum_ok:
            return "通过"
        if self.checksum_advisory:
            return "参照值"
        return "不符"


def decode_band(payload: bytes):
    """解一块屏幕画面：返回 (x, y, w, h, 像素字节)。

    负载布局：x, y, w, h 各 2 字节小端，随后是 w*h 个 RGB565 像素（小端）。
    尺寸不符或长度不足返回 None。
    """
    if len(payload) < BAND_DESC_LEN:
        return None
    x, y, w, h = struct.unpack_from("<4H", payload, 0)
    px = payload[BAND_DESC_LEN:]
    if w == 0 or h == 0 or len(px) < w * h * 2:
        return None
    return x, y, w, h, px[:w * h * 2]


class FrameParser:
    """增量帧解析器。串口数据按块送入，按需返回完整帧。"""

    def __init__(self):
        self._buf = bytearray()
        self.bytes_discarded = 0
        self.frames_ok = 0
        self.frames_bad_chk = 0

    def reset(self):
        self._buf.clear()

    def feed(self, chunk: bytes) -> list[Frame]:
        out: list[Frame] = []
        self._buf += chunk
        while True:
            head = self._buf.find(b"\xA5\x5A")
            if head < 0:
                if len(self._buf) > 1:
                    self.bytes_discarded += len(self._buf) - 1
                    del self._buf[:-1]
                break
            if head > 0:
                self.bytes_discarded += head
                del self._buf[:head]
            if len(self._buf) < HEADER_LEN:
                break
            n = self._buf[2] | (self._buf[3] << 8)
            if n == 0 or n > MAX_SAMPLES:
                self.bytes_discarded += 1
                del self._buf[:1]
                continue
            total = HEADER_LEN + n + 1
            if len(self._buf) < total:
                break
            body = bytes(self._buf[:HEADER_LEN + n])
            chk = self._buf[HEADER_LEN + n]
            ok = (sum(body) & 0xFF) == chk
            frame = Frame(
                seq=body[4] | (body[5] << 8),
                flags=body[6],
                samples=body[HEADER_LEN:],
                checksum_ok=ok,
                checksum=chk,
            )
            if ok:
                self.frames_ok += 1
            else:
                self.frames_bad_chk += 1
            del self._buf[:total]
            out.append(frame)
        return out


def align_trigger(samples: bytes, pre_ratio: float = 0.1) -> bytes:
    """把原始缓冲快照按上升沿对齐，消除逐帧横向漂移。

    上位机拿到的是环形缓冲中固定 4096 字节的一段，而 DMA 持续写入，信号在
    这个窗口里随时间平移；每帧起点不同，画出来就是左右飘。这里在数据中找
    第一个上穿中电平的位置，把触发点之前的一段裁掉，使触发点落在
    pre_ratio（默认 10%）处，等价于示波器的触发对齐 + 固定预触发。

    只裁头部、不循环拼接：窗口内样本按时间顺序排列，若把尾部（最新）接到
    头部（最旧）之前，时间顺序被颠倒，波形会被拼接得变形。代价是显示长度
    每帧相差不到一个周期（4096 点中不足 1%），横向比例无明显变化。

    屏幕波形帧（0x03）由设备固件触发对齐后写入，不应再对齐。
    """
    n = len(samples)
    if n < 16:
        return samples

    lo = min(samples)
    hi = max(samples)
    mid = (lo + hi) // 2
    if hi - lo < 8:
        return samples                      # 幅度过小，无从判定边沿

    pre = int(n * pre_ratio)

    # 只在 pre 之后找上升沿，保证触发点前面永远留得出 pre 个点，从而每帧
    # 触发点都落在同一个位置、显示长度只差不到一个周期。
    idx = -1
    for i in range(pre, n - 1):
        if samples[i] < mid <= samples[i + 1]:
            idx = i
            break
    if idx < 0:
        for i in range(n - 1):              # 周期长于窗口：退回第一个上升沿
            if samples[i] < mid <= samples[i + 1]:
                idx = i
                break
    if idx < 0:
        return samples                      # 窗口内没有完整上升沿

    start = idx - pre
    if start <= 0:
        return samples
    return samples[start:]


def decode_diag(payload: bytes) -> dict | None:
    """解析命令 0x04 上报的 20 字节诊断信息。

    布局（均由固件小端写入）：
        0..3   'STK1'
        4..7   观测到的最低栈地址
        8..9   栈峰值字节数
        10     水印是否已铺设（1 = 有效）
        11     端点忙导致发送失败的次数
        12..15 原厂 stack_base
        16..19 初始 SP（栈顶）
    校验魔数不匹配时返回 None。
    """
    if len(payload) < 20 or payload[:4] != b"STK1":
        return None
    min_sp = int.from_bytes(payload[4:8], "little")
    peak = int.from_bytes(payload[8:10], "little")
    painted = payload[10]
    err = payload[11]
    base = int.from_bytes(payload[12:16], "little")
    top = int.from_bytes(payload[16:20], "little")
    total = (top - base) if top > base else 0
    return {
        "stack_base": base,
        "stack_top": top,
        "stack_total": total,
        "stack_peak": peak,
        "stack_free": (total - peak) if total >= peak else 0,
        "min_sp": min_sp,
        "painted": painted,
        "tx_errors": err,
    }


def decode_fault(payload: bytes) -> dict | None:
    """解析命令 0x05 上报的 HardFault 现场（14 个字，56 字节）。

    字序：CFSR / HFSR / MMFAR / BFAR / R0 / R1 / R2 / R3 / R12 / LR / PC /
    xPSR / 计数 / 魔数。魔数不是 'FLT1' 表示本次上电未记录到异常。
    """
    if len(payload) < 56:
        return None
    words = [int.from_bytes(payload[i * 4:i * 4 + 4], "little") for i in range(14)]
    if words[13] != 0x464C5431:     # "FLT1"
        return None
    return {
        "cfsr": words[0], "hfsr": words[1],
        "mmfar": words[2], "bfar": words[3],
        "r0": words[4], "r1": words[5], "r2": words[6], "r3": words[7],
        "r12": words[8], "lr": words[9], "pc": words[10], "xpsr": words[11],
        "count": words[12],
    }


def fault_explain(info: dict) -> list[str]:
    """把 CFSR / HFSR 翻译成一行行中文说明。"""
    out = []
    cfsr = info["cfsr"]
    if cfsr & 0xFF:                     # MMFSR
        if cfsr & 0x01:
            out.append("MMFSR.IACCVIOL：取指访问违例")
        if cfsr & 0x02:
            out.append("MMFSR.DACCVIOL：数据访问违例 —— 典型的栈溢出破坏指针")
        if cfsr & 0x08:
            out.append("MMFSR.MUNSTKERR：异常返回时出栈失败")
        if cfsr & 0x10:
            out.append("MMFSR.MSTKERR：异常入栈失败 —— 栈已越过下界")
    if (cfsr >> 8) & 0xFF:              # BFSR
        if cfsr & 0x0100:
            out.append("BFSR.IBUSERR：取指总线错误（跳到了非代码地址）")
        if cfsr & 0x0200:
            out.append("BFSR.PRECISERR：精确总线错误，BFAR=0x%08X" % info["bfar"])
        if cfsr & 0x0400:
            out.append("BFSR.IMPRECISERR：非精确总线错误")
        if cfsr & 0x1000:
            out.append("BFSR.STKERR：入栈时总线错误")
        if cfsr & 0x2000:
            out.append("BFSR.UNSTKERR：出栈时总线错误")
    if (cfsr >> 16) & 0xFFFF:           # UFSR
        if cfsr & 0x00010000:
            out.append("UFSR.UNDEFINSTR：未定义指令")
        if cfsr & 0x00020000:
            out.append("UFSR.INVSTATE：非法状态切换（多见于函数指针低位未置 1）")
        if cfsr & 0x00080000:
            out.append("UFSR.NOCP：协处理器不可用（FPU 未使能）")
        if cfsr & 0x01000000:
            out.append("UFSR.UNALIGNED：非对齐访问")
        if cfsr & 0x02000000:
            out.append("UFSR.DIVBYZERO：除零")
    if info["hfsr"] & 0x40000000:
        out.append("HFSR.FORCED：其它异常被升级为 HardFault")
    if info["hfsr"] & 0x00000002:
        out.append("HFSR.VECTTBL：取向量时总线错误")
    if not out:
        out.append("CFSR/HFSR 未置位，异常原因需结合 PC 人工判断")
    return out


def encode_frame(seq: int, flags: int, samples: bytes) -> bytes:
    n = len(samples)
    body = struct.pack("<2sHHBB", b"\xA5\x5A", n, seq & 0xFFFF, flags, 0) + samples
    return body + bytes([sum(body) & 0xFF])


@dataclass
class PortInfo:
    device: str
    description: str
    vid: int | None
    pid: int | None

    @property
    def is_target(self) -> bool:
        return self.vid == VID and self.pid == PID

    def label(self) -> str:
        tag = "" if self.is_target else "  (非目标设备)"
        return "%s  %s%s" % (self.device, self.description or "-", tag)


def require_serial():
    try:
        import serial
        from serial.tools import list_ports
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("缺少 pyserial，请执行: python -m pip install pyserial") from exc
    return serial, list_ports


def list_ports() -> list[PortInfo]:
    _, list_ports_mod = require_serial()
    result = []
    for p in list_ports_mod.comports():
        vid = pid = None
        hwid = p.hwid or ""
        if "VID:PID=" in hwid:
            try:
                tag = hwid.split("VID:PID=")[1][:9]
                vid, pid = (int(x, 16) for x in tag.split(":"))
            except (ValueError, IndexError):
                pass
        result.append(PortInfo(p.device, p.description, vid, pid))
    return result


def find_port() -> str | None:
    for p in list_ports():
        if p.is_target:
            return p.device
    return None


# USB CDC 为虚拟串口，波特率不参与实际速率（由 USB 批量传输带宽决定）。
# 该值需与固件 cdc_class.c 中 linecoding 的默认波特率保持一致，
# 仅为名义值，用于主机侧工具显示。
DEFAULT_BAUD = 921600


def open_port(port: str, timeout: float = 0.2, baud: int = DEFAULT_BAUD):
    serial, _ = require_serial()
    ser = serial.Serial(port, baud, timeout=timeout)
    ser.reset_input_buffer()
    return ser


@dataclass
class SignalStats:
    count: int
    minimum: int
    maximum: int
    mean: float
    rms: float
    peak_to_peak: int
    code_mid: float

    @classmethod
    def from_samples(cls, samples: bytes) -> "SignalStats":
        n = len(samples)
        if n == 0:
            return cls(0, 0, 0, 0.0, 0.0, 0, 0.0)
        lo = min(samples)
        hi = max(samples)
        total = sum(samples)
        mean = total / n
        rms = math.sqrt(sum(v * v for v in samples) / n)
        return cls(n, lo, hi, mean, rms, hi - lo, mean)


class SimulatedSerial:
    """离线仿真设备，接口与 serial.Serial 的常用子集一致。

    用于在没有硬件时验证上位机界面与解析逻辑。内存模型按真机已知布局：
    设置结构在 0x2000001C（垂直 +0x01、探头 +0x03、时基 +0x0D、
    触发电平 +0x24 半字、触发模式 +0x2A），底栏选中项 +0x0E（从 1 起）。
    """

    # (相对 0x2000001C 的偏移, 上限, 步进, 字节数)；顺序对应选中项 1..4
    SIM_SLOTS = ((0x01, VOLTAGE_MAX, 1, 1), (0x03, PROBE_MAX, 1, 1),
                 (0x0D, TIMEBASE_MAX, 1, 1), (0x24, 232, 8, 2))
    SIM_BASE = 0x2000001C
    SIM_SLOT_ADDR = 0x20004FCC
    SIM_RAM_SPAN = 0x10000        # 覆盖 0x20000000..0x2000FFFF

    def __init__(self, frame_interval: float = 0.12):
        self._buf = bytearray()
        self._stream = False
        self._interval = frame_interval
        self._last = 0.0
        self._seq = 0
        self._phase = 0.0
        self.is_open = True
        self._ram = bytearray(self.SIM_RAM_SPAN)
        self._slot = 0
        self._auto()

    def _samples(self, n: int, freq_cycles: float) -> bytes:
        data = bytearray(n)
        for i in range(n):
            v = 128.0 + 92.0 * math.sin(2 * math.pi * (freq_cycles * i / n))
            v += 6.0 * math.sin(2 * math.pi * 31 * i / n)
            data[i] = max(0, min(255, int(v)))
        return bytes(data)

    def _queue(self, flags: int, n: int, freq: float):
        samples = self._samples(n, freq)
        self._buf += encode_frame(self._seq, flags, samples)
        self._seq = (self._seq + 1) & 0xFFFF

    def write(self, data: bytes) -> int:
        i = 0
        while i < len(data):
            cmd = data[i]
            if cmd == CMD_KEY and i + 1 < len(data):
                self._key(data[i + 1])
                i += 2
                continue
            if cmd == CMD_SET_PARAM and i + 2 < len(data):
                role, target = data[i + 1], data[i + 2]
                if role == PARAM_TIMEBASE and 0 <= target <= TIMEBASE_MAX:
                    self._field_set(TIMEBASE_OFFSET, 1, target)
                elif role == PARAM_VOLTAGE and 0 <= target <= VOLTAGE_MAX:
                    self._field_set(VOLTAGE_OFFSET, 1, target)
                elif role == PARAM_PROBE and 0 <= target <= PROBE_MAX:
                    self._field_set(PROBE_OFFSET, 1, target)
                i += 3
                continue
            if cmd == CMD_WRITE_RAM and i + 5 < len(data):
                addr = int.from_bytes(data[i + 1:i + 5], "little")
                n = data[i + 5]
                if n and i + 6 + n <= len(data):
                    self._ram_write(addr, data[i + 6:i + 6 + n])
                i += 6 + n
                continue
            if cmd == CMD_APPLY and i + 1 < len(data):
                i += 2          # 仿真不需要重新应用
                continue
            if cmd == CMD_RAMREAD and i + 5 < len(data):
                addr = int.from_bytes(data[i + 1:i + 5], "little")
                n = data[i + 5] or 256
                payload = self._ram_read(addr, n)
                if payload is not None:
                    self._buf += encode_frame(self._seq, 0, payload)
                    self._seq = (self._seq + 1) & 0xFFFF
                i += 6
                continue
            if cmd == CMD_SINGLE:
                self._queue(FLAG_LIVE | FLAG_CHK_ADV, FRAME_SAMPLES, 5.0)
            elif cmd == CMD_SCREEN:
                self._queue(FLAG_SCREEN, SCREEN_SAMPLES, 5.0)
            elif cmd == CMD_SETTINGS:
                self._buf += encode_frame(
                    self._seq, FLAG_SETTINGS,
                    bytes(self._ram[self.SIM_BASE:
                                    self.SIM_BASE + SETTINGS_LEN]))
                self._seq = (self._seq + 1) & 0xFFFF
            elif cmd == CMD_STREAM:
                self._stream = True
                self._last = 0.0
            elif cmd == CMD_STOP:
                self._stream = False
            i += 1
        return len(data)

    def _ram_read(self, addr: int, n: int) -> bytes | None:
        lo, hi = 0x20000000, 0x20000000 + self.SIM_RAM_SPAN
        if not (lo <= addr and addr + n <= hi):
            return None
        return bytes(self._ram[addr - lo:addr - lo + n])

    def _ram_write(self, addr: int, payload: bytes) -> None:
        lo, hi = 0x20000000, 0x20000000 + self.SIM_RAM_SPAN
        if not (lo <= addr and addr + len(payload) <= hi):
            return
        off = addr - lo
        self._ram[off:off + len(payload)] = payload

    def _field_get(self, off: int, size: int) -> int:
        lo = self.SIM_BASE - 0x20000000
        b = self._ram[lo + off:lo + off + size]
        return int.from_bytes(b, "little")

    def _field_set(self, off: int, size: int, val: int) -> None:
        lo = self.SIM_BASE - 0x20000000
        self._ram[lo + off:lo + off + size] = val.to_bytes(size, "little")

    def _auto(self) -> None:
        self._field_set(TIMEBASE_OFFSET, 1, 10)   # 时基 1mS
        self._field_set(VOLTAGE_OFFSET, 1, 7)     # 垂直 2V/div
        self._field_set(PROBE_OFFSET, 1, 0)       # 探头 ×1
        self._field_set(0x24, 2, 120)             # 触发电平
        self._field_set(0x2A, 1, 1)               # 触发模式
        self._slot = 0
        self._ram[self.SIM_SLOT_ADDR - 0x20000000] = 0
        self._field_set(CURSOR_OFFSET, 1, 1)      # 选中项从 1 起（真机口径）

    def _key(self, code: int) -> None:
        """模拟按键对设置结构的影响。"""
        if code in (3, 7):                    # 上 / 拨轮右
            off, upper, step, size = self.SIM_SLOTS[self._slot]
            self._field_set(off, size,
                            min(upper, self._field_get(off, size) + step))
        elif code in (4, 9):                  # 下 / 拨轮左
            off, upper, step, size = self.SIM_SLOTS[self._slot]
            self._field_set(off, size,
                            max(0, self._field_get(off, size) - step))
        elif code == 5:                       # MODE，循环切换槽位
            self._slot = (self._slot + 1) % len(self.SIM_SLOTS)
            self._ram[self.SIM_SLOT_ADDR - 0x20000000] = self._slot
            # 真机的选中项落在设置结构 +0x0E，且**从 1 起**（1 档位 / 2 探头
            # / 3 时基 / 4 耦合）；0x20004FCC 是 UI 页面号，不是选中项。
            self._field_set(CURSOR_OFFSET, 1, self._slot + 1)
        elif code == 6:                       # AUTO，恢复到一组典型档位
            self._auto()

    def read(self, size: int = 1) -> bytes:
        if self._stream and time.time() - self._last >= self._interval:
            self._last = time.time()
            self._phase += 0.37
            self._queue(FLAG_LIVE | FLAG_CHK_ADV, FRAME_SAMPLES, 5.0)
        if not self._buf:
            return b""
        out = bytes(self._buf[:size])
        # 只删实际取走的长度：slice 与 del 之间若别的线程追加了应答帧，
        # 按 size 整段删会把刚写入的帧吞掉。
        del self._buf[:len(out)]
        return out

    def reset_input_buffer(self):
        self._buf.clear()

    def flush(self):
        pass

    def close(self):
        self.is_open = False
