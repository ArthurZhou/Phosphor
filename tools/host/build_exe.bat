@echo off
rem Phosphor 上位机打包脚本（Nuitka + MSVC）
rem
rem   用法:  tools\host\build_exe.bat            单文件 exe（推荐分发）
rem          tools\host\build_exe.bat dist       目录版（启动快，便于排错）
rem          tools\host\build_exe.bat mingw      没有 MSVC 时用 MinGW（自动下载）
rem
rem   产物:  tools\build\host\PhosphorHost.exe
rem          tools\build\host\phosphor_host.dist\PhosphorHost.exe（目录版）
rem
rem   前置:  官方 Python 3.12（含 tkinter）
rem          py -3.12 -m pip install nuitka zstandard pyserial
rem
rem   ！重要：必须在普通 CMD 窗口里运行（资源管理器双击，或 Win+R 输 cmd）。
rem          在 WorkBuddy 或其他沙箱终端里运行时，安全策略会拦掉 reg.exe，
rem          vcvars64.bat 因此读不到 Windows SDK，Nuitka 会报
rem          "Windows SDK must be installed in Visual Studio"。本脚本在预检
rem          阶段就会指出这一点，不会白等一次编译。
chcp 65001 >nul
setlocal
pushd "%~dp0..\.."

set "MODE=onefile"
set "FORCE_MINGW=0"
if /i "%~1"=="dist" set "MODE=standalone"
if /i "%~1"=="standalone" set "MODE=standalone"
if /i "%~1"=="mingw" set "FORCE_MINGW=1"
if not defined HOST_VER set "HOST_VER=1.6.1"

rem ---- 1. 挑一个带 tkinter 的解释器 ----
set "PY="
if defined PHOS_PY set "PY=%PHOS_PY%"
if not defined PY (
  py -3.12 -c "import tkinter" >nul 2>nul && set "PY=py -3.12"
)
if not defined PY (
  python -c "import tkinter" >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo [错误] 找不到带 tkinter 的 Python 解释器。
  echo        请安装官方 Python 3.12（默认含 tcl/tk），或用 PHOS_PY 指定。
  popd
  exit /b 2
)

echo 解释器  %PY%
%PY% -c "import nuitka" >nul 2>nul
if errorlevel 1 (
  echo [错误] 缺少 Nuitka，请先执行:  %PY% -m pip install nuitka zstandard
  popd
  exit /b 2
)
%PY% -c "import serial" >nul 2>nul
if errorlevel 1 (
  echo [错误] 缺少 pyserial，请先执行:  %PY% -m pip install pyserial
  popd
  exit /b 2
)

rem ---- 2. 预检 C 工具链 ----
set "MINGW_FLAG="
if "%FORCE_MINGW%"=="1" (
  echo 编译器  MinGW（Nuitka 自动下载）
  goto :have_compiler
)

set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
set "VSDIR="
if not exist "%VSWHERE%" (
  echo [错误] 未找到 Visual Studio，无法使用 MSVC。
  echo        可改用:  tools\host\build_exe.bat mingw
  popd
  exit /b 2
)
for /f "usebackq delims=" %%I in (`"%VSWHERE%" -latest -property installationPath`) do set "VSDIR=%%I"
if not defined VSDIR (
  echo [错误] vswhere 没有返回 Visual Studio 安装路径。
  echo        可改用:  tools\host\build_exe.bat mingw
  popd
  exit /b 2
)
if not exist "%VSDIR%\VC\Auxiliary\Build\vcvars64.bat" (
  echo [错误] 缺少 vcvars64.bat，Visual Studio 未安装 C++ 生成工具。
  echo        可改用:  tools\host\build_exe.bat mingw
  popd
  exit /b 2
)

call "%VSDIR%\VC\Auxiliary\Build\vcvars64.bat" >nul 2>nul
if not defined WindowsSDKVersion (
  echo.
  echo [错误] vcvars64 没有拿到 Windows SDK 路径（WindowsSDKVersion 为空）。
  echo        说明当前终端里 reg.exe 跑不起来，最常见的原因是终端受安全策略
  echo        限制（WorkBuddy 的「命令安全 - 程序黑名单」就含 reg.exe）。
  echo        处理办法（任选其一）:
  echo          1. 资源管理器里双击本脚本，或在 Win+R 打开的 cmd 里运行；
  echo          2. 或改用 MinGW:  tools\host\build_exe.bat mingw
  popd
  exit /b 2
)
echo 编译器  MSVC %VCToolsVersion%  SDK %WindowsSDKVersion%

:have_compiler
rem ---- 3. 打包 ----
echo 模式    %MODE%
echo 版本    %HOST_VER%
echo.

%PY% -m nuitka ^
  --mode=%MODE% ^
  --enable-plugins=tk-inter ^
  --include-package=serial.tools ^
  --windows-console-mode=disable ^
  --company-name=Phosphor ^
  --product-name="Phosphor Host" ^
  --file-description="Phosphor DSO-153 Host" ^
  --file-version=%HOST_VER%.0 ^
  --product-version=%HOST_VER%.0 ^
  --output-dir=tools\build\host ^
  --output-filename=PhosphorHost.exe ^
  --assume-yes-for-downloads ^
  %MINGW_FLAG% ^
  tools\host\phosphor_host.py

if errorlevel 1 goto :build_fail

set "ART=tools\build\host\PhosphorHost.exe"
if /i "%MODE%"=="standalone" set "ART=tools\build\host\phosphor_host.dist\PhosphorHost.exe"

if not exist "%ART%" goto :no_artifact

echo.
echo 产物    %ART%
for %%F in ("%ART%") do echo 体积    %%~zF 字节
echo 提示    挡位配置读写于 exe 同目录的 phosphor_gear.json
echo.

popd
if /i not "%~1"=="--nopause" pause
endlocal
exit /b 0

:build_fail
echo.
echo [失败] Nuitka 编译未通过，原因见上方日志。
echo        若是 Windows SDK 相关报错，请改用普通 CMD 窗口重跑，
echo        或执行:  tools\host\build_exe.bat mingw
popd
exit /b 1

:no_artifact
echo [失败] 未找到产物: %ART%
popd
exit /b 1
