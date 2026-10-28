@echo off
cd /d "%~dp0"
rem Safe defaults: local browser only. Add LAN settings to the ignored start-sage.local.bat file.
set "SAGE_HOST=127.0.0.1"
set "SAGE_PORT=8001"
set "SAGE_VOICE_PORT=8002"
if exist "%~dp0start-sage.local.bat" call "%~dp0start-sage.local.bat"
python -m sage
if errorlevel 1 pause
