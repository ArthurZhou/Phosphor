@echo off
rem Phosphor 图形上位机启动脚本
rem 优先使用带 tkinter 的 Python 解释器；未安装 tkinter 时回退到 py -3.12。
chcp 65001 >nul
setlocal

python -c "import tkinter" >nul 2>nul
if errorlevel 1 (
    py -3.12 "%~dp0phosphor_host.py" %*
) else (
    python "%~dp0phosphor_host.py" %*
)
endlocal
