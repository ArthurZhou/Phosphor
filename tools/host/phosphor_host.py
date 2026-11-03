#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phosphor 图形上位机

功能:
    串口自动识别、单帧与连续采集、屏幕波形读取、波形显示、统计信息、CSV 导出。

运行要求:
    Python 3.10 及以上，含 tkinter 与 pyserial。
    Windows 官方安装包默认包含 tkinter。

用法:
    python tools/host/phosphor_host.py
    python tools/host/phosphor_host.py --port COM13
    python tools/host/phosphor_host.py --simulate        # 离线仿真，无需硬件

协议说明见 docs/PROTOCOL.md，命令行工具见 phosphor_cli.py。
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
from collections import deque

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import phosphor_proto as proto
import phosphor_steps as steps


def app_dir() -> str:
    """配置文件的读写目录。

    源码运行时是脚本所在目录；Nuitka 打包后 __file__ 指向解包出来的临时
    目录（onefile）或只读的模块路径，挡位设置会写到临时目录里丢掉，因此
    打包形态一律以可执行文件所在目录为准。
    """
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))

# 显示样式
BG_ROOT = "#11161d"
BG_PANEL = "#161d26"
BG_PLOT = "#0b1017"
FG_TEXT = "#c8d3e0"
FG_DIM = "#7d8b9c"
GRID = "#1c2733"
TRACE = "#3ddc97"
TRACE_AUX = "#4d8df7"
WARN = "#e0a33d"
ERROR = "#e05561"

FONT_UI = ("Microsoft YaHei UI", 9)
FONT_MONO = ("Consolas", 9)

# 设备按键码见 proto.KEY_NAMES（命令 0x06 注入）。上/下与拨轮的作用取决于
# MODE 当前选中的项目，与在设备上操作等价，因此不需要知道设置结构各字段
# 的含义。1（关机）、13（自动校正）有副作用，注入前单独确认。

# 会造成不可逆副作用或需要前置条件的键码：注入前弹确认框
KEY_CONFIRM = {
    1: "将向设备注入「关机」键码，设备会立即关机。确定继续？",
    13: "这是设备上的「长按 AUTO」，触发自动校正，会改写内部的零点/增益\n"
        "校准值。说明书要求校准时拔掉 BNC 探头或将探头正负极短接，\n"
        "探头未断开就校正会得到错误的基线。确定继续？",
}


def _fmt(value: float, digits: int = 2) -> str:
    return ("%." + str(digits) + "f") % value


