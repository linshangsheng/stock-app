@echo off
rem 股票 · 趋势波段 —— 一键安装依赖（新电脑第一次用，装好 Python 之后双击本文件）
rem 用法：双击；或命令行  安装.bat /quiet  （不提问、不暂停，测试用）
chcp 936 >nul
setlocal
cd /d "%~dp0"
title 股票 · 趋势波段 - 安装
set QUIET=0
if /i "%~1"=="/quiet" set QUIET=1

echo ==========================================================
echo   股票 · 趋势波段 —— 安装运行所需的组件（只需要做一次）
echo ==========================================================
echo.

rem ---- 1. 检查 Python ----
python -c "import sys" >nul 2>nul
if errorlevel 1 goto nopython
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 goto oldpython
for /f "delims=" %%v in ('python -c "import sys; print(sys.version.split()[0])"') do set PYV=%%v
for /f "delims=" %%p in ('python -c "import sys; print(sys.executable)"') do set PYEXE=%%p
echo [1/3] 已找到 Python %PYV%
echo       位置：%PYEXE%
echo.

rem ---- 2. 安装依赖 ----
echo [2/3] 正在安装依赖（第一次约 3~10 分钟，取决于网速）...
python -m pip install --upgrade pip --disable-pip-version-check -q
python -m pip install -r server\requirements.txt --disable-pip-version-check
if errorlevel 1 (
  echo.
  echo 默认下载源安装失败（可能是网络慢或被拦截），改用清华镜像重试...
  python -m pip install -r server\requirements.txt --disable-pip-version-check -i https://pypi.tuna.tsinghua.edu.cn/simple
  if errorlevel 1 goto pipfail
)
echo.

rem ---- 3. 检查是否装好 ----
echo [3/3] 检查安装结果...
python -c "import fastapi, uvicorn, pandas, numpy, yaml, baostock, yfinance, exchange_calendars, requests" 2>nul
if errorlevel 1 goto pipfail
if not exist data mkdir data
echo.
echo ==========================================================
echo   安装完成！
echo   以后每次使用：双击「启动股票.vbs」（第一次运行会在桌面
echo   创建「Stock App」快捷方式，以后双击桌面图标即可）。
echo   第一次打开后，按软件里的提示到「设置」页初始化数据。
echo ==========================================================
echo.
if "%QUIET%"=="1" goto end
choice /c YN /m "现在就启动软件吗"
if errorlevel 2 goto end
start "" wscript "%~dp0启动股票.vbs"
goto end

:nopython
echo [错误] 没有找到可用的 Python。
echo.
echo   1. 打开 https://www.python.org/downloads/windows/
echo      下载 Python 3.12 的「Windows installer (64-bit)」并运行；
echo   2. 安装界面最下面一定要勾选「Add python.exe to PATH」，再点「Install Now」；
echo   3. 装好后关掉本窗口，重新双击「安装.bat」。
echo.
echo   如果已经装过 Python 还是提示这个（或者弹出微软商店）：
echo   打开 设置 → 应用 → 高级应用设置 → 应用执行别名
echo   （Windows 10 在 设置 → 应用 → 应用和功能 → 应用执行别名），
echo   把「python.exe」「python3.exe」两项关掉，再重新双击「安装.bat」。
echo.
if "%QUIET%"=="1" goto fail
choice /c YN /m "现在打开 Python 下载页吗"
if errorlevel 2 goto fail
start "" https://www.python.org/downloads/windows/
goto fail

:oldpython
echo [错误] Python 版本太旧，需要 3.10 或以上（推荐 3.12）。
echo   到 https://www.python.org/downloads/windows/ 下载 Python 3.12 安装，
echo   安装时勾选「Add python.exe to PATH」，再重新双击「安装.bat」。
goto fail

:pipfail
echo.
echo [错误] 依赖没有装好。可以这样排查：
echo   1. 检查网络后重新双击「安装.bat」（下载中断时重试一般就好）；
echo   2. 如果提示需要「Microsoft Visual C++」或编译失败：说明 Python 版本太新、
echo      还没有现成的安装包——卸载后改装 Python 3.12 再试；
echo   3. 公司网络有代理时，请在家里或手机热点下安装。
goto fail

:fail
echo.
if "%QUIET%"=="0" pause
endlocal
exit /b 1

:end
if "%QUIET%"=="0" pause
endlocal
exit /b 0
