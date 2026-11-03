#!/usr/bin/env bash
# Phosphor 固件构建脚本
#
#   用法:  tools/build.sh [revision]
#   示例:  tools/build.sh R1        -> firmware/PHOS_V1.6R1.bin
#          tools/build.sh           -> 取最新 v1.6rN 标签的 N（如 v1.6r14 -> R14）；
#                                      一个标签都没有时用占位 R0（开发构建）
#
#   命名规则: PHOS_V<原厂基线版本>R<修订号>
#
# 流程:
#   1. 编译：补丁源码 2 个 .c + 官方库 11 个 .c
#   2. 链接：代码段置于 0x08035B00，数据段置于 0x2000A580
#   3. 校验段布局（.bss 不得越过让出区）
#   4. 校验 256KB Flash 边界
#   5. 打补丁：主循环钩子、OTGFS1 向量、stack_base
#   6. 改写版本号字符串
#   7. 静态自检
# 任一步失败即终止；自检未通过不得刷写。
set -e
set -o pipefail

BASE_VER=1.6
REV=${1:-}
if [ -z "$REV" ]; then
  # 未指定修订号：取最新的 v1.6rN 标签转成 RN；没有则退回占位 R0
  REV=$(git tag --list 2>/dev/null \
        | sed -n 's/^[vV]1\.6[Rr]0*\([0-9][0-9]*\)$/R\1/p' \
        | sort -V | tail -n1)
  REV=${REV:-R0}
  echo "[i] 未指定修订号，按最新 git 标签使用 $REV"
fi

if ! [[ "$REV" =~ ^R[0-9]+$ ]]; then
  echo "修订号格式应为 R<数字>，例如 R1"
  exit 2
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

SRC=firmware/vendor/APP_DSO153_V1.6_20240427.bin
# 版本串（About 页显示，硬上限 6 字符）可用 PHOS_VER 覆盖，
# 例如 PHOS_VER=V1.6S1 bash tools/build.sh R10
# 未覆盖时自动取 V<基线><修订号>；修订号超过两位会超长，此时去掉前缀 V
#（如 R14 -> "1.6R14"），否则 set_version 在 8 字节站点写不下。
VER_STR="${PHOS_VER:-V${BASE_VER}${REV}}"
if [ -z "$PHOS_VER" ] && [ "${#VER_STR}" -gt 6 ]; then
  VER_STR="${VER_STR#V}"
fi
OUT="firmware/PHOS_V${BASE_VER}${REV}.bin"
TMP=tools/build/tmp

LIB=tools/build/at32lib
OBJ=tools/build/obj
FW=tools/fw

CC="clang --target=arm-none-eabi -mcpu=cortex-m4 -mthumb -O2 -ffreestanding \
    -fno-builtin -nostdlib -fshort-enums -ffunction-sections -fdata-sections \
    -fno-common -std=gnu11 -Wall -Wno-unused-function -Wno-unused-variable \
    -DAT32F435CCT7"
INC="-I${FW} -I${FW}/usb -I${LIB}/usb/inc -I${LIB}/usb/cdc \
     -I${LIB}/at32/inc -I${LIB}/cmsis -I${LIB}/cmsis/core"

SRCS="
  ${FW}/hook.c
  ${FW}/img.c
  ${FW}/img_hook.S
  ${FW}/about_logo.c
  ${FW}/hook_about.S
  ${FW}/logo_wide.c
  ${FW}/usb/phosphor_usb.c
  ${LIB}/usb/src/usb_core.c
  ${LIB}/usb/src/usbd_core.c
  ${LIB}/usb/src/usbd_int.c
  ${LIB}/usb/src/usbd_sdr.c
  ${LIB}/usb/cdc/cdc_class.c
  ${LIB}/usb/cdc/cdc_desc.c
  ${LIB}/at32/src/at32f435_437_usb.c
  ${LIB}/at32/src/at32f435_437_crm.c
  ${LIB}/at32/src/at32f435_437_gpio.c
  ${LIB}/at32/src/at32f435_437_misc.c
  ${LIB}/at32/src/at32f435_437_acc.c
"

echo "=== 0/6 官方库局部修改（设备名 / 线路编码） ==="
python tools/patch_lib.py | tail -2 || exit 1

