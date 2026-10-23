@echo off
chcp 936 >nul
rem 前台启动（能看到日志）。合成演示数据：start.bat demo
cd /d "%~dp0"
python -m server.deps >nul 2>nul
if errorlevel 1 (
  echo.
  echo 还没有安装运行所需的组件（或者没有找到 Python），现在运行「安装.bat」……
  echo.
  call "%~dp0安装.bat" /nolaunch
  if errorlevel 1 exit /b 1
)
if /i "%1"=="demo" (
  python -m server.cli serve --demo
) else (
  python -m server.main
)
