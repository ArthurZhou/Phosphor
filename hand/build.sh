#!/usr/bin/env bash
# DSO-153 手写寄存器版（hand 线）一键构建
#   用法: hand/build.sh [标签] [版本号]
# 仅读写 hand/ 下的文件与 firmware/ 下的产物，不涉及 tools/fw/。
set -e

TAG=${1:-dev}
VER=${2:-V1.8HX}

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

SRC=APP_DSO153_V1.6_20240427.bin
OUT="firmware/legacy/APP_DSO153_hand_${TAG}.bin"
H=hand
OBJ=hand/obj
mkdir -p "$OBJ" firmware/legacy hand/build

CC="clang --target=arm-none-eabi -mcpu=cortex-m4 -mthumb -O2 -ffreestanding \
    -fno-builtin -nostdlib -fshort-enums -std=gnu11 -Wall \
    -Wno-unused-function -Wno-unused-variable"

echo "=== 1/4 编译（手写寄存器版） ==="
OBJS=""
for s in "$H/hook.c" "$H/usb_ctrl.c" "$H/usb_dwc2.c"; do
  o="$OBJ/$(basename "${s%.c}").o"
  $CC -I"$H" -c "$s" -o "$o"
  OBJS="$OBJS $o"
done

echo "=== 2/4 链接到 0x08035B00 ==="
ld.lld -T "$H/link.ld" --gc-sections -o "$H/patch.elf" $OBJS
llvm-nm -n "$H/patch.elf" > "$H/patch.sym"
llvm-objcopy -O binary "$H/patch.elf" "$H/patch.bin"
llvm-size "$H/patch.elf"
python - "$H/patch.sym" <<'PY'
import sys
sym = {}
for line in open(sys.argv[1]):
    p = line.split()
    if len(p) == 3:
        try: sym[p[2]] = int(p[0], 16)
        except ValueError: pass
bss = sym["__bss_end"] - sym["__bss_start"]
print("  .bss %d 字节 (0x%08X..0x%08X)" % (bss, sym["__bss_start"], sym["__bss_end"]))
if sym["__bss_end"] > 0x2000A800:
    print("  [XX] .bss 越界 0x2000A800"); sys.exit(1)
PY

echo "=== 3/4 打钩子 + 装 USB 向量 ==="
python "$H/patch.py" "$SRC" -o "hand/build/${TAG}_raw.bin" \
    --stub "$H/patch.bin" --sym "$H/patch.sym" --hook-site 0x0801A7BC
python "$H/patch_strings.py" "hand/build/${TAG}_raw.bin" \
    --sym "$H/patch.sym" -o "hand/build/${TAG}_str.bin" | tail -1
python "$H/set_version.py" "hand/build/${TAG}_str.bin" "$VER" -o "$OUT"

echo "=== 4/4 自检 ==="
python "$H/verify_patch.py" "$OUT" --sym "$H/patch.sym" \
    --hook-site 0x0801A7BC --orig 0x08008EE0 | tail -10

echo
echo "产物: $OUT"
ls -l "$OUT"
