@echo off
rem Phosphor 固件构建脚本（tools/build.sh 的 Windows 批处理版）
rem
rem   用法:  tools\build.bat [revision]
rem   示例:  tools\build.bat R36      -> firmware\PHOS_V1.6R36.bin
rem          tools\build.bat          -> 取最新 v1.6rN 标签的 N（如 v1.6r14 -> R14）；
rem                                      一个标签都没有时用占位 R0（开发构建）
rem
rem   环境变量与 build.sh 一致:
rem     PHOS_VER      版本串（About 页显示，硬上限 6 字符），如 V1.6SI
rem     NO_IMG        设为任意值则不安装画面流钩子（对照版）
rem     THEME_PRESET  主题色预设（默认 phosphor2）
rem     SARASA_FONT   字库 ttf 路径（不设则自动查找）
rem
rem   任一步失败即终止；自检未通过不得刷写。
setlocal enabledelayedexpansion
cd /d "%~dp0.."

set BASE_VER=1.6
set "REV=%~1"
if not "%REV%"=="" goto rev_ok
rem 未指定修订号：取最新的 v1.6rN 标签转成 RN；没有则退回占位 R0
set /a REV_N=0
for /f "usebackq tokens=2 delims=rR" %%a in (`git tag --list 2^>nul ^| findstr /ri "v1.6r[0-9]*"`) do (
    set "N="
    set /a N=%%a 2>nul
    if defined N if !N! GTR !REV_N! set /a REV_N=!N!
)
set "REV=R0"
if !REV_N! GTR 0 set "REV=R!REV_N!"
echo [i] 未指定修订号，按最新 git 标签使用 !REV!
:rev_ok
echo %REV%| findstr /r "^[R][0-9][0-9]*$" >nul
if errorlevel 1 (
    echo 修订号格式应为 R^<数字^>，例如 R1
    exit /b 2
)

if defined PHOS_VER (set "VER_STR=%PHOS_VER%") else set "VER_STR=V!BASE_VER!!REV!"
rem 显示串硬上限 6 字符（版本号站点只有 8 字节，含冒号与 NUL）：
rem 修订号超过两位时去掉前缀 V，例如 V1.6R14 -> 1.6R14
if not defined PHOS_VER if not "!REV:~2!"=="" set "VER_STR=!VER_STR:~1!"
set "OUT=firmware\PHOS_V!BASE_VER!!REV!.bin"
set "BUILD_TMP=tools\build\tmp"
set "LIB=tools\build\at32lib"
set "OBJ=tools\build\obj"
set "FW=tools\fw"
set "SRC=firmware\vendor\APP_DSO153_V1.6_20240427.bin"

set "CC=clang --target=arm-none-eabi -mcpu=cortex-m4 -mthumb -O2 -ffreestanding -fno-builtin -nostdlib -fshort-enums -ffunction-sections -fdata-sections -fno-common -std=gnu11 -Wall -Wno-unused-function -Wno-unused-variable -DAT32F435CCT7"
set "INC=-I%FW% -I%FW%\usb -I%LIB%\usb\inc -I%LIB%\usb\cdc -I%LIB%\at32\inc -I%LIB%\cmsis -I%LIB%\cmsis\core"

echo === 0/6 官方库局部修改（设备名 / 线路编码） ===
python tools\patch_lib.py || exit /b 1

echo === 1/6 编译 ===
if not exist "%OBJ%" mkdir "%OBJ%"
if not exist "%BUILD_TMP%" mkdir "%BUILD_TMP%"
if not exist "firmware" mkdir "firmware"
set OBJS=
for %%s in (
    "%FW%\hook.c"
    "%FW%\img.c"
    "%FW%\img_hook.S"
    "%FW%\about_logo.c"
    "%FW%\hook_about.S"
    "%FW%\logo_wide.c"
    "%FW%\usb\phosphor_usb.c"
    "%LIB%\usb\src\usb_core.c"
    "%LIB%\usb\src\usbd_core.c"
    "%LIB%\usb\src\usbd_int.c"
    "%LIB%\usb\src\usbd_sdr.c"
    "%LIB%\usb\cdc\cdc_class.c"
    "%LIB%\usb\cdc\cdc_desc.c"
    "%LIB%\at32\src\at32f435_437_usb.c"
    "%LIB%\at32\src\at32f435_437_crm.c"
    "%LIB%\at32\src\at32f435_437_gpio.c"
    "%LIB%\at32\src\at32f435_437_misc.c"
    "%LIB%\at32\src\at32f435_437_acc.c"
) do (
    %CC% %INC% -c "%%s" -o "%OBJ%\%%~ns.o" || exit /b 1
    set OBJS=!OBJS! "%OBJ%\%%~ns.o"
)

