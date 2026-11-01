@echo off
chcp 65001 >nul
title ATN service (close this window to stop)
cd /d "%~dp0"
echo Starting service on http://127.0.0.1:8000 ... close this window to stop it.
python -m backend
echo.
echo Service stopped. Press any key to close.
pause >nul
