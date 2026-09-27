@echo off
rem feishu-card plugin installer launcher (ASCII only, no pause).
rem Keeps the window open so the result stays visible; close it yourself when done.
setlocal
set SCRIPT_DIR=%~dp0
powershell -NoProfile -ExecutionPolicy Bypass -NoExit -File "%SCRIPT_DIR%install-plugin.ps1" %*
endlocal
