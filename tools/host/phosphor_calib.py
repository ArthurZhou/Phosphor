#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phosphor 设置结构标定工具

目的:
    把设备设置结构 0x2000A528 里「时基索引」「垂直档位索引」等字段的字节
    偏移测出来，之后上位机就能显示当前档位名并直接选档，不必再模拟拨轮。

原理:
    按键由设备自身逻辑处理，改档必然反映到设置结构。因此逐个槽位（MODE
    选中项）先连按「上」到顶、再连按「下」到底，每按一次读一次设置快照：
    观察哪个字节随按键单调步进。先到顶再到底可覆盖全部档位，于是「不同
    取值个数」等于档位数——24 即时间基准，13 即垂直档位。再比较各槽位的
    起始快照，出现重复即说明 MODE 已循环回起点。

用法:
    python tools/host/phosphor_calib.py -p COM13
    python tools/host/phosphor_calib.py -p COM13 --steps 26 --slots 6
    python tools/host/phosphor_calib.py --simulate            # 离线自测
    python tools/host/phosphor_calib.py -p COM13 --out calib.json

注意:
    - 标定会把档位推到两端，结束后自动注入 AUTO 恢复；
    - 全程约 2 分钟，期间不要碰设备；
    - 设备须停在主界面（打开菜单时按键被菜单接管）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import phosphor_proto as proto

KEY_UP, KEY_DOWN, KEY_MODE, KEY_AUTO = 3, 4, 5, 6
KEY_WHEEL_R, KEY_WHEEL_L = 7, 9


# --------------------------------------------------------------- 收发原语
def read_settings(link, parser, timeout: float = 1.2) -> bytes | None:
    """请求一次设置结构快照（命令 0x07）。"""
    link.write(bytes([proto.CMD_SETTINGS]))
    link.flush()
    deadline = time.time() + timeout
    while time.time() < deadline:
        chunk = link.read(4096)
        if not chunk:
            continue
        for frame in parser.feed(chunk):
            if len(frame.samples) == proto.SETTINGS_LEN:
                return bytes(frame.samples)
    return None


def inject(link, code: int) -> None:
    link.write(bytes([proto.CMD_KEY, code]))
    link.flush()


def press(link, parser, code: int, delay: float) -> bytes | None:
    """按一次键，等设备处理完，返回新的设置快照。"""
    inject(link, code)
    time.sleep(delay)
    return read_settings(link, parser)


def sweep(link, parser, code: int, count: int, delay: float,
          start: bytes) -> list[bytes] | None:
    """连按同一键 count 次，返回每一步之后的设置快照（含起始快照）。"""
    series = [start]
    for i in range(count):
        cur = press(link, parser, code, delay)
        if cur is None:
            return None
        series.append(cur)
        print(".", end="", flush=True)
    return series


# --------------------------------------------------------------- 分析
def is_monotonic(vals: list[int], increasing: bool) -> bool:
    for i in range(len(vals) - 1):
        if increasing and vals[i + 1] < vals[i]:
            return False
        if not increasing and vals[i + 1] > vals[i]:
            return False
    return True


def analyze_field(up: list[bytes], down: list[bytes]) -> list[dict]:
    """找出随按键单调步进的字节偏移。"""
    found = []
    for off in range(proto.SETTINGS_LEN):
        uv = [s[off] for s in up]
        dv = [s[off] for s in down]
        merged = uv + dv
        distinct = len(set(merged))
        if distinct < 3:
            continue
        if not (is_monotonic(uv, True) and is_monotonic(dv, False)):
            continue
        # 有效步数：非零变化次数
        moves = sum(1 for i in range(len(uv) - 1) if uv[i + 1] != uv[i])
        moves += sum(1 for i in range(len(dv) - 1) if dv[i + 1] != dv[i])
        if moves < 3:
            continue
        peak = max(uv)
        floor = min(dv)
        found.append({
            "offset": off,
            "addr": proto.SETTINGS_ADDR + off,
            "distinct": distinct,
            "min": floor,
            "max": peak,
            "moves": moves,
            "top_saturated": len(set(uv[-3:])) == 1,
            "bottom_saturated": len(set(dv[-3:])) == 1,
        })
    # 变化最明显的排前面
    found.sort(key=lambda f: (-f["distinct"], f["offset"]))
    return found


def guess_role(f: dict) -> str:
    """按不同取值个数猜字段角色。"""
    d = f["distinct"]
    sat = f["top_saturated"] and f["bottom_saturated"]
    if d >= 20 and sat:
        return "时间基准索引（24 档 → distinct 应为 24）"
    if 11 <= d <= 16 and sat:
        return "垂直档位索引（13 档 → distinct 应为 13）"
    if d <= 4 and sat:
        return "枚举项（触发模式 / 耦合 / 探头倍率）"
    if not sat:
        return "连续量（触发电平 / 触发位置），未触及边界"
    return "待定"


def changed_bytes(a: bytes, b: bytes) -> list[int]:
    return [i for i in range(min(len(a), len(b))) if a[i] != b[i]]