echo === 2/6 链接 ===
ld.lld -T "%FW%\link.ld" --gc-sections --no-rosegment -o "%FW%\phosphor_patch.elf" %OBJS% || exit /b 1
llvm-nm -n "%FW%\phosphor_patch.elf" > "%FW%\phosphor_patch.sym"
llvm-objcopy -O binary "%FW%\phosphor_patch.elf" "%FW%\phosphor_patch.bin" || exit /b 1
llvm-size "%FW%\phosphor_patch.elf"

echo === 3/6 段布局 ===
python tools\check_layout.py "%FW%\phosphor_patch.elf" || exit /b 1

echo === 4/6 Flash 边界（AT32F435CCT7，256KB） ===
python tools\check_layout.py "%FW%\phosphor_patch.bin" || exit /b 1

echo === 5/6 打补丁 ===
set "IMG_FLAG="
if defined NO_IMG (
    set "IMG_FLAG=--no-img"
    echo   [i] NO_IMG 已设置：不安装画面流钩子（对照版）
)
python tools\patch.py "%SRC%" -o "%BUILD_TMP%\patched.bin" --stub "%FW%\phosphor_patch.bin" --sym "%FW%\phosphor_patch.sym" --hook-site 0x0801A7BC %IMG_FLAG% || exit /b 1

echo === 6/6 改写版本号并自检 ===
python tools\set_version.py "%BUILD_TMP%\patched.bin" "%VER_STR%" -o "%OUT%" || exit /b 1
python tools\verify_patch.py "%OUT%" --sym "%FW%\phosphor_patch.sym" --hook-site 0x0801A7BC --orig 0x08008EE0 || exit /b 1

del /q "%BUILD_TMP%\patched.bin" 2>nul

rem 第 7 步：主题色 + 字形替换。
rem 主题色无条件套用（纯字节改写，不依赖 Pillow），预设可用 THEME_PRESET 覆盖。
rem 字形替换依赖 Sarasa 等宽字体与 Pillow，查找顺序同 build.sh：
rem   SARASA_FONT -> tools\fonts\*.ttf -> ~/Downloads/SarasaTerm-TTF-*/*.ttf
rem 找不到时只跳过字形（并大声提示），绝不静默跳过整个第 7 步。
echo.
echo === 7/7 主题与字形 ===
if not defined THEME_PRESET set "THEME_PRESET=phosphor2"
python tools\set_theme.py "%OUT%" --preset %THEME_PRESET% || exit /b 1

if defined SARASA_FONT goto font_check
if exist "tools\fonts\SarasaTermSC-Regular.ttf" set "SARASA_FONT=tools\fonts\SarasaTermSC-Regular.ttf"
if defined SARASA_FONT goto font_check
rem 只认 SC Regular（R14 定稿所用）；CL/Bold 变体字宽与字重都不同，不能替代
for /d %%d in ("%USERPROFILE%\Downloads\SarasaTerm-TTF-*") do (
    if not defined SARASA_FONT if exist "%%d\SarasaTermSC-Regular.ttf" set "SARASA_FONT=%%d\SarasaTermSC-Regular.ttf"
)
if defined SARASA_FONT goto font_check
for /d %%d in ("%USERPROFILE%\Downloads\SarasaTerm-TTF-*") do (
    if not defined SARASA_FONT for %%f in ("%%d\*TermSC-Regular.ttf") do (
        if not defined SARASA_FONT set "SARASA_FONT=%%~ff"
    )
)

:font_check
if not defined SARASA_FONT goto font_skip
if not exist "%SARASA_FONT%" goto font_skip
echo   字库: %SARASA_FONT%
python -c "import PIL" >nul 2>nul
if errorlevel 1 (
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" tools\set_font.py "%OUT%" --font "%SARASA_FONT%" || exit /b 1
) else (
    python tools\set_font.py "%OUT%" --font "%SARASA_FONT%" || exit /b 1
)
goto final

:font_skip
echo   [!!] 未找到 Sarasa 字体，本次跳过字形替换（主题色已套用）。
echo        用 SARASA_FONT=^<ttf 路径^> 指定，或放到 tools\fonts\ 下。

:final
echo.
for %%f in ("%OUT%") do echo 产物: %%~ff  （%%~zf 字节）
endlocal