echo "=== 1/6 编译 ==="
mkdir -p "$OBJ" "$TMP" "$(dirname "$OUT")"
OBJS=""
for s in $SRCS; do
  o="$OBJ/$(basename "${s%.c}").o"
  $CC $INC -c "$s" -o "$o"
  OBJS="$OBJS $o"
done

echo "=== 2/6 链接 ==="
ld.lld -T "${FW}/link.ld" --gc-sections --no-rosegment -o "${FW}/phosphor_patch.elf" $OBJS
llvm-nm -n "${FW}/phosphor_patch.elf" > "${FW}/phosphor_patch.sym"
llvm-objcopy -O binary "${FW}/phosphor_patch.elf" "${FW}/phosphor_patch.bin"
llvm-size "${FW}/phosphor_patch.elf"

echo "=== 3/6 段布局 ==="
python tools/check_layout.py "${FW}/phosphor_patch.elf" || exit 1

echo "=== 4/6 Flash 边界（AT32F435CCT7，256KB） ==="
python tools/check_layout.py "${FW}/phosphor_patch.bin" || exit 1

echo "=== 5/6 打补丁 ==="
# NO_IMG=1 生成不含 push_band 画面流钩子的对照版：设备异常时用来区分
# "故障是否来自这条链路"，步骤与 T1 相同。
IMG_FLAG=""
if [ -n "$NO_IMG" ]; then
  IMG_FLAG="--no-img"
  echo "  [i] NO_IMG 已设置：不安装画面流钩子（对照版）"
fi
python tools/patch.py "$SRC" -o "${TMP}/patched.bin" \
    --stub "${FW}/phosphor_patch.bin" --sym "${FW}/phosphor_patch.sym" \
    --hook-site 0x0801A7BC $IMG_FLAG

echo "=== 6/6 改写版本号并自检 ==="
python tools/set_version.py "${TMP}/patched.bin" "$VER_STR" -o "$OUT" | tail -3
python tools/verify_patch.py "$OUT" --sym "${FW}/phosphor_patch.sym" \
    --hook-site 0x0801A7BC --orig 0x08008EE0 | tail -12

rm -f "${TMP}/patched.bin"

# 第 7 步：主题色 + 字形替换。
#
# 主题色无条件套用（纯字节改写，不依赖 Pillow），预设可用 THEME_PRESET 覆盖，
# 默认 phosphor2 = R14 定稿的绿 / 白双主题。
#
# 字形替换依赖 Sarasa 等宽字体与 Pillow，字体按以下顺序查找：
#   $SARASA_FONT → tools/fonts/*.ttf → ~/Downloads/SarasaTerm-TTF-*/*.ttf
# 找不到时只跳过字形（并大声提示），绝不静默跳过整个第 7 步——R15 / R16
# 就是因为整步被 SARASA_FONT 门控，把主题和字库一起丢了。
echo
echo "=== 7/7 主题与字形 ==="
THEME_PRESET="${THEME_PRESET:-phosphor2}"
python tools/set_theme.py "$OUT" --preset "$THEME_PRESET" | tail -5

SARASA_FONT="${SARASA_FONT:-}"
if [ -z "$SARASA_FONT" ]; then
  # 只认 SC Regular（R14 定稿所用）；CL/Bold 变体字宽与字重都不同，不能替代
  for cand in "$ROOT"/tools/fonts/SarasaTermSC-Regular.ttf \
              "$HOME"/Downloads/SarasaTerm-TTF-*/SarasaTermSC-Regular.ttf \
              "$HOME"/Downloads/SarasaTerm-TTF-*/*TermSC-Regular.ttf; do
    if [ -f "$cand" ]; then SARASA_FONT="$cand"; break; fi
  done
fi

if [ -n "$SARASA_FONT" ] && [ -f "$SARASA_FONT" ]; then
  FONT_PY=python
  python -c "import PIL" 2>/dev/null || \
    FONT_PY="$LOCALAPPDATA/Programs/Python/Python312/python.exe"
  echo "  字库: $SARASA_FONT"
  "$FONT_PY" tools/set_font.py "$OUT" --font "$SARASA_FONT" | tail -7
else
  echo "  [!!] 未找到 Sarasa 字体，本次跳过字形替换（主题色已套用）。"
  echo "       用 SARASA_FONT=<ttf 路径> 指定，或放到 tools/fonts/ 下。"
fi

echo
echo "产物: $OUT"
ls -l "$OUT"
