@echo off
rem Stop the services started from the offline bundle (docs/adr/046).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0app\deploy\windows\start-services.ps1" -Stop
pause
