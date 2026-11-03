@echo off
rem Offline installer entry (docs/adr/046). Copied to the bundle root by deploy\offline\pack.ps1.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0app\deploy\offline\install-offline.ps1" %*
pause
