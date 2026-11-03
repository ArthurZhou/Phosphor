#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build 步骤 3/4 的检查（tools/build.sh 与 tools/build.bat 共用）

用法:
    python tools/check_layout.py <phosphor_patch.elf>   段布局检查
    python tools/check_layout.py <phosphor_patch.bin>   256KB Flash 边界检查
任一检查失败返回非 0，构建即终止。
"""
import os
import subprocess
import sys


def check_layout(elf: str) -> int:
    RAM_BASE = 0x2000B800      # 补丁变量区：初始 SP(0x2000B580) 之上，不侵占原厂栈
    RAM_END = 0x2000C000
    out = subprocess.run(["llvm-nm", "-n", elf],
                         capture_output=True, text=True).stdout
    sym = {}
    for line in out.splitlines():
        p = line.split()
        if len(p) == 3:
            try:
                sym[p[2]] = int(p[0], 16)
            except ValueError:
                pass
    need = ["hook", "usb_irq", "fault_stub", "hook_about", "__data_lma",
            "__data_start", "__data_end", "__bss_start", "__bss_end",
            "__fault_rec"]
    miss = [n for n in need if n not in sym]
    if miss:
        print("  缺少符号:", miss)
        return 1
    print("  hook      0x%08X" % sym["hook"])
    print("  usb_irq   0x%08X" % sym["usb_irq"])
    print("  fault     0x%08X" % sym["fault_stub"])
    print("  about     0x%08X  (hook_about)" % sym["hook_about"])
    print("  .data     %d 字节 (0x%08X..0x%08X)  初值 @0x%08X"
          % (sym["__data_end"] - sym["__data_start"], sym["__data_start"],
             sym["__data_end"], sym["__data_lma"]))
    print("  .bss      %d 字节 (0x%08X..0x%08X)  余量 %d"
          % (sym["__bss_end"] - sym["__bss_start"], sym["__bss_start"],
             sym["__bss_end"], RAM_END - sym["__bss_end"]))
    if sym["__bss_end"] > RAM_END:
        print("  [XX] .bss 超出变量区 0x%08X..0x%08X" % (RAM_BASE, RAM_END))
        return 1
    if sym["__data_start"] < RAM_BASE:
        print("  [XX] .data 起始低于 0x%08X" % RAM_BASE)
        return 1
    if sym["__data_start"] < 0x2000B580:
        print("  [XX] .data 落入原厂栈区（初始 SP 0x2000B580 以下）")
        return 1
    if sym["__bss_end"] > sym["__fault_rec"]:
        print("  [XX] .bss 与 HardFault 记录区重叠")
        return 1
    if sym["__fault_rec"] != 0x2000BF00:
        print("  [XX] HardFault 记录区地址 0x%08X 与故障入口的立即数 0x2000BF00 不一致"
              % sym["__fault_rec"])
        return 1
    if sym["__fault_rec"] + 256 > RAM_END:
        print("  [XX] HardFault 记录区超出变量区")
        return 1
    print("  故障记录 0x%08X，256 字节（跨复位保留）" % sym["__fault_rec"])
    return 0


def check_flash(path: str) -> int:
    stub = os.path.getsize(path)
    end = 0x08035B00 + stub
    print("  补丁 %d 字节，末尾 0x%08X，剩余 %d 字节"
          % (stub, end, 0x08040000 - end))
    if end > 0x08040000:
        print("  [XX] 超出 256KB Flash 边界")
        return 1
    return 0


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    path = sys.argv[1]
    if path.endswith(".elf"):
        return check_layout(path)
    if path.endswith(".bin"):
        return check_flash(path)
    print("参数应为 .elf 或 .bin：%s" % path)
    return 2


if __name__ == "__main__":
    sys.exit(main())
