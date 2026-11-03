#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""对官方库施加本项目所需的局部修改。

官方库由 `tools/build/at32lib/fetch.py` 从上游仓库抓取，重新抓取会覆盖本地
修改。本脚本以幂等的字符串替换方式重新施加这些修改，供 fetch.py 与
build.sh 调用：文件已是目标状态时不做任何改动。

修改项
  1. cdc_desc.h  设备描述符字符串：厂商、产品、配置、接口。
     Windows 依据产品字符串命名 COM 端口，默认 "AT32 Virtual Com Port"
     无法体现设备身份。
  2. cdc_class.c CDC 线路编码默认波特率。USB CDC 为虚拟串口，波特率不
     参与实际速率（由 USB 批量传输决定），但主机侧工具会读取该值，
     保留过低的名义值会误导使用者。
"""

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.join(ROOT, "build", "at32lib")

# (相对路径, 原文, 新文)
PATCHES = [
    (
        "usb/cdc/cdc_desc.h",
        '#define USBD_CDC_DESC_MANUFACTURER_STRING    "Artery"',
        '#define USBD_CDC_DESC_MANUFACTURER_STRING    "Phosphor"',
    ),
    (
        "usb/cdc/cdc_desc.h",
        '#define USBD_CDC_DESC_PRODUCT_STRING         "AT32 Virtual Com Port  "',
        '#define USBD_CDC_DESC_PRODUCT_STRING         "Phosphor DSO-153"',
    ),
    (
        "usb/cdc/cdc_desc.h",
        '#define USBD_CDC_DESC_CONFIGURATION_STRING   "Virtual ComPort Config"',
        '#define USBD_CDC_DESC_CONFIGURATION_STRING   "Phosphor CDC Config"',
    ),
    (
        "usb/cdc/cdc_desc.h",
        '#define USBD_CDC_DESC_INTERFACE_STRING       "Virtual ComPort Interface"',
        '#define USBD_CDC_DESC_INTERFACE_STRING       "Phosphor CDC Data"',
    ),
    (
        "usb/cdc/cdc_class.c",
        """linecoding_type linecoding =
{
  115200,""",
        """linecoding_type linecoding =
{
  921600,""",
    ),
]


def apply_patch(path, old, new):
    """替换单处文本。返回 True 表示本次实际改动，False 表示已是目标状态。"""
    if not os.path.exists(path):
        print("  [XX] 缺少文件: %s" % path)
        return None
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    if new in text:
        return False
    if old not in text:
        print("  [XX] 未找到待替换文本: %s" % os.path.basename(path))
        return None
    with open(path, "w", encoding="utf-8") as f:
        f.write(text.replace(old, new, 1))
    return True


def main():
    print("=== 官方库局部修改 ===")
    changed = 0
    failed = 0
    for rel, old, new in PATCHES:
        path = os.path.join(LIB, rel)
        r = apply_patch(path, old, new)
        if r is None:
            failed += 1
            print("  [XX] %s" % rel)
        elif r:
            changed += 1
            print("  已改 %s" % rel)
    if failed:
        print("  [XX] %d 项修改失败" % failed)
        return 1
    print("  共 %d 项需要修改，%d 项已是目标状态"
          % (changed, len(PATCHES) - changed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
