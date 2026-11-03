#!/usr/bin/env bash
# Phosphor 上位机打包脚本（Nuitka）
#
#   用法:  tools/host/build_exe.sh              单文件 exe（默认）
#          tools/host/build_exe.sh --standalone 目录版（启动快，便于排错）
#          HOST_VER=1.6.1 tools/host/build_exe.sh
#
#   产物:  tools/build/host/PhosphorHost.exe                 单文件
#          tools/build/host/phosphor_host.dist/             目录版
#
# 说明:
#   - 图形界面依赖 tkinter，必须用带 tkinter 的解释器打包。精简发行版
#     （Microsoft Store 版、部分 embeddable 包）不带 tkinter，脚本会拒绝执行。
#   - 解释器按 PHOS_PY 环境变量、py -3.12、python 的顺序挑选，可用
#     PHOS_PY 指定，例如 PHOS_PY=python3.12 tools/host/build_exe.sh。
#   - 挡位配置 phosphor_gear.json 与布局文件 phosphor_layout.json 在打包
#     形态下读写 exe 所在目录（见 phosphor_host.app_dir），不写临时解包目录。
#   - 首次打包需 C 编译器：本机已装 Visual Studio 2022，Nuitka 自动探测；
#     无 MSVC 时可加 --mingw64 让 Nuitka 下载 MinGW。
set -e

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

MODE="onefile"
for arg in "$@"; do
  case "$arg" in
    --standalone) MODE="standalone" ;;
    --onefile)    MODE="onefile" ;;
    -h|--help)    sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "未知参数：$arg"; exit 2 ;;
  esac
done

# 上位机版本号，取自固件版本串 V1.6S1 -> 1.6.1
VER="${HOST_VER:-1.6.1}"

SRC=tools/host/phosphor_host.py
OUTDIR=tools/build/host

# ---- 挑选解释器 -------------------------------------------------------
if [ -n "$PHOS_PY" ]; then
  PY="$PHOS_PY"
elif py -3.12 -c "import tkinter" >/dev/null 2>&1; then
  PY="py -3.12"
elif python -c "import tkinter" >/dev/null 2>&1; then
  PY="python"
else
  echo "找不到带 tkinter 的 Python 解释器。" >&2
  echo "请安装官方 Python 3.12（含 tcl/tk），或用 PHOS_PY 指定。" >&2
  exit 2
fi

echo "解释器  $($PY -c 'import sys;print(sys.executable)')"

$PY -c "import nuitka" 2>/dev/null || {
  echo "未安装 Nuitka：$PY -m pip install nuitka zstandard" >&2
  exit 2
}
$PY -c "import serial" 2>/dev/null || {
  echo "未安装 pyserial：$PY -m pip install pyserial" >&2
  exit 2
}

# ---- 打包 -------------------------------------------------------------
echo "模式    $MODE"
echo "目标    $OUTDIR/PhosphorHost.exe"

$PY -m nuitka \
  --mode="$MODE" \
  --enable-plugins=tk-inter \
  --include-package=serial.tools \
  --windows-console-mode=disable \
  --company-name="Phosphor" \
  --product-name="Phosphor Host" \
  --file-description="Phosphor DSO-153 上位机" \
  --file-version="$VER.0" \
  --product-version="$VER.0" \
  --output-dir="$OUTDIR" \
  --output-filename=PhosphorHost.exe \
  --assume-yes-for-downloads \
  "$SRC"

echo
if [ "$MODE" = "onefile" ]; then
  ART="$OUTDIR/PhosphorHost.exe"
else
  ART="$OUTDIR/phosphor_host.dist/PhosphorHost.exe"
fi

if [ ! -f "$ART" ]; then
  echo "打包结束但未找到产物：$ART" >&2
  exit 1
fi

# 产物自检：只确认文件存在。窗口模式无控制台，--help 不会输出，靠
# 启动观察（见 docs/HOST.md）验证，不在此处跑图形界面。
echo "产物    $ART"
echo "体积    $(du -h "$ART" | cut -f1)"
echo "SHA-256 $(sha256sum "$ART" | cut -d' ' -f1)"