class PhosphorHost:
    """上位机主窗口。"""

    def __init__(self, root, simulate: bool = False, port: str | None = None,
                 autostart: bool = False):
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = root
        self.root.title("Phosphor 上位机")
        self.root.configure(bg=BG_ROOT)
        self.root.minsize(980, 620)

        self.link = None
        self.port_name = ""
        self.simulate = simulate
        self.parser = proto.FrameParser()
        self.frames: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.reader: threading.Thread | None = None

        self.last_frame: proto.Frame | None = None
        self.prev_frame: proto.Frame | None = None
        self.frame_count = 0
        self.lost_count = 0
        self.rx_bytes = 0
        self.auto_connect = port

        self._frame_times: deque[float] = deque(maxlen=120)
        self._last_seq: int | None = None
        self._streaming = False
        self._screen_live = False        # 屏幕帧由主机重复请求，无设备侧连续模式
        self.last_settings: bytes | None = None
        self._last_draw = 0.0

        # 档位面板：只有当存在可用布局时才启用下拉，否则等「识别档位」
        self.ctrl = None
        self._io_lock = threading.Lock()     # 串口写互斥（读全部走采集线程）
        self._ram_pending: list = []         # 等待 0x08 应答的 (长度, 事件, 结果)
        self.step_index: dict = {}           # 最近一次回读到的各角色档位索引
        self._cmd_pending: dict = {}         # 已下达未校正的直设 (目标, 时间)
        self._uiq: queue.Queue = queue.Queue()   # 后台线程 → Tk 线程的回调
        # 字段布局已由固件反汇编确认，不再需要运行期识别。
        self.layout = steps.builtin_layout()
        if simulate:
            self.layout["simulated"] = True

        # 挡位选择（主机侧记录当前设备挡位，用于真实单位换算，不下发设备）
        self.gear: dict = self._load_gear()

        self._build_style()
        self._build_ui()
        self._refresh_ports()
        self.root.after(40, self._poll)

        if simulate:
            self._open_link(None)
        elif port:
            self.port_var.set(port)
            self._open()

        if autostart:
            self.mode_var.set("stream")
            self.root.after(300, self._start)

    # ---------------------------------------------------------------- 界面
    def _build_style(self):
        style = self.ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(".", background=BG_ROOT, foreground=FG_TEXT, font=FONT_UI)
        style.configure("TFrame", background=BG_ROOT)
        style.configure("Panel.TFrame", background=BG_PANEL)
        style.configure("TLabel", background=BG_ROOT, foreground=FG_TEXT)
        style.configure("Dim.TLabel", background=BG_ROOT, foreground=FG_DIM)
        style.configure("Panel.TLabel", background=BG_PANEL, foreground=FG_TEXT)
        style.configure("Value.TLabel", background=BG_PANEL, foreground=TRACE,
                        font=FONT_MONO)
        style.configure("TButton", background="#22303f", foreground="#dce6f2",
                        borderwidth=0, padding=(10, 4))
        style.map("TButton",
                  background=[("active", "#2d3f52"), ("disabled", "#1a222c")],
                  foreground=[("disabled", "#5f6a77")])
        style.configure("Accent.TButton", background="#1d6b4f",
                        foreground="#eafff6", borderwidth=0, padding=(10, 4))
        style.map("Accent.TButton",
                  background=[("active", "#248a65"), ("disabled", "#1b2b26")],
                  foreground=[("disabled", "#576c63")])
        style.configure("TCheckbutton", background=BG_ROOT, foreground=FG_TEXT)
        style.map("TCheckbutton", background=[("active", BG_ROOT)])
        style.configure("TRadiobutton", background=BG_ROOT, foreground=FG_TEXT)
        style.map("TRadiobutton", background=[("active", BG_ROOT)])
        style.configure("TCombobox", fieldbackground="#1b242f",
                        background="#22303f", foreground="#dce6f2",
                        arrowcolor="#dce6f2", selectbackground="#1b242f",
                        selectforeground="#dce6f2", borderwidth=0)
        style.map("TCombobox",
                  fieldbackground=[("readonly", "#1b242f"), ("disabled", "#151c25")],
                  foreground=[("readonly", "#dce6f2"), ("disabled", "#5f6a77")],
                  selectbackground=[("readonly", "#1b242f")],
                  selectforeground=[("readonly", "#dce6f2")])
        self.root.option_add("*TCombobox*Listbox.background", "#1b242f")
        self.root.option_add("*TCombobox*Listbox.foreground", "#dce6f2")
        self.root.option_add("*TCombobox*Listbox.selectBackground", "#248a65")
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#eafff6")
        style.configure("TCheckbutton", background=BG_ROOT)
        style.configure("Status.TLabel", background="#0c1118", foreground=FG_DIM,
                        padding=(8, 4))

    def _build_ui(self):
        tk, ttk = self.tk, self.ttk

        bar = ttk.Frame(self.root, padding=(10, 8, 10, 4))
        bar.pack(side="top", fill="x")

        ttk.Label(bar, text="端口").pack(side="left")
        self.port_var = tk.StringVar()
        self.port_box = ttk.Combobox(bar, textvariable=self.port_var, width=32,
                                     state="readonly", font=FONT_MONO)
        self.port_box.pack(side="left", padx=(6, 4))
        ttk.Button(bar, text="刷新", command=self._refresh_ports).pack(side="left")
        self.btn_link = ttk.Button(bar, text="连接", command=self._toggle_link)
        self.btn_link.pack(side="left", padx=(6, 12))

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=6)

        # 默认用屏幕帧：它就是设备实际绘制的那一帧（已触发对齐、已按 V/div
        # 缩放），与屏幕一致；原始帧用于分析导出，需要主机侧触发对齐才不飘。
        self.mode_var = tk.StringVar(value="screen")
        ttk.Radiobutton(bar, text="屏幕", value="screen",
                        variable=self.mode_var).pack(side="left")
        ttk.Radiobutton(bar, text="单帧", value="single",
                        variable=self.mode_var).pack(side="left")
        ttk.Radiobutton(bar, text="连续", value="stream",
                        variable=self.mode_var).pack(side="left", padx=(2, 10))

        self.btn_start = ttk.Button(bar, text="采集", style="Accent.TButton",
                                    command=self._start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(bar, text="停止", command=self._stop)
        self.btn_stop.pack(side="left", padx=4)
        self.btn_screen = ttk.Button(bar, text="屏幕波形", command=self._screen)
        self.btn_screen.pack(side="left", padx=4)
        self.btn_csv = ttk.Button(bar, text="导出 CSV", command=self._export_csv)
        self.btn_csv.pack(side="left", padx=4)

        # 原始帧是未经垂直缩放的 ADC 码值，固定 0..255 会把小信号压成一条
        # 细线；默认按数据自适应，与设备 AUTO 后的观感一致。屏幕帧已是屏幕
        # 坐标，固定 0..255 更贴近设备。
        self.autoscale = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="自动缩放", variable=self.autoscale,
                        command=self._redraw).pack(side="right")

        self.align = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="触发对齐", variable=self.align,
                        command=self._redraw).pack(side="right", padx=(0, 10))

        # 遥控条。档位面板与键码按钮在同一块区域：底层都是命令 0x06 按键
        # 注入，改档、重绘与保存全部由设备自身逻辑完成，补丁不改写设置结构。
        # 没有布局时档位下拉显示「未识别」，点「识别档位」现场实测，
        # 不需要预先跑标定脚本。
        self.key_btns = []
        self.step_btns = []
        self.step_vars = {}
        self.step_combos = {}
        self._build_param_bar(tk, ttk)
        self._build_key_bar(tk, ttk)

        body = ttk.Frame(self.root, padding=(10, 0, 10, 0))
        body.pack(side="top", fill="both", expand=True)

        self.canvas = tk.Canvas(body, bg=BG_PLOT, highlightthickness=0)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda _e: self._redraw())

        panel = ttk.Frame(body, style="Panel.TFrame", width=232,
                          padding=(12, 10))
        panel.pack(side="right", fill="y", padx=(10, 0))
        panel.pack_propagate(False)

        self.info_labels: dict[str, object] = {}
        self._panel_section(panel, "帧信息", [
            ("序号", "seq"), ("点数", "count"), ("标志", "flags"), ("校验", "chk"),
        ])
        self._panel_section(panel, "幅度（8 位码值）", [
            ("最小值", "min"), ("最大值", "max"), ("峰峰值", "pp"),
            ("均值", "mean"), ("有效值", "rms"),
        ])
        self._panel_section(panel, "测量（电压）", [
            ("Vpp", "mvpp"), ("Vmax", "vmax"), ("Vmin", "vmin"),
            ("直流", "vdc"), ("Vrms", "vrms"),
        ])
        self._panel_section(panel, "测量（时间）", [
            ("频率", "freq"), ("周期", "per"), ("占空比", "duty"),
            ("时间窗", "twin"),
        ])
        self._panel_section(panel, "会话", [
            ("已收帧数", "frames"), ("序号缺失", "lost"), ("速率", "rate"),
            ("数据量", "bytes"),
        ])

        tk.Label(panel, text="说明", bg=BG_PANEL, fg=FG_DIM, anchor="w",
                 font=FONT_UI).pack(fill="x", pady=(14, 2))
        note = ("“屏幕”模式读取设备实际绘制的那一帧，已触发对齐并按\n"
                "V/div 缩放，与设备屏幕一致。\n"
                "“单帧 / 连续”为原始 ADC 码值，需触发对齐才不横向漂移。\n\n"
                "时基 / 垂直档位由固件命令 0x0D 直接设定，设备自己改档并\n"
                "保存；◀ ▶ 微调一档。采样率用设备 1kHz 校准方波实测，\n"
                "零点在探头短接时校准。")
        if self.layout.get("simulated"):
            note += "\n（当前为仿真设备，仅供离线演示）"
        tk.Label(panel, text=note,
                 bg=BG_PANEL, fg="#8b98a8", anchor="w", justify="left",
                 wraplength=204, font=("Microsoft YaHei UI", 8)).pack(fill="x")

        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(self.root, textvariable=self.status_var, style="Status.TLabel",
                  anchor="w").pack(side="bottom", fill="x")

        self._set_controls_enabled(False)

    # -------------------------------------------------------- 挡位与校准
    GEAR_PATH = os.path.join(app_dir(), "phosphor_gear.json")

    def _load_gear(self) -> dict:
        gear = {"vertical": None, "timebase": None, "probe": 0,
                "sample_rate": None, "zero_code": None, "mode_key": 5}
        try:
            with open(self.GEAR_PATH, encoding="utf-8") as fh:
                data = json.load(fh)
            for k in gear:
                if k in data:
                    gear[k] = data[k]
        except (OSError, ValueError):
            pass
        if not isinstance(gear.get("mode_key"), int):
            gear["mode_key"] = 5
        return gear

    def _save_gear(self):
        try:
            with open(self.GEAR_PATH, "w", encoding="utf-8") as fh:
                json.dump(self.gear, fh, ensure_ascii=False, indent=1)
        except OSError as exc:
            self._status("挡位保存失败：%s" % exc)

    def _build_param_bar(self, tk, ttk):
        """时基 / 垂直档位直选，以及采样率与零点校准。

        档位由固件命令 0x0D 直接设定（见 phosphor_steps._set_direct），
        不需要识别 MODE 选中项，也不再注入一串上/下按键。垂直档位换算
        常数来自固件数据表：每格 25 个码值，mV/格为 1-2-5 序列。
        """
        bar = ttk.Frame(self.root, padding=(10, 0, 10, 4))
        bar.pack(side="top", fill="x")
        ttk.Label(bar, text="档位").pack(side="left", padx=(0, 8))
        for role, label in (("timebase", "时基"), ("voltage", "垂直")):
            table = self._step_table(role)
            ttk.Label(bar, text=label, width=4).pack(side="left")
            var = tk.StringVar(value="未读取")
            combo = ttk.Combobox(bar, textvariable=var, width=9,
                                 state="readonly", values=table or ("未读取",))
            combo.pack(side="left", padx=(2, 2))
            combo.bind("<<ComboboxSelected>>",
                       lambda _e, ro=role: self._on_step_pick(ro))
            prev = ttk.Button(bar, text="◀", width=2,
                              command=lambda ro=role: self._nudge_step(ro, -1))
            prev.pack(side="left", padx=(2, 0))
            nxt = ttk.Button(bar, text="▶", width=2,
                             command=lambda ro=role: self._nudge_step(ro, +1))
            nxt.pack(side="left", padx=(2, 0))
            cur = tk.StringVar(value="未读取")
            tk.Label(bar, textvariable=cur, bg=BG_ROOT, fg=TRACE,
                     font=FONT_MONO, width=12, anchor="w").pack(side="left",
                                                               padx=(8, 14))
            self.step_vars[role] = cur
            self.step_combos[role] = combo
            self.step_btns += [combo, prev, nxt]

        # 探头倍率：×1 / ×10，直接切设备设置，并参与电压换算
        ttk.Label(bar, text="探头", width=4).pack(side="left")
        probe_table = self._step_table("probe")
        self.probe_var = tk.StringVar(value="未读取")
        probe_combo = ttk.Combobox(bar, textvariable=self.probe_var, width=6,
                                   state="readonly",
                                   values=probe_table or ("未读取",))
        probe_combo.pack(side="left", padx=(2, 2))
        probe_combo.bind("<<ComboboxSelected>>",
                         lambda _e: self._on_step_pick("probe"))
        probe_lbl = tk.StringVar(value="")
        tk.Label(bar, textvariable=probe_lbl, bg=BG_ROOT, fg=TRACE,
                 font=FONT_MONO, width=6, anchor="w").pack(side="left",
                                                            padx=(6, 14))
        self.step_vars["probe"] = probe_lbl
        self.step_combos["probe"] = probe_combo
        self.step_btns.append(probe_combo)

        self.cal_btns = [
            ttk.Button(bar, text="校准零点", command=self._cal_zero),
            ttk.Button(bar, text="校准采样率", command=self._cal_sample_rate),
        ]
        for btn in self.cal_btns:
            btn.pack(side="right", padx=(4, 0))
        self.gear_info_var = tk.StringVar(value="")
        tk.Label(bar, textvariable=self.gear_info_var, bg=BG_ROOT,
                 fg=FG_DIM, anchor="e", font=FONT_UI
                 ).pack(side="right", padx=(0, 10))
        self._update_gear_info()

    def _update_gear_info(self):
        rate = self.gear.get("sample_rate")
        zero = self.gear.get("zero_code")
        parts = []
        v = self.gear.get("vertical")
        t = self.gear.get("timebase")
        if isinstance(v, int) and 0 <= v < len(proto.VOLTAGE_STEPS):
            parts.append(proto.VOLTAGE_STEPS[v] + "/div")
        if isinstance(t, int) and 0 <= t < len(proto.TIMEBASE_STEPS):
            parts.append(proto.TIMEBASE_STEPS[t] + "/div")
        if self.gear.get("probe") == 1:
            parts.append("探头 ×10")
        parts.append("采样率 " + ("%.0f Sa/s" % rate if rate else "未校准"))
        parts.append("零点 " + ("%.1f" % zero if zero is not None else "128"))
        self.gear_info_var.set("  |  ".join(parts))

    def _cal_sample_rate(self):
        """用设备自带的 1kHz 校准方波实测 ADC 采样率。"""
        frame = self.last_frame
        if frame is None:
            self._status("还没有收到波形，请先采集一帧")
            return
        if frame.flags & proto.FLAG_SCREEN:
            self._status("屏幕帧已被抽取，请切到“单帧”采集原始数据再校准")
            return
        m = proto.measure_wave(frame.samples,
                               self.gear.get("zero_code") or 128.0)
        if not m["period"] or m["cycles"] < 2:
            self._status("帧内不足 2 个周期：请把探头接到校准方波，"
                         "时基调小让屏幕出现 3 个以上周期")
            return
        rate = proto.CAL_1KHZ * m["period"]
        self.gear["sample_rate"] = rate
        self._save_gear()
        self._update_gear_info()
        self._update_panel()
        self._status("采样率校准：%.0f Sa/s（按 %d 个周期平均）"
                     % (rate, m["cycles"]))

    def _cal_zero(self):
        """输入短接/接地时的均值即零点码值。"""
        frame = self.last_frame
        if frame is None:
            self._status("还没有收到波形")
            return
        if frame.flags & proto.FLAG_SCREEN:
            self._status("请切到“单帧”采集原始数据再校准零点")
            return
        m = proto.measure_wave(frame.samples, 128.0)
        self.gear["zero_code"] = round(m["mean"], 1)
        self._save_gear()
        self._update_gear_info()
        self._update_panel()
        self._status("零点校准：%.1f 码值（探头应短接或接地）"
                     % m["mean"])

    # ------------------------------------------------------------ 遥控条
    def _build_key_bar(self, tk, ttk):
        """常用实体键直接放出来，不常用的收进可折叠区域。

        上 / 下与 AUTO / MODE / RUN 是日常最常用的键；拨轮、确定、返回
        以及有副作用的「关机」「长按 AUTO 校正」放在「更多按键」里。
        所有按钮都只注入键码（命令 0x06），与在设备上按下等价。
        """
        container = ttk.Frame(self.root, padding=(10, 0, 10, 6))
        container.pack(side="top", fill="x")
        row = ttk.Frame(container)
        row.pack(fill="x")
        ttk.Label(row, text="按键").pack(side="left", padx=(0, 8))
        main = (("AUTO", 6), ("MODE", 5), ("RUN / 停止", 2),
                ("上", 3), ("下", 4))
        for text, code in main:
            btn = ttk.Button(row, text=text,
                             command=lambda c=code: self._inject(c))
            btn.pack(side="left", padx=(0, 4))
            self.key_btns.append(btn)

        self.more_visible = False
        self.btn_more = ttk.Button(row, text="更多按键 ▾",
                                   command=self._toggle_more)
        self.btn_more.pack(side="right")
        self.key_btns.append(self.btn_more)

        self.more_frame = ttk.Frame(container)
        more = (("拨轮 ◀", 9), ("拨轮 ▶", 7), ("确定", 8), ("返回", 14),
                ("详细信息", 11), ("触发边沿", 12),
                ("长按 AUTO（自动校正）", 13), ("关机", 1))
        for text, code in more:
            btn = ttk.Button(self.more_frame, text=text,
                             command=lambda c=code: self._inject(c))
            btn.pack(side="left", padx=(0, 4), pady=(0, 2))
            self.key_btns.append(btn)

    def _toggle_more(self):
        if self.more_visible:
            self.more_frame.pack_forget()
            self.btn_more.configure(text="更多按键 ▾")
        else:
            self.more_frame.pack(fill="x", pady=(4, 0))
            self.btn_more.configure(text="更多按键 ▴")
        self.more_visible = not self.more_visible

    def _step_table(self, role: str) -> tuple:
        if not self.layout:
            return ()
        key = steps.STEPS_KEY.get(role, "")
        return tuple(self.layout.get(key) or ())

    def _on_step_pick(self, role: str):
        combo = self.step_combos.get(role)
        if combo is None:
            return
        index = combo.current()
        if index < 0:                       # 只回填显示，不算一次选择
            return
        self._goto_step(role, index)

    def _nudge_step(self, role: str, delta: int):
        """微调一档：按面板上的当前值直接 ±1，不等回读。"""
        if self.ctrl is None or self.link is None:
            self._status("尚未连接设备")
            return
        table = self._step_table(role)
        if not table:
            self._status("档位布局未识别，无法调整")
            return
        cur = self.step_index.get(role)
        if cur is None:
            self._status("还没读到当前档位，稍候再试")
            self._refresh_steps()
            return
        target = cur + delta
        if not (0 <= target < len(table)):
            self._status("已在档位端点（索引 %d）" % cur)
            return
        self._send_param(role, target, table[target])

    def _goto_step(self, role: str, index: int):
        if self.ctrl is None or self.link is None:
            self._status("尚未连接设备")
            return
        table = self._step_table(role)
        if not (0 <= index < len(table)):
            return
        self._send_param(role, index, table[index])

    def _send_param(self, role: str, target: int, name: str):
        """命令 0x0D 直设：发完即返回，面板直接同步到目标值。

        R23+ 固件在 USB 轮询里直接改字段、切前端衰减并推 UI 事件，设备侧
        毫秒级完成 —— 主机再轮询确认只是白等，还会打断采集。

        回读只用于校正：0x08 的应答在连续流期间会被固件憋到停流才发出
        （实机确认），所以不等回读刷新，先按目标值乐观更新；回读回来后
        不一致再纠偏并提示。
        """
        code = steps.PARAM_CODE.get(role)
        if code is None:
            self._status("未知档位角色：%s" % role)
            return
        try:
            self._wr(bytes([proto.CMD_SET_PARAM, code, target]))
        except Exception as exc:
            self._status("发送失败：%s" % exc)
            return
        self._cmd_pending[role] = (target, time.time())
        self._apply_optimistic(role, target)
        self._status("已下达 %s → %s" % (steps.ROLE_NAMES.get(role, role), name))
        self.root.after(500, self._refresh_steps)

    def _apply_optimistic(self, role: str, idx: int):
        """直设后立即把面板、档位索引与主机侧换算同步到目标值。"""
        self.step_index[role] = idx
        table = self._step_table(role)
        var = self.step_vars.get(role)
        if var is not None:
            if table and 0 <= idx < len(table):
                var.set("%s  (%d)" % (table[idx], idx))
                combo = self.step_combos.get(role)
                if combo is not None:
                    combo.set(table[idx])     # set 不触发选择事件
            else:
                var.set("索引 %d" % idx)
        # 注意 gear 的键是 vertical（测量换算沿用的老名字），不是 voltage
        key = {"voltage": "vertical", "probe": "probe"}.get(role, "timebase")
        if self.gear.get(key) != idx:
            self.gear[key] = idx
            self._save_gear()
            self._update_gear_info()
        self._update_panel()

    def _wr(self, data: bytes):
        """串口写互斥。读方向全部由采集线程处理，写可能来自 UI 线程。"""
        with self._io_lock:
            self.link.write(data)
            self.link.flush()

    def _refresh_steps(self, force: bool = False):
        """后台回读当前档位并同步到面板与换算表（不打断采集）。"""
        if self.ctrl is None or self.link is None:
            return

        def run():
            st = None
            try:
                st = self.ctrl.read()
            except Exception:
                pass
            self._ui(self._apply_steps, st)

        threading.Thread(target=run, daemon=True).start()

    def _apply_steps(self, st):
        """把回读到的设置结构反映到面板，并同步主机侧换算用的档位。"""
        if st is None:
            # 流模式下 0x08 应答会被憋到停流，超时是常态；直设已乐观更新
            # 过面板，这里静默跳过，等下一次回读成功再校正。
            return
        changed = False
        for role, var in self.step_vars.items():
            e = (self.layout or {}).get("roles", {}).get(role)
            if not e:
                continue
            table = self._step_table(role)
            idx = st[e["offset"]]
            self.step_index[role] = idx
            if table and 0 <= idx < len(table):
                var.set("%s  (%d)" % (table[idx], idx))
                combo = self.step_combos.get(role)
                if combo is not None:
                    combo.set(table[idx])     # set 不触发选择事件
            else:
                var.set("索引 %d" % idx)
            # 直设后的校正：字段没走到目标说明固件没吃下 0x0D。只查 5 秒
            # 内的命令 —— 流模式下回读会被憋到停流，陈旧记录不算数。
            want = self._cmd_pending.pop(role, None)
            if want is not None and time.time() - want[1] < 5.0 \
                    and idx != want[0]:
                self._status("设备停在索引 %d，没有应用 %s=%d —— "
                             "需要 R23 以上固件支持命令 0x0D"
                             % (idx, steps.ROLE_NAMES.get(role, role), want[0]))
            # 注意 gear 的键是 vertical（测量换算沿用的老名字），不是 voltage
            key = {"voltage": "vertical", "probe": "probe"}.get(role, "timebase")
            if self.gear.get(key) != idx:
                self.gear[key] = idx
                changed = True
        if changed:
            self._save_gear()
            self._update_gear_info()
        self._update_panel()

    @staticmethod
    def _gear_table(key: str) -> tuple:
        return proto.VOLTAGE_STEPS if key == "vertical" else proto.TIMEBASE_STEPS

    def _panel_section(self, parent, title, rows):
        tk = self.tk
        tk.Label(parent, text=title, bg=BG_PANEL, fg=FG_DIM, anchor="w",
                 font=FONT_UI).pack(fill="x", pady=(8, 2))
        grid = tk.Frame(parent, bg=BG_PANEL)
        grid.pack(fill="x")
        for name, key in rows:
            tk.Label(grid, text=name, bg=BG_PANEL, fg=FG_DIM, anchor="w",
                     font=FONT_UI).grid(row=len(self.info_labels), column=0,
                                        sticky="w", pady=2)
            value = tk.Label(grid, text="-", bg=BG_PANEL, fg=TRACE, anchor="e",
                             font=FONT_MONO)
            value.grid(row=len(self.info_labels), column=1, sticky="e", pady=2)
            grid.columnconfigure(0, weight=1)
            self.info_labels[key] = value

    def _set_controls_enabled(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        for btn in (self.btn_start, self.btn_stop, self.btn_screen, self.btn_csv,
                    *getattr(self, "cal_btns", ()), *self.key_btns,
                    *self.step_btns):
            btn.configure(state=state)
        for combo in self.step_combos.values():
            combo.configure(state="readonly" if enabled else "disabled")

    # ------------------------------------------------------------ 后台交互
    def _ui(self, fn, *args):
        """后台线程更新界面：一律投递到 Tk 线程执行。"""
        self._uiq.put((fn, args))

    def _drain_ui(self):
        while True:
            try:
                fn, args = self._uiq.get_nowait()
            except queue.Empty:
                return
            try:
                fn(*args)
            except Exception:
                pass

    # ---------------------------------------------------------------- 遥控
    def _inject(self, code: int, name: str = ""):
        """把键码塞进设备的按键队列（命令 0x06）。

        等同于按下对应按键：改档位、重绘与保存全部由设备自身逻辑完成。
        危险键码（关机、长按 AUTO 校正）先弹确认框。
        """
        if self.link is None:
            self._status("尚未连接设备")
            return
        tip = KEY_CONFIRM.get(code)
        if tip:
            from tkinter import messagebox
            if not messagebox.askyesno("确认", tip):
                self._status("已取消")
                return
        try:
            self.link.write(bytes([proto.CMD_KEY, code]))
        except Exception as exc:
            self._status("注入失败：%s" % exc)
            return
        self._status("已注入 %s（键码 %d），设备将在下一轮主循环分派"
                     % (name or proto.KEY_NAMES.get(code, "键码 %d" % code),
                        code))
        # 主循环约 200ms 消化一个键码，稍后再回读档位
        self.root.after(int(steps.StepController.KEY_GAP * 1000 + 400),
                        self._refresh_steps)


    # -------------------------------------------------------------- 连接管理
    def _refresh_ports(self):
        ports = proto.list_ports()
        values = [p.label() for p in ports]
        self.port_box["values"] = values
        self._ports = ports
        if values and not self.port_var.get():
            target = next((p for p in ports if p.is_target), None)
            if target:
                self.port_var.set(target.label())
        self._status("发现 %d 个串口%s" % (
            len(ports), "" if any(p.is_target for p in ports) else "，未发现目标设备"))

    def _selected_port(self) -> str | None:
        label = self.port_var.get()
        if not label:
            return None
        return label.split()[0]

    def _toggle_link(self):
        if self.link is None:
            self._open()
        else:
            self._close()

    def _open(self):
        if self.simulate:
            self._open_link(None)
            return
        port = self._selected_port()
        if not port:
            self._status("请先选择端口")
            return
        try:
            self._open_link(port)
        except Exception as exc:
            self._status("打开 %s 失败：%s" % (port, exc))

    def _open_link(self, port: str | None):
        self._close()
        if port is None:
            self.link = proto.SimulatedSerial()
            self.port_name = "仿真设备"
            self.port_var.set("仿真设备（离线）")
        else:
            self.link = proto.open_port(port)
            self.port_name = port

        self.parser = proto.FrameParser()
        # 控制器始终建立：即使没有布局，「读设置」也需要它（只需要基址）。
        # 布局缺失时 roles 为空，档位下拉保持禁用，等「识别档位」填。
        ctrl_layout = self.layout or {
            "base": proto.STRUCT_ADDR,
            "slot_addr": proto.SLOT_ADDR,
            "cycle": 9,
            "roles": {},
        }
        ctrl_layout.setdefault("mode_key", int(self.gear.get("mode_key") or 5))
        self.ctrl = steps.StepController(lambda: self.link, ctrl_layout,
                                         ram_read=self._ram_read)
        self.stop_event.clear()
        self.reader = threading.Thread(target=self._reader_loop, daemon=True)
        self.reader.start()

        self.btn_link.configure(text="断开")
        self._set_controls_enabled(True)
        self._status("已连接 %s" % self.port_name)
        self.root.after(200, self._refresh_steps)

    def _close(self):
        self.stop_event.set()
        if self.reader is not None and self.reader.is_alive():
            self.reader.join(timeout=0.5)
        self.reader = None
        if self.link is not None:
            try:
                if self._streaming:
                    self.link.write(bytes([proto.CMD_STOP]))
                    time.sleep(0.05)
                self.link.close()
            except Exception:
                pass
        self.link = None
        self.ctrl = None
        self._streaming = False
        self._screen_live = False
        self._ram_pending.clear()        # 未应答的回读等待者自行超时返回
        self._cmd_pending.clear()
        if hasattr(self, "btn_link"):
            self.btn_link.configure(text="连接")
            self._set_controls_enabled(False)

    def _reader_loop(self):
        while not self.stop_event.is_set():
            if self.link is None:
                time.sleep(0.02)
                continue
            try:
                chunk = self.link.read(8192)
            except Exception as exc:
                self._status("读取中断：%s" % exc)
                return
            if not chunk:
                continue
            self.rx_bytes += len(chunk)
            for frame in self.parser.feed(chunk):
                if self._serve_ram(frame):
                    continue
                self.frames.put(frame)

    def _serve_ram(self, frame) -> bool:
        """把 0x08 应答交给等待者，不进波形队列。

        按长度匹配：回读是设置窗口 128 字节或单字段 1 字节，与波形帧
        （301 / 1000 点）不重叠，等待窗口又只有几百毫秒，误配不成立。
        """
        if not self._ram_pending:
            return False
        for entry in list(self._ram_pending):
            n, ev, holder = entry
            if frame.count == n and not ev.is_set():
                holder[0] = bytes(frame.samples)
                ev.set()
                return True
        return False

    def _ram_read(self, addr: int, n: int, timeout: float = 0.6):
        """命令 0x08 的读回走采集线程：不独占串口，采集不断流。

        StepController 通过 ram_read 钩子调用这里；写入只占一把写锁，
        应答由 _reader_loop 解析后按长度分发给等待者。超时返回 None。
        """
        link = self.link
        if link is None:
            return None
        ev = threading.Event()
        holder: list = [None]
        entry = (n, ev, holder)
        self._ram_pending.append(entry)
        try:
            self._wr(bytes([proto.CMD_RAMREAD])
                     + int(addr).to_bytes(4, "little") + bytes([n]))
        except Exception:
            if entry in self._ram_pending:
                self._ram_pending.remove(entry)
            return None
        ok = ev.wait(timeout)
        if entry in self._ram_pending:
            self._ram_pending.remove(entry)
        return bytes(holder[0]) if ok and holder[0] is not None else None

    # -------------------------------------------------------------- 采集控制
    def _start(self):
        if self.link is None:
            self._status("尚未连接设备")
            return
        mode = self.mode_var.get()
        if mode == "stream":
            self._screen_live = False
            self.link.write(bytes([proto.CMD_STREAM]))
            self._streaming = True
            self._status("连续采集已启动（原始帧）")
        elif mode == "screen":
            self._screen_live = True
            self._request_screen()
            self._status("连续采集已启动（屏幕帧，与设备屏幕一致）")
        else:
            self._screen_live = False
            self.link.write(bytes([proto.CMD_SINGLE]))
            self._status("已请求一帧原始数据")

    def _request_screen(self):
        """屏幕帧没有设备侧连续模式，由主机按固定间隔重复请求。

        屏幕帧 301 点共 309 字节，约 5 个 64 字节包，120ms 足够一帧发完。
        """
        if not self._screen_live or self.link is None:
            return
        try:
            self.link.write(bytes([proto.CMD_SCREEN]))
        except Exception:
            self._screen_live = False

    def _stop(self):
        if self.link is None:
            return
        self.link.write(bytes([proto.CMD_STOP]))
        self._streaming = False
        self._screen_live = False
        self._status("已停止连续采集")

    def _screen(self):
        if self.link is None:
            self._status("尚未连接设备")
            return
        if self._streaming:
            self.link.write(bytes([proto.CMD_STOP]))
            self._streaming = False
        self.link.write(bytes([proto.CMD_SCREEN]))
        self._status("已请求屏幕波形数据")

    def _export_csv(self):
        if self.last_frame is None:
            self._status("暂无可导出的数据")
            return
        from tkinter import filedialog
        path = filedialog.asksaveasfilename(
            title="导出样本",
            defaultextension=".csv",
            filetypes=[("CSV 文件", "*.csv"), ("全部文件", "*.*")],
            initialfile="phosphor_seq%d.csv" % self.last_frame.seq)
        if not path:
            return
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write("index,code\n")
            for i, v in enumerate(self.last_frame.samples):
                fh.write("%d,%d\n" % (i, v))
        self._status("已导出 %d 点至 %s" % (self.last_frame.count, path))

    # -------------------------------------------------------------- 周期刷新
    def _poll(self):
        self._drain_ui()
        limit = 200
        new_frames = 0
        while new_frames < limit:
            try:
                frame = self.frames.get_nowait()
            except queue.Empty:
                break
            if frame.flags & proto.FLAG_SETTINGS:
                continue                        # 设置快照不参与波形绘制
                continue
            self.prev_frame = self.last_frame
            self.last_frame = frame
            self.frame_count += 1
            new_frames += 1
            self._frame_times.append(time.time())
            if self._last_seq is not None:
                gap = (frame.seq - self._last_seq) & 0xFFFF
                if gap > 1:
                    self.lost_count += gap - 1
            self._last_seq = frame.seq

        if new_frames:
            self._update_panel()
            now = time.time()
            if now - self._last_draw >= 0.05:
                self._last_draw = now
                self._redraw()
            if self._screen_live:
                self.root.after(120, self._request_screen)
            self._status("接收中：%s  序号 %d  共 %d 帧" % (
                self.port_name, self.last_frame.seq, self.frame_count))

        self.root.after(40, self._poll)

    def _update_panel(self):
        frame = self.last_frame
        if frame is None:
            return
        st = proto.SignalStats.from_samples(frame.samples)
        self._set("seq", str(frame.seq))
        self._set("count", str(frame.count))
        self._set("flags", "0x%02X" % frame.flags)
        self._set("chk", frame.checksum_text,
                  TRACE if frame.checksum_ok else WARN)
        self._set("min", str(st.minimum))
        self._set("max", str(st.maximum))
        self._set("pp", str(st.peak_to_peak))
        self._set("mean", _fmt(st.mean))
        self._set("rms", _fmt(st.rms))
        self._set("frames", str(self.frame_count))
        self._set("lost", str(self.lost_count),
                  WARN if self.lost_count else TRACE)
        if len(self._frame_times) >= 2:
            span = self._frame_times[-1] - self._frame_times[0]
            fps = (len(self._frame_times) - 1) / span if span > 0 else 0.0
            self._set("rate", "%.1f 帧/秒" % fps)
        else:
            self._set("rate", "-")
        self._set("bytes", "%.1f KB" % (self.rx_bytes / 1024.0))

        # ---- 真实单位测量（依赖挡位选择与校准） ----
        m = proto.measure_wave(frame.samples,
                               self.gear.get("zero_code") or 128.0)
        vpc = proto.volts_per_code(self.gear.get("vertical"))
        if vpc is not None and self.gear.get("probe") == 1:
            vpc *= 10.0                    # ×10 探头：每格电压放大十倍
        z = self.gear.get("zero_code")
        if vpc is not None:
            z = 128.0 if z is None else z
            self._set("mvpp", proto.fmt_volts(st.peak_to_peak * vpc))
            self._set("vmax", proto.fmt_volts((st.maximum - z) * vpc))
            self._set("vmin", proto.fmt_volts((st.minimum - z) * vpc))
            self._set("vdc", proto.fmt_volts((st.mean - z) * vpc))
            self._set("vrms", proto.fmt_volts(st.rms * vpc))
        else:
            for k in ("mvpp", "vmax", "vmin", "vdc", "vrms"):
                self._set(k, "-")
        rate = self.gear.get("sample_rate")
        if m["period"] and rate:
            freq = rate / m["period"]
            self._set("freq", self._fmt_freq(freq))
            self._set("per", proto.fmt_seconds(1.0 / freq))
            self._set("twin", proto.fmt_seconds(len(frame.samples) / rate))
        elif m["period"]:
            self._set("freq", "需校准采样率", WARN)
            self._set("per", "%.1f 样本" % m["period"])
            self._set("twin", "-")
        else:
            self._set("freq", "-")
            self._set("per", "-")
            self._set("twin", "-")
        if m["duty"] is not None:
            self._set("duty", "%.1f %%" % (m["duty"] * 100))
        else:
            self._set("duty", "-")

    @staticmethod
    def _fmt_freq(f: float) -> str:
        if f >= 1e6:
            return "%.4f MHz" % (f / 1e6)
        if f >= 1e3:
            return "%.3f kHz" % (f / 1e3)
        return "%.2f Hz" % f

    def _set(self, key: str, text: str, color: str = TRACE):
        label = self.info_labels.get(key)
        if label is not None:
            label.configure(text=text, fg=color)

    def _status(self, text: str):
        self.status_var.set(text)

    # ------------------------------------------------------------------ 绘图
    def _redraw(self):
        c = self.canvas
        w = c.winfo_width()
        h = c.winfo_height()
        if w < 60 or h < 60:
            return
        c.delete("all")

        left, right, top, bottom = 52, 14, 24, 30
        plot_w = w - left - right
        plot_h = h - top - bottom
        if plot_w <= 10 or plot_h <= 10:
            return

        # 网格
        for i in range(9):
            x = left + plot_w * i / 8
            c.create_line(x, top, x, top + plot_h, fill=GRID)
        for i in range(5):
            y = top + plot_h * i / 4
            c.create_line(left, y, left + plot_w, y, fill=GRID)

        frame = self.last_frame
        if frame is None:
            c.create_text(left + plot_w / 2, top + plot_h / 2,
                          text="等待数据", fill=FG_DIM, font=FONT_UI)
            self._draw_axes(left, top, plot_w, plot_h, 0, 0, 255)
            return

        samples = frame.samples
        # 屏幕帧由设备触发对齐并按 V/div 缩放后写入，直接使用；原始帧是环形
        # 缓冲的固定窗口，信号随时间在其中平移，需主机侧触发对齐才不横向漂移。
        if self.align.get() and not frame.is_screen:
            samples = proto.align_trigger(samples)
        n = len(samples)
        if self.autoscale.get():
            lo = min(samples)
            hi = max(samples)
            span = max(1, hi - lo)
            if span < 16:
                mid = (lo + hi) // 2         # 近乎平直：不要把噪声放大到满屏
                lo, hi = mid - 8, mid + 8
            else:
                margin = max(2, int(span * 0.12))
                lo -= margin
                hi += margin
            lo = max(0, lo)
            hi = min(255, hi)
            if hi - lo < 8:
                hi = min(255, lo + 8)
        else:
            lo, hi = 0, 255

        def y_of(v: int) -> float:
            return top + plot_h - (v - lo) * plot_h / float(hi - lo)

        step = n / float(plot_w)
        points = []
        for px in range(int(plot_w)):
            i0 = int(px * step)
            i1 = min(n, max(i0 + 1, int((px + 1) * step)))
            seg = samples[i0:i1]
            if not seg:
                continue
            x = left + px
            points.append((x, y_of(max(seg))))
            points.append((x, y_of(min(seg))))

        if len(points) >= 2:
            flat = [coord for pt in points for coord in pt]
            c.create_line(*flat, fill=TRACE, width=1)

        self._draw_axes(left, top, plot_w, plot_h, n, lo, hi)
        c.create_text(left + 2, top - 8, anchor="w",
                      text="序号 %d   点数 %d" % (frame.seq, n),
                      fill=FG_DIM, font=FONT_MONO)

    def _draw_axes(self, left, top, plot_w, plot_h, n, lo, hi):
        c = self.canvas
        c.create_text(left - 10, top, anchor="e", text=str(hi),
                      fill=FG_DIM, font=FONT_MONO)
        c.create_text(left - 10, top + plot_h, anchor="e", text=str(lo),
                      fill=FG_DIM, font=FONT_MONO)
        c.create_text(left - 10, top + plot_h / 2, anchor="e",
                      text=str((lo + hi) // 2), fill=FG_DIM, font=FONT_MONO)
        c.create_text(left, top + plot_h + 12, anchor="w", text="0",
                      fill=FG_DIM, font=FONT_MONO)
        if n:
            c.create_text(left + plot_w, top + plot_h + 12, anchor="e",
                          text=str(n - 1), fill=FG_DIM, font=FONT_MONO)
            c.create_text(left + plot_w / 2, top + plot_h + 12,
                          text="样本序号", fill=FG_DIM, font=FONT_UI)


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Phosphor 图形上位机")
    ap.add_argument("-p", "--port", help="串口名，缺省自动查找 2E3C:5740")
    ap.add_argument("--simulate", action="store_true", help="使用内置仿真设备")
    ap.add_argument("--autostart", action="store_true", help="启动后立即开始连续采集")
    args = ap.parse_args()

    try:
        import tkinter as tk
    except ImportError:
        print("当前 Python 缺少 tkinter，无法启动图形界面。")
        print("请使用带 tkinter 的 Python（Windows 官方安装包默认包含），"
              "或改用命令行工具 phosphor_cli.py。")
        return 2

    root = tk.Tk()
    try:
        root.tk.call("tk", "scaling", 1.25)
    except Exception:
        pass
    PhosphorHost(root, simulate=args.simulate, port=args.port,
                 autostart=args.autostart)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
