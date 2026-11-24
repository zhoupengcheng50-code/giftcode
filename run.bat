@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -B "%~dp0web_server.py" --open
pause
