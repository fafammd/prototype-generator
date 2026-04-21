@echo off
:: 下载 CDN 资源到本地（解决跨域问题）

cd /d "%~dp0.."

echo [INFO] 下载 CDN 资源到本地...
echo ==============================

:: 创建目录
if not exist "static\css" mkdir "static\css"
if not exist "static\webfonts" mkdir "static\webfonts"
if not exist "static\js" mkdir "static\js"

:: 1. 下载 Font Awesome CSS
echo [1/3] 下载 Font Awesome CSS...
if not exist "static\css\all.min.css" (
    curl -L -o static\css\all.min.css https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css
) else (
    echo [1/3] Font Awesome CSS 已存在^，跳过
)

:: 2. 下载 Font Awesome 字体文件
echo [2/3] 下载 Font Awesome 字体文件...
if not exist "static\webfonts\fa-solid-900.woff2" (
    curl -L -o static\webfonts\fa-solid-900.woff2 https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/webfonts/fa-solid-900.woff2
    curl -L -o static\webfonts\fa-regular-400.woff2 https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/webfonts/fa-regular-400.woff2
    curl -L -o static\webfonts\fa-brands-400.woff2 https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/webfonts/fa-brands-400.woff2
    curl -L -o static\webfonts\fa-v4compatibility.woff2 https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/webfonts/fa-v4compatibility.woff2
) else (
    echo [2/3] Font Awesome 字体文件已存在^，跳过
)

:: 3. 下载 Tailwind CSS
echo [3/3] 下载 Tailwind CSS（开发版）...
if not exist "static\js\tailwindcss.js" (
    curl -L -o static\js\tailwindcss.js https://cdn.tailwindcss.com
) else (
    echo [3/3] Tailwind CSS 已存在^，跳过
)

echo.
echo [完成] CDN 资源已下载到 static\ 目录
echo ==============================
echo 提示：运行 python bin\patch_html.py 来更新 HTML 文件引用

pause
