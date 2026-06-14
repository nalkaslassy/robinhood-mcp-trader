@echo off
cd /d "%~dp0"
for /f "usebackq tokens=1,* delims==" %%a in (".env") do set "%%a=%%b"

set LOG_FILE=%~dp0logs\agent_%date:~-4,4%%date:~-10,2%%date:~-7,2%.txt
python -m trading_agent.main_agent >> "%LOG_FILE%" 2>&1
