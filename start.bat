@echo off
rem Foreground launcher (shows server log). Use  start.bat demo  for synthetic demo data.
cd /d "%~dp0"
if /i "%1"=="demo" (
  python -m server.cli serve --demo
) else (
  python -m server.main
)
