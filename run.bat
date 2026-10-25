@echo off
rem ============================================
rem  dsh-pet 独立桌宠 —— 一键启动
rem  （双击本文件即可；不弹出控制台窗口）
rem ============================================
cd /d "%~dp0"
rem 优先使用项目自带虚拟环境（含语音输入法所需的 vosk 依赖）。
if exist "%~dp0.venv\Scripts\pythonw.exe" (
    "%~dp0.venv\Scripts\pythonw.exe" -m pet
) else (
    pythonw -m pet
)
