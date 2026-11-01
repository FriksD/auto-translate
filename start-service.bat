@echo off
chcp 65001 >nul
title ATN service (close this window to stop)
cd /d "%~dp0"

rem 在线精校地址（设置页「在线精校地址」）：配置了就一起启动 ngrok，输出写到 data\ngrok.log
set "DOMAIN="
for /f "delims=" %%i in ('python -c "from backend import db; print((db.get_settings()['feishu'].get('public_url') or '').replace('https://','').replace('http://','').strip('/'))" 2^>nul') do set "DOMAIN=%%i"
if defined DOMAIN (
    echo Starting ngrok: %DOMAIN%
    start /b "" ngrok http 8000 --url https://%DOMAIN% >data\ngrok.log 2>&1
) else (
    echo No public_url set, skipping ngrok.
)

echo Starting service on http://127.0.0.1:8000 ... close this window to stop it.
python -m backend

echo.
echo Service stopped. Press any key to close.
pause >nul
