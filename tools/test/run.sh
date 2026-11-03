#!/usr/bin/env bash
# img.c 的离线验证：宿主机原生编译 -> 驱动落盘 -> 用生产解析器比对
#
# 纯 C：img_test_main.c（窗口替身 + USB 桩 + main）与 img.c 同编。
# 验证的是分块、描述字段、校验、预算与越界拒绝，不含 USB 与真实内存布局。
set -e
cd "$(dirname "$0")/../.."

EXE=tools/test/img_test
if command -v cc >/dev/null 2>&1; then CC=cc; else CC=gcc; fi

echo "== 编译 img.c（宿主机原生）=="
"$CC" -O1 -Wall -Wextra -DIMG_HOST_TEST -o "$EXE" \
      tools/test/img_test_main.c tools/fw/img.c
if [ -f "$EXE.exe" ]; then EXE="$EXE.exe"; fi
echo "  $EXE  $(wc -c < "$EXE") 字节"

echo "== 运行并比对 =="
python tools/test/img_check.py
