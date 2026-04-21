#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""将 HTML 文件中的 CDN 链接替换为本地资源"""

import os
import re
import sys

# 设置输出编码为 UTF-8
if sys.platform == 'win32':
    import codecs
    sys.stdout = codecs.getwriter('utf-8')(sys.stdout.buffer, 'strict')
    sys.stderr = codecs.getwriter('utf-8')(sys.stderr.buffer, 'strict')

# 项目根目录
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 替换规则
REPLACEMENTS = [
    # Google Fonts -> 完全移除（使用系统字体）
    (r'<link[^>]*href="https://fonts\.googleapis\.com/[^"]*"[^>]*>', ''),

    # Font Awesome
    (r'<link[^>]*href="https://cdnjs\.cloudflare\.com/ajax/libs/font-awesome/[^"]*"[^>]*>', ''),

    # Tailwind CSS
    (r'<script src="https://cdn\.tailwindcss\.com"></script>',
     '<script src="static/js/tailwindcss.js"></script>'),
]

def patch_html_file(filepath):
    """修补单个 HTML 文件"""
    print(f"  修补: {os.path.relpath(filepath, BASE_DIR)}")

    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    original_content = content

    for pattern, replacement in REPLACEMENTS:
        content = re.sub(pattern, replacement, content)

    if content != original_content:
        # 备份原文件
        backup_path = filepath + '.bak'
        with open(backup_path, 'w', encoding='utf-8') as f:
            f.write(original_content)

        # 写入修改后的内容
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)

        print(f"    [OK] 已创建备份: {os.path.basename(backup_path)}")
        return True
    else:
        print(f"    - 无需修改")
        return False

def main():
    print("[INFO] 修补 HTML 文件使用本地资源")
    print("==============================")

    html_files = [
        os.path.join(BASE_DIR, 'src', 'index.html'),
        os.path.join(BASE_DIR, 'src', 'viewer.html'),
        os.path.join(BASE_DIR, 'src', 'viewer_standalone.html'),
    ]

    modified_count = 0
    for filepath in html_files:
        if os.path.exists(filepath):
            if patch_html_file(filepath):
                modified_count += 1
        else:
            print(f"  跳过: {filepath} (文件不存在)")

    print("")
    print(f"[完成] 已修改 {modified_count} 个文件")
    print("==============================")
    print("提示：如需恢复，可使用 .bak 备份文件")

if __name__ == '__main__':
    main()
