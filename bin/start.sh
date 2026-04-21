#!/bin/bash
# 原型生成器 Linux 启动脚本

set -e

# 获取项目根目录（脚本所在目录的上一级）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SCRIPT_DIR"

echo "[INFO] 原型生成器启动脚本"
echo "=============================="

# 检查 Python 是否安装
if ! command -v python3 &> /dev/null; then
    echo "[ERROR] Python3 未安装，请先安装 Python 3.8+"
    echo "        Ubuntu/Debian: sudo apt install python3 python3-pip"
    echo "        CentOS/RHEL:   sudo yum install python3 python3-pip"
    exit 1
fi

PYTHON_VERSION=$(python3 --version | awk '{print $2}')
echo "[INFO] Python 版本: $PYTHON_VERSION"

# 检查 requests 依赖
if ! python3 -c "import requests" 2>/dev/null; then
    echo "[WARN] requests 模块未安装，正在安装..."
    pip3 install requests || {
        echo "[ERROR] 安装失败，请手动运行: pip3 install requests"
        exit 1
    }
fi

# 检查 config.json 是否存在
if [ ! -f "config.json" ]; then
    echo "[INFO] config.json 不存在，创建默认配置..."
    cat > config.json << EOF
{
    "port": 8080,
    "aiModel": "gpt-4o",
    "aiTemperature": 0.7,
    "aiMaxTokens": 4000
}
EOF
fi

# 启动服务器
echo "[INFO] 正在启动服务器..."
echo "[INFO] 访问地址: http://localhost:$(python3 -c "import json; print(json.load(open('config.json'))['port'])")/src/index.html"
echo ""
echo "按 Ctrl+C 停止服务器"
echo "=============================="

python3 server.py
