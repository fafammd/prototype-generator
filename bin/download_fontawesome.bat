@echo off
cd /d "%~dp0.."

echo [INFO] 下载 Font Awesome 到本地...
echo ==============================

if not exist "static\css" mkdir "static\css"
if not exist "static\webfonts" mkdir "static\webfonts"

echo [1/2] 下载 Font Awesome CSS...
curl -L -o static\css\all.min.css https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css

echo [2/2] 下载字体文件...
curl -L -o static\webfonts\fa-solid-900.woff2 https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/webfonts/fa-solid-900.woff2
curl -L -o static\webfonts\fa-regular-400.woff2 https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/webfonts/fa-regular-400.woff2
curl -L -o static\webfonts\fa-brands-400.woff2 https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/webfonts/fa-brands-400.woff2
curl -L -o static\webfonts\fa-v4compatibility.woff2 https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/webfonts/fa-v4compatibility.woff2

echo.
echo [完成] Font Awesome 已下载到 static\ 目录
echo ==============================

pause
