@echo off
:: 1. 进入项目根目录（脚本所在目录的上一级）
cd /d "%~dp0.."

echo [INFO] 正在启动服务...

:: 2. 自动打开浏览器
start http://localhost:8080/src/index.html

:: 3. 启动服务器 (使用系统 PATH 中的 Python)
python server.py

pause
