#!/bin/bash
# 后台启动脚本 - 适合服务器部署
# 使用方法: ./start_server.sh {start|stop|restart|status}

set -e

# 获取项目根目录（脚本所在目录的上一级）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SCRIPT_DIR"

# 配置
PID_FILE="prototype_generator.pid"
LOG_FILE="logs/server.log"
PYTHON_CMD="python3"

# 创建日志目录
mkdir -p logs

# 获取端口号
get_port() {
    python3 -c "import json; print(json.load(open('config.json'))['port'])" 2>/dev/null || echo "8080"
}

# 检查进程状态
check_status() {
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if ps -p "$PID" > /dev/null 2>&1; then
            echo "[运行中] PID: $PID, 端口: $(get_port)"
            return 0
        else
            rm -f "$PID_FILE"
            echo "[已停止] PID 文件存在但进程未运行"
            return 1
        fi
    else
        # 检查端口是否被占用
        PORT=$(get_port)
        if lsof -i :"$PORT" > /dev/null 2>&1 || netstat -tuln 2>/dev/null | grep -q ":$PORT "; then
            echo "[警告] 端口 $PORT 被占用，但无 PID 文件"
            return 2
        fi
        echo "[未运行]"
        return 1
    fi
}

# 启动服务
start_server() {
    if check_status > /dev/null 2>&1; then
        echo "[INFO] 服务已在运行中"
        check_status
        return 0
    fi

    echo "[INFO] 正在启动服务..."

    # 检查依赖
    if ! $PYTHON_CMD -c "import requests" 2>/dev/null; then
        echo "[WARN] 正在安装 requests..."
        pip3 install requests
    fi

    # 后台启动
    nohup $PYTHON_CMD server.py >> "$LOG_FILE" 2>&1 &
    PID=$!
    echo $PID > "$PID_FILE"

    sleep 2

    if ps -p "$PID" > /dev/null 2>&1; then
        echo "[成功] 服务已启动"
        echo "       PID: $PID"
        echo "       端口: $(get_port)"
        echo "       日志: $LOG_FILE"
        echo ""
        echo "访问地址: http://localhost:$(get_port)/src/index.html"
        echo "查看日志: tail -f $LOG_FILE"
        echo "停止服务: $0 stop"
    else
        echo "[失败] 服务启动失败，请查看日志: $LOG_FILE"
        rm -f "$PID_FILE"
        return 1
    fi
}

# 停止服务
stop_server() {
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if ps -p "$PID" > /dev/null 2>&1; then
            echo "[INFO] 正在停止服务 (PID: $PID)..."
            kill "$PID"
            sleep 2
            if ps -p "$PID" > /dev/null 2>&1; then
                echo "[WARN] 进程未响应，强制终止..."
                kill -9 "$PID"
            fi
            rm -f "$PID_FILE"
            echo "[成功] 服务已停止"
        else
            rm -f "$PID_FILE"
            echo "[INFO] 服务未运行"
        fi
    else
        echo "[INFO] 服务未运行"
    fi
}

# 重启服务
restart_server() {
    echo "[INFO] 正在重启服务..."
    stop_server
    sleep 1
    start_server
}

# 主逻辑
case "${1:-start}" in
    start)
        start_server
        ;;
    stop)
        stop_server
        ;;
    restart)
        restart_server
        ;;
    status)
        check_status
        ;;
    *)
        echo "用法: $0 {start|stop|restart|status}"
        echo ""
        echo "  start   - 启动服务（后台运行）"
        echo "  stop    - 停止服务"
        echo "  restart - 重启服务"
        echo "  status  - 查看运行状态"
        exit 1
        ;;
esac