def detect_slot_field(link, parser, cycle: int, delay: float) -> dict:
    """测「当前选中槽位」字段：连按 MODE，找按 0,1,2,… 循环的字节。"""
    delay = max(0.45, delay)
    seq = [read_settings(link, parser)]
    for _ in range(cycle + 1):
        cur = press(link, parser, KEY_MODE, delay)
        if cur is None:
            return {"offset": None, "values": [], "reason": "读设置超时"}
        seq.append(cur)
    seq = [s for s in seq if s is not None]
    cands = []
    for off in range(proto.SETTINGS_LEN):
        vals = [s[off] for s in seq]
        if len(set(vals)) != cycle:
            continue
        # 相邻两步必须是 (v+1) mod cycle
        ok = all(vals[i + 1] == (vals[i] + 1) % cycle
                 for i in range(len(vals) - 1))
        if ok:
            cands.append({"offset": off, "addr": proto.SETTINGS_ADDR + off,
                          "values": vals})
    best = cands[0] if cands else {"offset": None, "values": [],
                                   "reason": "未找到循环字段"}
    best["candidates"] = cands
    return best


def build_layout(result: dict, slot_info: dict) -> dict:
    """把标定结果整理成上位机直接可用的字段布局。"""
    cycle = result["cycle_len"]
    roles = {}
    unknown = []
    for i, slot in enumerate(result["slots"][:cycle]):
        if not slot["fields"]:
            continue
        f = slot["fields"][0]                 # 取该槽位跨度最大的字段
        role = None
        if f["distinct"] >= 20 and f["top_saturated"] and f["bottom_saturated"]:
            role = "timebase"
        elif 11 <= f["distinct"] <= 16 and f["top_saturated"] \
                and f["bottom_saturated"]:
            role = "voltage"
        elif f["distinct"] <= 4 and f["top_saturated"] and f["bottom_saturated"]:
            role = "enum"
        entry = {"offset": f["offset"], "slot": i,
                 "distinct": f["distinct"], "min": f["min"], "max": f["max"]}
        if role:
            roles[role] = entry
        else:
            entry["role"] = "continuous"
            unknown.append(entry)
    return {
        "cycle": cycle,
        "slot_field": slot_info.get("offset"),
        "roles": roles,
        "others": unknown,
        "voltage_steps": result["voltage_steps"],
        "timebase_steps": result["timebase_steps"],
    }


# --------------------------------------------------------------- 报告
def render_report(result: dict) -> str:
    slots = result["slots"]
    cycle = result["cycle_len"]
    L = []
    L.append("设置结构标定报告")
    L.append("=" * 70)
    L.append("设置结构基址 0x%08X，快照 %d 字节"
             % (proto.SETTINGS_ADDR, proto.SETTINGS_LEN))
    L.append("MODE 循环长度：%d 个槽位" % cycle)
    L.append("")

    for i, slot in enumerate(slots[:cycle]):
        L.append("── 槽位 %d ──" % i)
        if not slot["fields"]:
            L.append("  未发现单调步进字段：该槽位可能不是档位选择项")
        for f in slot["fields"]:
            L.append("  +%02X (0x%08X)  范围 %d..%d  不同取值 %d  步数 %d%s"
                     % (f["offset"], f["addr"], f["min"], f["max"],
                        f["distinct"], f["moves"],
                        "（触顶触底）" if f["top_saturated"]
                        and f["bottom_saturated"] else ""))
            L.append("      → %s" % guess_role(f))
        others = [o for o in slot["changed"]
                  if o not in {f["offset"] for f in slot["fields"]}]
        if others:
            L.append("  其他变动字节: %s"
                     % " ".join("+%02X" % o for o in others))
        L.append("")

    L.append("── 对照档位表 ──")
    L.append("时基 24 档: %s" % " ".join(proto.TIMEBASE_STEPS))
    L.append("垂直 13 档: %s" % " ".join(proto.VOLTAGE_STEPS))
    L.append("")
    L.append("判定：某字段从 0 逐档走到 23 且触顶 → 时基索引；")
    L.append("      从 0 走到 12 → 垂直档位索引。")
    return "\n".join(L)


