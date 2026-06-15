@echo off
cd /d "%~dp0"
for /f "usebackq tokens=1,* delims==" %%a in (".env") do set "%%a=%%b"

set LOG_FILE=%~dp0logs\monitor_%date:~-4,4%%date:~-10,2%%date:~-7,2%.txt
C:\Python313\python.exe -m trading_agent.main_agent monitor >> "%LOG_FILE%" 2>&1
