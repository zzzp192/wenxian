@echo off
chcp 65001 >nul
echo ========================================================
echo       EFMAT 文献下载器 (Literature Downloader)
echo ========================================================
echo.

cd /d "%~dp0literature"

echo 正在启动 Web 服务...
echo 请在浏览器中访问: http://127.0.0.1:7860
echo 按 Ctrl+C 可停止服务
echo.

python app.py
pause