# --------------------------------------------------------------- 主流程
def main() -> int:
    ap = argparse.ArgumentParser(
        description="标定设备设置结构：时基/垂直档位的字段偏移与 MODE 循环")
    ap.add_argument("-p", "--port", help="串口名，缺省自动查找 2E3C:5740")
    ap.add_argument("--slots", type=int, default=6, help="最多测试几个 MODE 槽位")
    ap.add_argument("--steps", type=int, default=26,
                    help="每个方向连按次数，默认 26（多于最长档位表 24）")
    ap.add_argument("--delay", type=float, default=0.35,
                    help="每次按键后的等待秒数，默认 0.35")
    ap.add_argument("--simulate", action="store_true", help="使用内置仿真设备")
    ap.add_argument("--out", default="tools/host/calib_result.json",
                    help="原始数据输出路径")
    ap.add_argument("--layout", default="tools/host/phosphor_layout.json",
                    help="字段布局输出路径（上位机据此显示档位面板）")
    ap.add_argument("--no-restore", action="store_true",
                    help="结束时不注入 AUTO 恢复档位")
    args = ap.parse_args()

    if args.simulate:
        link = proto.SimulatedSerial()
        port = "仿真设备"
        args.delay = 0.0
    else:
        port = args.port or proto.find_port()
        if not port:
            print("未找到设备（2E3C:5740）。可用 --list 查看串口。")
            return 2
        link = proto.open_port(port)
    parser = proto.FrameParser()
    print("端口: %s" % port)
    per_slot = args.steps * 2
    print("每槽位上下各扫 %d 次，最多 %d 槽位，预计 %.0f 秒"
          % (args.steps, args.slots,
             args.slots * per_slot * (args.delay + 0.35)))
    print("请让设备停在主界面，标定期间不要操作设备。\n")

    base = read_settings(link, parser)
    if base is None:
        print("读不到设置结构。请确认固件为 R4 及以上（支持命令 0x07）。")
        link.close()
        return 1
    print("基线设置: %s ..."
          % " ".join("%02X" % b for b in base[:16]))

    slots = []
    cycle = args.slots
    for i in range(args.slots):
        print("\n[槽位 %d] 向上扫 " % i, end="", flush=True)
        up = sweep(link, parser, KEY_UP, args.steps, args.delay, base)
        if up is None:
            print("\n  读设置超时，中止。")
            link.close()
            return 1
        print(" 向下扫 ", end="", flush=True)
        down = sweep(link, parser, KEY_DOWN, args.steps, args.delay, up[-1])
        if down is None:
            print("\n  读设置超时，中止。")
            link.close()
            return 1
        slot = {
            "base": list(base),
            "fields": analyze_field(up, down),
            "changed": changed_bytes(base, down[-1]),
        }
        slots.append(slot)
        print("  步进字段 %d 个" % len(slot["fields"]))

        # 主字段（跨度最大）与前面某槽位相同 → MODE 已绕回起点
        if slot["fields"]:
            main_off = slot["fields"][0]["offset"]
            for j in range(i):
                if slots[j]["fields"] and \
                        slots[j]["fields"][0]["offset"] == main_off:
                    cycle = i
                    print("  主字段 +%02X 与槽位 %d 相同，MODE 循环长度 = %d，"
                          "提前结束" % (main_off, j, i))
                    break
            if cycle != args.slots:
                break

        # 切到下一个槽位
        base = press(link, parser, KEY_MODE, max(0.45, args.delay))
        if base is None:
            print("  切槽位超时，中止。")
            link.close()
            return 1

    print("\n[槽位字段] 连按 MODE 查找「当前选中项」…", end="", flush=True)
    slot_info = detect_slot_field(link, parser, cycle, args.delay)
    if slot_info.get("offset") is None:
        print(" 未找到（%s）" % slot_info.get("reason", "未知"))
    else:
        print(" +%02X  序列 %s" % (slot_info["offset"],
                                 " ".join(str(v) for v in
                                          slot_info["values"])))

    result = {
        "port": port,
        "cycle_len": cycle,
        "slots": slots,
        "slot_field": slot_info.get("offset"),
        "voltage_steps": list(proto.VOLTAGE_STEPS),
        "timebase_steps": list(proto.TIMEBASE_STEPS),
    }
    print()
    print(render_report(result))

    layout = build_layout(result, slot_info)
    try:
        d = os.path.dirname(args.out)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=1)
        print("\n原始数据已写入 %s" % args.out)
        with open(args.layout, "w", encoding="utf-8") as fh:
            json.dump(layout, fh, ensure_ascii=False, indent=1)
        print("字段布局已写入 %s" % args.layout)
    except OSError as exc:
        print("\n写入失败: %s" % exc)
        return 1

    print()
    print("── 字段布局 ──")
    for role, e in layout["roles"].items():
        name = {"timebase": "时基", "voltage": "垂直档位",
                "enum": "枚举项"}.get(role, role)
        print("  %s  +%02X (0x%08X)  槽位 %d  %d..%d"
              % (name, e["offset"], proto.SETTINGS_ADDR + e["offset"],
                 e["slot"], e["min"], e["max"]))
    for e in layout["others"]:
        print("  连续量 +%02X (0x%08X)  槽位 %d"
              % (e["offset"], proto.SETTINGS_ADDR + e["offset"], e["slot"]))
    if layout["slot_field"] is None:
        print("  当前槽位字段：未找到 —— 上位机将用「读设置后比对」的方式定位")
    else:
        print("  当前槽位字段 +%02X" % layout["slot_field"])
    print("\n请把上面的字段布局与设备屏幕上的实际档位核对一遍。")

    if not args.no_restore:
        print("\n注入 AUTO 恢复档位…", end="", flush=True)
        press(link, parser, KEY_AUTO, 1.2)
        print(" 完成")
    link.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
