#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""档位直选控制

字段布局已由固件静态分析确认（时基 +0x0D、垂直 +0x01 0..8、探头 +0x03、
底栏选中项 +0x0E 从 1 起），内置布局 direct=True。GUI 上位机走命令 0x0D
直设：发完即返回，固件在设备内部改字段、切前端衰减、推 UI 事件；本模块
的 StepController 只提供读回（0x08，GUI 经 ram_read 钩子由采集线程分发）。

按键注入路径（goto/_ensure_slot/probe_cursor）保留给旧固件与独立 CLI 工具，
行为约束不变：

1. 键码不是「按下即生效」。命令 0x06 只是把键码交给补丁的一个待注入槽位，
   由主循环（约 200ms 一轮）取走、入队，再由设备按键任务分派。因此注入间隔
   必须大于主循环周期，否则后到的键码会覆盖前一个，连按 8 次只走 1 档。
   本模块以 KEY_GAP 逐个下发，并在每批之后回读确认。
2. 旧固件上垂直档位偏移可用 discover() 运行期实测：逐个 MODE 槽位按一下
   「下」，看哪个字节跟着减一。探测过程逐键复原，结束后把原值写回。

字段布局缓存在 phosphor_layout.json，格式见 docs/HOST.md 5.1；base 与
slot_addr 允许写成 "0x2000001C" 这样的字符串，也可以是整数。
"""

from __future__ import annotations

import json
import os
import sys
import time

import phosphor_proto as proto

KEY_UP, KEY_DOWN, KEY_MODE = 3, 4, 5

SETTINGS_WINDOW = 128          # 读设置结构的窗口字节数（垂直字段可能超过 +0x3F）
SLOT_MAX = 16                  # MODE 循环长度上限（真机 9 项）
VOLTAGE_MAX = proto.VOLTAGE_MAX   # 垂直档位索引上限，原厂键 3 停在 8（5V/div）
TIMEBASE_MAX = proto.TIMEBASE_MAX  # 时基索引上限（24 档）
TIMEBASE_OFFSET = proto.TIMEBASE_OFFSET   # 静态分析已定位：cmp #23 后递增


def app_dir() -> str:
    """配置文件所在目录；打包后取可执行文件目录（详见 phosphor_host.app_dir）。"""
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


LAYOUT_PATH = os.path.join(app_dir(), "phosphor_layout.json")

ROLE_NAMES = {
    "timebase": "时基",
    "voltage": "垂直档位",
    "probe": "探头倍率",
    "enum": "触发模式",
}

#: 角色 → 布局中的档位表键名
STEPS_KEY = {"timebase": "timebase_steps", "voltage": "voltage_steps",
              "probe": "probe_steps"}

#: 角色 → 固件命令 0x0D 的角色编号
PARAM_CODE = {"timebase": proto.PARAM_TIMEBASE,
              "voltage": proto.PARAM_VOLTAGE,
              "probe": proto.PARAM_PROBE}


def _addr(value, default: int) -> int:
    """地址字段可能是 int、'0x2000001C' 或 '536870940'。"""
    if value is None:
        return default
    if isinstance(value, str):
        return int(value, 0)
    return int(value)


def _normalize(data: dict | None) -> dict | None:
    """校验并规范化布局；字段缺失或越界时返回 None。

    旧版 phosphor_calib.py 产出的布局以 0x2000A528（UI 状态区）为基址且不带
    base，若直接套到 0x2000001C 上会读到完全无关的字节。这类文件一律作废，
    让界面退回键码面板，而不是执行错误的跳档。
    """
    if not isinstance(data, dict):
        return None
    roles = data.get("roles") or {}
    if not roles:
        return None
    # base 必须显式写出：旧版标定以 0x2000A528（UI 状态区）为基址且不带该
    # 字段，若默认补成 0x2000001C 就会把 offset 套到错误的地址上。
    if data.get("base") is None:
        return None
    try:
        data["base"] = _addr(data.get("base"), proto.STRUCT_ADDR)
        if data.get("slot_addr") is not None:
            data["slot_addr"] = _addr(data.get("slot_addr"), proto.SLOT_ADDR)
    except (TypeError, ValueError):
        return None
    for role, entry in roles.items():
        off = entry.get("offset") if isinstance(entry, dict) else None
        if not isinstance(off, int) or not (0 <= off < SETTINGS_WINDOW):
            return None
    off = data.get("cursor_offset")
    if off is not None and (not isinstance(off, int)
                            or not (0 <= off < SETTINGS_WINDOW)):
        data.pop("cursor_offset", None)
    try:
        data["mode_key"] = int(data.get("mode_key", 5))
    except (TypeError, ValueError):
        data["mode_key"] = 5
    if not data.get("cycle"):
        data["cycle"] = int(SLOT_MAX)
    if not data.get("timebase_steps"):
        data["timebase_steps"] = list(proto.TIMEBASE_STEPS)
    if not data.get("voltage_steps"):
        data["voltage_steps"] = list(proto.VOLTAGE_STEPS)
    return data


def load_layout(path: str = LAYOUT_PATH) -> dict | None:
    """读取标定得到的字段布局；不存在或无效时返回 None。"""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return _normalize(data)


def builtin_layout() -> dict:
    """固件静态分析确认的字段布局，不需要运行期识别。

    时基索引 +0x0D、垂直档位索引 +0x01、探头倍率 +0x03 均已由反汇编 + 实机
    双向核对确认（垂直档位在轮盘处理函数中带 `cmp #8`，前端衰减由
    0x08008754(idx) 按同一索引切）。`direct` 表示优先用固件命令 0x0D 直接
    设定，不再依赖选中项与按键步进。
    """
    return {
        "base": proto.STRUCT_ADDR,
        "direct": True,
        "cursor_offset": proto.CURSOR_OFFSET,
        "roles": {
            "timebase": {"offset": proto.TIMEBASE_OFFSET,
                         "min": 0, "max": TIMEBASE_MAX},
            "voltage": {"offset": proto.VOLTAGE_OFFSET,
                        "min": 0, "max": VOLTAGE_MAX},
            "probe": {"offset": proto.PROBE_OFFSET,
                      "min": 0, "max": len(proto.PROBE_STEPS) - 1},
        },
        "others": [],
        "timebase_steps": list(proto.TIMEBASE_STEPS),
        # 下拉里只列设备真能走到的档位；9..12（10V..100V/div）原厂加了也不生效
        "voltage_steps": list(proto.VOLTAGE_STEPS[:VOLTAGE_MAX + 1]),
        "probe_steps": list(proto.PROBE_STEPS),
    }


def save_layout(layout: dict, path: str = LAYOUT_PATH) -> bool:
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(layout, fh, ensure_ascii=False, indent=1)
        return True
    except OSError:
        return False


def sim_layout() -> dict:
    """仿真设备专用的字段布局，对应 SimulatedSerial.SIM_SLOTS。

    仅用于无硬件时演示与测试档位面板，切勿用于真机。
    """
    return {
        "cycle": 4,
        "base": proto.STRUCT_ADDR,
        "slot_addr": proto.SLOT_ADDR,
        "cursor_offset": proto.CURSOR_OFFSET,
        "simulated": True,
        "direct": True,
        "roles": {
            "timebase": {"offset": proto.TIMEBASE_OFFSET, "slot": 2,
                         "min": 0, "max": TIMEBASE_MAX},
            "voltage": {"offset": proto.VOLTAGE_OFFSET, "slot": 0,
                        "min": 0, "max": VOLTAGE_MAX},
            "probe": {"offset": proto.PROBE_OFFSET, "slot": 1,
                      "min": 0, "max": 1},
        },
        "others": [{"offset": 0x24, "slot": 3, "role": "continuous"}],
        "timebase_steps": list(proto.TIMEBASE_STEPS),
        "voltage_steps": list(proto.VOLTAGE_STEPS[:VOLTAGE_MAX + 1]),
    }


class StepController:
    """通过按键注入直接跳到指定档位。"""

    #: 相邻两个键码之间的间隔。设备主循环约 200ms 取走一个键码，短于此值
    #: 时后到的键码会覆盖待注入槽位（老固件只留一个字节）。
    KEY_GAP = 0.24
    #: MODE 之后等待设备重绘并保存设置。
    MODE_WAIT = 0.30
    #: 每批按键之后的额外稳定时间。
    SETTLE = 0.35
    #: 回读轮询间隔。
    POLL_GAP = 0.15
    #: 单次跳档的最大往返轮数（每轮按一小批并回读）。
    MAX_ROUNDS = 10
    #: 选中项一圈最少几项。运行模式 / 边沿 / 耦合这类 2–3 项开关看起来也像
    #: 「跟着按键循环」，用这个下限把它们排除。
    MIN_CURSOR_CYCLE = 4

    def __init__(self, get_link, layout: dict, ram_read=None):
        self._get_link = get_link          # callable -> serial 对象或 None
        self.layout = layout
        # 可选钩子：ram_read(addr, n, timeout) -> bytes | None。GUI 上位机传
        # 入由采集线程分发的实现，读回不独占串口、采集不断流；为 None 时
        # （独立 CLI 工具）在调用线程里直接等应答。
        self.ram_read = ram_read
        self._parser = proto.FrameParser()
        self.last_settings: bytes | None = None

    # ------------------------------------------------------------ 基础
    def _read_window(self, addr: int, n: int,
                     timeout: float = 0.6, retries: int = 2) -> bytes | None:
        """命令 0x08：读任意 SRAM。需要 R5 固件。"""
        if self.ram_read is not None:
            return self.ram_read(addr, n, timeout)
        link = self._get_link()
        if link is None:
            return None
        cmd = bytes([proto.CMD_RAMREAD]) + int(addr).to_bytes(4, "little") + \
            bytes([n])
        for attempt in range(retries):
            link.write(cmd)
            link.flush()
            deadline = time.time() + timeout
            while time.time() < deadline:
                chunk = link.read(4096)
                if not chunk:
                    continue
                for frame in self._parser.feed(chunk):
                    if len(frame.samples) == n:
                        return bytes(frame.samples)
            if attempt + 1 < retries:
                time.sleep(0.05)
        return None

    def read(self, window: int = SETTINGS_WINDOW) -> bytes | None:
        """读设置结构（base 起 window 字节）。"""
        base = int(self.layout.get("base", proto.STRUCT_ADDR))
        data = self._read_window(base, window)
        if data is not None:
            self.last_settings = data
        return data

    def press(self, code: int, repeat: int = 1, gap: float | None = None) -> None:
        """注入键码。逐个下发并按 KEY_GAP 间隔，避免覆盖待注入槽位。"""
        link = self._get_link()
        if link is None:
            return
        gap = self.KEY_GAP if gap is None else gap
        for i in range(repeat):
            link.write(bytes([proto.CMD_KEY, code]))
            link.flush()
            if i + 1 < repeat:
                time.sleep(gap)

    # ------------------------------------------------------------ 查询
    def index_of(self, role: str) -> int | None:
        """当前档位索引。"""
        if role in PARAM_CODE and self.layout.get("direct"):
            return self.read_field(role)
        entry = self.layout["roles"].get(role)
        if not entry:
            return None
        st = self.read()
        if st is None:
            return None
        return st[entry["offset"]]

    def read_field(self, role: str) -> int | None:
        """只读该角色对应的 1 个字节（比读 128 字节快很多）。"""
        entry = self.layout["roles"].get(role)
        if not entry:
            return None
        base = int(self.layout.get("base", proto.STRUCT_ADDR))
        data = self._read_window(base + entry["offset"], 1,
                                 timeout=0.35, retries=1)
        return None if data is None else data[0]

    def name_of(self, role: str, index: int | None = None) -> str | None:
        if index is None:
            index = self.index_of(role)
        if index is None:
            return None
        table = self.layout.get(STEPS_KEY.get(role, "voltage_steps"))
        if not table or not (0 <= index < len(table)):
            return None
        return table[index]

    def slot_now(self) -> int | None:
        """MODE 当前选中项。

        优先用实测到的结构内偏移（cursor_offset），其次读 0x20004FCC，最后
        退回窗口字段。真机上 0x20004FCC 在有些状态下并不跟着 MODE 键变化，
        而结构 `+0x01`（0..8 递增）才是选中项本体，因此偏移优先。
        """
        off = self.layout.get("cursor_offset")
        if isinstance(off, int):
            st = self.read()
            return None if st is None else st[off]
        sa = self.layout.get("slot_addr")
        if sa is not None:
            b = self._read_window(int(sa), 1)
            if b is not None:
                return b[0]
        off = self.layout.get("slot_field")
        if off is None:
            return None
        st = self.read()
        if st is None:
            return None
        return st[off]

    def _settle(self, offset: int, timeout: float = 3.5) -> int | None:
        """等设备把按键队列走完，返回稳定后的字段值。

        设备大约每 200ms 消化一个键码，两档之间字段会停在中间值上，所以
        判据取「连续三次、间隔 150ms 读数相同」（约 300ms 无变化），短于
        队列消化节奏的停顿不会被误判为结束。超时后返回最后一次读数。
        """
        deadline = time.time() + timeout
        last = None
        stable = 0
        while time.time() < deadline:
            st = self.read()
            if st is None:
                return None
            value = st[offset]
            if value == last:
                stable += 1
                if stable >= 3:
                    return value
            else:
                last = value
                stable = 0
            time.sleep(self.POLL_GAP)
        return last

    # ------------------------------------------------------------ 定位槽位
    def _mode_key(self) -> int:
        return int(self.layout.get("mode_key", KEY_MODE))

    def _ensure_slot(self, target: int, budget: int = SLOT_MAX + 4) -> bool:
        """把 MODE 选中项切到 target 槽位（回读确认）。

        连续两次按键都读不到变化就停下：真机上曾出现键码 5 并不移动选中项，
        旧实现会一直按键，设备屏幕上的运行模式（Single/Normal/Auto）被反复
        切换，看起来像「卡住」。
        """
        if (self.layout.get("cursor_offset") is None
                and self.layout.get("slot_addr") is None
                and self.layout.get("slot_field") is None):
            return False
        key = self._mode_key()
        misses = 0
        for _ in range(budget):
            cur = self.slot_now()
            if cur is None:
                return False
            if cur == target:
                return True
            self.press(key)
            time.sleep(self.MODE_WAIT)
            if self.slot_now() == cur:
                misses += 1
                if misses >= 2:
                    return False
            else:
                misses = 0
        return self.slot_now() == target

    # ------------------------------------------------------------ 跳档
    def _set_direct(self, role: str, target: int,
                    status=None) -> tuple[bool, str]:
        """命令 0x0D：让固件在设备内部直接走到目标档位。

        固件把「上/下」或「拨轮」按键注入到设备队列，并按设备主循环节奏
        一次走一档，因此这里轮询单字节字段，直到到达目标值。比主机逐键
        注入快，也不受 MODE 选中项与按键队列深度影响。
        """
        entry = self.layout["roles"].get(role)
        if not entry:
            return False, "布局中没有该档位"
        link = self._get_link()
        if link is None:
            return False, "尚未连接设备"
        table = self.layout.get(STEPS_KEY.get(role, "")) or []
        code = PARAM_CODE.get(role)
        if code is None:
            return False, "未知档位角色"
        if status:
            status("设定%s → %s…" % (ROLE_NAMES.get(role, role),
                                     table[target] if table else target))
        try:
            link.write(bytes([proto.CMD_SET_PARAM, code, target]))
            link.flush()
        except Exception as exc:
            return False, "发送命令失败：%s" % exc

        started = time.time()
        last = None
        last_change = started
        while time.time() - started < 15.0:
            cur = self.read_field(role)
            if cur is None:
                return False, "读取设置失败（需要支持 0x08 的固件）"
            if cur == target:
                return True, "已切到 %s" % (table[target] if table else target)
            if cur != last:
                last = cur
                last_change = time.time()
            elif time.time() - last_change > 3.0:
                # 固件不认识 0x0D（旧版本）、设备停在端点或已卡住：
                # 3 秒内字段完全不动就立即报错，不让界面一直“卡着”。
                return False, ("设备没有继续改档（停在索引 %d）。"
                               "请确认已刷入 R23 以上固件，且设备在主界面"
                               % cur)
            time.sleep(0.08)
        return False, "设备停在索引 %s（期望 %s）" % (last, target)

    def goto(self, role: str, target: int, status=None) -> tuple[bool, str]:
        """跳到指定档位。返回 (成功, 说明)。"""
        entry = self.layout["roles"].get(role)
        if not entry:
            return False, "布局中没有该档位"
        table = self.layout.get(STEPS_KEY.get(role, "")) or []
        if table and not (0 <= target < len(table)):
            return False, "目标档位超出范围"
        if self.layout.get("direct"):
            ok, msg = self._set_direct(role, target, status)
            if ok:
                return True, msg
            # 旧固件不认识 0x0D 时退回按键导航；内置布局没有槽位信息，
            # 这时只能如实报告失败。
            if "slot" not in entry:
                return False, msg + "（当前固件不支持命令 0x0D，请刷 R22 以上）"
        if status:
            status("定位到「%s」选中项…" % ROLE_NAMES.get(role, role))
        if not self._ensure_slot(entry["slot"]):
            return False, "无法切到该档位所在的选中项（设备可能不在主界面）"

        off = entry["offset"]
        cur = self._settle(off)
        if cur is None:
            return False, "读取设置失败"
        if cur == target:
            return True, "已在目标档位"

        rounds = 0
        while cur != target and rounds < self.MAX_ROUNDS:
            rounds += 1
            delta = target - cur
            code = KEY_UP if delta > 0 else KEY_DOWN
            batch = min(abs(delta), 8)        # 设备按键队列 20 项，8 次一批
            self.press(code, batch)
            if status:
                status("调整档位 %d → %d…" % (cur, target))
            time.sleep(self.SETTLE)
            new = self._settle(off)
            if new is None:
                return False, "确认时读取设置失败"
            if new == cur:
                return False, ("设备停在 %s（期望 %s），按键未生效或已到端点"
                               % (cur, target))
            cur = new

        if cur == target:
            return True, "已切到 %s" % (table[target] if table else target)
        return False, "设备停在 %s（期望 %s）" % (cur, target)

    # ------------------------------------------------------------ 识别
    def probe_cursor(self, status=None) -> tuple[int | None, int, str]:
        """连按 MODE，从设置结构里找出「当前选中项」字段。

        返回 (偏移, 循环长度, 说明)。判据是「跟着每次按键递增、走一圈后回到
        起点，且一圈不少于 MIN_CURSOR_CYCLE 项」。连续三次按键都没有任何字段
        变化就放弃，不再空按——旧实现在这里会一直按 MODE，设备屏幕上的运行
        模式（Single/Normal/Auto）被反复切换。

        之所以不直接读 0x20004FCC：该字节在真机某些状态下不跟随 MODE 变化，
        而结构 `+0x01`（`0x0800AE8C` 处递增、0..8）才是选中项本体。
        """
        key = self._mode_key()
        base = self.read()
        if base is None:
            return None, 0, "读取设置失败（需要 R5 以上固件）"

        prev = base
        seq: dict[int, list[int]] = {}
        idle = 0
        for step in range(1, SLOT_MAX + 1):
            self.press(key)
            time.sleep(self.MODE_WAIT)
            st = self.read()
            if st is None:
                return None, 0, "读取设置失败"
            changed = False
            for off in range(min(len(prev), len(st))):
                if st[off] == prev[off] or not (0 <= st[off] <= SLOT_MAX):
                    continue
                changed = True
                vals = seq.setdefault(off, [prev[off]])
                vals.append(st[off])
                if vals[-1] == base[off]:
                    if step >= self.MIN_CURSOR_CYCLE:
                        return off, step, ("选中项 +0x%02X，循环 %d 项"
                                           % (off, step))
                    # 一圈只有 2、3 项：典型的运行模式 / 边沿 / 耦合开关
                    return None, 0, ("键码 %d 只让 +0x%02X 在 %s 之间循环"
                                     "（%d 项），不像选中项 —— 换一个 MODE 键码"
                                     % (key, off,
                                        "/".join(map(str, sorted(set(vals)))),
                                        step))
            prev = st
            idle = 0 if changed else idle + 1
            if step >= 3 and idle >= 3:
                break

        if seq:
            off, vals = max(seq.items(), key=lambda kv: len(kv[1]))
            return None, 0, ("键码 %d 作用于 +0x%02X（%s），但没有回到起点，"
                             "无法确定循环"
                             % (key, off, "->".join(map(str, vals[-8:]))))
        return None, 0, ("键码 %d 没有改变设置结构里的任何字段：设备可能不在"
                         "主界面，或该键码不是 MODE" % key)

    def _probe_slot(self) -> tuple[list[tuple[int, int, int]], int | None, bool]:
        """在当前槽位按一下键，返回 (变化列表, 探测键, 是否复原)。

        变化列表元素为 (偏移, 旧值, 新值)。先按「下」，没反应说明该字段已在
        下端（例如垂直档位在 10mV），再改按「上」。探测后按实测步长逐键还原。
        """
        before = self.read()
        if before is None:
            return [], None, False
        for key in (KEY_DOWN, KEY_UP):
            self.press(key)
            time.sleep(self.SETTLE)
            after = self.read()
            if after is None:
                return [], None, False
            changed = [(off, before[off], after[off])
                       for off in range(min(len(before), len(after)))
                       if before[off] != after[off]]
            if changed:
                return changed, key, self._restore(changed, key)
        return [], KEY_DOWN, True

    def _restore(self, changed, probe_key: int) -> bool:
        """把探测时被改动的字节还原。

        按「按键 → 字段变化量」的实测步长反推次数：触发电平一类字段每按一次
        走 8 个码值，逐键 +1 地补是补不回来的（会把值推得更高）。
        """
        if not changed:
            return True
        st = self.read()
        if st is None:
            return False
        for off, old, new in changed:
            step = new - old
            cur = st[off]
            if cur == old:
                continue
            diff = old - cur
            if step == 0 or diff % step != 0 or abs(diff // step) > 32:
                return False
            n = diff // step
            # n 为正表示还要沿探测键的方向继续，为负则按反方向键
            key = probe_key if n > 0 else (KEY_UP if probe_key == KEY_DOWN
                                           else KEY_DOWN)
            self.press(key, abs(n))
            time.sleep(self.SETTLE)
            st = self.read()
            if st is None:
                return False
        return all(st[off] == old for off, old, _new in changed)

    def _measure_range(self, offset: int) -> tuple[int, int, bool]:
        """把字段推到两端，返回 (下端, 上端, 是否成功)。"""
        span = VOLTAGE_MAX + 1             # 9 次足以从任意 0..8 走到端点
        self.press(KEY_DOWN, span)
        time.sleep(self.SETTLE)
        low = self._settle(offset)
        self.press(KEY_UP, span)
        time.sleep(self.SETTLE)
        high = self._settle(offset)
        if low is None or high is None:
            return 0, 0, False
        return low, high, True

    def discover(self, status=None) -> tuple[dict | None, str]:
        """实测出时基/垂直两个选中项，返回 (布局, 说明)。

        时基字段的偏移由静态分析确定为 +0x0D；垂直字段靠「按一下下、看谁减一」
        找候选，再用「下到底回到 0、上到顶停在第 8 档（5V/div）」把 9 档档位
        与取值 0..2 的枚举项区分开。全程不写设备内存，改动逐键复原。
        """
        if self.read() is None:
            return None, "设备无响应：需要 R5 以上固件（命令 0x08）"

        def note(msg):
            if status:
                status(msg)

        note("连按 MODE，定位当前选中项字段…")
        cursor, cycle, why = self.probe_cursor(status)
        if cursor is None:
            return None, why
        self.layout["cursor_offset"] = cursor
        note(why)

        timebase = None
        candidates = []
        start_slot = self.slot_now()
        for slot in range(cycle):
            note("探测选中项 %d/%d…" % (slot + 1, cycle))
            if not self._ensure_slot(slot):
                return None, "无法定位到选中项 %d" % slot
            changed, _key, restored = self._probe_slot()
            if not restored:
                return None, "选中项 %d 的探测未能复原，已中止" % slot
            for off, old, new in changed:
                if abs(new - old) != 1:
                    continue                     # 触发电平一类大步长字段
                if off == TIMEBASE_OFFSET and 0 <= new <= TIMEBASE_MAX:
                    timebase = {"offset": off, "slot": slot, "min": 0,
                                "max": TIMEBASE_MAX}
                elif 0 <= new <= VOLTAGE_MAX:
                    candidates.append({"offset": off, "slot": slot,
                                       "value": old, "min": 0,
                                       "max": VOLTAGE_MAX})

        voltage = None
        # 候选按出现顺序确认；取值小的多半是枚举（耦合/探头倍率/触发模式）
        for cand in sorted(candidates, key=lambda c: -c["value"])[:2]:
            note("确认候选 +0x%02X（选中项 %d）的档位范围…"
                 % (cand["offset"], cand["slot"]))
            if not self._ensure_slot(cand["slot"]):
                continue
            low, high, ok = self._measure_range(cand["offset"])
            if not ok:
                continue
            restore = high - cand["value"]
            if restore > 0:
                self.press(KEY_DOWN, restore)
                time.sleep(self.SETTLE)
            if low == 0 and high == VOLTAGE_MAX:
                voltage = {"offset": cand["offset"], "slot": cand["slot"],
                           "min": 0, "max": VOLTAGE_MAX}
                break

        if start_slot is not None:
            self._ensure_slot(start_slot, budget=SLOT_MAX + 4)

        if timebase is None and voltage is None:
            return None, "没有找到可步进的档位字段（设备是否停在主界面？）"
        layout = {
            "base": int(self.layout.get("base", proto.STRUCT_ADDR)),
            "slot_addr": int(self.layout.get("slot_addr", proto.SLOT_ADDR)),
            "cursor_offset": cursor,
            "mode_key": self._mode_key(),
            "cycle": cycle,
            "roles": {},
            "others": [],
            "timebase_steps": list(self.layout.get("timebase_steps")
                                   or proto.TIMEBASE_STEPS),
            "voltage_steps": list(self.layout.get("voltage_steps")
                                  or proto.VOLTAGE_STEPS),
        }
        if timebase:
            layout["roles"]["timebase"] = timebase
        if voltage:
            layout["roles"]["voltage"] = voltage
        parts = []
        if timebase:
            parts.append("时基 +0x%02X/选中项 %d" % (timebase["offset"],
                                                 timebase["slot"]))
        if voltage:
            parts.append("垂直 +0x%02X/选中项 %d" % (voltage["offset"],
                                                 voltage["slot"]))
        else:
            parts.append("垂直档位未确认")
        return layout, "识别完成：" + "，".join(parts)

    # ------------------------------------------------------------ 单槽位探测
    def probe_slot_field(self, slot: int) -> dict | None:
        """只探测一个槽位（供 tools/host/phosphor_scan.py --learn 调用）。"""
        if not self._ensure_slot(slot):
            return None
        changed, _key, restored = self._probe_slot()
        if not restored:
            return None
        return {"slot": slot,
                "fields": [{"offset": off, "before": old, "after": new}
                           for off, old, new in changed]}
