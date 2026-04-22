#!/bin/bash
# 快速切换到离线模式（无需下载）

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

echo "[INFO] 切换到离线模式..."
echo "=============================="

# 替换 src/index.html
sed -i.bak 's|<script src="static/js/tailwindcss.js"></script>|<link rel="stylesheet" href="static/css/offline.css">|' src/index.html
sed -i 's|<link href="https://fonts.googleapis.com/[^"]*" rel="stylesheet">||' src/index.html
sed -i 's|<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/[^"]*"||' src/index.html

# 替换 src/viewer.html
sed -i.bak 's|<script src="static/js/tailwindcss.js"></script>|<link rel="stylesheet" href="static/css/offline.css">|' src/viewer.html
sed -i 's|<link href="https://fonts.googleapis.com/[^"]*" rel="stylesheet">||' src/viewer.html
sed -i 's|<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/[^"]*"||' src/viewer.html

# 替换 src/viewer_standalone.html
sed -i.bak 's|<script src="../static/js/tailwindcss.js"></script>|<link rel="stylesheet" href="../static/css/offline.css">|' src/viewer_standalone.html
sed -i 's|<link href="https://fonts.googleapis.com/[^"]*" rel="stylesheet">||' src/viewer_standalone.html
sed -i 's|<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/[^"]*"||' src/viewer_standalone.html

echo "✓ 已切换到离线模式"
echo "=============================="
echo "恢复方法: mv src/*.bak src/*.html"
