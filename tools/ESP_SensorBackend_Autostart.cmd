@echo off
rem ===========================================================================
rem  ESP_SensorBackend_Autostart.cmd
rem  Auto-start the sensor backend at logon (no admin rights required).
rem
rem  This file is placed in the Windows Startup folder.
rem  The real supervisor logic lives in tools\run_backend_forever.py
rem  (auto-restart on crash + log rotation).
rem
rem  Manual start : double-click this file
rem  Stop         : .venv\Scripts\python.exe tools\stop_backend.py
rem  Log          : evidence\backend_run.log
rem
rem  NOTE: keep this file pure ASCII. cmd.exe reads .cmd as OEM codepage
rem        (GBK on zh-CN), so UTF-8 Chinese comments garble into commands.
rem ===========================================================================
setlocal

set "ROOT=D:\ESP"
set "PYW=%ROOT%\.venv\Scripts\pythonw.exe"
set "PY=%ROOT%\.venv\Scripts\python.exe"
set "GUARD=%ROOT%\tools\run_backend_forever.py"

if not exist "%PYW%" set "PYW=%PY%"
if not exist "%PYW%" exit /b 1
if not exist "%GUARD%" exit /b 1

rem /b detaches the supervisor so it keeps running after this script exits
start "ESP-SensorBackend" /b "%PYW%" "%GUARD%"

exit /b 0