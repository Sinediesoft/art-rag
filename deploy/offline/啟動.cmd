@echo off
rem Start all services from the offline bundle (docs/adr/046), then open the browser.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0app\deploy\windows\start-services.ps1" -SkipOrtho2cad -SkipScheduler
if %errorlevel%==0 start "" http://127.0.0.1:8000
pause
