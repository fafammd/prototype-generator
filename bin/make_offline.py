#!/usr/bin/env python3
"""
将 HTML 文件改为完全离线模式
- 使用系统字体替代 Google Fonts
- 使用内联 CSS 替代 Tailwind CDN
- 移除 Font Awesome（使用 emoji 或简单图标）
"""

import os
import re

# 项目根目录
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_offline_html(filepath):
    """将 HTML 文件改为离线模式"""
    print(f"处理: {os.path.relpath(filepath, BASE_DIR)}")

    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    # 备份
    backup_path = filepath + '.offline.bak'
    with open(backup_path, 'w', encoding='utf-8') as f:
        f.write(content)

    original = content

    # 1. 移除 Google Fonts
    content = re.sub(
        r'<link href="https://fonts\.googleapis\.com/[^"]*" rel="stylesheet">\s*',
        '',
        content
    )

    # 2. 移除 Font Awesome
    content = re.sub(
        r'<link rel="stylesheet" href="https://cdnjs\.cloudflare\.com/ajax/libs/font-awesome/[^"]*">\s*',
        '',
        content
    )

    # 3. 替换 Tailwind CDN 为内联样式（简化版）
    # 添加基础离线样式
    offline_style = '''
    <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; line-height: 1.6; }
    .flex { display: flex; }
    .flex-col { flex-direction: column; }
    .items-center { align-items: center; }
    .justify-center { justify-content: center; }
    .justify-between { justify-content: space-between; }
    .gap-1 { gap: 0.25rem; }
    .gap-2 { gap: 0.5rem; }
    .gap-3 { gap: 0.75rem; }
    .gap-4 { gap: 1rem; }
    .p-2 { padding: 0.5rem; }
    .p-3 { padding: 0.75rem; }
    .p-4 { padding: 1rem; }
    .px-3 { padding-left: 0.75rem; padding-right: 0.75rem; }
    .py-1 { padding-top: 0.25rem; padding-bottom: 0.25rem; }
    .py-2 { padding-top: 0.5rem; padding-bottom: 0.5rem; }
    .mt-2 { margin-top: 0.5rem; }
    .mt-4 { margin-top: 1rem; }
    .mb-2 { margin-bottom: 0.5rem; }
    .mb-4 { margin-bottom: 1rem; }
    .ml-2 { margin-left: 0.5rem; }
    .mr-2 { margin-right: 0.5rem; }
    .w-full { width: 100%; }
    .h-full { height: 100%; }
    .text-sm { font-size: 0.875rem; }
    .text-xs { font-size: 0.75rem; }
    .text-lg { font-size: 1.125rem; }
    .font-bold { font-weight: 700; }
    .font-medium { font-weight: 500; }
    .text-gray-400 { color: #9ca3af; }
    .text-gray-500 { color: #6b7280; }
    .text-gray-700 { color: #374151; }
    .text-blue-500 { color: #3b82f6; }
    .text-red-500 { color: #ef4444; }
    .text-green-500 { color: #22c55e; }
    .bg-white { background-color: white; }
    .bg-gray-50 { background-color: #f9fafb; }
    .bg-gray-100 { background-color: #f3f4f6; }
    .bg-blue-500 { background-color: #3b82f6; }
    .bg-red-50 { background-color: #fef2f2; }
    .rounded { border-radius: 0.25rem; }
    .rounded-lg { border-radius: 0.5rem; }
    .hover\\:bg-gray-100:hover { background-color: #f3f4f6; }
    .cursor-pointer { cursor: pointer; }
    .hidden { display: none; }
    .relative { position: relative; }
    .absolute { position: absolute; }
    .fixed { position: fixed; }
    .inset-0 { top: 0; right: 0; bottom: 0; left: 0; }
    .z-10 { z-index: 10; }
    .z-50 { z-index: 50; }
    </style>
    '''

    # 替换 Tailwind CDN
    content = re.sub(
        r'<script src="https://cdn\.tailwindcss\.com"></script>',
        offline_style,
        content
    )

    if content != original:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f"  ✓ 已修改，备份: {os.path.basename(backup_path)}")
        return True
    else:
        print(f"  - 无需修改")
        return False


def main():
    print("[INFO] 转换为离线模式")
    print("==============================")

    html_files = [
        os.path.join(BASE_DIR, 'src', 'index.html'),
        os.path.join(BASE_DIR, 'src', 'viewer.html'),
        os.path.join(BASE_DIR, 'src', 'viewer_standalone.html'),
    ]

    for filepath in html_files:
        if os.path.exists(filepath):
            make_offline_html(filepath)

    print("")
    print("[完成] 已转换为离线模式")
    print("==============================")
    print("恢复方法: mv src/*.offline.bak src/*.html")


if __name__ == '__main__':
    main()
