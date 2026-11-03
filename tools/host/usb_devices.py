#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""列出当前**插着**的 USB 设备（VID/PID + 状态 + 错误码），刷机排错用

用法:
    python tools/host/usb_devices.py              # 只看 USB 设备，标出 2E3C:5740
    python tools/host/usb_devices.py --all        # 连串口/HID 一起列
    python tools/host/usb_devices.py --watch      # 每秒刷新，插拔时实时看变化

为什么不用注册表 Enum\\USB：那个键里全是**历史记录**，拔了也不删，
会让人误判"设备还在"。这里走 WMI/CIM( Win32_PnPEntity )，只列当前存在的。

错误码（ConfigManagerErrorCode）:
    0  正常
    43 设备描述符请求失败 / 枚举失败 ← 之前 USB 没调通时就是这个
    10 设备无法启动
    28 驱动未安装
"""
import argparse
import json
import subprocess
import sys
import time

OURS = (0x2E3C, 0x5740)      # DSO-153 补丁版的 VID:PID
PS = ("$ErrorActionPreference='SilentlyContinue';"
      "Get-CimInstance Win32_PnPEntity | Where-Object {$_.PNPDeviceID -like 'USB*'} | "
      "Select-Object Name,PNPDeviceID,Status,ConfigManagerErrorCode | ConvertTo-Json -Compress")


def query():
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", PS],
            capture_output=True, text=True, timeout=25,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as e:
        print("调用 PowerShell 失败：%s" % e)
        return []
    txt = (out.stdout or "").strip()
    if not txt:
        return []
    try:
        data = json.loads(txt)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = [data]
    return data


def parse_vidpid(pnp):
    """USB\\VID_2E3C&PID_5740\\... -> (0x2E3C, 0x5740)"""
    vid = pid = None
    if not pnp:
        return None, None
    for seg in pnp.split("\\")[1:2]:
        for kv in seg.split("&"):
            if kv.startswith("VID_"):
                try:
                    vid = int(kv[4:8], 16)
                except ValueError:
                    pass
            elif kv.startswith("PID_"):
                try:
                    pid = int(kv[4:8], 16)
                except ValueError:
                    pass
    return vid, pid


def show(all_dev):
    devs = query()
    if not devs:
        print("查不到 USB 设备（PowerShell/WMI 不可用？）")
        return 1
    rows = []
    for d in devs:
        pnp = d.get("PNPDeviceID") or ""
        vid, pid = parse_vidpid(pnp)
        name = d.get("Name") or "(无名)"
        code = d.get("ConfigManagerErrorCode")
        status = d.get("Status") or "?"
        if not all_dev and vid is None:
            continue
        rows.append((vid, pid, name, status, code))
    rows.sort(key=lambda r: (r[0] or 0, r[1] or 0, r[2]))

    hit = False
    print("%-9s %-9s %-40s %-8s %s" % ("VID", "PID", "名称", "状态", "错误码"))
    for vid, pid, name, status, code in rows:
        mark = ""
        if (vid, pid) == OURS:
            mark = "  <== DSO-153"
            hit = True
        elif code not in (0, None):
            mark = "  <== 有问题"
        print("%-9s %-9s %-40s %-8s %s%s"
              % ("0x%04X" % vid if vid else "-", "0x%04X" % pid if pid else "-",
                 name[:40], status, code, mark))

    print()
    if hit:
        print("Phosphor 设备在：VID:PID = 0x2E3C:0x5740（CDC 虚拟串口，用 phosphor_cli.py 或 phosphor_host.py 取数）")
    else:
        print("没看到 2E3C:5740。若刚进 IAP，盘符版是 AT32 Msc iap，用 check_iap.py 看。")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="连没有 VID 的条目一起列")
    ap.add_argument("--watch", action="store_true", help="每秒刷新")
    a = ap.parse_args()

    if not a.watch:
        return show(a.all)

    last = None
    try:
        while True:
            snap = query()
            sig = json.dumps(snap, sort_keys=True, ensure_ascii=False)
            if sig != last:
                print("\n=== %s ===" % time.strftime("%H:%M:%S"))
                show(a.all)
                last = sig
            time.sleep(1.0)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
