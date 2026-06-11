# -*- coding: utf-8 -*-
"""
原型生成器后端服务
- 提供静态文件服务
- 处理图片上传
- 调用AI大模型生成原型
- 管理项目文件
- 下载外部图片到本地
"""

import http.server
import socketserver
import os
import json
import re
import datetime
import urllib.request
import urllib.parse
import base64
import ssl
import hashlib
import requests # Add requests import
import subprocess
import io
import zipfile
from PIL import Image
import tempfile
import shlex
import threading
import time
import sys
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib3

# 上下文工程模块（多轮生成）
from server_context_engineering import (
    determine_strategy, MultiRoundGenerator, estimate_tokens,
    should_skip_spec_round, build_spec_prompt, extract_spec_from_response,
    build_spec_summary_for_page, estimate_messages_tokens,
    get_nav_visible_indices
)

# ==================== 日志配置 ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger('prototype')

# ==================== 预编译正则（性能优化） ====================
# 常用 HTML 标签清理（合并 script/svg/noscript 为单个模式）
_RE_REMOVE_TAGS = re.compile(
    r'<(script|svg|noscript)[^>]*>.*?</\1>',
    re.DOTALL | re.IGNORECASE
)
# style 标签清理
_RE_REMOVE_STYLE = re.compile(
    r'<style[^>]*>.*?</style>',
    re.DOTALL | re.IGNORECASE
)
# SVG 替换（保留占位符）
_RE_SVG_PLACEHOLDER = re.compile(
    r'<svg[^>]*>.*?</svg>',
    re.DOTALL | re.IGNORECASE
)
# data URL 图片替换
_RE_DATA_IMG = re.compile(
    r'<img[^>]*src=["\']data:image/[^"\']*["\'][^>]*>',
    re.IGNORECASE
)
# 外部图片 URL 匹配
_RE_EXT_IMG = re.compile(
    r'<img[^>]*src=["\'][^"\']{200,}["\'][^>]*>',
    re.IGNORECASE
)
# 图片 src 提取
_RE_IMG_SRC = re.compile(
    r'src=["\']?(https?://[^"\'>\s]+\.(?:jpg|jpeg|png|gif|webp|svg)[^"\'>\s]*)["\']?',
    re.IGNORECASE
)
# CSS: @font-face 移除
_RE_FONT_FACE = re.compile(
    r'@font-face\s*\{[^}]*\}',
    re.DOTALL | re.IGNORECASE
)
# CSS: data URL 替换
_RE_CSS_DATA_URL = re.compile(
    r'url\(data:[^)]*\)',
    re.IGNORECASE
)
# CSS: 版权注释移除
_RE_CSS_COMMENT = re.compile(r'/\*![\s\S]*?\*/')
# CSS: 空行合并
_RE_BLANK_LINES = re.compile(r'\n\s*\n')
# 空白归一化（edit_file 归一化匹配用）
_norm_re = re.compile(r'\s+')
# 第三方库 CSS 前缀（合并为单个正则）
_RE_THIRD_PARTY_CSS = re.compile(
    r'[^{}]*\.(?:ql-[\w-]+|monaco[\w-]*|CodeMirror[\w-]*|cm-[\w-]+'
    r'|katex[\w-]*|hljs[\w-]*|swiper[\w-]*|cropper[\w-]*|video-js[\w-]*)'
    r'[^{}]*\{[^{}]*\}',
    re.IGNORECASE
)
# style 标签内容提取
_RE_STYLE_BLOCKS = re.compile(
    r'<style[^>]*>(.*?)</style>',
    re.DOTALL | re.IGNORECASE
)
# body 内容提取
_RE_BODY = re.compile(
    r'<body[^>]*>(.*)</body>',
    re.DOTALL | re.IGNORECASE
)
# CSP meta 标签移除
_RE_CSP_META = re.compile(
    r'<meta[^>]*http-equiv=["\']?content-security-policy["\']?[^>]*>',
    re.IGNORECASE
)
# sandbox 属性移除
_RE_SANDBOX = re.compile(
    r'\s*\bsandbox=["\'][^"\']*["\']',
    re.IGNORECASE
)
# 外部链接替换
_RE_EXT_HREF = re.compile(
    r'href=(["\']?)https?://[^"\s>]+\1',
    re.IGNORECASE
)
# IE 条件注释移除
_RE_IE_COND = re.compile(
    r'<!--\[if lt IE [\d]+\]>.*?<!\[endif\]-->',
    re.DOTALL | re.IGNORECASE
)
# location.href 跳转脚本移除
_RE_LOCATION_HREF = re.compile(
    r"window\.location\.href\s*=\s*['\"][^'\"]*['\"]",
    re.IGNORECASE
)
# img 属性提取
_RE_IMG_ALT = re.compile(r'alt=["\']([^"\']*)["\']', re.IGNORECASE)
_RE_IMG_WIDTH = re.compile(r'width=["\']([^"\']*)["\']', re.IGNORECASE)
_RE_IMG_HEIGHT = re.compile(r'height=["\']([^"\']*)["\']', re.IGNORECASE)

# ==================== PyInstaller 兼容 ====================
def get_base_path():
    """获取应用根目录（兼容 PyInstaller 打包）"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

# 切换工作目录到应用根目录
os.chdir(get_base_path())

# ==================== 异步任务管理 ====================
generating_tasks = {}  # {project_id: {status, progress, error, thread}}
import_tasks = {}  # {task_id: {status, progress, error, data}}
tasks_lock = threading.Lock()  # 线程锁

# 状态常量
STATUS_PENDING = 'pending'
STATUS_GENERATING = 'generating'
STATUS_COMPLETED = 'completed'
STATUS_FAILED = 'failed'

# ==================== 模型配置 ====================
MODELS_FILE = 'models.json'

def load_models():
    """加载模型配置文件"""
    if not os.path.exists(MODELS_FILE):
        # 创建默认模型配置
        default_models = {
            "models": [{
                "id": "default",
                "name": "Default Model",
                "provider": "",
                "base_url": "",
                "api_key": "YOUR_API_KEY_HERE",
                "model": "gpt-4"
            }],
            "selected_model_id": "default"
        }
        save_models(default_models)
        return default_models
    with open(MODELS_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_models(data):
    """保存模型配置文件"""
    with open(MODELS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def get_selected_model():
    """获取当前选中的模型配置"""
    data = load_models()
    selected_id = data.get('selected_model_id', '')
    for m in data.get('models', []):
        if m['id'] == selected_id:
            return m
    # 兜底返回第一个
    models = data.get('models', [])
    return models[0] if models else None

# ==================== 配置加载 ====================
CONFIG_FILE = 'config.json'

def load_config():
    """加载配置文件"""
    if not os.path.exists(CONFIG_FILE):
        logger.info("=" * 60)
        logger.error("❌ 错误: 配置文件 config.json 不存在!")
        logger.info("")
        logger.info("请创建 config.json 文件，内容格式如下:")
        logger.info(json.dumps({
            "server": {
                "port": 8080
            },
            "ai_options": {
                "max_tokens": 100000,
                "temperature": 0.7,
                "timeout": 300,
                "system_prompt": "You are a professional UI/UX Developer."
            }
        }, indent=2, ensure_ascii=False))
        logger.info("")
        logger.info("模型配置请在 models.json 或页面「管理模型」中设置")
        logger.info("=" * 60)
        raise FileNotFoundError("config.json 不存在，请创建配置文件")
    
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        config = json.load(f)
    
    # 验证模型配置（从 models.json）
    try:
        selected = get_selected_model()
        if not selected or not selected.get('api_key') or selected.get('api_key') == 'YOUR_API_KEY_HERE':
            logger.info("=" * 60)
            logger.warning("⚠️ 警告: 请在 models.json 中配置有效的 API 密钥!")
            logger.info("   或启动后在页面顶栏「管理模型」中配置")
            logger.info("=" * 60)
        else:
            logger.info(f"[INFO] 当前模型: {selected.get('name', '未命名')}")
    except Exception as e:
        logger.info("=" * 60)
        logger.warning(f"⚠️ 警告: models.json 加载失败，请检查文件格式 ({e})")
        logger.info("=" * 60)
    
    return config

# 加载配置
CONFIG = load_config()

# 从配置文件读取设置
PORT = CONFIG.get('server', {}).get('port', 8080)
API_CONFIG = CONFIG.get('api', {})
AI_OPTIONS = CONFIG.get('ai_options', {
    'max_tokens': 100000,
    'temperature': 0.7,
    'timeout': 300,
    'system_prompt': 'You are a professional UI/UX Developer. Generate complete, standalone HTML prototypes with realistic data.'
})

# 上下文工程配置（多轮生成策略）
CONTEXT_ENGINEERING_CONFIG = CONFIG.get('context_engineering', {
    'enabled': True,
    'single_page_token_threshold': 40000,
    'multi_page_token_threshold': 80000,
    'force_strategy': 'auto'
})


UPLOAD_DIR = 'uploads'
DATA_DIR = 'data'
PROJECTS_DIR = 'projects'
DELETED_DIR = 'deleted'
PROJECTS_FILE = os.path.join(DATA_DIR, 'projects.json')
DELETED_PROJECTS_FILE = os.path.join(DATA_DIR, 'deleted_projects.json')

# 创建必要的目录
for dir_path in [UPLOAD_DIR, DATA_DIR, PROJECTS_DIR, DELETED_DIR]:
    if not os.path.exists(dir_path):
        os.makedirs(dir_path)

if not os.path.exists(PROJECTS_FILE):
    with open(PROJECTS_FILE, 'w', encoding='utf-8') as f:
        json.dump([], f, ensure_ascii=False, indent=2)

if not os.path.exists(DELETED_PROJECTS_FILE):
    with open(DELETED_PROJECTS_FILE, 'w', encoding='utf-8') as f:
        json.dump([], f, ensure_ascii=False, indent=2)


def chinese_to_pinyin(text):
    """将中文转换为拼音（简化版，只保留英文和数字）"""
    # 简单处理：保留英文字母和数字，去掉中文和特殊字符
    result = re.sub(r'[^\w\s-]', '', text)
    result = re.sub(r'[\s]+', '_', result)
    
    # 如果结果为空或只有下划线，使用默认名称
    if not result or result == '_':
        return 'project'
    
    # 如果包含中文，尝试使用简单映射（常用词）
    chinese_map = {
        '首页': 'home', '登录': 'login', '注册': 'register',
        '作业': 'homework', '列表': 'list', '详情': 'detail',
        '用户': 'user', '设置': 'settings', '个人': 'profile',
        '管理': 'manage', '系统': 'system', '数据': 'data',
        '分析': 'analysis', '报告': 'report', '统计': 'stats',
        '订单': 'order', '商品': 'product', '购物': 'shopping',
        '消息': 'message', '通知': 'notice', '搜索': 'search',
        '批改': 'grading', '智能': 'smart', 'AI': 'ai',
        '学生': 'student', '老师': 'teacher', '课程': 'course',
        '考试': 'exam', '成绩': 'score', '答案': 'answer',
    }
    
    for cn, en in chinese_map.items():
        result = result.replace(cn, en)
    
    # 移除剩余的非ASCII字符
    result = re.sub(r'[^\x00-\x7F]+', '', result)
    result = re.sub(r'_+', '_', result)  # 合并多个下划线
    result = result.strip('_')
    
    return result if result else 'project'


def generate_project_id(project_name):
    """生成项目文件夹名称：项目名_年月日_时间"""
    # 时间格式: 20260114_4-15-23pm
    now = datetime.datetime.now()
    hour = now.hour
    am_pm = 'am' if hour < 12 else 'pm'
    hour_12 = hour if hour <= 12 else hour - 12
    if hour_12 == 0:
        hour_12 = 12
    timestamp = now.strftime(f'%Y%m%d_{hour_12}-%M-%S{am_pm}')
    
    # 保留中文名称，但替换不安全字符
    safe_name = re.sub(r'[\\/:*?"<>|]', '', project_name)  # 移除Windows不允许的字符
    safe_name = safe_name.replace(' ', '_')
    # 限制长度
    if len(safe_name) > 30:
        safe_name = safe_name[:30]
    
    return f"{safe_name}_{timestamp}"


def _push_sse_event(project_id, event_type, data):
    """向 SSE 流推送结构化事件（不需要 MultiRoundGenerator 实例）。"""
    event = json.dumps({
        'type': event_type,
        'data': data,
        'timestamp': time.time()
    }, ensure_ascii=False)

    with tasks_lock:
        task = generating_tasks.get(project_id)
        if task:
            sl = task.get('stream_lock')
            if sl:
                with sl:
                    task['stream_chunks'].append(event)
            se = task.get('stream_event')
            if se:
                se.set()
        else:
            logger.warning(f"[SSE] 推送失败: 任务 {project_id[:30]} 不存在")


# ==================== 流式实时预览：HTML 提取与推送 ====================

STREAMING_HTML_THRESHOLD = 400  # 每积累 400 字符尝试一次提取（降低以加速实时预览）

def _extract_streaming_html(accumulated_content, last_extract_len=0):
    """从流式累积的 AI 文本中提取可渲染的 HTML 片段（轻量级，用于实时预览）。

    与 extract_html() 的区别：
    - 容忍未闭合的 ``` 代码块（流式输出中 ``` 可能还没出现）
    - 容忍未闭合的 </html>（HTML 可能还在生成中）
    - 不做 fallback_error_page
    - 维护 last_extract_len 避免重复扫描

    Returns:
        (html_or_None, updated_last_extract_len)
    """
    if not accumulated_content or len(accumulated_content) < 100:
        return None, last_extract_len

    # 去除 [think]...[/think] 思考内容和未闭合的 [think] 块
    cleaned = accumulated_content.replace('\[think\]', '[think]').replace('\[/think\]', '[/think]')
    import re as _re
    cleaned = _re.sub(r'\[think\][\s\S]*?\[/think\]', '', cleaned)
    cleaned = _re.sub(r'\[think\][\s\S]*$', '', cleaned)

    # 策略 0：<artifact> 标签提取（优先级最高）
    # 闭合的 <artifact>...</artifact>
    artifact_match = _re.search(r'<artifact[^>]*>([\s\S]*?)</artifact>', cleaned, _re.IGNORECASE)
    if artifact_match:
        html = artifact_match.group(1).strip()
        if html:
            return html, len(accumulated_content)

    # 未闭合的 <artifact>（流式输出中，还没有 </artifact>）
    artifact_open = _re.search(r'<artifact[^>]*>([\s\S]+)$', cleaned, _re.IGNORECASE)
    if artifact_open:
        html = artifact_open.group(1).strip()
        if len(html) > 200:
            return html, len(accumulated_content)

    # 策略 1：闭合的 ```html 代码块
    for marker in ('```html', '```HTML'):
        idx = cleaned.find(marker)
        if idx == -1:
            continue
        start = cleaned.find('\n', idx) + 1
        if start == 0:
            start = idx + len(marker)
        # 找闭合的 ```
        end = cleaned.find('```', start)
        if end > start:
            html = cleaned[start:end].strip()
            if ('<!DOCTYPE html>' in html or '<html' in html or
                    ('<div' in html and len(html) > 200)):
                return html, len(accumulated_content)

    # 策略 2：未闭合的 ```html 代码块（流式输出中，代码块还没结束）
    for marker in ('```html', '```HTML'):
        idx = cleaned.rfind(marker)
        if idx == -1:
            continue
        start = cleaned.find('\n', idx) + 1
        if start == 0:
            start = idx + len(marker)
        html = cleaned[start:].strip()
        if len(html) > 200 and ('<html' in html or '<!DOCTYPE' in html or
                                 ('<div' in html and '<style' in html)):
            return html, len(accumulated_content)

    # 策略 3：完整的 <!DOCTYPE html>...</html>
    doctype_idx = cleaned.find('<!DOCTYPE html>')
    if doctype_idx != -1:
        end_idx = cleaned.rfind('</html>')
        if end_idx > doctype_idx:
            return cleaned[doctype_idx:end_idx + 7], len(accumulated_content)
        # 未完成的 HTML（没有 </html>）— 取从 <!DOCTYPE 到尾部
        html = cleaned[doctype_idx:]
        if len(html) > 300:
            return html, len(accumulated_content)

    # 策略 4：直接以 <html 开头的内容（无 DOCTYPE）
    html_idx = cleaned.find('<html')
    if html_idx != -1:
        end_idx = cleaned.rfind('</html>')
        if end_idx > html_idx:
            return cleaned[html_idx:end_idx + 7], len(accumulated_content)
        html = cleaned[html_idx:]
        if len(html) > 300:
            return html, len(accumulated_content)

    return None, last_extract_len


def _maybe_push_streaming_html(project_id, accumulated_content, last_push_len, page_name):
    """如果累积了足够的新内容，提取 HTML 并推送 streaming_html 事件。

    参考 Open Design 的流式 artifact 解析模式：
    服务端做 HTML 提取，前端只管渲染。

    Returns:
        更新后的 last_push_len
    """
    if not project_id or not accumulated_content:
        return last_push_len

    content_len = len(accumulated_content)
    if content_len - last_push_len < STREAMING_HTML_THRESHOLD:
        return last_push_len

    html, new_len = _extract_streaming_html(accumulated_content, last_push_len)
    if html and len(html) > 50:
        _push_sse_event(project_id, 'streaming_html', {
            'html': html,
            'page': page_name or ''
        })
        return new_len

    return content_len  # 提取失败也推进位置，避免反复扫描同样内容


def extract_title_from_html(html_content):
    """从HTML中提取title标签的内容"""
    match = re.search(r'<title[^>]*>([^<]+)</title>', html_content, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return None


def inject_missing_css_variables(html_content, css_variables):
    """检测 HTML 中使用的 CSS 变量是否已定义，将缺失的变量注入到 <style> 中。

    Args:
        html_content: 页面 HTML 内容
        css_variables: 设计系统的 :root CSS 变量文本（含 :root { ... } 块）
    Returns:
        修复后的 HTML（如无需修复则原样返回）
    """
    if not css_variables or not html_content:
        return html_content

    # 1. 提取 HTML 中已定义的 CSS 变量
    defined_vars = set(re.findall(r'--([a-zA-Z0-9_-]+)\s*:', html_content))

    # 2. 提取 HTML 中使用 var(--xxx) 的变量
    used_vars = set(re.findall(r'var\(--([a-zA-Z0-9_-]+)\)', html_content))

    # 3. 计算缺失的变量
    missing_vars = used_vars - defined_vars
    if not missing_vars:
        return html_content

    # 4. 从设计系统中提取缺失变量的定义
    ds_var_defs = {}
    for m in re.finditer(r'--([a-zA-Z0-9_-]+)\s*:\s*([^;]+);', css_variables):
        ds_var_defs[m.group(1)] = m.group(2).strip()

    inject_vars = {k: v for k, v in ds_var_defs.items() if k in missing_vars}
    if not inject_vars:
        return html_content

    # 5. 构建 :root 注入块
    inject_block = ':root {\n'
    for k, v in sorted(inject_vars.items()):
        inject_block += f'  --{k}: {v};\n'
    inject_block += '}\n'

    # 6. 注入到第一个 <style> 标签中
    style_match = re.search(r'(<style[^>]*>)', html_content, re.IGNORECASE)
    if style_match:
        insert_pos = style_match.end()
        html_content = html_content[:insert_pos] + '\n' + inject_block + html_content[insert_pos:]
    else:
        # 没有 <style> 标签，在 <head> 中创建
        head_match = re.search(r'<head[^>]*>', html_content, re.IGNORECASE)
        if head_match:
            insert_pos = head_match.end()
            html_content = html_content[:insert_pos] + '\n<style>\n' + inject_block + '</style>\n' + html_content[insert_pos:]

    logger.info(f"[CSS注入] 补全 {len(inject_vars)} 个缺失的 CSS 变量: {', '.join(sorted(inject_vars.keys())[:5])}{'...' if len(inject_vars) > 5 else ''}")
    return html_content


def download_image(url, save_folder, filename=None):
    """下载图片到本地"""
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        
        req = urllib.request.Request(url)
        req.add_header('User-Agent', 'Mozilla/5.0')
        
        with urllib.request.urlopen(req, context=ctx, timeout=30) as response:
            content_type = response.headers.get('Content-Type', '')
            data = response.read()
            
            # 确定文件扩展名
            if not filename:
                # 从URL或content-type推断
                ext = '.jpg'
                if 'png' in content_type or url.endswith('.png'):
                    ext = '.png'
                elif 'gif' in content_type or url.endswith('.gif'):
                    ext = '.gif'
                elif 'webp' in content_type or url.endswith('.webp'):
                    ext = '.webp'
                
                # 用URL的hash作为文件名
                url_hash = hashlib.md5(url.encode()).hexdigest()[:10]
                filename = f"img_{url_hash}{ext}"
            
            save_path = os.path.join(save_folder, filename)
            with open(save_path, 'wb') as f:
                f.write(data)
            
            return filename
    except Exception as e:
        logger.error(f"[图片下载失败] {url}: {e}")
        return None


def save_base64_image(base64_data, save_folder, filename):
    """保存base64图片到本地"""
    try:
        # 移除data:image/xxx;base64,前缀
        if ',' in base64_data:
            header, data = base64_data.split(',', 1)
            # 从header推断扩展名
            if 'png' in header:
                ext = '.png'
            elif 'gif' in header:
                ext = '.gif'
            elif 'webp' in header:
                ext = '.webp'
            else:
                ext = '.jpg'
        else:
            data = base64_data
            ext = '.jpg'
        
        # 确保文件名有正确扩展名
        if not any(filename.endswith(e) for e in ['.jpg', '.png', '.gif', '.webp']):
            filename = filename + ext
        
        save_path = os.path.join(save_folder, filename)
        with open(save_path, 'wb') as f:
            f.write(base64.b64decode(data))
        
        return filename
    except Exception as e:
        logger.error(f"[Base64图片保存失败] {filename}: {e}")
        return None


def download_html_images(html_content, save_folder):
    """下载HTML中的所有外部图片并替换URL（并行下载）"""
    images_folder = os.path.join(save_folder, 'images')
    os.makedirs(images_folder, exist_ok=True)

    # 使用预编译正则匹配图片 URL
    matches = _RE_IMG_SRC.findall(html_content)
    if not matches:
        return html_content

    # 去重
    unique_urls = list(dict.fromkeys(matches))
    logger.info(f"[下载] 发现 {len(unique_urls)} 张外部图片，开始并行下载")

    # 并行下载
    url_map = {}
    with ThreadPoolExecutor(max_workers=6) as executor:
        future_to_url = {
            executor.submit(download_image, url, images_folder): url
            for url in unique_urls
        }
        for future in as_completed(future_to_url):
            url = future_to_url[future]
            try:
                filename = future.result()
                if filename:
                    url_map[url] = f"images/{filename}"
                    logger.info(f"[下载] {url} -> {filename}")
            except Exception as e:
                logger.error(f"[下载失败] {url}: {e}")

    # 替换 URL
    for old_url, new_path in url_map.items():
        html_content = html_content.replace(old_url, new_path)

    return html_content


def parse_document(file_path, file_type):
    """解析文档文件，提取文本和图片。

    Returns:
        dict: {
            'text': str,           # 纯文本内容（图片位置用 [图片N] 标记）
            'images': list[dict]   # [{'name': str, 'base64': str, 'para_index': int}]
        }
    """
    result = {'text': '', 'images': []}

    if file_type == 'docx':
        try:
            from docx import Document
            import zipfile as zf

            doc = Document(file_path)

            # Step 1: 从 ZIP 中提取所有图片原始数据
            image_blobs = {}
            mime_map = {
                '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                '.gif': 'image/gif', '.bmp': 'image/bmp',
                '.webp': 'image/webp', '.svg': 'image/svg+xml',
                '.tiff': 'image/tiff', '.tif': 'image/tiff', '.emf': 'image/x-emf',
                '.wmf': 'image/x-wmf',
            }
            with zf.ZipFile(file_path, 'r') as z:
                for name in z.namelist():
                    if name.startswith('word/media/') and not name.endswith('/'):
                        ext = os.path.splitext(name)[1].lower()
                        mime = mime_map.get(ext, 'image/png')
                        image_blobs[os.path.basename(name)] = {'data': z.read(name), 'mime': mime}

            # Step 2: 建立 rel_id -> 图片名称映射
            ns_a = 'http://schemas.openxmlformats.org/drawingml/2006/main'
            ns_r = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
            rel_id_to_name = {}
            for rel_id, rel in doc.part.rels.items():
                if rel.target_ref.startswith('media/'):
                    rel_id_to_name[rel_id] = os.path.basename(rel.target_ref)

            # Step 3: 遍历段落，收集文本和图片位置
            entries = []  # [(text, [img_name, ...]), ...]
            image_count = 0

            for para_idx, para in enumerate(doc.paragraphs):
                # 查找段落中的图片引用
                img_names = []
                for elem in para._element.iter():
                    for blip in elem.findall(f'{{{ns_a}}}blip'):
                        embed = blip.get(f'{{{ns_r}}}embed')
                        if embed and embed in rel_id_to_name:
                            img_names.append(rel_id_to_name[embed])

                text = para.text.strip()
                if text or img_names:
                    # 为图片生成 base64 和占位标记
                    markers = []
                    for img_name in img_names:
                        if img_name in image_blobs:
                            image_count += 1
                            blob = image_blobs[img_name]
                            b64 = base64.b64encode(blob['data']).decode('utf-8')
                            marker = f'[图片{image_count}]'
                            result['images'].append({
                                'name': img_name,
                                'base64': f"data:{blob['mime']};base64,{b64}",
                                'para_index': para_idx,
                                'marker': marker
                            })
                            markers.append(marker)
                    entries.append((text, markers))

            # 表格
            for table in doc.tables:
                for row in table.rows:
                    row_text = " | ".join([cell.text.strip() for cell in row.cells])
                    if row_text.strip():
                        entries.append((row_text, []))

            # Step 4: 拼接文本（在有图片的段落前插入 [图片N] 标记）
            lines = []
            for text, markers in entries:
                if markers:
                    prefix = ' '.join(markers)
                    lines.append(f"{prefix}\n{text}" if text else prefix)
                else:
                    lines.append(text)
            result['text'] = "\n".join(lines)

        except ImportError:
            raise Exception("python-docx 库未安装，请运行: pip install python-docx")
        except Exception as e:
            raise Exception(f"解析 Word 文档失败: {str(e)}")

    elif file_type in ['md', 'txt']:
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                result['text'] = f.read()
        except UnicodeDecodeError:
            with open(file_path, 'r', encoding='gbk') as f:
                result['text'] = f.read()
    else:
        raise Exception(f"不支持的文件类型: {file_type}")

    return result


class _ReturnMatch:
    """简易 re.Match 兼容对象，供 _match_return_block 返回使用。"""
    __slots__ = ('_start', '_end', '_full', '_body')

    def __init__(self, start, end, full, body):
        self._start = start
        self._end = end
        self._full = full
        self._body = body

    def start(self):
        return self._start

    def end(self):
        return self._end

    def group(self, n=0):
        if n == 0:
            return self._full
        if n == 1:
            return self._body
        raise IndexError(f'no such group {n}')


def _normalize_html_lines(html, max_line=2000):
    """将超长行在标签边界处拆分，使每行不超过 max_line 字符。

    SingleFile 导出的 HTML 常有单行 50K-180K 的情况（SVG sprite、
    压缩 CSS 等），拆分后 AI 的 read_page 可以正常返回内容。
    """
    if not html:
        return html
    lines = html.split('\n')
    result = []
    for line in lines:
        if len(line) <= max_line:
            result.append(line)
            continue
        # 在标签/CSS 语句边界处拆分
        while len(line) > max_line:
            cut = -1
            for sep in ['>', ';}', '}{', '; ', ' ']:
                idx = line[:max_line].rfind(sep)
                if idx >= max_line // 4:
                    cut = idx + len(sep)
                    break
            if cut < 0:
                cut = max_line
            result.append(line[:cut])
            line = line[cut:]
        if line:
            result.append(line)
    return '\n'.join(result)


def _ensure_session_attrs(session):
    """确保 session 对象包含所有必需属性（兼容不同版本的 GenerationSession）。

    当 server_session.py 版本不一致（如迁移后缺少 srcdoc_frame_html 属性）时，
    自动补齐缺失属性，避免 AttributeError。
    """
    if not hasattr(session, 'srcdoc_frame_html'):
        session.srcdoc_frame_html = ''
    if not hasattr(session, '_chat_head_html'):
        session._chat_head_html = ''
    if not hasattr(session, 'conversation_id'):
        session.conversation_id = ''
    if not hasattr(session, 'title'):
        session.title = ''
    return session


def _split_head_body(html):
    """将完整 HTML 拆分为框架和可编辑内容两部分。

    框架部分包括：head（CSS）、body 中的 SVG sprite 等不需要 AI 编辑的资源。
    可编辑部分：body 中除框架外的 HTML 内容。

    保存时通过 _merge_head_body 重新拼合。

    Returns:
        (frame_html, content_html) 或 (None, html) 无法拆分时
    """
    if not html:
        return None, html
    low = html.lower()
    head_end = low.find('</head>')
    body_start = low.find('<body')
    # 必须有明确的 head/body 分界
    if head_end < 0 or body_start < 0:
        return None, html
    # head 太小不值得拆分（<10K）
    if head_end < 10000:
        return None, html
    # 找到 body 标签的结束位置 >
    body_tag_end = html.find('>', body_start) + 1

    body_content = html[body_tag_end:]
    body_end_tag = body_content.lower().rfind('</body>')
    if body_end_tag > 0:
        body_content = body_content[:body_end_tag]

    # 检测 body 中的 SVG sprite 块（包含大量 <symbol> 的超长行）
    # 将其也归入框架部分
    svg_frame = ''
    edit_content = body_content
    lines = body_content.split('\n')
    svg_lines = []
    other_lines = []
    for line in lines:
        if (len(line) > 10000
                and '<svg' in line[:3000]
                and '<symbol' in line[:3000]):
            svg_lines.append(line)
        else:
            other_lines.append(line)

    if svg_lines:
        svg_frame = '\n'.join(svg_lines)
        edit_content = '\n'.join(other_lines)

    # 框架 = head + body标签 + SVG + 结束标签
    frame_html = (
        html[:body_tag_end]
        + '\n' + svg_frame + '\n'
        + '</body>\n</html>'
    )

    return frame_html, edit_content


def _merge_head_body(head_html, body_html):
    """将拆分的框架和内容重新合并为完整 HTML。"""
    if not head_html:
        return body_html
    # head_html 结尾是 \n</body>\n</html>，去掉它
    h = head_html
    for suffix in ['\n</body>\n</html>', '</body>\n</html>',
                    '</body></html>']:
        if h.endswith(suffix):
            h = h[:-len(suffix)]
            break
    return h + body_html + '\n</body>\n</html>'


class CustomHandler(http.server.SimpleHTTPRequestHandler):
    
    def end_headers(self):
        """添加禁用缓存的响应头"""
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        super().end_headers()
    
    def do_GET(self):
        """处理 GET 请求"""
        parsed_path = urllib.parse.urlparse(self.path)
        path = parsed_path.path
        query = urllib.parse.parse_qs(parsed_path.query)

        if path == '/api/prd/load':
            self.handle_prd_load(query)
        elif path == '/api/pages':
            self.handle_get_pages(query)
        elif path == '/api/flowchart':
            self.handle_get_flowchart(query)
        elif path == '/api/generation-status':
            self.handle_generation_status(query)
        elif path == '/api/generation-stream':
            self.handle_generation_stream(query)
        elif path == '/api/requirements/import-status':
            self.handle_requirements_import_status()
        elif path == '/api/requirements/stream':
            self.handle_requirements_stream(query)
        elif path == '/api/models':
            self.handle_get_models()
        elif path == '/api/canvas-layout':
            self.handle_get_canvas_layout(query)
        elif path == '/api/github/config':
            self.handle_github_config_get()
        elif path == '/api/chat-history':
            self.handle_chat_history()
        elif path == '/api/conversations':
            self.handle_conversations_list()
        elif path == '/api/conversations/search':
            self.handle_conversations_search()
        elif path == '/api/prd/discussions':
            self.handle_prd_discussions_list()
        elif path == '/api/prd/discussion/status':
            self.handle_prd_discussion_status(query)
        elif path.startswith('/api/prd/discussion/file/'):
            self.handle_prd_discussion_file()
        elif path.startswith('/api/conversations/export'):
            self.handle_conversation_export()
        elif path.startswith('/api/download-export'):
            self.handle_download_export()
        elif path == '/data/projects.json':
            # 拦截项目列表请求，确保返回最新数据
            self.load_projects()
            super().do_GET()
        else:
            # 默认静态文件服务
            super().do_GET()
    
    def do_POST(self):
        if self.path == '/upload':
            self.handle_upload()
        elif self.path == '/generate':
            self.handle_generate()
        elif self.path == '/generate-async':
            self.handle_generate_async()
        elif self.path == '/generate-spec':
            self.handle_generate_spec()
        elif self.path == '/save-project':
            self.handle_save_project()
        elif self.path == '/delete-project':
            self.handle_delete_project()
        elif self.path == '/rename-project':
            self.handle_rename_project()
        elif self.path == '/restore-project':
            self.handle_restore_project()
        elif self.path == '/deleted-projects':
            self.handle_get_deleted_projects()
        elif self.path == '/copy-project':
            self.handle_copy_project()
        elif self.path == '/create-placeholder':
            self.handle_create_placeholder()
        elif self.path == '/api/prd/save':
            self.handle_prd_save()
        elif self.path == '/api/inspector/apply':
            self.handle_inspector_apply()
        elif self.path == '/api/migrate-multifile':
            self.handle_migrate_multifile()
        elif self.path == '/api/chat':
            self.handle_chat()
        elif self.path == '/api/chat-rollback':
            self.handle_chat_rollback()
        elif self.path == '/api/conversations/create':
            self.handle_conversation_create()
        elif self.path == '/api/conversations/switch':
            self.handle_conversation_switch()
        elif self.path == '/api/conversations/delete':
            self.handle_conversation_delete()
        elif self.path == '/api/conversations/rename':
            self.handle_conversation_rename()
        elif self.path == '/api/conversations/pin':
            self.handle_conversation_pin()
        elif self.path == '/api/conversations/undo':
            self.handle_conversation_undo()
        elif self.path == '/api/stop-generation':
            self.handle_stop_generation()
        elif self.path == '/api/models/select':
            self.handle_model_select()
        elif self.path == '/api/models/save':
            self.handle_model_save()
        elif self.path == '/api/models/delete':
            self.handle_model_delete()
        elif self.path == '/api/models/test':
            self.handle_model_test()
        elif self.path == '/api/canvas-layout':
            self.handle_save_canvas_layout()
        elif self.path == '/api/export':
            self.handle_export()
        elif self.path == '/api/github/config':
            self.handle_github_config_save()
        elif self.path == '/api/github/test':
            self.handle_github_test()
        elif self.path == '/api/github/publish':
            self.handle_github_publish()
        elif self.path == '/api/github/unpublish':
            self.handle_github_unpublish()
        elif self.path == '/api/requirements/import':
            self.handle_requirements_import()
        elif self.path == '/api/prd/discussion/start':
            self.handle_prd_discussion_start()
        elif self.path == '/api/prd/discussion/message':
            self.handle_prd_discussion_message()
        elif self.path == '/api/prd/discussion/generate':
            self.handle_prd_discussion_generate()
        elif self.path == '/api/prd/discussion/apply':
            self.handle_prd_discussion_apply()
        elif self.path == '/api/resume-generation':
            self.handle_resume_generation()
        elif self.path == '/api/template/parse':
            self.handle_template_parse()
        else:
            self.send_error(404, "Not Found")

    def handle_upload(self):
        """处理图片上传"""
        try:
            content_type = self.headers['Content-Type']
            if not content_type.startswith('multipart/form-data'):
                self.send_error(400, "Expected multipart/form-data")
                return
            
            boundary_match = re.search(r'boundary=([^;]+)', content_type)
            if not boundary_match:
                self.send_error(400, "Missing boundary")
                return
            boundary = boundary_match.group(1).encode()
            
            content_length = int(self.headers['Content-Length'])
            body = self.rfile.read(content_length)
            
            saved_paths = []
            parts = body.split(b'--' + boundary)
            
            for part in parts:
                if not part or part == b'--\r\n' or part == b'--':
                    continue
                if part.startswith(b'\r\n'):
                    part = part[2:]
                if part.endswith(b'\r\n'):
                    part = part[:-2]
                    
                header_end = part.find(b'\r\n\r\n')
                if header_end == -1:
                    continue
                    
                headers = part[:header_end].decode('utf-8', errors='ignore')
                file_data = part[header_end+4:]
                
                filename_match = re.search(r'filename="([^"]+)"', headers)
                if filename_match:
                    filename = filename_match.group(1)
                    filename = os.path.basename(filename)
                    
                    save_path = os.path.join(UPLOAD_DIR, filename)
                    with open(save_path, 'wb') as f:
                        f.write(file_data)
                        
                    saved_paths.append({
                        'name': filename,
                        'path': os.path.abspath(save_path),
                        'url': f'/{UPLOAD_DIR}/{filename}'
                    })

            self.send_json_response({'files': saved_paths})
            
        except Exception as e:
            self.send_error_response(str(e))

    def handle_generate(self):
        """处理AI生成请求（支持增量更新）"""
        try:
            content_length = int(self.headers['Content-Length'])
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))

            prompt = data.get('prompt', '')
            images = data.get('images', [])  # base64 images
            project_name = data.get('projectName', '未命名项目')
            form_data = data.get('formData', {})  # 用户输入的表单数据

            # 增量更新参数
            is_incremental = data.get('incremental', False)
            source_project_id = data.get('sourceProjectId', None)
            changes = data.get('changes', None)

            # 模板 ZIP
            template_zip = data.get('templateZip', None)
            logger.info(f"[模板] 收到 templateZip: {'有 (' + str(len(template_zip)) + ' 字符)' if template_zip else '无'}")
            template_css_path = None       # 保存的 CSS 文件相对路径
            template_design_tokens = ''    # 设计令牌摘要
            template_html_summary = ''     # HTML 结构摘要
            template_layout_type = 'plain'  # 布局类型
            template_sidebar_meta = {}  # 侧边栏元数据
            if template_zip:
                try:
                    if ',' in template_zip:
                        _, tpl_b64 = template_zip.split(',', 1)
                    else:
                        tpl_b64 = template_zip
                    tpl_bytes = base64.b64decode(tpl_b64)
                    all_css_parts = []
                    all_html_parts = []
                    all_tokens = []
                    template_frame_html = ''     # 外框架 HTML（精简版，用于 AI prompt）
                    template_raw_frame_html = '' # 原始框架 HTML（完整版，用于生成后组装）
                    template_is_iframe = False   # 是否为 iframe 布局
                    template_layout_type = 'plain'  # 'iframe' | 'sidebar' | 'plain'
                    with zipfile.ZipFile(io.BytesIO(tpl_bytes)) as zf:
                        for name in zf.namelist():
                            if name.startswith('__MACOSX') or name.startswith('.') or name.endswith('/'):
                                continue
                            if name.endswith(('.html', '.htm')):
                                content = zf.read(name).decode('utf-8', errors='ignore')
                                split = self.split_singlefile_html(content)
                                if split['css']:
                                    all_css_parts.append(f'/* === {os.path.basename(name)} === */\n{split["css"]}')
                                if split['html_structure']:
                                    html = split['html_structure'][:4000]
                                    all_html_parts.append(f'<!-- {os.path.basename(name)} -->\n{html}')
                                if split['design_tokens']:
                                    all_tokens.append(split['design_tokens'])
                                # 保存框架 HTML（取最后一个有框架的文件）
                                # 注意：is_iframe_layout 是关键标志，不能依赖 frame_html 是否非空
                                if split.get('is_iframe_layout'):
                                    template_is_iframe = True
                                    template_frame_html = split.get('frame_html', '')
                                    if split.get('raw_frame_html'):
                                        template_raw_frame_html = split['raw_frame_html']
                                    template_layout_type = split.get('layout_type', 'iframe')
                                    template_sidebar_meta = split.get('sidebar_meta', {})
                    # 合并 CSS（所有页面的样式合并为一个文件）
                    if all_css_parts:
                        combined_css = '\n\n'.join(all_css_parts)
                        template_design_tokens = '\n'.join(set(all_tokens)) if all_tokens else ''
                        # HTML 结构合并，控制总量
                        template_html_summary = '\n\n'.join(all_html_parts)[:12000]
                        logger.info(f"[模板] CSS: {len(combined_css)}字符, HTML结构: {len(template_html_summary)}字符, 设计令牌: {len(template_design_tokens)}字符")
                        # 注意: CSS 文件需要在项目创建后保存（需要 project_folder）
                        # 先暂存，等项目目录创建后再写入
                        self._pending_template_css = combined_css
                    else:
                        logger.warning("[模板] 未从 ZIP 中提取到 CSS 内容")

                    # 暂存模板框架信息（等目录创建后保存）
                    if template_is_iframe and (template_raw_frame_html or template_frame_html):
                        self._pending_template_frame = template_raw_frame_html or template_frame_html
                        self._pending_template_is_iframe = True
                        self._pending_template_layout_type = template_layout_type
                except Exception as e:
                    logger.warning(f"[模板] ZIP 解析失败，忽略模板: {e}")
            
            if not prompt:
                self.send_error_response("缺少 prompt")
                return
            
            logger.info(f"[生成] 项目: {project_name}, 图片数: {len(images)}, 增量模式: {is_incremental}")
            
            # 生成项目ID（日期时间_英文名）
            project_id = generate_project_id(project_name)
            project_folder = os.path.join(PROJECTS_DIR, project_id)
            os.makedirs(project_folder, exist_ok=True)

            # 保存模板 CSS 文件（如果有的话）
            pending_css = getattr(self, '_pending_template_css', None)
            if pending_css:
                template_dir = os.path.join(project_folder, 'template')
                os.makedirs(template_dir, exist_ok=True)
                css_path = os.path.join(template_dir, 'template.css')
                with open(css_path, 'w', encoding='utf-8') as f:
                    f.write(pending_css)
                template_css_path = f'template/template.css'
                self._pending_template_css = None
                logger.info(f"[模板] CSS 已保存: {css_path} ({len(pending_css)}字符)")

            # 保存模板框架 HTML（供 Canvas Studio 展示为独立卡片）
            pending_frame = getattr(self, '_pending_template_frame', None)
            if pending_frame:
                template_dir = os.path.join(project_folder, 'template')
                os.makedirs(template_dir, exist_ok=True)
                frame_path = os.path.join(template_dir, 'frame.html')
                with open(frame_path, 'w', encoding='utf-8') as f:
                    f.write(pending_frame)
                logger.info(f"[模板] 框架 HTML 已保存: {frame_path} ({len(pending_frame)}字符)")
                self._pending_template_frame = None
                self._pending_template_is_iframe = None
                self._pending_template_layout_type = None

            # 保存用户上传的参考图片
            ref_images_folder = os.path.join(project_folder, 'reference')
            os.makedirs(ref_images_folder, exist_ok=True)
            
            reused_pages = 0
            source_html_content = None
            
            # ==================== 增量更新处理 ====================
            if is_incremental and source_project_id and changes:
                source_folder = os.path.join(PROJECTS_DIR, source_project_id)
                
                # 检查是否完全无变化
                if not changes.get('hasChanges', True):
                    logger.info(f"[增量] 无变化，复制原项目")
                    return self.copy_project(source_project_id, project_name)
                
                # 复制原项目的reference图片（未变化的页面）
                source_ref_folder = os.path.join(source_folder, 'reference')
                if os.path.exists(source_ref_folder):
                    import shutil
                    for f in os.listdir(source_ref_folder):
                        src = os.path.join(source_ref_folder, f)
                        dst = os.path.join(ref_images_folder, f)
                        if os.path.isfile(src):
                            shutil.copy2(src, dst)
                    logger.info(f"[增量] 复制原项目参考图片")
                
                # 读取原项目的HTML
                source_html_path = os.path.join(source_folder, 'index.html')
                if os.path.exists(source_html_path):
                    with open(source_html_path, 'r', encoding='utf-8') as f:
                        source_html_content = f.read()
                    logger.info(f"[增量] 读取原项目HTML: {len(source_html_content)} 字符")
                
                # 复制原项目的images文件夹
                source_images_folder = os.path.join(source_folder, 'images')
                dest_images_folder = os.path.join(project_folder, 'images')
                if os.path.exists(source_images_folder):
                    import shutil
                    shutil.copytree(source_images_folder, dest_images_folder)
                    logger.info(f"[增量] 复制原项目images文件夹")
                
                reused_pages = len(changes.get('pagesUnchanged', []))
                logger.info(f"[增量] 未变化页面数: {reused_pages}, 变化页面数: {len(changes.get('pagesChanged', []))}")
            
            # 保存新上传的图片并记录文件名
            saved_image_names = []
            for i, img_base64 in enumerate(images):
                filename = f"ref_{i+1}"
                saved = save_base64_image(img_base64, ref_images_folder, filename)
                if saved:
                    saved_image_names.append(saved)
                    logger.info(f"[保存参考图] {saved}")
            
            # 构建并保存record.json（用户输入记录）
            record = {
                'global': form_data.get('global', {}),
                'pages': [],
                'createdAt': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'sourceProjectId': source_project_id if is_incremental else None
            }
            
            # 为每个页面分配图片文件名
            pages_data = form_data.get('pages', [])
            img_index = 0
            for page in pages_data:
                page_record = {
                    'name': page.get('name', ''),
                    'description': page.get('description', ''),
                    'layout': page.get('layout', ''),
                    'features': page.get('features', ''),
                    'dataStructure': page.get('dataStructure', ''),
                    'interaction': page.get('interaction', ''),
                    'userFlow': page.get('userFlow', ''),
                    'similarity': page.get('similarity', 'layout'),
                    'images': []
                }
                # 分配图片
                img_count = page.get('imageCount', 0)
                for _ in range(img_count):
                    if img_index < len(saved_image_names):
                        page_record['images'].append(saved_image_names[img_index])
                        img_index += 1
                record['pages'].append(page_record)
            
            # 保存record.json
            record_path = os.path.join(project_folder, 'record.json')
            with open(record_path, 'w', encoding='utf-8') as f:
                json.dump(record, f, ensure_ascii=False, indent=2)
            logger.info(f"[保存] record.json")
            
            # ==================== 决定是否调用AI ====================
            html_content = None

            # 构建增强 prompt（模板 + 增量）
            final_prompt = prompt

            # 注入模板设计信息
            has_template_info = template_design_tokens or template_html_summary or template_frame_html
            logger.info(f"[模板注入] has_template_info={has_template_info}, design_tokens={len(template_design_tokens)}字符, html_summary={len(template_html_summary)}字符, frame_html={len(template_frame_html)}字符, is_iframe={template_is_iframe}, layout_type={template_layout_type}")
            if has_template_info:
                # ===== 统一模板注入策略 =====
                # 不管是否 iframe/sidebar 布局，都向 AI 注入模板的视觉规范和结构
                # 让 AI 明确知道要基于此模板风格来生成

                template_section = "\n\n# 现有系统模板（必须严格遵循此模板的视觉风格！）\n\n"
                template_section += "用户提供了一个现有系统的页面模板。你必须在保持此模板视觉风格的前提下，生成新的页面内容。\n"
                template_section += "**绝对不能偏离模板的配色、字体、组件风格！**\n\n"

                # 注入设计令牌
                if template_design_tokens:
                    template_section += f"## 模板设计规范（从模板 CSS 提取）\n{template_design_tokens}\n\n"

                # 注入 HTML 结构（清理 base64 图片）
                if template_html_summary:
                    cleaned_summary = self._clean_frame_html_for_prompt(template_html_summary, 12000)
                    template_section += f"## 模板页面结构（供参考）\n以下是模板的 HTML 结构，你应当使用相同的布局模式和 CSS class：\n```html\n{cleaned_summary}\n```\n\n"

                # 如果检测到 iframe/sidebar 框架模式，注入框架信息
                if template_is_iframe and template_frame_html:
                    is_sidebar_mode = (template_layout_type == 'sidebar')

                    if is_sidebar_mode:
                        template_section += "## 模板注入说明\n"
                        template_section += "此模板采用「侧边栏 + 顶栏 + 内容区」布局。系统已保留完整的模板 HTML（含侧边栏、顶栏、CSS），你**只需生成主内容区域的 HTML 片段**。\n"
                        template_section += "**关键要求**：\n"
                        template_section += "- **不要**生成完整的 HTML 页面（不要 <!DOCTYPE html>、<html>、<head>、<body>）\n"
                        template_section += "- **不要**生成侧边栏、顶栏、导航栏（模板已有，系统会自动处理）\n"
                        template_section += "- 只生成主内容区域的 HTML 代码片段（即 `<div class=pageContent>` 内部的内容）\n"
                        # 自动检测模板的 UI 框架并提示对应的 CSS class
                        _frame_hint = ''
                        if template_design_tokens and 'ant-' in (template_design_tokens + (pending_css or '')):
                            _frame_hint = '（ant-btn、ant-table、ant-form、ant-card 等 Ant Design 组件）'
                        elif template_design_tokens and 'el-' in (template_design_tokens + (pending_css or '')):
                            _frame_hint = '（el-button、el-table、el-form 等 Element UI 组件）'
                        template_section += f"- 使用模板已有的 CSS class{_frame_hint}，模板的 CSS 已全部内联\n"
                        template_section += "- 如需额外样式，用 `<style>` 标签包裹（会放在内容片段中）\n"
                        template_section += "- 可以使用 Vue 3 (CDN) 实现交互（搜索、过滤、弹窗等），用 `<script>` 标签包裹\n"
                        template_section += "- 如果使用 Vue，必须在 Vue 根元素（如 `<div id=\"app\">`）上添加 `v-cloak` 属性，并在 `<style>` 中加 `[v-cloak] { display: none; }`，防止模板未编译时显示 {{ }} 原始变量\n"
                        template_section += "- **不要**复制模板原始页面的特有数据字段（如「数据周期」「指标波动」等），按用户需求生成全新的内容\n\n"
                        template_section += "**修改侧边栏**（可选）：\n"
                        template_section += "- 如果需要在侧边栏添加新菜单项，在输出末尾加 `<!-- SIDEBAR_ADD: 菜单名称 -->`\n"
                        template_section += "- 如果需要设置某个菜单项为激活状态，在输出末尾加 `<!-- SIDEBAR_ACTIVE: 菜单名称 -->`\n\n"
                    else:
                        template_section += "## 框架说明\n"
                        template_section += "此模板采用「侧边栏 + 顶栏 + iframe 内容区」布局。\n"
                        template_section += "不要生成侧边栏、顶栏、导航等外框架元素，只生成 iframe 内部的页面内容。\n"
                        template_section += f"引用模板 CSS: `<link rel=\"stylesheet\" href=\"{template_css_path or 'template/template.css'}\">`\n"
                        template_section += "生成的 HTML 应该是一个完整的独立页面（有 <!DOCTYPE html>、<head>、<body>）。\n\n"

                    # 注入侧边栏菜单
                    template_section += "## 侧边栏菜单（供参考，当前激活项用★标记）\n```\n"
                    # 尝试多种模式提取菜单项
                    sidebar_items = re.findall(r'<span>([^<]+)</span>', template_frame_html[:8000])
                    if not sidebar_items:
                        sidebar_items = re.findall(r'ant-menu-title-content>([^<]+)', template_frame_html[:8000])
                    if not sidebar_items:
                        sidebar_items = re.findall(r'<li[^>]*title="([^"]+)"', template_frame_html[:8000])

                    active_match = re.search(r'is-active[^>]*>.*?<span>([^<]+)</span>', template_frame_html[:8000], re.DOTALL)
                    if not active_match:
                        active_match = re.search(r'ant-menu-item-selected[^>]*>.*?title="([^"]+)"', template_frame_html[:8000], re.DOTALL)
                    active_text = active_match.group(1) if active_match else ''
                    for item in sidebar_items:
                        marker = ' ★ (当前激活)' if item == active_text else ''
                        template_section += f"  - {item}{marker}\n"
                    template_section += "```\n\n"

                    # 注入框架 HTML 代码供 AI 参考（去除 base64 图片以节省 token）
                    if len(template_frame_html) > 500:
                        cleaned_frame = self._clean_frame_html_for_prompt(template_frame_html, 15000)
                        template_section += f"## 框架 HTML 代码（参考其组件样式和 class 命名）\n```html\n{cleaned_frame}\n```\n\n"
                else:
                    # 非 iframe/sidebar 模板，注入 CSS 引用
                    if template_css_path:
                        template_section += f"## 模板 CSS 文件\n已保存到 `{template_css_path}`，请在生成的 HTML 中通过 `<link rel=\"stylesheet\" href=\"{template_css_path}\">` 引用它。\n\n"

                # 统一的样式强制要求
                template_section += """## 模板还原要求（非常重要，必须严格遵守）
1. 配色方案**必须**与模板设计规范一致，不要自创配色
2. 组件样式（按钮、表格、表单、卡片、下拉框、输入框）必须与模板一致
3. 布局结构参考模板的页面结构
4. 字体、字号、间距与模板保持统一
5. 如果模板使用了 Ant Design，你也必须使用 Ant Design 风格的组件
6. 如果模板使用了 Element UI，你也必须使用 Element UI 风格的组件
7. 不要引入与模板不协调的 CDN 库（如果模板用的是 Ant Design，不要用 Tailwind 做布局）
8. 你可以在模板基础上添加新功能，但视觉风格绝对不能偏离

### 布局要求
- 生成的内容必须是**单个连续页面**，不要使用 Tab 标签页分页
- 页面应是可垂直滚动的长表单/长页面，所有内容在一个视图中
- 如果需要多个状态（如列表/编辑），使用按钮跳转而不是 Tab 切换
- **宽度自适应**：根容器不要使用 max-width、container、mx-auto 等限制宽度，使用 width: 100% 占满空间
- **高度填满**：页面根容器使用 min-height: 100vh，内容区使用 flex: 1 自适应高度
- **表格/网格溢出处理**：表格和网格等宽内容必须用 `overflow-x: auto` 的容器包裹
- **图表自适应**：图表容器使用 width: 100%，不要设置固定像素宽度

### 跨页面一致性要求
如果生成了多个页面，以下组件必须在所有页面中保持完全统一的样式：
1. **面包屑导航**: 所有页面的面包屑必须使用相同的 HTML 结构和 CSS 样式（只定义一次全局样式）
   - 统一使用: `<nav class="breadcrumb"><a>父级</a><span class="sep">/</span><span class="current">当前页</span></nav>`
   - 禁止每个页面各自定义不同的面包屑 CSS
2. **侧边栏/导航栏**: 所有页面共享同一套导航结构和样式
3. **基础组件**: 按钮、表格、表单、卡片等在不同页面中风格一致

### 侧边栏菜单修改（可选）
如果用户需求中提到要在侧边栏添加新菜单项，请在 HTML 代码最后添加：
```html
<!-- SIDEBAR_ADD: 菜单名称 -->
```
"""
                final_prompt += template_section

            if is_incremental and source_html_content and reused_pages > 0:
                # 部分页面可复用，但仍需要调用AI（因为有变化的页面）
                # 在prompt中提示AI参考原有内容，并注入源 HTML
                incremental_hint = f"\n\n# 增量更新上下文（重要）\n这是一个增量更新任务。原项目中有{reused_pages}个页面内容未变化。请保持整体风格一致，重点关注变化的部分。"
                if source_html_content:
                    html_preview = source_html_content[:15000]
                    incremental_hint += f"\n\n## 原项目HTML代码（供参考，请保持风格一致）\n```html\n{html_preview}\n```\n\n## 增量更新要求\n1. 保持原项目的整体设计风格、配色方案、组件风格\n2. 新增页面必须与已有页面风格一致\n3. 不要重新设计已有页面，除非用户明确要求"
                final_prompt += incremental_hint
                logger.info(f"[增量] 使用增强prompt调用AI")
                html_content = self.call_ai_model(final_prompt, images)
            else:
                # 正常调用AI
                html_content = self.call_ai_model(final_prompt, images)
            
            if not html_content:
                self.send_error_response("AI未返回有效内容")
                return
            
            # 下载HTML中的外部图片并替换URL
            logger.info("[处理] 下载HTML中的外部图片...")
            html_content = download_html_images(html_content, project_folder)

            # iframe 框架拼接：iframe/srcdoc 模式 和 sidebar 模式都拼框架
            if template_is_iframe and (template_raw_frame_html or template_frame_html):
                if template_layout_type == 'iframe':
                    logger.info("[组装] 检测到 iframe 框架布局，拼接框架+内容...")
                else:
                    logger.info("[组装] 检测到 sidebar 框架布局，模板注入内容区...")
                # 优先使用原始框架 HTML（保留完整样式），如果不存在则用精简版
                frame_to_use = template_raw_frame_html or template_frame_html
                html_content = self.assemble_iframe_html(html_content, frame_to_use, template_css_path, template_sidebar_meta, project_name)

            # 注入页面切换消息监听器（用于 viewer.html 的页面导航）
            html_content = self.inject_page_navigation_listener(html_content)
            
            # 保存 HTML
            html_path = os.path.join(project_folder, 'index.html')
            with open(html_path, 'w', encoding='utf-8') as f:
                f.write(html_content)
            
            # 从HTML中提取title作为项目名称
            html_title = extract_title_from_html(html_content)
            if html_title and html_title != project_name:
                logger.info(f"[提取] HTML title: {html_title}")
                # 使用HTML中的title重新生成项目ID
                new_project_id = generate_project_id(html_title)
                new_project_folder = os.path.join(PROJECTS_DIR, new_project_id)
                
                # 重命名文件夹
                if not os.path.exists(new_project_folder):
                    import shutil
                    shutil.move(project_folder, new_project_folder)
                    project_folder = new_project_folder
                    project_id = new_project_id
                    project_name = html_title
                    logger.info(f"[重命名] 项目文件夹: {project_id}")
            
            # 保存 final_prompt (用于调试，含模板注入后的完整 prompt)
            prompt_path = os.path.join(project_folder, 'prompt.txt')
            with open(prompt_path, 'w', encoding='utf-8') as f:
                f.write(final_prompt)
            
            # 获取当前选中的模型名称
            current_model = get_selected_model()
            current_model_name = current_model.get('name', '') if current_model else ''
            
            # 更新项目列表
            projects = self.load_projects()
            new_project = {
                'id': project_id,
                'name': project_name,
                'model_name': current_model_name,
                'url': f'/projects/{project_id}/index.html',
                'date': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            # 检查是否已存在（避免重复）
            existing_idx = next((i for i, p in enumerate(projects) if p['id'] == project_id), None)
            if existing_idx is not None:
                projects[existing_idx] = new_project
            else:
                projects.insert(0, new_project)
            self.save_projects(projects)
            
            logger.info(f"[完成] 项目已保存: {project_folder}")
            
            # 返回结果，包含增量信息
            response_data = {
                'success': True, 
                'project': new_project,
                'incremental': is_incremental,
                'reusedPages': reused_pages
            }
            self.send_json_response(response_data)
            
        except Exception as e:
            logger.error(f"[错误] 生成失败: {e}")
            import traceback
            traceback.print_exc()
            self.send_error_response(str(e))

    def handle_generate_spec(self):
        """异步生成跨页规格：立即返回 projectId，后台线程通过 SSE 推送 spec。

        请求体同前。
        立即返回:
        {
            "success": True,
            "projectId": "xxx",
            "estimatedPages": 4
        }

        后台线程通过 SSE (/api/generation-stream?id=xxx) 推送:
        - type='content'  — AI 流式输出的文本
        - type='spec_complete' — spec 生成完成，data={spec, canSkip}
        - type='spec_error'   — spec 生成失败，data={error}
        """
        try:
            content_length = int(self.headers['Content-Length'])
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))

            prompt = data.get('prompt', '')
            images = data.get('images', [])
            project_name = data.get('projectName', '未命名项目')
            form_data = data.get('formData', {})
            template_zip = data.get('templateZip', None)

            pages_data = form_data.get('pages', [])
            global_config = form_data.get('global', {})
            generation_config = form_data.get('generationConfig', {})

            # 用户调整反馈（用于重新分析 spec）
            adjustment_note = data.get('adjustmentNote', '')
            previous_spec = data.get('previousSpec', None)
            if previous_spec is not None and not isinstance(previous_spec, dict):
                logger.warning(f"[Spec] Invalid previous_spec type: {type(previous_spec)}")
                previous_spec = None

            if not pages_data:
                self.send_json_response({'success': False, 'error': '未指定页面'})
                return

            # 生成项目 ID 并创建临时文件夹
            project_id = generate_project_id(project_name)
            project_folder = os.path.join(PROJECTS_DIR, project_id)
            os.makedirs(project_folder, exist_ok=True)

            # 注册 SSE 任务
            with tasks_lock:
                generating_tasks[project_id] = {
                    'status': 'spec_generating',
                    'progress': 10,
                    'error': '',
                    'accumulated_content': '',
                    'stream_chunks': [],
                    'stream_event': threading.Event(),
                    'stream_lock': threading.Lock(),
                    'round': 0,
                    'phase_description': '生成规格...',
                    'page_progress': None,
                    'strategy': 'spec_only',
                }

            # 解析模板
            template_tokens = ''
            template_html_summary = ''
            template_frame_html = ''
            template_layout_type = 'plain'
            template_sidebar_meta = {}

            if template_zip:
                try:
                    if ',' in template_zip:
                        _, tpl_b64 = template_zip.split(',', 1)
                    else:
                        tpl_b64 = template_zip
                    tpl_bytes = base64.b64decode(tpl_b64)
                    with zipfile.ZipFile(io.BytesIO(tpl_bytes)) as zf:
                        for name in zf.namelist():
                            if name.startswith('__MACOSX') or name.startswith('.') or name.endswith('/'):
                                continue
                            if name.endswith(('.html', '.htm')):
                                content = zf.read(name).decode('utf-8', errors='ignore')
                                split = self.split_singlefile_html(content)
                                if split.get('design_tokens'):
                                    template_tokens = split['design_tokens']
                                if split.get('is_iframe_layout'):
                                    template_frame_html = split.get('frame_html', '')
                                    template_layout_type = 'sidebar'
                except Exception as e:
                    logger.warning(f"[Spec] 模板解析失败: {e}")

            # 后台线程生成 spec
            def spec_in_background():
                threading.current_thread()._project_id = project_id
                try:
                    logger.info(f"[Spec] 后台生成 spec: {project_id}")

                    generator = MultiRoundGenerator(self, project_id, project_folder)
                    generator.generation_config = generation_config

                    result = generator._generate_spec_only(
                        pages_data=pages_data,
                        global_config=global_config,
                        template_tokens=template_tokens,
                        template_html_summary=template_html_summary,
                        template_frame_html=template_frame_html,
                        template_layout_type=template_layout_type,
                        template_sidebar_meta=template_sidebar_meta,
                        adjustment_note=adjustment_note,
                        previous_spec=previous_spec
                    )

                    # 发送 spec_complete 事件
                    _push_sse_event(project_id, 'spec_complete', {
                        'spec': result['spec'],
                        'canSkip': result['canSkip'],
                        'projectId': project_id,
                        'estimatedPages': len(pages_data)
                    })

                    # 标记完成（但不清理 generating_tasks，让前端能收到最后的事件）
                    with tasks_lock:
                        if project_id in generating_tasks:
                            generating_tasks[project_id]['status'] = 'completed'
                            generating_tasks[project_id]['progress'] = 100

                    logger.info(f"[Spec] 后台 spec 完成: {project_id}")

                except Exception as e:
                    logger.error(f"[Spec] 后台 spec 失败: {e}", exc_info=True)
                    _push_sse_event(project_id, 'spec_error', {'error': str(e)})
                    with tasks_lock:
                        if project_id in generating_tasks:
                            generating_tasks[project_id]['status'] = 'failed'
                            generating_tasks[project_id]['error'] = str(e)

            thread = threading.Thread(target=spec_in_background, daemon=True)
            thread.start()

            # 立即返回 projectId，前端通过 SSE 接收后续内容
            self.send_json_response({
                'success': True,
                'projectId': project_id,
                'estimatedPages': len(pages_data)
            })

        except Exception as e:
            logger.error(f"[Spec] 启动失败: {e}", exc_info=True)
            self.send_json_response({'success': False, 'error': str(e)})

    def _handle_incremental_generation(self, data, confirmed_spec, project_id):
        """处理用户已确认 spec 的增量生成（后台线程）。

        复用 generate-async 的模板解析和任务注册逻辑，
        但使用 mode='incremental' 调用 MultiRoundGenerator。
        """
        try:
            images = data.get('images', [])
            project_name = data.get('projectName', '未命名项目')
            form_data = data.get('formData', {})
            template_zip = data.get('templateZip', None)
            pages_data = form_data.get('pages', [])
            global_config = form_data.get('global', {})
            generation_config = form_data.get('generationConfig', {})

            # 提取用户调整文本，注入 confirmed_spec
            adjustment_note = data.get('adjustmentNote', '')
            if adjustment_note and confirmed_spec:
                confirmed_spec['_adjustment_note'] = adjustment_note

            project_folder = os.path.join(PROJECTS_DIR, project_id)

            # 解析模板（同 generate-async 逻辑）
            template_css_path = None
            template_design_tokens = ''
            template_html_summary = ''
            template_frame_html = ''
            template_raw_frame_html = ''
            template_is_iframe = False
            template_layout_type = 'plain'
            template_sidebar_meta = {}

            if template_zip:
                try:
                    if ',' in template_zip:
                        _, tpl_b64 = template_zip.split(',', 1)
                    else:
                        tpl_b64 = template_zip
                    tpl_bytes = base64.b64decode(tpl_b64)
                    all_css_parts = []
                    pending_css = None
                    with zipfile.ZipFile(io.BytesIO(tpl_bytes)) as zf:
                        for name in zf.namelist():
                            if name.startswith('__MACOSX') or name.startswith('.') or name.endswith('/'):
                                continue
                            if name.endswith(('.html', '.htm')):
                                content = zf.read(name).decode('utf-8', errors='ignore')
                                split = self.split_singlefile_html(content)
                                if split['css']:
                                    all_css_parts.append(f'/* === {os.path.basename(name)} === */\n{split["css"]}')
                                if split.get('design_tokens'):
                                    template_design_tokens = split['design_tokens']
                                if split.get('is_iframe_layout'):
                                    template_is_iframe = True
                                    template_frame_html = split.get('frame_html', '')
                                    if split.get('raw_frame_html'):
                                        template_raw_frame_html = split['raw_frame_html']
                                    template_layout_type = 'sidebar'
                                    template_sidebar_meta = split.get('sidebar_meta', {})
                            elif name.endswith('.css'):
                                css_content = zf.read(name).decode('utf-8', errors='ignore')
                                all_css_parts.append(f'/* === {os.path.basename(name)} === */\n{css_content}')

                    if all_css_parts:
                        pending_css = '\n'.join(all_css_parts)

                    if pending_css:
                        css_path = os.path.join(project_folder, 'template', 'template.css')
                        os.makedirs(os.path.dirname(css_path), exist_ok=True)
                        with open(css_path, 'w', encoding='utf-8') as f:
                            f.write(pending_css)
                        template_css_path = 'template/template.css'

                    # 保存模板框架 HTML（供 Canvas Studio 展示为独立卡片）
                    if template_is_iframe and (template_raw_frame_html or template_frame_html):
                        frame_dir = os.path.join(project_folder, 'template')
                        os.makedirs(frame_dir, exist_ok=True)
                        frame_path = os.path.join(frame_dir, 'frame.html')
                        frame_html = template_raw_frame_html or template_frame_html
                        with open(frame_path, 'w', encoding='utf-8') as f:
                            f.write(frame_html)
                        logger.info(f"[增量] 框架 HTML 已保存: {frame_path} ({len(frame_html)}字符)")

                except Exception as e:
                    logger.warning(f"[增量] 模板解析失败: {e}")

            # 保存 record.json
            record = {
                'global': global_config,
                'pages': pages_data,
                'generationConfig': generation_config,
                'status': STATUS_GENERATING,
                'createdAt': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'mode': 'incremental'
            }
            record_path = os.path.join(project_folder, 'record.json')
            with open(record_path, 'w', encoding='utf-8') as f:
                json.dump(record, f, ensure_ascii=False, indent=2)

            # 更新项目列表
            current_model = get_selected_model()
            current_model_name = current_model.get('name', '') if current_model else ''
            projects = self.load_projects()
            projects.insert(0, {
                'id': project_id,
                'name': project_name,
                'model_name': current_model_name,
                'status': STATUS_GENERATING,
                'url': f'/projects/{project_id}/index.html',
                'date': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            })
            self.save_projects(projects)

            # 注册异步任务
            with tasks_lock:
                generating_tasks[project_id] = {
                    'status': STATUS_GENERATING,
                    'progress': 0,
                    'error': '',
                    'accumulated_content': '',
                    'stream_chunks': [],
                    'stream_event': threading.Event(),
                    'stream_lock': threading.Lock(),
                    'round': 0,
                    'phase_description': '增量生成...',
                    'page_progress': None,
                    'strategy': 'multi_round_incremental',
                }

            # 保存 prompt.txt（增量模式：保存页面规格摘要供调试）
            try:
                prompt_lines = [f"增量生成 — {project_name}", ""]
                prompt_lines.append(f"全局配置: {json.dumps(global_config, ensure_ascii=False)}")
                prompt_lines.append(f"生成配置: {json.dumps(generation_config, ensure_ascii=False)}")
                prompt_lines.append("")
                prompt_lines.append("=== 页面列表 ===")
                for pi, pg in enumerate(pages_data):
                    prompt_lines.append(f"\n--- 页面 {pi+1}: {pg.get('name', '')} ---")
                    prompt_lines.append(f"描述: {pg.get('description', '')}")
                    prompt_lines.append(f"布局: {pg.get('layout', '')}")
                    prompt_lines.append(f"功能: {pg.get('features', '')}")
                if confirmed_spec:
                    prompt_lines.append("\n=== 已确认的跨页规格 ===")
                    prompt_lines.append(json.dumps(confirmed_spec, ensure_ascii=False, indent=2))
                prompt_path = os.path.join(project_folder, 'prompt.txt')
                with open(prompt_path, 'w', encoding='utf-8') as f:
                    f.write('\n'.join(prompt_lines))
            except Exception:
                pass

            # 启动后台线程
            def generate_in_background():
                threading.current_thread()._project_id = project_id
                try:
                    logger.info(f"[增量] 开始后台生成: {project_id}")

                    generator = MultiRoundGenerator(self, project_id, project_folder)
                    generator.generation_config = generation_config

                    html_content = generator.run(
                        prompt='',
                        pages_data=pages_data,
                        images=images,
                        global_config=global_config,
                        template_tokens=template_design_tokens,
                        template_html_summary=template_html_summary,
                        template_css_path=template_css_path,
                        template_is_iframe=template_is_iframe,
                        template_frame_html=template_frame_html,
                        template_raw_frame_html=template_raw_frame_html,
                        template_layout_type=template_layout_type,
                        template_sidebar_meta=template_sidebar_meta,
                        mode='incremental',
                        confirmed_spec=confirmed_spec
                    )

                    if not html_content:
                        raise Exception("增量生成失败：无内容返回")

                    # 更新项目状态
                    projects = self.load_projects()
                    for p in projects:
                        if p['id'] == project_id:
                            p['status'] = None
                            break
                    self.save_projects(projects)

                    with tasks_lock:
                        if project_id in generating_tasks:
                            generating_tasks[project_id]['status'] = STATUS_COMPLETED
                            generating_tasks[project_id]['progress'] = 100

                    logger.info(f"[增量] 完成: {project_id}")

                except Exception as e:
                    logger.error(f"[增量] 失败: {e}")
                    with tasks_lock:
                        if project_id in generating_tasks:
                            generating_tasks[project_id]['status'] = STATUS_FAILED
                            generating_tasks[project_id]['error'] = str(e)

            thread = threading.Thread(target=generate_in_background, daemon=True)
            thread.start()

            self.send_json_response({
                'success': True,
                'project': {
                    'id': project_id,
                    'status': STATUS_GENERATING,
                    'name': project_name,
                    'model_name': current_model_name,
                    'url': f'/projects/{project_id}/index.html',
                    'date': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                }
            })

        except Exception as e:
            logger.error(f"[增量] 启动失败: {e}")
            self.send_json_response({'success': False, 'error': str(e)})

    def handle_generate_async(self):
        """异步处理AI生成请求：立即返回项目信息，后台线程完成生成"""
        try:
            content_length = int(self.headers['Content-Length'])
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))

            prompt = data.get('prompt', '')
            images = data.get('images', [])
            project_name = data.get('projectName', '未命名项目')
            form_data = data.get('formData', {})
            is_incremental = data.get('incremental', False)
            source_project_id = data.get('sourceProjectId', None)
            changes = data.get('changes', None)
            template_zip = data.get('templateZip', None)

            # 增量模式：用户已确认 spec，直接进入增量生成
            confirmed_spec = data.get('confirmedSpec', None)
            pre_project_id = data.get('projectId', None)  # spec 阶段预分配的 ID
            if confirmed_spec and pre_project_id:
                return self._handle_incremental_generation(
                    data, confirmed_spec, pre_project_id
                )

            # 解析模板 ZIP：拆分为 CSS 文件 + HTML 结构 + 设计令牌
            template_css_path = None
            template_design_tokens = ''
            template_html_summary = ''
            template_frame_html = ''
            template_raw_frame_html = ''
            template_is_iframe = False
            template_layout_type = 'plain'
            template_sidebar_meta = {}
            pending_css = None
            if template_zip:
                try:
                    if ',' in template_zip:
                        _, tpl_b64 = template_zip.split(',', 1)
                    else:
                        tpl_b64 = template_zip
                    tpl_bytes = base64.b64decode(tpl_b64)
                    all_css_parts = []
                    all_html_parts = []
                    all_tokens = []
                    template_frame_html = ''     # 外框架 HTML（精简版，用于 AI prompt）
                    template_raw_frame_html = '' # 原始框架 HTML（完整版，用于生成后组装）
                    template_is_iframe = False   # 是否为 iframe 布局
                    template_layout_type = 'plain'  # 'iframe' | 'sidebar' | 'plain'
                    with zipfile.ZipFile(io.BytesIO(tpl_bytes)) as zf:
                        for name in zf.namelist():
                            if name.startswith('__MACOSX') or name.startswith('.') or name.endswith('/'):
                                continue
                            if name.endswith(('.html', '.htm')):
                                content = zf.read(name).decode('utf-8', errors='ignore')
                                split = self.split_singlefile_html(content)
                                if split['css']:
                                    all_css_parts.append(f'/* === {os.path.basename(name)} === */\n{split["css"]}')
                                if split['html_structure']:
                                    html = split['html_structure'][:4000]
                                    all_html_parts.append(f'<!-- {os.path.basename(name)} -->\n{html}')
                                if split['design_tokens']:
                                    all_tokens.append(split['design_tokens'])
                                # 保存框架 HTML（取最后一个有框架的文件）
                                # 注意：is_iframe_layout 是关键标志，不能依赖 frame_html 是否非空
                                if split.get('is_iframe_layout'):
                                    template_is_iframe = True
                                    template_frame_html = split.get('frame_html', '')
                                    if split.get('raw_frame_html'):
                                        template_raw_frame_html = split['raw_frame_html']
                                    template_layout_type = split.get('layout_type', 'iframe')
                                    template_sidebar_meta = split.get('sidebar_meta', {})
                    if all_css_parts:
                        pending_css = '\n\n'.join(all_css_parts)
                        template_design_tokens = '\n'.join(set(all_tokens)) if all_tokens else ''
                        template_html_summary = '\n\n'.join(all_html_parts)[:12000]
                        logger.info(f"[模板] CSS: {len(pending_css)}字符, HTML结构: {len(template_html_summary)}字符, 设计令牌: {len(template_design_tokens)}字符")
                    else:
                        logger.warning("[模板] 未从 ZIP 中提取到 CSS 内容")
                except Exception as e:
                    logger.warning(f"[模板] ZIP 解析失败，忽略模板: {e}")
            
            if not prompt:
                self.send_error_response("缺少 prompt")
                return
            
            # 生成项目ID
            project_id = generate_project_id(project_name)
            project_folder = os.path.join(PROJECTS_DIR, project_id)
            os.makedirs(project_folder, exist_ok=True)

            # 保存模板 CSS 文件
            if pending_css:
                template_dir = os.path.join(project_folder, 'template')
                os.makedirs(template_dir, exist_ok=True)
                css_path = os.path.join(template_dir, 'template.css')
                with open(css_path, 'w', encoding='utf-8') as f:
                    f.write(pending_css)
                template_css_path = 'template/template.css'
                logger.info(f"[模板] CSS 已保存: {css_path} ({len(pending_css)}字符)")

            # 保存模板框架 HTML（供 Canvas Studio 展示为独立卡片）
            if template_is_iframe and (template_raw_frame_html or template_frame_html):
                template_dir = os.path.join(project_folder, 'template')
                os.makedirs(template_dir, exist_ok=True)
                frame_path = os.path.join(template_dir, 'frame.html')
                frame_html = template_raw_frame_html or template_frame_html
                with open(frame_path, 'w', encoding='utf-8') as f:
                    f.write(frame_html)
                logger.info(f"[模板] 框架 HTML 已保存: {frame_path} ({len(frame_html)}字符)")

            # 保存参考图片
            ref_images_folder = os.path.join(project_folder, 'reference')
            os.makedirs(ref_images_folder, exist_ok=True)
            
            saved_image_names = []
            for i, img_base64 in enumerate(images):
                filename = f"ref_{i+1}"
                saved = save_base64_image(img_base64, ref_images_folder, filename)
                if saved:
                    saved_image_names.append(saved)
            
            # 创建初始 record.json
            record = {
                'global': form_data.get('global', {}),
                'pages': [],
                'status': STATUS_GENERATING,
                'createdAt': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'sourceProjectId': source_project_id if is_incremental else None
            }
            
            pages_data = form_data.get('pages', [])
            img_index = 0
            for page in pages_data:
                page_record = {
                    'name': page.get('name', ''),
                    'description': page.get('description', ''),
                    'layout': page.get('layout', ''),
                    'features': page.get('features', ''),
                    'dataStructure': page.get('dataStructure', ''),
                    'interaction': page.get('interaction', ''),
                    'userFlow': page.get('userFlow', ''),
                    'similarity': page.get('similarity', 'layout'),
                    'images': []
                }
                img_count = page.get('imageCount', 0)
                for _ in range(img_count):
                    if img_index < len(saved_image_names):
                        page_record['images'].append(saved_image_names[img_index])
                        img_index += 1
                record['pages'].append(page_record)
            
            record_path = os.path.join(project_folder, 'record.json')
            with open(record_path, 'w', encoding='utf-8') as f:
                json.dump(record, f, ensure_ascii=False, indent=2)
            
            # 保存 prompt（仅保存原始 prompt，enhanced_prompt 在线程内保存）
            # 注意：prompt_path 传递给线程，线程内模板注入后再保存完整版本
            
            # 获取当前选中的模型名称
            current_model = get_selected_model()
            current_model_name = current_model.get('name', '') if current_model else ''
            
            # 更新项目列表（带 generating 状态）
            projects = self.load_projects()
            new_project = {
                'id': project_id,
                'name': project_name,
                'model_name': current_model_name,
                'status': STATUS_GENERATING,
                'url': f'/projects/{project_id}/index.html',
                'date': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            projects.insert(0, new_project)
            self.save_projects(projects)
            
            # 注册异步任务（含流式传输字段 + 多轮生成扩展）
            generation_config = form_data.get('generationConfig', {})
            with tasks_lock:
                generating_tasks[project_id] = {
                    'status': STATUS_GENERATING,
                    'progress': 0,
                    'error': '',
                    'accumulated_content': '',   # 流式累积的完整文本
                    'stream_chunks': [],         # SSE 待推送的数据块
                    'stream_event': threading.Event(),  # 通知有新数据
                    'stream_lock': threading.Lock(),     # 保护 stream_chunks
                    # 多轮生成扩展字段
                    'round': 0,                          # 当前轮次 (0=未开始, 1=设计系统, 2=逐页, 3=组装)
                    'phase_description': '',             # 阶段描述
                    'page_progress': None,               # {current, total} 页面进度
                    'strategy': 'auto',                  # 生成策略
                    'a2ui_mode': generation_config.get('a2ui_mode', False),  # A2UI 组件树模式
                }
            
            # 启动后台线程
            def generate_in_background():
                # 将 project_id 绑定到线程对象，供 AI 调用时使用
                threading.current_thread()._project_id = project_id
                threading.current_thread()._page_name = project_name
                # 多轮生成跳过后续 iframe 组装标记
                skip_iframe_assembly = False
                try:
                    logger.info(f"[异步] 开始后台生成: {project_id}")

                    def is_cancelled():
                        with tasks_lock:
                            return (project_id in generating_tasks and
                                    generating_tasks[project_id].get('status') == 'cancelled')

                    # 更新进度
                    with tasks_lock:
                        if project_id in generating_tasks:
                            generating_tasks[project_id]['progress'] = 10
                    
                    # 增量处理
                    source_html_content = None
                    reused_pages = 0
                    if is_incremental and source_project_id and changes:
                        source_folder = os.path.join(PROJECTS_DIR, source_project_id)
                        source_html_path = os.path.join(source_folder, 'index.html')
                        if os.path.exists(source_html_path):
                            with open(source_html_path, 'r', encoding='utf-8') as f:
                                source_html_content = f.read()
                        
                        # 复制原项目图片
                        source_images_folder = os.path.join(source_folder, 'images')
                        dest_images_folder = os.path.join(project_folder, 'images')
                        if os.path.exists(source_images_folder):
                            import shutil
                            if not os.path.exists(dest_images_folder):
                                shutil.copytree(source_images_folder, dest_images_folder)
                        
                        reused_pages = len(changes.get('pagesUnchanged', []))
                    
                    with tasks_lock:
                        if project_id in generating_tasks:
                            generating_tasks[project_id]['progress'] = 20

                    # 发送模板框架事件给 Canvas Studio（在 AI 调用前，让框架卡片尽早出现）
                    _frame_path = os.path.join(project_folder, 'template', 'frame.html')
                    if os.path.exists(_frame_path):
                        _layout_type = template_layout_type or 'unknown'
                        _push_sse_event(project_id, 'template_frame', {
                            'layout_type': _layout_type,
                            'filename': 'template/frame.html'
                        })

                    # 检查是否已被取消
                    if is_cancelled():
                        logger.info(f"[异步] 任务已取消，中止生成: {project_id}")
                        # 保存部分内容
                        with tasks_lock:
                            if project_id in generating_tasks:
                                partial = generating_tasks[project_id].get('accumulated_content', '')
                        if partial:
                            self._save_partial_content(project_id, partial, project_folder)
                            # 通知 SSE 客户端
                            stream_event = generating_tasks.get(project_id, {}).get('stream_event')
                            if stream_event:
                                stream_event.set()
                        return

                    # 调用AI（这里复用现有逻辑）
                    enhanced_prompt = prompt

                    # 注入模板设计信息（CSS 设计令牌 + HTML 结构）
                    has_template_info = template_design_tokens or template_html_summary or template_frame_html
                    if has_template_info:
                        # ===== 统一模板注入策略 =====
                        template_section = "\n\n# 现有系统模板（必须严格遵循此模板的视觉风格！）\n\n"
                        template_section += "用户提供了一个现有系统的页面模板。你必须在保持此模板视觉风格的前提下，生成新的页面内容。\n"
                        template_section += "**绝对不能偏离模板的配色、字体、组件风格！**\n\n"

                        if template_design_tokens:
                            template_section += f"## 模板设计规范（从模板 CSS 提取）\n{template_design_tokens}\n\n"

                        if template_html_summary:
                            template_section += f"## 模板页面结构（供参考）\n以下是模板的 HTML 结构，你应当使用相同的布局模式和 CSS class：\n```html\n{template_html_summary}\n```\n\n"

                        if template_is_iframe and template_frame_html:
                            is_sidebar_mode = (template_layout_type == 'sidebar')

                            if is_sidebar_mode:
                                template_section += "## 模板注入说明\n"
                                template_section += "此模板采用「侧边栏 + 顶栏 + 内容区」布局。系统已保留完整的模板 HTML（含侧边栏、顶栏、CSS），你**只需生成主内容区域的 HTML 片段**。\n"
                                template_section += "**关键要求**：\n"
                                template_section += "- **不要**生成完整的 HTML 页面（不要 <!DOCTYPE html>、<html>、<head>、<body>）\n"
                                template_section += "- **不要**生成侧边栏、顶栏、导航栏（模板已有，系统会自动处理）\n"
                                template_section += "- 只生成主内容区域的 HTML 代码片段（即 `<div class=pageContent>` 内部的内容）\n"
                                _frame_hint = ''
                                if template_design_tokens and 'ant-' in (template_design_tokens + (pending_css or '')):
                                    _frame_hint = '（ant-btn、ant-table、ant-form、ant-card 等 Ant Design 组件）'
                                elif template_design_tokens and 'el-' in (template_design_tokens + (pending_css or '')):
                                    _frame_hint = '（el-button、el-table、el-form 等 Element UI 组件）'
                                template_section += f"- 使用模板已有的 CSS class{_frame_hint}，模板的 CSS 已全部内联\n"
                                template_section += "- 如需额外样式，用 `<style>` 标签包裹（会放在内容片段中）\n"
                                template_section += "- 可以使用 Vue 3 (CDN) 实现交互（搜索、过滤、弹窗等），用 `<script>` 标签包裹\n"
                                template_section += "- 如果使用 Vue，必须在 Vue 根元素（如 `<div id=\"app\">`）上添加 `v-cloak` 属性，并在 `<style>` 中加 `[v-cloak] { display: none; }`，防止模板未编译时显示 {{ }} 原始变量\n"
                                template_section += "- **不要**复制模板原始页面的特有数据字段（如「数据周期」「指标波动」等），按用户需求生成全新的内容\n\n"
                                template_section += "**修改侧边栏**（可选）：\n"
                                template_section += "- 如果需要在侧边栏添加新菜单项，在输出末尾加 `<!-- SIDEBAR_ADD: 菜单名称 -->`\n"
                                template_section += "- 如果需要设置某个菜单项为激活状态，在输出末尾加 `<!-- SIDEBAR_ACTIVE: 菜单名称 -->`\n\n"
                            else:
                                template_section += "## 框架说明\n"
                                template_section += "此模板采用「侧边栏 + 顶栏 + iframe 内容区」布局。\n"
                                template_section += "不要生成侧边栏、顶栏、导航等外框架元素，只生成 iframe 内部的页面内容。\n"
                                template_section += f"引用模板 CSS: `<link rel=\"stylesheet\" href=\"{template_css_path or 'template/template.css'}\">`\n"
                                template_section += "生成的 HTML 应该是一个完整的独立页面（有 <!DOCTYPE html>、<head>、<body>）。\n\n"

                            template_section += "## 侧边栏菜单（供参考，当前激活项用★标记）\n```\n"
                            sidebar_items = re.findall(r'<span>([^<]+)</span>', template_frame_html[:8000])
                            if not sidebar_items:
                                sidebar_items = re.findall(r'ant-menu-title-content>([^<]+)', template_frame_html[:8000])
                            if not sidebar_items:
                                sidebar_items = re.findall(r'<li[^>]*title="([^"]+)"', template_frame_html[:8000])
                            active_match = re.search(r'is-active[^>]*>.*?<span>([^<]+)</span>', template_frame_html[:8000], re.DOTALL)
                            if not active_match:
                                active_match = re.search(r'ant-menu-item-selected[^>]*>.*?title="([^"]+)"', template_frame_html[:8000], re.DOTALL)
                            active_text = active_match.group(1) if active_match else ''
                            for item in sidebar_items:
                                marker = ' ★ (当前激活)' if item == active_text else ''
                                template_section += f"  - {item}{marker}\n"
                            template_section += "```\n\n"

                            if len(template_frame_html) > 500:
                                template_section += f"## 框架 HTML 代码（参考其组件样式和 class 命名）\n```html\n{template_frame_html[:15000]}\n```\n\n"
                        else:
                            if template_css_path:
                                template_section += f"## 模板 CSS 文件\n已保存到 `{template_css_path}`，请在生成的 HTML 中通过 `<link rel=\"stylesheet\" href=\"{template_css_path}\">` 引用它。\n\n"

                        template_section += """## 模板还原要求（非常重要，必须严格遵守）
1. 配色方案**必须**与模板设计规范一致，不要自创配色
2. 组件样式（按钮、表格、表单、卡片、下拉框、输入框）必须与模板一致
3. 布局结构参考模板的页面结构
4. 字体、字号、间距与模板保持统一
5. 如果模板使用了 Ant Design，你也必须使用 Ant Design 风格的组件
6. 如果模板使用了 Element UI，你也必须使用 Element UI 风格的组件
7. 不要引入与模板不协调的 CDN 库（如果模板用的是 Ant Design，不要用 Tailwind 做布局）
8. 你可以在模板基础上添加新功能，但视觉风格绝对不能偏离

### 布局要求
- 生成的内容必须是**单个连续页面**，不要使用 Tab 标签页分页
- 页面应是可垂直滚动的长表单/长页面，所有内容在一个视图中
- 如果需要多个状态（如列表/编辑），使用按钮跳转而不是 Tab 切换
- **宽度自适应**：根容器不要使用 max-width、container、mx-auto 等限制宽度，使用 width: 100% 占满空间
- **高度填满**：页面根容器使用 min-height: 100vh，内容区使用 flex: 1 自适应高度
- **表格/网格溢出处理**：表格和网格等宽内容必须用 `overflow-x: auto` 的容器包裹
- **图表自适应**：图表容器使用 width: 100%，不要设置固定像素宽度

### 跨页面一致性要求
如果生成了多个页面，以下组件必须在所有页面中保持完全统一的样式：
1. **面包屑导航**: 所有页面的面包屑必须使用相同的 HTML 结构和 CSS 样式（只定义一次全局样式）
   - 统一使用: `<nav class="breadcrumb"><a>父级</a><span class="sep">/</span><span class="current">当前页</span></nav>`
   - 禁止每个页面各自定义不同的面包屑 CSS
2. **侧边栏/导航栏**: 所有页面共享同一套导航结构和样式
3. **基础组件**: 按钮、表格、表单、卡片等在不同页面中风格一致

### 侧边栏菜单修改（可选）
如果用户需求中提到要在侧边栏添加新菜单项，请在 HTML 代码最后添加：
```html
<!-- SIDEBAR_ADD: 菜单名称 -->
```
"""
                        enhanced_prompt += template_section

                    if is_incremental and source_html_content and reused_pages > 0:
                        incremental_hint = f"\n\n# 增量更新上下文（重要）\n这是一个增量更新任务。原项目中有{reused_pages}个页面内容未变化。请保持整体风格一致。"
                        if source_html_content:
                            html_preview = source_html_content[:15000]
                            incremental_hint += f"\n\n## 原项目HTML代码（供参考，请保持风格一致）\n```html\n{html_preview}\n```\n\n## 增量更新要求\n1. 保持原项目的整体设计风格、配色方案、组件风格\n2. 新增页面必须与已有页面风格一致\n3. 不要重新设计已有页面，除非用户明确要求"
                        enhanced_prompt += incremental_hint

                    # 保存完整 prompt（含模板注入后的版本）
                    try:
                        ep_path = os.path.join(project_folder, 'prompt.txt')
                        with open(ep_path, 'w', encoding='utf-8') as f:
                            f.write(enhanced_prompt)
                    except Exception:
                        pass

                    # 使用类似 call_ai_model 的逻辑
                    # ---- 上下文工程：策略选择 ----
                    ce_config = CONTEXT_ENGINEERING_CONFIG
                    pages_data = form_data.get('pages', [])
                    generation_strategy = data.get('generationStrategy', 'auto')

                    # 用户强制覆盖
                    if generation_strategy != 'auto':
                        ce_config = dict(ce_config)
                        ce_config['force_strategy'] = generation_strategy

                    # 合并前端高级配置到 ce_config
                    generation_config = form_data.get('generationConfig', {})
                    if generation_config.get('generationStrategy', 'auto') != 'auto':
                        ce_config['force_strategy'] = generation_config['generationStrategy']

                    strategy_result = determine_strategy(
                        prompt=enhanced_prompt,
                        page_count=len(pages_data),
                        image_count=len(images),
                        config=ce_config
                    )
                    logger.info(f"[策略] {strategy_result['strategy']}: {strategy_result['reason']} "
                                f"(估算 {strategy_result['estimated_tokens']} tokens)")

                    # 更新任务元数据
                    with tasks_lock:
                        if project_id in generating_tasks:
                            generating_tasks[project_id]['strategy'] = strategy_result['strategy']

                    if strategy_result['strategy'] == 'multi_round' and ce_config.get('enabled', True):
                        # 多轮生成路径
                        generator = MultiRoundGenerator(self, project_id, project_folder)
                        generator.generation_config = form_data.get('generationConfig', {})
                        html_content = generator.run(
                            prompt=enhanced_prompt,
                            pages_data=pages_data,
                            images=images,
                            global_config=form_data.get('global', {}),
                            template_tokens=template_design_tokens,
                            template_html_summary=template_html_summary,
                            template_css_path=template_css_path,
                            template_is_iframe=template_is_iframe,
                            template_frame_html=template_frame_html,
                            template_raw_frame_html=template_raw_frame_html,
                            template_layout_type=template_layout_type,
                            template_sidebar_meta=template_sidebar_meta
                        )
                        if html_content is None:
                            # 被取消
                            logger.info(f"[异步] 多轮生成被取消: {project_id}")
                            return
                        # 多轮生成：Route B 已生成完整 index.html，跳过 iframe 拼接
                        pages_dir = os.path.join(project_folder, 'pages')
                        if os.path.isdir(pages_dir):
                            skip_iframe_assembly = True
                        elif template_layout_type == 'sidebar':
                            skip_iframe_assembly = False
                        else:
                            skip_iframe_assembly = True
                    else:
                        # 原有单次调用路径
                        html_content = self._call_ai_for_async(enhanced_prompt, images)
                    
                    with tasks_lock:
                        if project_id in generating_tasks:
                            generating_tasks[project_id]['progress'] = 80

                    # AI 完成后再检查是否已取消
                    if is_cancelled():
                        logger.info(f"[异步] 任务已取消（AI完成后），丢弃结果: {project_id}")
                        return

                    if not html_content:
                        raise Exception("AI未返回有效内容")

                    # 后处理计时开始
                    post_start = time.time()

                    # 下载图片（并行）
                    html_content = download_html_images(html_content, project_folder)
                    logger.info(f"[性能] 图片下载耗时: {time.time() - post_start:.2f}s")

                    # 单次调用路径：保存页面文件到 pages/ 目录，与多轮生成保持一致
                    if strategy_result['strategy'] != 'multi_round':
                        pages_dir = os.path.join(project_folder, 'pages')
                        os.makedirs(pages_dir, exist_ok=True)
                        # 生成安全的页面文件名
                        page_safe_name = project_name
                        page_filename = f"page_0_{page_safe_name}.html"
                        page_file_path = os.path.join(pages_dir, page_filename)
                        # 修正相对路径：pages/ 子目录中的页面需要 ../template/ 而非 template/
                        pages_html = html_content.replace('href="template/template.css"', 'href="../template/template.css"')
                        pages_html = pages_html.replace("href='template/template.css'", "href='../template/template.css'")
                        with open(page_file_path, 'w', encoding='utf-8') as f:
                            f.write(pages_html)
                        logger.info(f"[单次] 已保存页面文件: pages/{page_filename} ({len(pages_html)} 字符)")

                    # iframe 框架拼接：iframe/srcdoc 和 sidebar 模式都拼框架
                    if not skip_iframe_assembly and template_is_iframe and (template_raw_frame_html or template_frame_html):
                        if template_layout_type == 'iframe':
                            logger.info("[异步组装] 检测到 iframe 框架布局，拼接框架+内容...")
                        else:
                            logger.info("[异步组装] 检测到 sidebar 框架布局，模板注入内容区...")
                        frame_to_use = template_raw_frame_html or template_frame_html
                        html_content = self.assemble_iframe_html(html_content, frame_to_use, template_css_path, template_sidebar_meta, project_name)

                    # 注入导航监听器
                    html_content = self.inject_page_navigation_listener(html_content)

                    logger.info(f"[性能] 后处理总耗时: {time.time() - post_start:.2f}s")

                    # 保存HTML（审查前先保存拼接后的完整页面）
                    html_path = os.path.join(project_folder, 'index.html')
                    if os.path.exists(os.path.dirname(html_path)):
                        with open(html_path, 'w', encoding='utf-8') as f:
                            f.write(html_content)

                    # 单次调用路径：发送 page_written 和 complete 事件给 Canvas Studio
                    if strategy_result['strategy'] != 'multi_round':
                        _push_sse_event(project_id, 'page_written', {
                            'page': project_name, 'index': 0,
                            'path': f'pages/{page_filename}',
                            'size': len(html_content)
                        })
                        _push_sse_event(project_id, 'complete', {
                            'total_pages': 1, 'status': 'success'
                        })

                    # ===== 生成后 AI 智能审查 + 修复 =====
                    # 注意：多轮生成路径已在 server_context_engineering.py 内部完成审查，此处不再重复
                    if strategy_result['strategy'] != 'multi_round':
                        try:
                            def _push_review_event(data):
                                with tasks_lock:
                                    if project_id in generating_tasks:
                                        task = generating_tasks[project_id]
                                        with task.get('stream_lock', threading.Lock()):
                                            task['stream_chunks'].append(json.dumps({
                                                'type': 'diagnostic',
                                                'data': data
                                            }, ensure_ascii=False))
                                        evt = task.get('stream_event')
                                        if evt:
                                            evt.set()

                            # 提取页面名（从 project_name 去掉时间戳）
                            review_page_name = project_name
                            _ts_match = re.search(r'_(\d{4}\d{2}\d{2})_\d', project_name)
                            if _ts_match:
                                review_page_name = project_name[:_ts_match.start()].strip()

                            # 构建侧边栏上下文
                            review_sidebar_context = ''
                            if template_sidebar_meta and template_sidebar_meta.get('menu_items'):
                                sm = template_sidebar_meta
                                menu_texts = [m for m in sm['menu_items'] if m]
                                review_sidebar_context = (
                                    f"\n### 模板侧边栏信息\n"
                                    f"- 框架: {sm.get('framework', 'unknown')}\n"
                                    f"- 菜单项: {', '.join(menu_texts)}\n"
                                    f"- 当前页面名: {review_page_name}\n"
                                    f"- 激活态 CSS 类: {', '.join(sm.get('active_classes', []))}\n"
                                )

                            # 构建用户需求摘要
                            review_prompt_summary = ''
                            if prompt:
                                review_prompt_summary = (
                                    f"\n### 用户原始需求\n"
                                    f"```\n{prompt[:500]}\n"
                                    f"{'...(已截断)' if len(prompt) > 500 else ''}\n```\n"
                                )

                            html_content, _review_edits, _review_summary = self._run_code_review(
                                html_content, review_page_name, project_id, project_folder,
                                prompt_summary=review_prompt_summary,
                                sidebar_context=review_sidebar_context,
                                push_event_fn=_push_review_event
                            )

                        except Exception as diag_err:
                            import traceback
                            logger.warning(
                                f"[审查] AI 审查失败（不影响生成结果）: "
                                f"{diag_err}")
                            logger.debug(
                                f"[审查] 异常堆栈:\n"
                                f"{traceback.format_exc()}")

                    # ===== CSS 变量自动补全 =====
                    # 从设计系统中读取 CSS 变量，注入到使用了但未定义的页面中
                    try:
                        state_path = os.path.join(project_folder, 'multi_round_state.json')
                        ds_css_vars = ''
                        if os.path.exists(state_path):
                            with open(state_path, 'r', encoding='utf-8') as f:
                                state = json.load(f)
                            ds_css_vars = (state.get('design_system') or {}).get('css_variables', '')

                        if ds_css_vars:
                            # 对 pages/ 目录下的每个页面文件注入
                            _pages_dir = os.path.join(project_folder, 'pages')
                            if os.path.isdir(_pages_dir):
                                for _pf in os.listdir(_pages_dir):
                                    if not _pf.endswith('.html'):
                                        continue
                                    _ppath = os.path.join(_pages_dir, _pf)
                                    with open(_ppath, 'r', encoding='utf-8') as f:
                                        _phtml = f.read()
                                    _fixed = inject_missing_css_variables(_phtml, ds_css_vars)
                                    if _fixed is not _phtml:
                                        with open(_ppath, 'w', encoding='utf-8') as f:
                                            f.write(_fixed)

                            # 对最终 index.html 也注入
                            html_content = inject_missing_css_variables(html_content, ds_css_vars)
                            with open(html_path, 'w', encoding='utf-8') as f:
                                f.write(html_content)
                    except Exception as css_inject_err:
                        logger.warning(f"[CSS注入] 自动补全失败（不影响功能）: {css_inject_err}")

                    # 更新项目状态（仅在项目仍存在时）
                    projects = self.load_projects()
                    project_still_exists = any(p['id'] == project_id for p in projects)
                    if project_still_exists:
                        for p in projects:
                            if p['id'] == project_id:
                                p['status'] = None  # 清除 generating 状态
                                break
                        self.save_projects(projects)
                    
                    # 更新record.json状态
                    if os.path.exists(record_path):
                        with open(record_path, 'r', encoding='utf-8') as f:
                            record = json.load(f)
                        record['status'] = STATUS_COMPLETED
                        with open(record_path, 'w', encoding='utf-8') as f:
                            json.dump(record, f, ensure_ascii=False, indent=2)
                    
                    with tasks_lock:
                        generating_tasks[project_id]['status'] = STATUS_COMPLETED
                        generating_tasks[project_id]['progress'] = 100
                        # 通知 SSE 客户端
                        stream_event = generating_tasks[project_id].get('stream_event')
                        if stream_event:
                            stream_event.set()
                    
                    logger.info(f"[异步] 生成完成: {project_id}")

                except Exception as e:
                    # 如果是已取消的任务，保存部分内容后退出
                    if is_cancelled():
                        logger.info(f"[异步] 任务已取消，忽略错误: {project_id}")
                        with tasks_lock:
                            if project_id in generating_tasks:
                                partial = generating_tasks[project_id].get('accumulated_content', '')
                        if partial:
                            self._save_partial_content(project_id, partial, project_folder)
                        return

                    logger.info(f"[异步错误] {project_id}: {e}")
                    import traceback
                    traceback.print_exc()

                    # 保存部分内容
                    with tasks_lock:
                        if project_id in generating_tasks:
                            partial = generating_tasks[project_id].get('accumulated_content', '')
                    if partial and len(partial) > 100:
                        self._save_partial_content(project_id, partial, project_folder)

                    # 更新失败状态
                    with tasks_lock:
                        if project_id in generating_tasks:
                            generating_tasks[project_id]['status'] = STATUS_FAILED
                            generating_tasks[project_id]['error'] = str(e)
                            # 通知 SSE 客户端
                            stream_event = generating_tasks[project_id].get('stream_event')
                            if stream_event:
                                stream_event.set()

                    # 更新项目列表状态（仅在项目仍存在时）
                    projects = self.load_projects()
                    if any(p['id'] == project_id for p in projects):
                        for p in projects:
                            if p['id'] == project_id:
                                p['status'] = STATUS_FAILED
                                break
                    self.save_projects(projects)
            
            # 启动线程
            thread = threading.Thread(target=generate_in_background, daemon=True)
            thread.start()
            
            logger.info(f"[异步] 项目已创建，后台生成中: {project_id}")
            self.send_json_response({
                'success': True,
                'project': new_project,
                'async': True
            })
            
        except Exception as e:
            logger.error(f"[错误] 异步生成启动失败: {e}")
            import traceback
            traceback.print_exc()
            self.send_error_response(str(e))

    def _call_ai_for_async(self, prompt, images):
        """异步生成专用的 AI 调用 —— 流式版本，将数据块推送到 generating_tasks"""
        thread_project_id = getattr(threading.current_thread(), '_project_id', None)

        accumulated_content = ""
        _last_streaming_push = 0  # 流式 HTML 推送位置追踪

        # 发送 phase 事件给 Canvas Studio，触发占位卡片创建
        if thread_project_id:
            page_name = getattr(threading.current_thread(), '_page_name', None) or '页面'
            _push_sse_event(thread_project_id, 'phase', {
                'round': 2, 'step': 'page_0', 'status': 'running',
                'label': page_name, 'progress': {'current': 1, 'total': 1}
            })

        gen = self.call_ai_model_streaming(prompt, images, cancellable_project_id=thread_project_id)

        try:
            for chunk_text, full_content, done, _tool_calls, *_rest in gen:

                accumulated_content = full_content

                # 推送数据块到 SSE 缓冲区
                if thread_project_id and thread_project_id in generating_tasks:
                    with tasks_lock:
                        task = generating_tasks[thread_project_id]
                        task['accumulated_content'] = accumulated_content

                        # 处理推理/思考内容：转为 [think] 前缀推送给前端
                        reasoning_text = _rest[0] if _rest else ''
                        push_text = ''
                        if chunk_text and not chunk_text.startswith('[think]'):
                            push_text = chunk_text
                        elif reasoning_text:
                            push_text = '[think]' + reasoning_text

                        if push_text:
                            stream_lock = task.get('stream_lock')
                            if stream_lock:
                                with stream_lock:
                                    task['stream_chunks'].append(push_text)

                            # 通知 SSE 端点
                            stream_event = task.get('stream_event')
                            if stream_event:
                                stream_event.set()

                            # 启发式进度更新（20 ~ 80 区间）
                            if not push_text.startswith('[think]'):
                                estimated = min(80, 20 + len(accumulated_content) // 100)
                                task['progress'] = estimated

                    # ---- 流式 HTML 实时预览推送 ----
                    page_name = getattr(threading.current_thread(), '_page_name', None) or ''
                    _last_streaming_push = _maybe_push_streaming_html(
                        thread_project_id, accumulated_content,
                        _last_streaming_push, page_name
                    )

                if done:
                    break
        finally:
            try:
                gen.close()
            except RuntimeError:
                pass

        t0 = time.time()
        result = self.extract_html(accumulated_content)
        logger.info(f"[性能] extract_html 耗时: {time.time() - t0:.3f}s (输入 {len(accumulated_content)} 字符)")

        # 发送 preview 事件给 Canvas Studio，更新卡片缩略图
        if result and thread_project_id:
            page_name = getattr(threading.current_thread(), '_page_name', None) or '页面'
            _push_sse_event(thread_project_id, 'preview', {
                'page': page_name, 'html_fragment': result
            })

        return result

    def copy_project(self, source_project_id, new_project_name):
        """复制项目（当内容完全无变化时）"""
        try:
            import shutil
            
            source_folder = os.path.join(PROJECTS_DIR, source_project_id)
            if not os.path.exists(source_folder):
                self.send_error_response(f"源项目不存在: {source_project_id}")
                return
            
            # 生成新项目ID
            project_id = generate_project_id(new_project_name)
            project_folder = os.path.join(PROJECTS_DIR, project_id)
            
            # 如果目标目录已存在，先删除
            if os.path.exists(project_folder):
                shutil.rmtree(project_folder)
            
            # 复制整个文件夹
            shutil.copytree(source_folder, project_folder)
            logger.info(f"[复制] {source_folder} -> {project_folder}")
            
            # 更新record.json的时间戳
            record_path = os.path.join(project_folder, 'record.json')
            if os.path.exists(record_path):
                with open(record_path, 'r', encoding='utf-8') as f:
                    record = json.load(f)
                record['createdAt'] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                record['copiedFrom'] = source_project_id
                with open(record_path, 'w', encoding='utf-8') as f:
                    json.dump(record, f, ensure_ascii=False, indent=2)
            
            # 更新项目列表
            projects = self.load_projects()
            new_project = {
                'id': project_id,
                'name': new_project_name,
                'url': f'/projects/{project_id}/index.html',
                'date': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            # 检查是否已存在（避免重复）
            existing_idx = next((i for i, p in enumerate(projects) if p['id'] == project_id), None)
            if existing_idx is not None:
                projects[existing_idx] = new_project
            else:
                projects.insert(0, new_project)
            self.save_projects(projects)
            
            logger.info(f"[完成] 项目已复制: {project_folder} (0 API调用)")
            self.send_json_response({
                'success': True, 
                'project': new_project,
                'incremental': True,
                'reusedPages': 'all',
                'message': '内容无变化，已复制原项目'
            })
            
        except Exception as e:
            logger.error(f"[错误] 复制失败: {e}")
            import traceback
            traceback.print_exc()
            self.send_error_response(str(e))

    @staticmethod
    def compress_image_for_api(base64_data, max_size=1536, quality=85, max_bytes=2*1024*1024):
        """压缩 base64 图片，控制尺寸和质量，确保不超过大小限制

        Args:
            base64_data: data:image/xxx;base64,... 格式的 base64 字符串
            max_size: 最大边长（像素），默认 1024
            quality: JPEG 压缩质量（1-100），默认 75
            max_bytes: 压缩后最大字节数，默认 1MB
        Returns:
            压缩后的 data:image/jpeg;base64,... 字符串
        """
        try:
            # 分离 header 和 data
            if ',' in base64_data:
                _, data = base64_data.split(',', 1)
            else:
                data = base64_data

            image_bytes = base64.b64decode(data)

            # 如果已经小于限制，直接返回
            if len(image_bytes) <= max_bytes:
                return base64_data

            img = Image.open(io.BytesIO(image_bytes))

            # 转换 RGBA/P 模式为 RGB
            if img.mode in ('RGBA', 'P', 'LA'):
                background = Image.new('RGB', img.size, (255, 255, 255))
                if img.mode == 'P':
                    img = img.convert('RGBA')
                if img.mode == 'LA':
                    img = img.convert('RGBA')
                background.paste(img, mask=img.split()[-1])
                img = background
            elif img.mode != 'RGB':
                img = img.convert('RGB')

            # 等比缩放
            w, h = img.size
            if w > max_size or h > max_size:
                ratio = min(max_size / w, max_size / h)
                new_w = int(w * ratio)
                new_h = int(h * ratio)
                img = img.resize((new_w, new_h), Image.LANCZOS)

            # 压缩为 JPEG，逐步降低质量直到满足大小限制
            current_quality = quality
            while current_quality >= 30:
                buffer = io.BytesIO()
                img.save(buffer, format='JPEG', quality=current_quality, optimize=True)
                compressed = buffer.getvalue()
                if len(compressed) <= max_bytes:
                    b64 = base64.b64encode(compressed).decode('utf-8')
                    logger.info(f"[图片压缩] {w}x{h} -> {img.size[0]}x{img.size[1]}, "
                                f"质量={current_quality}, {len(image_bytes)//1024}KB -> {len(compressed)//1024}KB")
                    return f"data:image/jpeg;base64,{b64}"
                current_quality -= 10

            # 极端情况：缩小尺寸再压缩
            img = img.resize((img.size[0]//2, img.size[1]//2), Image.LANCZOS)
            buffer = io.BytesIO()
            img.save(buffer, format='JPEG', quality=30, optimize=True)
            compressed = buffer.getvalue()
            b64 = base64.b64encode(compressed).decode('utf-8')
            logger.info(f"[图片压缩] 极端压缩: {w}x{h} -> {img.size[0]}x{img.size[1]}, "
                        f"{len(image_bytes)//1024}KB -> {len(compressed)//1024}KB")
            return f"data:image/jpeg;base64,{b64}"
        except Exception as e:
            logger.error(f"[图片压缩] 失败，使用原图: {e}")
            return base64_data

    def _is_claude_api(self, selected_model):
        """判断模型是否使用 Claude/Anthropic API 格式"""
        api_format = selected_model.get('api_format', '').lower()
        if api_format == 'claude':
            return True
        # 自动检测：provider 或 base_url 包含 anthropic 关键字
        provider = (selected_model.get('provider') or '').lower()
        base_url = (selected_model.get('base_url') or '').lower()
        if 'anthropic' in provider or 'claude' in provider or 'anthropic' in base_url:
            return True
        return False

    def _build_claude_image_content(self, compressed_data_url):
        """将 OpenAI 格式的图片 data URL 转换为 Claude API 格式"""
        # compressed_data_url 格式: "data:image/jpeg;base64,xxxx"
        try:
            parts = compressed_data_url.split(',', 1)
            if len(parts) != 2:
                return None
            header = parts[0]  # "data:image/jpeg;base64"
            b64_data = parts[1]
            # 提取 media_type
            media_type = header.replace('data:', '').replace(';base64', '')
            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": b64_data
                }
            }
        except Exception:
            return None

    def _build_claude_messages(self, messages, api_format_claude):
        """将 OpenAI 格式消息列表转换为 Claude 格式，提取 system prompt 为顶层参数

        多条 system 消息会合并为一条（用换行分隔），避免丢失上下文。
        OpenAI 的 role:tool 消息会转换为 Claude 的 tool_result 格式。
        OpenAI 的 assistant+tool_calls 消息会转换为 Claude 的 tool_use 格式。

        Returns:
            (system_prompt_str_or_None, claude_messages)
        """
        system_parts = []
        claude_msgs = []
        # 收集连续的 tool 消息，合并为一条 user 消息
        pending_tool_results = []

        def flush_tool_results():
            """将累积的 tool results 作为一条 user 消息发出"""
            nonlocal pending_tool_results
            if not pending_tool_results:
                return
            claude_msgs.append({
                "role": "user",
                "content": pending_tool_results
            })
            pending_tool_results = []

        for msg in messages:
            role = msg.get('role', '')
            content = msg.get('content', '')

            if role == 'system':
                flush_tool_results()
                if isinstance(content, str) and content.strip():
                    system_parts.append(content)
                continue

            if not api_format_claude:
                flush_tool_results()
                claude_msgs.append(msg)
                continue

            # ---- Claude 格式转换 ----

            if role == 'tool':
                # OpenAI: {"role": "tool", "tool_call_id": "...", "content": "..."}
                # Claude: 作为 user 消息中的 tool_result content block
                flush_tool_results()
                tool_result = {
                    "type": "tool_result",
                    "tool_use_id": msg.get('tool_call_id', ''),
                    "content": content if isinstance(content, str) else str(content or '')
                }
                pending_tool_results.append(tool_result)
                continue

            if role == 'assistant' and msg.get('tool_calls'):
                # OpenAI: {"role": "assistant", "content": "...", "tool_calls": [...]}
                # Claude: {"role": "assistant", "content": [text_block, tool_use_blocks...]}
                flush_tool_results()
                content_blocks = []
                # 文本部分
                text = content or ''
                if text:
                    content_blocks.append({"type": "text", "text": text})
                # tool_use 部分
                for tc in msg['tool_calls']:
                    fn = tc.get('function', {})
                    args_str = fn.get('arguments', '{}')
                    try:
                        args = json.loads(args_str) if isinstance(args_str, str) else args_str
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    content_blocks.append({
                        "type": "tool_use",
                        "id": tc.get('id', ''),
                        "name": fn.get('name', ''),
                        "input": args
                    })
                claude_msgs.append({"role": "assistant", "content": content_blocks})
                continue

            if role == 'assistant' and content is None:
                # Claude 要求 assistant 消息有 content
                flush_tool_results()
                claude_msgs.append({"role": "assistant", "content": ""})
                continue

            # 普通消息（user / assistant 无 tool_calls）
            flush_tool_results()
            if isinstance(content, list):
                new_content = []
                for block in content:
                    if isinstance(block, dict) and block.get('type') == 'image_url':
                        img_url = block.get('image_url', {}).get('url', '')
                        claude_img = self._build_claude_image_content(img_url)
                        if claude_img:
                            new_content.append(claude_img)
                        else:
                            new_content.append({"type": "text", "text": "[图片]"})
                    else:
                        new_content.append(block)
                claude_msgs.append({"role": role, "content": new_content})
            else:
                claude_msgs.append({"role": role, "content": content})

        flush_tool_results()

        # Claude API 要求消息严格交替 user/assistant，不能有连续相同角色
        # 合并相邻的同角色消息
        merged = []
        for msg in claude_msgs:
            if (merged
                    and merged[-1].get('role') == msg.get('role')
                    and isinstance(merged[-1].get('content'), str)
                    and isinstance(msg.get('content'), str)):
                # 合并文本内容
                merged[-1]['content'] += '\n' + msg['content']
            elif (merged
                    and merged[-1].get('role') == 'assistant'
                    and msg.get('role') == 'assistant'
                    and isinstance(merged[-1].get('content'), list)
                    and isinstance(msg.get('content'), list)):
                # 合并 content blocks (tool_use 等)
                merged[-1]['content'] = merged[-1]['content'] + msg['content']
            elif (merged
                    and merged[-1].get('role') == 'assistant'
                    and msg.get('role') == 'assistant'):
                # 一个是 list 一个是 str，统一转为 list 再合并
                old = merged[-1]['content']
                new_content = msg['content']
                old_blocks = old if isinstance(old, list) else [{"type": "text", "text": old}]
                new_blocks = new_content if isinstance(new_content, list) else [{"type": "text", "text": new_content}]
                merged[-1]['content'] = old_blocks + new_blocks
            else:
                merged.append(msg)
        claude_msgs = merged

        # Claude 要求消息不能以 assistant 结尾（必须以 user 结尾才能继续对话）
        if claude_msgs and claude_msgs[-1].get('role') == 'assistant':
            claude_msgs.append({"role": "user", "content": "请继续。"})

        system_prompt = '\n\n'.join(system_parts) if system_parts else None
        return system_prompt, claude_msgs

    def _build_request_params(self, selected_model, messages, model_name, max_tokens,
                               temperature, stream=False, tools=None):
        """根据 api_format 构建 URL、headers、payload

        Returns:
            (url, headers, payload, is_claude)
        """
        base_url = selected_model.get('base_url', API_CONFIG.get('base_url', ''))
        api_key = selected_model.get('api_key', API_CONFIG.get('api_key', ''))
        is_claude = self._is_claude_api(selected_model)
        thinking_mode = selected_model.get('thinking_mode', False)

        if is_claude:
            # Claude API: 提取 system prompt，转换消息格式
            system_prompt, claude_msgs = self._build_claude_messages(messages, True)

            url = base_url.rstrip('/') + '/messages'
            headers = {
                'Content-Type': 'application/json',
                'x-api-key': api_key,
                'anthropic-version': '2023-06-01'
            }
            payload = {
                "model": model_name,
                "messages": claude_msgs,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
            if system_prompt:
                payload["system"] = system_prompt
            if stream:
                payload["stream"] = True
            # Claude 的 tool 格式与 OpenAI 不同，此处暂不转换，
            # 如需要可后续扩展
            if tools:
                payload["tools"] = self._convert_openai_tools_to_claude(tools)
            # Claude thinking 模式：开启时使用 extended thinking
            if thinking_mode:
                # Claude extended thinking 需要设置 thinking 参数和调整 max_tokens 为 budget_tokens
                payload["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": min(10000, max_tokens // 4) if max_tokens else 5000
                }
                # thinking 模式下 max_tokens 表示总预算（含 thinking + output）
                payload["max_tokens"] = max_tokens if max_tokens else 16000
        else:
            # OpenAI 兼容格式
            url = base_url.rstrip('/') + '/chat/completions'
            headers = {
                'Content-Type': 'application/json',
                'Authorization': f"Bearer {api_key}"
            }
            payload = {
                "model": model_name,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
            if stream:
                payload["stream"] = True
            if tools:
                payload["tools"] = tools
                payload["tool_choice"] = "auto"
            # OpenAI 兼容模型的思考模式控制
            if not thinking_mode:
                model_lower = model_name.lower()
                # Qwen 系列通过 enable_thinking: false 关闭
                if 'qwen' in model_lower or 'qwq' in model_lower:
                    payload["enable_thinking"] = False
                # DeepSeek 系列默认不开思考，无需额外参数
                # Gemini 通过 OpenAI 兼容接口无法直接关闭，靠 temperature 控制
                # 其他 OpenAI 兼容模型：不传 thinking 相关参数即为关闭
            else:
                model_lower = model_name.lower()
                if 'qwen' in model_lower or 'qwq' in model_lower:
                    payload["enable_thinking"] = True

        return url, headers, payload, is_claude

    def _convert_openai_tools_to_claude(self, openai_tools):
        """将 OpenAI function calling tools 格式转换为 Claude tools 格式"""
        claude_tools = []
        for tool in (openai_tools or []):
            func = tool.get('function', {})
            claude_tools.append({
                "name": func.get('name', ''),
                "description": func.get('description', ''),
                "input_schema": func.get('parameters', {"type": "object", "properties": {}})
            })
        return claude_tools

    def _parse_claude_non_streaming(self, result):
        """解析 Claude 非流式响应，返回 (content, finish_reason)"""
        content = ''
        finish_reason = ''
        # Claude 响应格式: {"content": [{"type": "text", "text": "..."}], "stop_reason": "end_turn"}
        content_blocks = result.get('content', [])
        for block in content_blocks:
            if block.get('type') == 'text':
                content += block.get('text', '')
        stop_reason = result.get('stop_reason', '')
        if stop_reason == 'end_turn':
            finish_reason = 'stop'
        elif stop_reason == 'max_tokens':
            finish_reason = 'length'
        else:
            finish_reason = stop_reason
        return content, finish_reason

    def call_ai_model(self, prompt, images, cancellable_project_id=None):
        """调用AI大模型 (使用 requests 库)

        Args:
            cancellable_project_id: 如果提供，将活跃 session 存入 generating_tasks 以支持外部中断
        """
        try:
            # 构建消息
            user_content = []
            user_content.append({
                "type": "text",
                "text": prompt
            })

            # 添加图片（压缩后）
            for img_base64 in images:
                compressed = self.compress_image_for_api(img_base64)
                user_content.append({
                    "type": "image_url",
                    "image_url": {"url": compressed}
                })

            # 从配置读取 system prompt
            system_prompt = AI_OPTIONS.get('system_prompt',
                'You are an expert UI/UX designer and senior frontend engineer specializing in high-fidelity HTML prototypes. '
                'CRITICAL OUTPUT FORMAT - You MUST follow this exact structure:\n'
                '<artifact type="html">\n'
                '(Your complete HTML code here)\n'
                '</artifact>\n\n'
                'RULES:\n'
                '1. Always wrap your HTML output in <artifact type="html">...</artifact> tags.\n'
                '2. Do NOT include any text before <artifact> or after </artifact>.\n'
                '3. Do NOT wrap in ```html``` markdown code blocks.\n'
                '4. When reference images or HTML templates are provided, reproduce the design as accurately as possible '
                'using HTML + Tailwind CSS. When an existing system HTML template is provided, match its design language exactly.\n'
                '5. Use real Chinese data, never use Lorem ipsum.\n'
                'Layout rules: use min-height:100vh for page root, wrap tables in overflow-x:auto containers, '
                'never use max-width or container class on root elements, ensure content fills available space.\n\n'
                'VISUAL QUALITY STANDARDS:\n'
                '- Page background: use #f5f7fa or #f0f2f5, NOT pure white. Cards should be white on gray background.\n'
                '- Shadows: use layered multi-box-shadows (e.g., box-shadow: 0 1px 2px rgba(0,0,0,0.04), 0 4px 12px rgba(0,0,0,0.08)), NOT flat single-layer shadows.\n'
                '- Buttons: must have hover color change + active scale(0.98) + smooth 0.2s transition.\n'
                '- Cards: must have hover shadow-deepen + translateY(-2px) + 0.3s transition.\n'
                '- Inputs: must have focus border-color change + outer glow (box-shadow: 0 0 0 3px rgba(primary,0.15)).\n'
                '- Tables: header bg #fafafa, row-height 54px, zebra stripes, hover highlight.\n'
                '- Border-radius: cards 12px, buttons/inputs 6-8px, badges 4px.\n'
                '- Typography: title font-weight 600, body 400. Size scale: page-title 20-24px, section-title 16-18px, body 14px, caption 12px.\n'
                '- Spacing: based on 4px grid (8, 12, 16, 20, 24, 32). Card padding 20-24px, page padding 24-32px.\n'
                '- Colors: primary only for key actions/active states. Use rgba variants for backgrounds. Text hierarchy: #1f1f1f > #595959 > #8c8c8c > #bfbfbf.\n'
                '- Micro-interactions: ALL interactive elements must have smooth transitions (0.2-0.3s ease).\n'
                '- Icons: use FontAwesome to enhance information density, not text-only layouts.\n'
                '- Empty states: centered large icon + gray description text + action button.')

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ]

            # 动态获取当前选中模型配置
            selected_model = get_selected_model()
            model_name = selected_model.get('model', API_CONFIG.get('model', 'gpt-4'))
            logger.info(f"[AI] 使用模型: {selected_model.get('name', model_name)} ({model_name})")

            # 准备请求数据（优先使用模型配置，fallback 到全局配置）
            max_tokens = selected_model.get('max_tokens') or AI_OPTIONS.get('max_tokens', 100000)
            temperature = selected_model.get('temperature') or AI_OPTIONS.get('temperature', 0.7)

            url, headers, payload, is_claude = self._build_request_params(
                selected_model, messages, model_name, max_tokens, temperature
            )
            
            timeout = selected_model.get('timeout') or AI_OPTIONS.get('timeout', 300)
            logger.info(f"[AI] 正在调用大模型... (超时: {timeout}s)")
            
            result = None
            max_retries = 3
            last_error = None
            
            for attempt in range(max_retries):
                try:
                    if attempt > 0:
                        logger.info(f"[AI] 重试第 {attempt+1} 次...")
                        
                    # 每次重试创建新 Session，确保无状态污染
                    session = requests.Session()
                    session.trust_env = False # 强制直连，不使用系统代理 (针对国内 API 域名优化)

                    # 存储活跃 session，支持外部中断 HTTP 请求
                    if cancellable_project_id:
                        with tasks_lock:
                            if cancellable_project_id in generating_tasks:
                                generating_tasks[cancellable_project_id]['session'] = session

                    # 伪装浏览器，并禁用长连接
                    session.headers.update({
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                        'Connection': 'close'
                    })
                    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                    # 使用 session 发送请求，verify=False 忽略 SSL 验证
                    response = session.post(
                        url,
                        json=payload,
                        headers=headers,
                        timeout=timeout,
                        verify=False
                    )

                    response.raise_for_status() # 检查 HTTP 错误
                    result = response.json()
                    break # 成功则跳出循环
                except Exception as e:
                    logger.info(f"[AI] 调用失败 (第 {attempt+1}/{max_retries} 次): {e}")
                    last_error = e
                    if attempt < max_retries - 1:
                        import time
                        time.sleep(1)
            
            # 如果 requests 全部失败，尝试使用 curl 命令行兜底
            if not result:
                logger.info("[AI] 尝试使用 curl 命令行兜底...")
                result = self.call_ai_model_via_curl(url, headers, payload, timeout)
            
            if not result:
                raise last_error

            # 根据格式解析响应
            if is_claude:
                content, finish_reason = self._parse_claude_non_streaming(result)
            else:
                content = result['choices'][0]['message']['content']
                finish_reason = result['choices'][0].get('finish_reason', '')

            logger.info(f"[AI] 响应长度: {len(content)} 字符, finish_reason: {finish_reason}")

            if finish_reason == 'length':
                logger.warning("[警告] AI响应可能被截断!")

            # 提取HTML代码
            return self.extract_html(content)

        except Exception as e:
            logger.error(f"[AI错误] {e}")
            import traceback
            traceback.print_exc()
            raise

    def call_ai_model_streaming(self, prompt_or_messages, images=None,
                                cancellable_project_id=None, tools=None):
        """流式调用 AI 大模型，逐步产出内容块。

        Args:
            prompt_or_messages: 字符串(prompt+images 构建消息) 或 预构建的 messages 列表
            images: base64 图片列表（仅 prompt_or_messages 为字符串时使用）
            cancellable_project_id: 用于支持外部中断的项目 ID
            tools: OpenAI function calling tools 定义列表

        Yields:
            (chunk_text, accumulated_content, done, tool_calls) 元组
            tool_calls: dict {id: {name, arguments}} 或 None
        """
        # 构建消息
        if isinstance(prompt_or_messages, list):
            messages = prompt_or_messages
        else:
            user_content = [{"type": "text", "text": prompt_or_messages}]
            for img_base64 in (images or []):
                compressed = self.compress_image_for_api(img_base64)
                user_content.append({
                    "type": "image_url",
                    "image_url": {"url": compressed}
                })
            # A2UI 模式使用专用系统提示
            a2ui_mode = False
            if cancellable_project_id:
                with tasks_lock:
                    task = generating_tasks.get(cancellable_project_id, {})
                    a2ui_mode = task.get('a2ui_mode', False)

            if a2ui_mode:
                from a2ui_protocol import A2UI_SYSTEM_PROMPT
                system_prompt = A2UI_SYSTEM_PROMPT
            else:
                system_prompt = AI_OPTIONS.get('system_prompt',
                    'You are an HTML code generator for high-fidelity UI prototypes. '
                    'CRITICAL OUTPUT RULES:\n'
                    '1. Output ONLY raw HTML code. Start your response with <!DOCTYPE html> or <div> immediately.\n'
                    '2. Do NOT include any explanation, commentary, greeting, or summary before or after the code.\n'
                    '3. Do NOT wrap code in ```html``` markdown code blocks.\n'
                    '4. When reference images or HTML templates are provided, reproduce the design as accurately as possible '
                    'using HTML + Tailwind CSS. When an existing system HTML template is provided, match its design language exactly.\n'
                    '5. Use real Chinese data, never use Lorem ipsum.\n'
                    'Layout rules: use min-height:100vh for page root, wrap tables in overflow-x:auto containers, '
                    'never use max-width or container class on root elements, ensure content fills available space.')
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ]

        # 获取模型配置
        selected_model = get_selected_model()
        model_name = selected_model.get('model', API_CONFIG.get('model', 'gpt-4'))
        max_tokens = selected_model.get('max_tokens') or AI_OPTIONS.get('max_tokens', 100000)
        temperature = selected_model.get('temperature') or AI_OPTIONS.get('temperature', 0.7)
        timeout = selected_model.get('timeout') or AI_OPTIONS.get('timeout', 300)

        url, headers, payload, is_claude = self._build_request_params(
            selected_model, messages, model_name, max_tokens, temperature,
            stream=True, tools=tools
        )

        logger.info(f"[AI流式] 使用模型: {selected_model.get('name', model_name)} ({model_name})")
        logger.info(f"[AI流式] API格式: {'Claude' if is_claude else 'OpenAI'}, URL: {url}")

        # 详细记录 payload 结构用于调试 400 错误
        payload_summary = {
            'model': payload.get('model'),
            'stream': payload.get('stream'),
            'max_tokens': payload.get('max_tokens'),
            'temperature': payload.get('temperature'),
            'has_system': 'system' in payload or any(m.get('role') == 'system' for m in payload.get('messages', [])),
            'system_length': len(payload.get('system', '')) if payload.get('system') else sum(len(m.get('content', '')) for m in payload.get('messages', []) if m.get('role') == 'system'),
            'messages_count': len(payload.get('messages', [])),
            'messages_roles': [m.get('role') for m in payload.get('messages', [])],
            'has_tools': 'tools' in payload,
            'tools_count': len(payload.get('tools', [])),
            'tools_names': [t.get('name') or t.get('function', {}).get('name') for t in payload.get('tools', [])] if payload.get('tools') else [],
        }
        logger.info(f"[AI流式] Payload 概要: {json.dumps(payload_summary, ensure_ascii=False)}")

        # 如果有 tools，记录第一个 tool 的结构（调试用）
        if payload.get('tools'):
            first_tool = payload['tools'][0]
            tool_debug = {k: (v if k != 'input_schema' else f'<schema with {len(json.dumps(v))} chars>')
                         for k, v in first_tool.items()}
            logger.info(f"[AI流式] 第一个 tool 结构: {json.dumps(tool_debug, ensure_ascii=False)}")

        # 估算输入 token 数
        total_input_chars = sum(len(m.get('content', '')) if isinstance(m.get('content'), str)
                                else len(str(m.get('content', '')))
                                for m in messages)
        est_tokens = int(total_input_chars * 0.4)  # 粗略估算
        logger.info(f"[AI流式] 输入: {len(messages)} 条消息, "
                    f"约 {total_input_chars} 字符 (≈{est_tokens} tokens)")

        # 尝试流式请求（3 次重试）
        last_error = None
        for attempt in range(3):
            try:
                if attempt > 0:
                    logger.info(f"[AI流式] 重试第 {attempt+1} 次...")

                session = requests.Session()
                session.trust_env = False
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                session.headers.update({
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    'Connection': 'close'
                })

                if cancellable_project_id:
                    with tasks_lock:
                        if cancellable_project_id in generating_tasks:
                            generating_tasks[cancellable_project_id]['session'] = session
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                response = session.post(
                    url, json=payload, headers=headers,
                    stream=True, timeout=timeout, verify=False
                )
                if response.status_code >= 400:
                    error_body = ''
                    try:
                        error_body = response.text[:2000]
                    except Exception:
                        pass
                    logger.error(
                        f"[AI流式] API 错误 {response.status_code}: {error_body}")
                    response.raise_for_status()
                response.encoding = 'utf-8'  # 强制 UTF-8，避免中文乱码

                accumulated = ""
                # tool_calls 增量拼接: {tool_call_id: {name, arguments_str}}
                tool_calls_accum = {}
                had_reasoning = False  # 模型是否返回了推理内容（reasoning_content）
                line_count = 0
                # Claude SSE 需要 track event type
                current_sse_event = ''
                for line in response.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    line_count += 1
                    # 前 3 行记录原始内容用于调试
                    if line_count <= 5:
                        logger.info(f"[AI流式] 原始行 {line_count}: {line[:300]}")

                    if is_claude:
                        # ===== Claude SSE 格式解析 =====
                        if line.startswith('event:'):
                            current_sse_event = line[6:].strip()
                            continue
                        if line.startswith('data:'):
                            data_str = line[5:].strip()
                            if data_str == '[DONE]':
                                break
                            try:
                                chunk = json.loads(data_str)
                            except json.JSONDecodeError:
                                continue

                            chunk_type = chunk.get('type', '')

                            # 错误处理
                            if chunk_type == 'error' or 'error' in chunk:
                                err_obj = chunk.get('error', chunk)
                                err_msg = err_obj if isinstance(err_obj, str) else err_obj.get('message', str(err_obj))
                                logger.warning(f"[AI流式-Claude] API 错误: {err_msg}")
                                retryable_keywords = [
                                    'busy', 'try again', 'overload',
                                    'rate limit', 'too many', '503', '429',
                                ]
                                is_retryable = any(kw.lower() in str(err_msg).lower()
                                                    for kw in retryable_keywords)
                                if is_retryable:
                                    raise Exception(f"API 可重试错误: {err_msg}")
                                else:
                                    accumulated = f"[API Error] {err_msg}"
                                    yield accumulated, accumulated, True, None
                                    return

                            # 文本增量
                            if chunk_type == 'content_block_delta':
                                delta = chunk.get('delta', {})
                                delta_type = delta.get('type', '')
                                if delta_type == 'text_delta':
                                    content = delta.get('text', '')
                                    if content:
                                        accumulated += content
                                        yield content, accumulated, False, tool_calls_accum
                                elif delta_type == 'thinking_delta':
                                    # Claude thinking/reasoning 增量
                                    thinking = delta.get('thinking', '')
                                    if thinking:
                                        had_reasoning = True
                                        yield '', accumulated, False, tool_calls_accum, thinking
                                elif delta_type == 'input_json_delta':
                                    # tool input 增量
                                    partial = delta.get('partial_json', '')
                                    # 使用 chunk 的 index 字段（Claude API 规范）
                                    # 或者回退到最后一个已有的 tool call
                                    chunk_index = chunk.get('index')
                                    if chunk_index is not None:
                                        tc_key = str(chunk_index)
                                    else:
                                        # 回退：追加到最后一个 tool call
                                        tc_key = str(len(tool_calls_accum) - 1)
                                    if tc_key not in tool_calls_accum:
                                        tool_calls_accum[tc_key] = {
                                            'id': '', 'name': '', 'arguments': ''
                                        }
                                        logger.info(
                                            f"[AI流式] input_json_delta 创建新条目: "
                                            f"chunk_index={chunk_index}, tc_key={tc_key}, "
                                            f"accum_keys={list(tool_calls_accum.keys())}")
                                    tool_calls_accum[tc_key]['arguments'] += partial
                                    yield '', accumulated, False, tool_calls_accum

                            # tool_use block 开始
                            elif chunk_type == 'content_block_start':
                                cb = chunk.get('content_block', {})
                                if cb.get('type') == 'tool_use':
                                    # 使用 chunk 的 index 字段作为 key
                                    chunk_index = chunk.get('index')
                                    tc_key = str(chunk_index) if chunk_index is not None else str(len(tool_calls_accum))
                                    tool_calls_accum[tc_key] = {
                                        'id': cb.get('id', ''),
                                        'name': cb.get('name', ''),
                                        'arguments': ''
                                    }
                                    logger.info(
                                        f"[AI流式] content_block_start tool_use: "
                                        f"index={chunk_index}, key={tc_key}, "
                                        f"name={cb.get('name', '')}, id={cb.get('id', '')}")
                                    yield '', accumulated, False, tool_calls_accum
                                elif line_count <= 30:
                                    logger.info(
                                        f"[AI流式] content_block_start (非 tool_use): "
                                        f"type={cb.get('type')}, "
                                        f"data={data_str[:300]}")

                            # 消息结束
                            elif chunk_type == 'message_stop':
                                break

                            # message_delta 包含 stop_reason
                            elif chunk_type == 'message_delta':
                                delta = chunk.get('delta', {})
                                stop_reason = delta.get('stop_reason', '')
                                if stop_reason == 'max_tokens':
                                    logger.warning("[警告] Claude API 响应被截断 (max_tokens)!")
                    else:
                        # ===== OpenAI SSE 格式解析 =====
                        # 兼容 "data:" 和 "data: " 两种前缀
                        if line.startswith('data:'):
                            data_str = line[5:].lstrip(' ')
                            if data_str.strip() == '[DONE]':
                                break
                            try:
                                chunk = json.loads(data_str)
                                # 检查 API 返回的错误信息（如 token 超限、模型不支持 tools）
                                if 'error' in chunk:
                                    err_msg = chunk['error']
                                    if isinstance(err_msg, dict):
                                        err_msg = err_msg.get('message', str(err_msg))
                                    logger.warning(f"[AI流式] API 返回错误: {err_msg}")

                                    # 可重试错误：系统繁忙、引擎内部错误等
                                    retryable_keywords = [
                                        'busy', 'try again', 'EngineInternal',
                                        'timeout', 'overload', 'rate limit',
                                        'too many', '503', '429',
                                    ]
                                    is_retryable = any(
                                        kw.lower() in str(err_msg).lower()
                                        for kw in retryable_keywords)

                                    if is_retryable:
                                        # 抛异常让外层 3 次重试捕获
                                        raise Exception(
                                            f"API 可重试错误: {err_msg}")
                                    else:
                                        # 不可重试错误（如 token 超限、
                                        # 模型不支持 tools）
                                        accumulated = f"[API Error] {err_msg}"
                                        yield accumulated, accumulated, True, None
                                        return
                                choice = chunk.get('choices', [{}])[0]
                                delta = choice.get('delta', {})
                                finish_reason = choice.get('finish_reason', '')

                                content = delta.get('content', '')
                                reasoning = delta.get('reasoning_content', '')

                                # 处理 tool_calls 增量
                                # OpenAI 流式格式：id 只在首 chunk 出现，
                                # index 在每个 chunk 都有，用作稳定 key
                                tc_deltas = delta.get('tool_calls')
                                if tc_deltas:
                                    for tc in tc_deltas:
                                        tc_idx = tc.get('index', 0)
                                        key = str(tc_idx)  # 用 index 做 key，稳定可靠
                                        if key not in tool_calls_accum:
                                            tool_calls_accum[key] = {
                                                'id': tc.get('id', ''),
                                                'name': '',
                                                'arguments': ''
                                            }
                                        elif tc.get('id'):
                                            # 后续 chunk 可能补充 id
                                            tool_calls_accum[key]['id'] = tc['id']
                                        fn = tc.get('function', {})
                                        if fn.get('name'):
                                            tool_calls_accum[key]['name'] = fn['name']
                                        if fn.get('arguments'):
                                            tool_calls_accum[key]['arguments'] += fn['arguments']

                                if content:
                                    accumulated += content
                                    yield content, accumulated, False, tool_calls_accum
                                elif reasoning:
                                    had_reasoning = True
                                    yield '', accumulated, False, tool_calls_accum, reasoning
                                elif tc_deltas:
                                    yield '', accumulated, False, tool_calls_accum
                            except json.JSONDecodeError:
                                logger.warning(f"[AI流式] JSON 解析失败，原始数据: {data_str[:200]}")
                                continue
                        else:
                            # 非 data: 前缀的行，可能是错误或非标准格式
                            if line_count <= 5:
                                logger.info(f"[AI流式] 非 SSE 格式行: {line[:200]}")

                # 流式完成
                # 解析 tool_calls
                parsed_tool_calls = None
                if tool_calls_accum:
                    parsed_tool_calls = []
                    for key, tc in tool_calls_accum.items():
                        try:
                            args = json.loads(tc['arguments']) if tc['arguments'] else {}
                            if not isinstance(args, dict):
                                args = {}
                        except json.JSONDecodeError:
                            args = {}
                        parsed_tool_calls.append({
                            'id': tc.get('id', key),
                            'name': tc['name'],
                            'arguments': args
                        })
                    logger.info(f"[AI流式] 解析到 {len(parsed_tool_calls)} 个 tool calls: "
                                f"{[tc['name'] for tc in parsed_tool_calls]}")

                logger.info(f"[AI流式] 响应完成，总长度: {len(accumulated)} 字符, "
                            f"共 {line_count} 行, tool_calls={len(parsed_tool_calls) if parsed_tool_calls else 0}"
                            + (f", had_reasoning=True" if had_reasoning else ""))
                # 空响应警告：可能是输入 token 超限或模型不支持 tools
                if not accumulated and not parsed_tool_calls:
                    logger.warning(f"[AI流式] 空响应！模型: {model_name}, "
                                   f"输入约 {est_tokens} tokens, "
                                   f"收到 {line_count} 行数据。可能原因: 输入超长/模型不支持tools/API错误"
                                   + (f" (但有 {len(accumulated)} 推理内容)" if had_reasoning else ""))
                yield '', accumulated, True, parsed_tool_calls
                return

            except Exception as e:
                last_error = e
                logger.warning(f"[AI流式] 第 {attempt+1} 次尝试失败: {e}")
                if attempt < 2:
                    time.sleep(3)

        # 流式全部失败，降级为非流式调用
        logger.info("[AI流式] 全部失败，降级为非流式调用...")
        payload_no_stream = {k: v for k, v in payload.items() if k != 'stream'}
        result = self.call_ai_model_via_curl(url, headers, payload_no_stream, timeout)
        if not result:
            try:
                session = requests.Session()
                session.trust_env = False
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                resp = session.post(url, json=payload_no_stream, headers=headers,
                                    timeout=timeout, verify=False)
                resp.raise_for_status()
                result = resp.json()
            except Exception as e2:
                logger.error(f"[AI流式降级] 非流式调用也失败: {e2}")
                raise last_error

        # 根据格式解析非流式响应
        if is_claude:
            content, _ = self._parse_claude_non_streaming(result)
            # Claude tool_use 解析
            parsed_tool_calls = None
            content_blocks = result.get('content', [])
            tool_blocks = [b for b in content_blocks if b.get('type') == 'tool_use']
            if tool_blocks:
                parsed_tool_calls = []
                for tb in tool_blocks:
                    parsed_tool_calls.append({
                        'id': tb.get('id', ''),
                        'name': tb.get('name', ''),
                        'arguments': tb.get('input', {})
                    })
        else:
            message = result.get('choices', [{}])[0].get('message', {})
            content = message.get('content', '')

            # 解析非流式响应中的 tool_calls
            parsed_tool_calls = None
            raw_tool_calls = message.get('tool_calls')
            if raw_tool_calls:
                parsed_tool_calls = []
                for tc in raw_tool_calls:
                    try:
                        args = json.loads(tc.get('function', {}).get('arguments', '{}'))
                        if not isinstance(args, dict):
                            args = {}
                    except json.JSONDecodeError:
                        args = {}
                    parsed_tool_calls.append({
                        'id': tc.get('id', ''),
                        'name': tc.get('function', {}).get('name', ''),
                        'arguments': args
                    })

        yield content, content, True, parsed_tool_calls

    def _save_partial_content(self, project_id, raw_content, project_folder):
        """保存部分 AI 内容（失败/中断时调用）"""
        if not raw_content or len(raw_content) < 50:
            return

        logger.info(f"[部分保存] 项目 {project_id}, 内容长度: {len(raw_content)}")

        # 保存原始文本（用于续传上下文）
        partial_path = os.path.join(project_folder, 'partial_content.txt')
        try:
            with open(partial_path, 'w', encoding='utf-8') as f:
                f.write(raw_content)
        except Exception as e:
            logger.error(f"[部分保存] 保存 raw 内容失败: {e}")

        # 尝试提取 HTML 并保存（用于预览）
        html = self.extract_html(raw_content)
        if html and ('<html' in html.lower() or '<div' in html.lower()):
            partial_html_path = os.path.join(project_folder, 'index.html')
            try:
                with open(partial_html_path, 'w', encoding='utf-8') as f:
                    f.write(html)
                logger.info(f"[部分保存] 已保存部分 HTML: {len(html)} 字符")
            except Exception as e:
                logger.error(f"[部分保存] 保存 HTML 失败: {e}")

        # 更新 record.json 状态为 partial
        record_path = os.path.join(project_folder, 'record.json')
        if os.path.exists(record_path):
            try:
                with open(record_path, 'r', encoding='utf-8') as f:
                    record = json.load(f)
                record['status'] = 'partial'
                record['partial_content_length'] = len(raw_content)
                with open(record_path, 'w', encoding='utf-8') as f:
                    json.dump(record, f, ensure_ascii=False, indent=2)
            except Exception:
                pass

    def call_ai_model_via_curl(self, url, headers, payload, timeout):
        """使用系统 curl 命令调用 AI (解决 SSL 问题)"""
        try:
            # 将 payload 写入临时文件以避免命令行长度限制和转义问题
            with tempfile.NamedTemporaryFile(mode='w', delete=False, encoding='utf-8', suffix='.json') as f:
                json.dump(payload, f, ensure_ascii=False)
                temp_payload_path = f.name
            
            # 构建 curl 命令
            # -k: 忽略 SSL 验证
            # -s: 静默模式
            cmd = ['curl', '-k', '-s', '-X', 'POST', url]
            
            # 添加 header
            for k, v in headers.items():
                cmd.extend(['-H', f'{k}: {v}'])
            
            # 添加 body 文件
            cmd.extend(['-d', f'@{temp_payload_path}'])
            
            logger.info(f"[AI] 执行 curl 命令: {' '.join(cmd)} ...")
            
            # 执行命令
            process = subprocess.run(
                cmd, 
                stdout=subprocess.PIPE, 
                stderr=subprocess.PIPE, 
                timeout=timeout,
                encoding='utf-8',
                errors='ignore'
            )
            
            # 清理临时文件
            try:
                os.remove(temp_payload_path)
            except:
                pass
            
            if process.returncode != 0:
                logger.error(f"[curl错误] returncode: {process.returncode}, stderr: {process.stderr}")
                return None
            
            # 解析结果
            return json.loads(process.stdout)
            
        except Exception as e:
            logger.error(f"[curl异常] {e}")
            return None

    def extract_html(self, content, fallback_error_page=False):
        """从AI响应中提取HTML代码

        Args:
            content: AI 响应文本
            fallback_error_page: True 时在找不到 HTML 时返回错误页面（用于初次生成），
                                 False 时返回 None（用于对话调整，避免覆盖原页面）
        """
        # 策略 0：<artifact> 标签提取（优先级最高）
        artifact_match = re.search(r'<artifact[^>]*>([\s\S]*?)</artifact>', content, re.IGNORECASE)
        if artifact_match:
            html = artifact_match.group(1).strip()
            if html:
                return html

        # 快速路径：用字符串操作定位 ```html 代码块（避免正则回溯）
        for marker in ('```html', '```HTML', '```\n'):
            idx = content.find(marker)
            if idx == -1:
                continue
            start = content.find('\n', idx) + 1
            if start == 0:  # \n 不存在
                start = idx + len(marker)
            end = content.find('```', start)
            if end > start:
                html = content[start:end].strip()
                if '<!DOCTYPE html>' in html or '<html' in html:
                    return html

        # 回退：正则匹配任意 ``` 代码块
        html_match = re.search(r'```(?:html|HTML)?\s*\n([\s\S]*?)```', content)
        if html_match:
            html = html_match.group(1).strip()
            if '<!DOCTYPE html>' in html or '<html' in html:
                return html

        # 直接查找HTML文档
        doctype_idx = content.find('<!DOCTYPE html>')
        if doctype_idx != -1:
            end_idx = content.rfind('</html>')
            if end_idx != -1:
                return content[doctype_idx:end_idx + 7]

        # HTML 片段提取（用于侧边栏布局等不需要完整页面的场景）
        # 从 ```html 代码块中提取片段（不需要 <!DOCTYPE html> 或 <html>）
        for marker in ('```html', '```HTML'):
            idx = content.find(marker)
            if idx == -1:
                continue
            start = content.find('\n', idx) + 1
            if start == 0:
                start = idx + len(marker)
            end = content.find('```', start)
            if end > start:
                html = content[start:end].strip()
                if html.startswith('<div') or html.startswith('<table') or html.startswith('<section') or html.startswith('<main') or html.startswith('<form') or html.startswith('<ul') or html.startswith('<nav'):
                    logger.info(f'[提取] 检测到 HTML 片段: {len(html)} 字符')
                    return html

        # 回退：尝试从 ``` 代码块中提取
        html_match = re.search(r'```(?:html|HTML)\s*\n([\s\S]*?)```', content)
        if html_match:
            html = html_match.group(1).strip()
            if len(html) > 200 and ('<div' in html or '<table' in html):
                logger.info(f'[提取] 回退提取 HTML 片段: {len(html)} 字符')
                return html

        # 找不到有效 HTML
        if fallback_error_page:
            # 返回错误页面（用于初次生成场景）
            return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>生成结果</title>
    <script src="/static/js/tailwindcss.js"></script>
</head>
<body class="bg-gray-100 p-8">
    <div class="bg-white rounded-lg shadow p-6 max-w-4xl mx-auto">
        <h1 class="text-xl font-bold text-red-600 mb-4">HTML提取失败</h1>
        <p class="text-gray-600 mb-4">AI返回内容格式不符合预期：</p>
        <pre class="bg-gray-50 p-4 rounded text-sm overflow-auto">{content[:5000]}</pre>
    </div>
</body>
</html>'''

        return None

    def handle_save_project(self):
        """保存项目（用于手动保存）"""
        try:
            content_length = int(self.headers['Content-Length'])
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))
            
            html_content = data.get('htmlContent')
            project_meta = data.get('projectData')
            
            if not html_content or not project_meta:
                self.send_error_response("Missing htmlContent or projectData")
                return

            project_id = project_meta.get('id', generate_project_id(project_meta.get('name', 'project')))
            project_folder = os.path.join(PROJECTS_DIR, project_id)
            os.makedirs(project_folder, exist_ok=True)
            
            file_path = os.path.join(project_folder, 'index.html')
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(html_content)
                
            projects = self.load_projects()
            existing_idx = next((i for i, p in enumerate(projects) if p['id'] == project_id), None)
            
            new_record = {
                "id": project_id,
                "name": project_meta['name'],
                "url": f"/projects/{project_id}/index.html",
                "date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            
            if existing_idx is not None:
                projects[existing_idx] = new_record
            else:
                projects.insert(0, new_record)
                
            self.save_projects(projects)
            self.send_json_response({'success': True, 'project': new_record})

        except Exception as e:
            self.send_error_response(str(e))

    def handle_delete_project(self):
        """删除项目（移动到回收站）"""
        try:
            content_length = int(self.headers['Content-Length'])
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))
            
            project_id = data.get('id')
            if not project_id:
                self.send_error_response("Missing project ID")
                return

            projects = self.load_projects()
            project = next((p for p in projects if p['id'] == project_id), None)
            
            if project:
                # 移动文件夹到deleted目录
                project_folder = os.path.join(PROJECTS_DIR, project_id)
                deleted_folder = os.path.join(DELETED_DIR, project_id)
                if os.path.exists(project_folder):
                    import shutil
                    # 如果目标已存在，先删除
                    if os.path.exists(deleted_folder):
                        shutil.rmtree(deleted_folder)
                    shutil.move(project_folder, deleted_folder)
                    logger.info(f"[删除] 项目移动到回收站: {project_id}")
                
                # 从项目列表移除
                projects = [p for p in projects if p['id'] != project_id]
                self.save_projects(projects)

                # 取消后台生成任务并中断 HTTP 请求
                active_session = None
                with tasks_lock:
                    if project_id in generating_tasks:
                        generating_tasks[project_id]['status'] = 'cancelled'
                        active_session = generating_tasks[project_id].get('session')
                        logger.info(f"[删除] 已取消后台生成任务: {project_id}")
                if active_session:
                    try:
                        active_session.close()
                    except Exception:
                        pass
                
                # 添加到已删除列表
                deleted_projects = self.load_deleted_projects()
                project['deletedAt'] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                project['url'] = f'/deleted/{project_id}/index.html'
                deleted_projects.insert(0, project)
                self.save_deleted_projects(deleted_projects)

            self.send_json_response({'success': True})

        except Exception as e:
            self.send_error_response(str(e))

    def handle_rename_project(self):
        """重命名项目（同时重命名文件夹）"""
        try:
            content_length = int(self.headers['Content-Length'])
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))
            
            project_id = data.get('id')
            new_name = data.get('newName', '').strip()
            
            if not project_id:
                self.send_error_response("Missing project ID")
                return
            if not new_name:
                self.send_error_response("Missing new name")
                return

            projects = self.load_projects()
            project = next((p for p in projects if p['id'] == project_id), None)
            
            if not project:
                self.send_error_response("Project not found")
                return
            
            old_name = project['name']
            old_folder = os.path.join(PROJECTS_DIR, project_id)
            
            # 生成新的文件夹名称（新名称 + 原时间戳）
            # 从原ID中提取时间戳部分
            parts = project_id.rsplit('_', 2)
            if len(parts) >= 3:
                timestamp = '_'.join(parts[-2:])  # 例如 "20260114_11-20-20"
            else:
                timestamp = datetime.datetime.now().strftime('%Y%m%d_%H-%M-%S')
            
            # 处理新名称，移除不安全字符
            safe_new_name = re.sub(r'[\\/:*?"<>|]', '', new_name)
            safe_new_name = safe_new_name.replace(' ', '_')
            if len(safe_new_name) > 30:
                safe_new_name = safe_new_name[:30]
            
            new_project_id = f"{safe_new_name}_{timestamp}"
            new_folder = os.path.join(PROJECTS_DIR, new_project_id)
            
            # 重命名文件夹
            if os.path.exists(old_folder) and old_folder != new_folder:
                import shutil
                if os.path.exists(new_folder):
                    # 如果目标已存在，添加随机后缀
                    new_project_id = f"{safe_new_name}_{timestamp}_{datetime.datetime.now().strftime('%S')}"
                    new_folder = os.path.join(PROJECTS_DIR, new_project_id)
                shutil.move(old_folder, new_folder)
                logger.info(f"[重命名文件夹] {project_id} -> {new_project_id}")
            
            # 更新项目信息
            project['id'] = new_project_id
            project['name'] = new_name
            project['url'] = f'/projects/{new_project_id}/index.html'
            self.save_projects(projects)
            
            logger.info(f"[重命名] {old_name} -> {new_name}")
            self.send_json_response({'success': True, 'project': project})

        except Exception as e:
            import traceback
            traceback.print_exc()
            self.send_error_response(str(e))

    def handle_restore_project(self):
        """恢复已删除的项目"""
        try:
            content_length = int(self.headers['Content-Length'])
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))
            
            project_id = data.get('id')
            if not project_id:
                self.send_error_response("Missing project ID")
                return

            deleted_projects = self.load_deleted_projects()
            project = next((p for p in deleted_projects if p['id'] == project_id), None)
            
            if not project:
                self.send_error_response("Deleted project not found")
                return
            
            # 移动文件夹回projects目录
            deleted_folder = os.path.join(DELETED_DIR, project_id)
            project_folder = os.path.join(PROJECTS_DIR, project_id)
            
            if os.path.exists(deleted_folder):
                import shutil
                # 如果目标已存在，先删除
                if os.path.exists(project_folder):
                    shutil.rmtree(project_folder)
                shutil.move(deleted_folder, project_folder)
                logger.info(f"[恢复] 项目从回收站恢复: {project_id}")
            
            # 从已删除列表移除
            deleted_projects = [p for p in deleted_projects if p['id'] != project_id]
            self.save_deleted_projects(deleted_projects)
            
            # 添加回项目列表
            projects = self.load_projects()
            # 移除deletedAt字段，更新url
            if 'deletedAt' in project:
                del project['deletedAt']
            project['url'] = f'/projects/{project_id}/index.html'
            # 检查是否已存在（避免重复）
            existing_idx = next((i for i, p in enumerate(projects) if p['id'] == project_id), None)
            if existing_idx is not None:
                projects[existing_idx] = project
            else:
                projects.insert(0, project)
            self.save_projects(projects)
            
            self.send_json_response({'success': True, 'project': project})

        except Exception as e:
            self.send_error_response(str(e))

    def handle_copy_project(self):
        """复制项目"""
        try:
            content_length = int(self.headers['Content-Length'])
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))
            
            source_project_id = data.get('sourceProjectId')
            new_project_name = data.get('newProjectName', '').strip()
            
            if not source_project_id:
                self.send_error_response("缺少源项目ID")
                return
            if not new_project_name:
                self.send_error_response("缺少新项目名称")
                return
            
            source_folder = os.path.join(PROJECTS_DIR, source_project_id)
            if not os.path.exists(source_folder):
                self.send_error_response("源项目不存在")
                return
            
            # 生成新项目ID
            new_project_id = generate_project_id(new_project_name)
            new_folder = os.path.join(PROJECTS_DIR, new_project_id)
            
            # 复制整个文件夹
            import shutil
            shutil.copytree(source_folder, new_folder)
            logger.info(f"[复制项目] {source_project_id} -> {new_project_id}")
            
            # 更新项目列表 (load_projects会自动同步新文件夹)
            projects = self.load_projects()
            new_project = {
                'id': new_project_id,
                'name': new_project_name,
                'url': f'/projects/{new_project_id}/index.html',
                'date': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            # 检查是否已被load_projects自动添加，避免重复
            existing_idx = next((i for i, p in enumerate(projects) if p['id'] == new_project_id), None)
            if existing_idx is not None:
                # 更新名称为用户指定的名称
                projects[existing_idx] = new_project
            else:
                projects.insert(0, new_project)
            self.save_projects(projects)
            
            self.send_json_response({'success': True, 'project': new_project})
            
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.send_error_response(str(e))

    def handle_get_deleted_projects(self):
        """获取已删除项目列表"""
        try:
            deleted_projects = self.load_deleted_projects()
            self.send_json_response({'success': True, 'projects': deleted_projects})
        except Exception as e:
            self.send_error_response(str(e))

    def load_projects(self):
        """加载项目列表（自动与文件夹同步）"""
        projects = []
        if os.path.exists(PROJECTS_FILE):
            try:
                with open(PROJECTS_FILE, 'r', encoding='utf-8') as f:
                    projects = json.load(f)
            except:
                pass
        
        # 扫描projects文件夹获取实际存在的项目
        # 包含有 index.html 的项目 和 有 record.json 的占位项目
        existing_folders = set()
        folders_with_html = set()  # 有 index.html 的文件夹
        if os.path.exists(PROJECTS_DIR):
            for folder_name in os.listdir(PROJECTS_DIR):
                folder_path = os.path.join(PROJECTS_DIR, folder_name)
                if os.path.isdir(folder_path):
                    has_html = os.path.exists(os.path.join(folder_path, 'index.html'))
                    has_record = os.path.exists(os.path.join(folder_path, 'record.json'))
                    if has_html or has_record:
                        existing_folders.add(folder_name)
                    if has_html:
                        folders_with_html.add(folder_name)
        
        original_count = len(projects)
        original_ids = [p['id'] for p in projects]
        status_updated = False
        
        # 1. 移除不存在的项目
        projects = [p for p in projects if p['id'] in existing_folders]
        
        # 2. 去重：确保每个ID只出现一次（保留第一个）
        seen_ids = set()
        unique_projects = []
        for p in projects:
            if p['id'] not in seen_ids:
                seen_ids.add(p['id'])
                unique_projects.append(p)
        projects = unique_projects
        
        # 3. 检查并更新占位项目状态（pending_external -> 正常）
        for p in projects:
            if p.get('status') == 'pending_external' and p['id'] in folders_with_html:
                # 占位项目现在有 index.html 了，更新状态
                logger.info(f"[状态更新] 项目 {p['id']} 已完成外部生成")
                p['status'] = None  # 清除 pending 状态
                p['name'] = p['name'].replace(' (待外部生成)', '')  # 移除后缀
                p['url'] = f"/projects/{p['id']}/index.html"  # 更新URL
                status_updated = True
        
        # 4. 添加新发现的项目（不在列表中的文件夹）
        existing_ids = {p['id'] for p in projects}
        new_added = False
        for folder_name in existing_folders:
            if folder_name not in existing_ids:
                # 从文件夹名称提取项目名和日期
                parts = folder_name.rsplit('_', 2)
                if len(parts) >= 3:
                    name = parts[0]
                    date_part = parts[1]
                    time_part = parts[2]
                    
                    # 解析日期
                    try:
                        year = date_part[:4]
                        month = date_part[4:6]
                        day = date_part[6:8]
                        date_str = f"{year}-{month}-{day}"
                    except:
                        date_str = datetime.datetime.now().strftime('%Y-%m-%d')
                    
                    # 解析时间 (format: 4-15-23pm)
                    try:
                        # 移除am/pm后缀
                        is_pm = time_part.lower().endswith('pm')
                        time_pure = time_part[:-2] if (time_part.lower().endswith('am') or time_part.lower().endswith('pm')) else time_part
                        
                        t_parts = time_pure.split('-')
                        if len(t_parts) >= 3:
                            h = int(t_parts[0])
                            m = int(t_parts[1])
                            s = int(t_parts[2])
                            
                            # 转换12小时制到24小时制
                            if is_pm and h < 12:
                                h += 12
                            elif not is_pm and h == 12:  # 12am is 00:00
                                h = 0
                                
                            time_str = f"{h:02d}:{m:02d}:{s:02d}"
                        else:
                            time_str = "00:00:00"
                    except:
                        time_str = "00:00:00"
                        
                    date = f"{date_str} {time_str}"
                else:
                    name = folder_name
                    date = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                
                new_project = {
                    'id': folder_name,
                    'name': name,
                    'url': f'/projects/{folder_name}/index.html',
                    'date': date
                }
                projects.append(new_project)
                new_added = True
                logger.info(f"[同步] 发现新项目: {folder_name}")
        
        # 按日期排序（新的在前）
        projects.sort(key=lambda p: p.get('date', ''), reverse=True)
        
        # 只在有变化时保存
        new_ids = [p['id'] for p in projects]
        if len(projects) != original_count or new_ids != original_ids or new_added or status_updated:
            self.save_projects(projects)
            logger.info(f"[同步] 项目列表已更新: {len(projects)}个项目")
        
        return projects

    def save_projects(self, projects):
        """保存项目列表"""
        with open(PROJECTS_FILE, 'w', encoding='utf-8') as f:
            json.dump(projects, f, ensure_ascii=False, indent=2)

    def load_deleted_projects(self):
        """加载已删除项目列表"""
        if os.path.exists(DELETED_PROJECTS_FILE):
            try:
                with open(DELETED_PROJECTS_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except:
                pass
        return []

    def save_deleted_projects(self, projects):
        """保存已删除项目列表"""
        with open(DELETED_PROJECTS_FILE, 'w', encoding='utf-8') as f:
            json.dump(projects, f, ensure_ascii=False, indent=2)

    def assemble_iframe_html(self, ai_html, frame_html, css_path='template/template.css', sidebar_meta=None, page_name=''):
        """将 AI 生成的内容 HTML 与外框架 HTML 拆分拼接成最终页面。

        支持两种模式：
        1. iframe srcdoc 模式：AI 内容作为 srcdoc 属性值嵌入（需 HTML 实体编码）
        2. 侧边栏+内容区模式：AI 内容直接插入到框架占位符位置（无需编码）

        同时进行以下修正：
        1. 移除/修改 iframe 的 sandbox 属性，允许脚本执行和外部资源加载
        2. 将框架中的外部系统链接替换为 javascript:void(0)，防止跳转离开原型页面
        """
        if not frame_html or not ai_html:
            return ai_html

        # sidebar 模式：frame_html 包含 sideMenuWrapper，AI 只生成内容片段
        # 始终进行框架拼接，不做「AI 已有框架」的检测
        is_sidebar_frame = 'sideMenuWrapper' in frame_html or 'mainLayout' in frame_html
        if not is_sidebar_frame:
            # iframe srcdoc 模式：检查 AI 是否已经生成了框架元素（侧边栏等）
            has_sidebar = bool(re.search(r'<(?:div|nav|aside)[^>]*(?:sidebar|side-bar)', ai_html, re.IGNORECASE))
            has_navbar = bool(re.search(r'<(?:div|nav|header)[^>]*(?:navbar|nav-bar|top-bar)', ai_html, re.IGNORECASE))
            if has_sidebar and has_navbar:
                logger.info("[组装] AI 已生成包含框架的完整页面，跳过框架拼接")
                return ai_html

        # ===== 修正框架 HTML =====

        # 1. 移除 CSP (Content-Security-Policy) meta 标签
        frame_html = _RE_CSP_META.sub('', frame_html)

        # 2. 移除 iframe sandbox 属性
        frame_html = _RE_SANDBOX.sub('', frame_html)

        # 3. 将框架中的外部系统链接（href）替换为 javascript:void(0)
        frame_html = _RE_EXT_HREF.sub('href="javascript:void(0)"', frame_html)

        # 4. 移除 IE 版本检测跳转脚本
        frame_html = _RE_IE_COND.sub('', frame_html)
        frame_html = _RE_LOCATION_HREF.sub('', frame_html)

        # 5. 注入导航拦截脚本，阻止 Vue Router 和所有链接跳转
        # 在 <body> 标签后注入，用捕获阶段拦截所有点击事件
        nav_blocker = '''<script>
// [原型生成器注入] 阻止所有导航跳转
document.addEventListener('click', function(e) {
    var el = e.target;
    while (el && el.tagName !== 'A') el = el.parentElement;
    if (el && el.tagName === 'A') {
        e.preventDefault();
        e.stopPropagation();
        e.stopImmediatePropagation();
        return false;
    }
}, true);
// 阻止 popstate（浏览器前进后退）
try { window.addEventListener('popstate', function(e) { e.preventDefault(); }, true); } catch(ex) {}
// 拦截通过 JS 设置 location 的跳转
try {
    var _origAssign = window.location.assign;
    var _origReplace = window.location.replace;
    if (_origAssign) window.location.assign = function(){};
    if (_origReplace) window.location.replace = function(){};
} catch(ex) {}
</script>'''
        body_tag_end = frame_html.find('>', frame_html.find('<body')) + 1 if '<body' in frame_html else 0
        if body_tag_end > 0:
            frame_html = frame_html[:body_tag_end] + nav_blocker + frame_html[body_tag_end:]
        else:
            frame_html = nav_blocker + frame_html

        # 6. 自动处理页面名称：基于 page_name 自动添加侧边栏菜单项、设置激活项、替换 title
        # 不依赖 AI 输出 SIDEBAR_ADD/SIDEBAR_ACTIVE 标记（AI 经常忘记使用）
        sidebar_additions = re.findall(r'<!--\s*SIDEBAR_ADD:\s*(.+?)\s*-->', ai_html)
        sidebar_active = re.findall(r'<!--\s*SIDEBAR_ACTIVE:\s*(.+?)\s*-->', ai_html)

        if page_name and sidebar_meta and sidebar_meta.get('menu_count', 0) >= 2:
            existing_items = [item.lower() for item in sidebar_meta.get('menu_items', [])]
            # 如果页面名不在已有菜单中，自动添加
            if page_name.lower() not in existing_items:
                sidebar_additions.append(page_name)
                logger.info(f"[组装] 自动添加侧边栏菜单项: {page_name}")
            # 自动设置激活项为当前页面名
            sidebar_active.append(page_name)
            logger.info(f"[组装] 自动设置激活菜单项: {page_name}")

            # 替换 <title> 标签内容
            old_title = re.search(r'<title>([^<]*)</title>', frame_html)
            if old_title:
                frame_html = frame_html[:old_title.start()] + f'<title>{page_name}</title>' + frame_html[old_title.end():]
                logger.info(f"[组装] 已替换页面标题: {old_title.group(1)} → {page_name}")

            # 替换原页面标题文本（模板中可能有显眼的标题文字如"指标波动异常监测"）
            active_item_text = sidebar_meta.get('active_item_text', '')
            if active_item_text and active_item_text != page_name:
                # 只替换 body 区域中独立出现的原标题（避免误替换 CSS 等内容）
                body_pos = frame_html.find('<body')
                if body_pos >= 0:
                    body_region = frame_html[body_pos:]
                    # 替换 <h1>/<h2>/header 中出现的原标题
                    body_region = re.sub(
                        rf'(<h[1-6][^>]*>)({re.escape(active_item_text)})(</h[1-6]>)',
                        rf'\1{page_name}\3',
                        body_region
                    )
                    frame_html = frame_html[:body_pos] + body_region

        if sidebar_additions or sidebar_active:
            logger.info(f"[组装] 侧边栏修改: 添加 {sidebar_additions}, 激活 {sidebar_active}")

            # ===== 添加新菜单项 =====
            if sidebar_additions:
                has_meta = sidebar_meta and sidebar_meta.get('menu_count', 0) >= 2

                if has_meta:
                    # 通用方案：使用 sidebar_meta 中的 item_template 克隆新菜单项
                    item_template = sidebar_meta['item_template']
                    active_cls = sidebar_meta.get('active_classes', [])

                    # 找到插入位置：最后一个已知菜单项文本在 frame_html 中的位置
                    known_items = sidebar_meta.get('menu_items', [])
                    insert_pos = -1
                    # 从后往前找，找最后一个已知菜单项的文本
                    for known_text in reversed(known_items):
                        if known_text:
                            # 在 frame_html 的 body 区域搜索
                            body_pos = frame_html.find('<body')
                            search_start = body_pos if body_pos >= 0 else 0
                            idx = frame_html.find(known_text, search_start)
                            if idx >= 0:
                                # 找到文本后，向后找到下一个标签边界（<li 或 </ul）
                                next_tag = frame_html.find('<li ', idx + len(known_text))
                                next_ul = frame_html.find('</ul>', idx + len(known_text))
                                candidates = [p for p in [next_tag, next_ul] if p > idx]
                                insert_pos = min(candidates) if candidates else -1
                                break

                    if insert_pos > 0:
                        for menu_name in sidebar_additions:
                            # 从模板生成新项：替换文本内容
                            new_item = item_template
                            # 替换 <span>文本</span> 中的文本
                            new_item = re.sub(
                                r'(<span[^>]*>)([^<]*)(</span>)',
                                rf'\1{menu_name}\3',
                                new_item, count=1
                            )
                            # 替换 title="文本" 或 title=文本
                            new_item = re.sub(r'title\s*=\s*["\']?[^"\'\s>]+', f'title={menu_name}', new_item, count=1)
                            # 清除激活标记
                            for ac in active_cls:
                                new_item = new_item.replace(f' {ac}', '')

                            frame_html = frame_html[:insert_pos] + new_item + frame_html[insert_pos:]
                            logger.info(f"[组装] 已添加菜单项: {menu_name} (框架: {sidebar_meta.get('framework','?')})")
                    else:
                        logger.warning("[组装] 未找到菜单项插入位置")
                else:
                    # 无元数据回退：尝试通用 nest-menu 模式
                    menu_item_pattern = r'(<div\s+class=nest-menu><a\s+href=[^>]*><li\s[^>]*>)(.*?<span>)([^<]*)(</span>.*?</li></a></div>)'
                    existing_items = list(re.finditer(menu_item_pattern, frame_html, re.DOTALL | re.IGNORECASE))
                    if existing_items:
                        template_item = existing_items[-1]
                        template_prefix = template_item.group(1).replace(' is-active', '')
                        template_inner_before = template_item.group(2)
                        template_inner_after = template_item.group(4)
                        for menu_name in sidebar_additions:
                            new_item = template_prefix + template_inner_before + menu_name + template_inner_after
                            insert_pos = template_item.end()
                            frame_html = frame_html[:insert_pos] + new_item + frame_html[insert_pos:]
                        logger.info(f"[组装] 已向 nest-menu 侧边栏注入 {len(sidebar_additions)} 个菜单项")

            # ===== 设置激活菜单项 =====
            if sidebar_active:
                target_name = sidebar_active[-1]
                logger.info(f"[组装] 设置激活菜单项: {target_name}")

                has_meta = sidebar_meta and sidebar_meta.get('menu_count', 0) >= 2
                if has_meta:
                    active_cls = sidebar_meta.get('active_classes', [])
                    if active_cls:
                        # 1. 去掉所有现有的激活 class
                        for ac in active_cls:
                            frame_html = frame_html.replace(f' {ac}', '')
                        # 2. 找到目标项并添加激活 class
                        active_suffix = ' ' + ' '.join(active_cls)
                        body_pos = frame_html.find('<body')
                        search_start = body_pos if body_pos >= 0 else 0
                        # 按文本内容匹配目标菜单项
                        idx = frame_html.find(target_name, search_start)
                        if idx >= 0:
                            # 向前找到包含 class 的标签
                            tag_start = frame_html.rfind('<', 0, idx)
                            if tag_start >= 0:
                                tag_end = frame_html.find('>', tag_start)
                                if tag_end >= 0:
                                    tag_content = frame_html[tag_start:tag_end]
                                    # 找到 class 属性并追加激活类
                                    cls_match = re.search(r'(class\s*=\s*["\']?)([^"\'<>]+)(["\']?)', tag_content)
                                    if cls_match:
                                        old_cls = cls_match.group(0)
                                        base_cls = cls_match.group(2)
                                        new_cls = f'{cls_match.group(1)}{base_cls}{active_suffix}{cls_match.group(3)}'
                                        frame_html = frame_html[:tag_start] + tag_content.replace(old_cls, new_cls, 1) + frame_html[tag_end:]
                                        logger.info(f"[组装] 已激活菜单项: {target_name}")
                        else:
                            logger.warning(f"[组装] 未找到要激活的菜单项: {target_name}")
                else:
                    # 无元数据回退
                    frame_html = frame_html.replace(' is-active', '', 1)
                    active_target = re.search(
                        rf'<div\s+class=nest-menu>.*?<span>{re.escape(target_name)}</span>',
                        frame_html, re.DOTALL | re.IGNORECASE
                    )
                    if active_target:
                        li_pos = frame_html.find('class=el-menu-item', active_target.start())
                        if li_pos >= 0:
                            frame_html = frame_html[:li_pos + len('class=el-menu-item')] + ' is-active' + frame_html[li_pos + len('class=el-menu-item'):]

            # 确保新增菜单项的 href 也被替换
            frame_html = _RE_EXT_HREF.sub('href="javascript:void(0)"', frame_html)

            # 从 AI 输出中移除标记注释
            ai_html = re.sub(r'<!--\s*SIDEBAR_(?:ADD|ACTIVE):\s*.+?\s*-->', '', ai_html)

        # ===== 拼接 AI 内容 =====

        # 判断是 iframe srcdoc 模式还是侧边栏直接嵌入模式
        is_srcdoc_mode = 'srcdoc=' in frame_html[:frame_html.find('{{AI_GENERATED_CONTENT}}') + 100] if '{{AI_GENERATED_CONTENT}}' in frame_html else 'srcdoc=' in frame_html

        if is_srcdoc_mode:
            # srcdoc 模式：需要 HTML 实体编码（& → &amp; 等）
            ai_escaped = ai_html.replace('&', '&amp;').replace('"', '&quot;')

            if '{{AI_GENERATED_CONTENT}}' in frame_html:
                assembled = frame_html.replace('{{AI_GENERATED_CONTENT}}', ai_escaped)
            else:
                logger.warning("[组装] 框架 HTML 中未找到占位符，使用回退方案")
                assembled = re.sub(
                    r'(<iframe[^>]*)\bsrcdoc=(["\'])(.*?)\2',
                    lambda m: m.group(1) + f'srcdoc={m.group(2)}{ai_escaped}{m.group(2)}',
                    frame_html,
                    flags=re.DOTALL | re.IGNORECASE
                )
                if 'srcdoc=' not in assembled:
                    assembled = frame_html + f'\n<iframe srcdoc="{ai_escaped}" style="flex:1;border:none;width:100%;height:100%;"></iframe>'
        else:
            # 侧边栏+内容区模式：AI 内容直接嵌入，无需 srcdoc 编码
            if '{{AI_GENERATED_CONTENT}}' in frame_html:
                assembled = frame_html.replace('{{AI_GENERATED_CONTENT}}', ai_html)
            else:
                logger.warning("[组装] 侧边栏框架中未找到占位符，回退到直接拼接")
                assembled = frame_html + '\n' + ai_html

        # 注入 v-cloak CSS，防止 Vue 未加载时显示 {{ }} 原始变量
        cloak_style = '<style>[v-cloak] { display: none !important; }</style>\n'
        if '</head>' in assembled:
            assembled = assembled.replace('</head>', cloak_style + '</head>', 1)
            logger.info("[组装] 已注入 v-cloak CSS")

        logger.info(f"[组装] 框架+内容拼接完成 (模式={'srcdoc' if is_srcdoc_mode else '侧边栏直接嵌入'}): 框架 {len(frame_html)} 字符 + 内容 {len(ai_html)} 字符 → 总计 {len(assembled)} 字符")
        return assembled

    def inject_page_navigation_listener(self, html_content):
        """在 HTML 中注入页面切换消息监听器"""

        # -1. 移除 iframe sandbox 属性，防止阻止脚本执行
        html_content = _RE_SANDBOX.sub('', html_content)

        # 0. 注入 v-cloak CSS，防止 Vue 未加载时显示 {{ }} 原始变量
        cloak_style = '<style>[v-cloak] { display: none !important; }</style>\n'
        if '</head>' in html_content:
            html_content = html_content.replace('</head>', cloak_style + '</head>', 1)

        # 1. 首先在 Vue 的 return 语句前注入暴露代码
        # 查找模式: "return {" 前插入 "window.currentPage = currentPage;"
        expose_pattern = r'(return\s*\{\s*\n?\s*currentPage)'
        expose_replacement = r'// 暴露 currentPage 到 window (由原型生成器注入)\n                window.currentPage = currentPage;\n\n                \1'
        
        if re.search(expose_pattern, html_content):
            html_content = re.sub(expose_pattern, expose_replacement, html_content, count=1)
            logger.info("[注入] currentPage 已暴露到 window")
        
        # 2. 注入消息监听器
        listener_script = '''
<!-- 页面导航监听器 (由原型生成器自动注入) -->
<script>
(function() {
    // 等待 Vue 应用挂载完成
    var checkInterval = setInterval(function() {
        if (window.currentPage) {
            clearInterval(checkInterval);
            console.log('[原型] currentPage 已就绪');
        }
    }, 100);
    
    // 监听来自 viewer 的页面切换消息
    window.addEventListener('message', function(event) {
        if (event.data && event.data.type === 'navigateTo') {
            var pageName = event.data.page;
            console.log('[原型] 收到页面切换请求:', pageName);
            
            // 使用 window.currentPage (Vue ref)
            if (window.currentPage && window.currentPage.value !== undefined) {
                window.currentPage.value = pageName;
                console.log('[原型] 已切换到页面:', pageName);
            }
            
            // 通知父窗口页面已切换
            if (window.parent !== window) {
                window.parent.postMessage({ type: 'pageChange', page: pageName }, '*');
            }
        }
    });
    
    // 定期向父窗口报告当前页面
    if (window.parent !== window) {
        setInterval(function() {
            if (window.currentPage && window.currentPage.value) {
                window.parent.postMessage({ type: 'pageChange', page: window.currentPage.value }, '*');
            }
        }, 500);
    }
})();
</script>
'''
        
        # 在最后一个 </body> 标签前注入（避免替换 pageData 字符串内的 </body>）
        body_pos = html_content.rfind('</body>')
        if body_pos != -1:
            html_content = (html_content[:body_pos]
                            + listener_script + '\n</body>'
                            + html_content[body_pos + len('</body>'):])
        else:
            html_pos = html_content.rfind('</html>')
            if html_pos != -1:
                html_content = (html_content[:html_pos]
                                + listener_script + '\n</html>'
                                + html_content[html_pos + len('</html>'):])
            else:
                html_content += listener_script
        
        logger.info("[注入] 页面导航监听器已添加")
        return html_content

    # ==================== PRD 相关 API ====================
    
    def handle_prd_save(self):
        """保存 PRD 文档"""
        try:
            content_length = int(self.headers['Content-Length'])
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))
            
            project_id = data.get('projectId')
            page_name = data.get('pageName', 'default')
            content = data.get('content', '')
            
            if not project_id:
                self.send_error_response("缺少 projectId")
                return
            
            # 创建 PRD 目录
            prd_dir = os.path.join(PROJECTS_DIR, project_id, 'prd')
            os.makedirs(prd_dir, exist_ok=True)
            
            # 保存 PRD 文件
            # 清理页面名称，防止路径注入
            safe_page_name = re.sub(r'[^\w\u4e00-\u9fff-]', '_', page_name)
            prd_file = os.path.join(prd_dir, f'{safe_page_name}.md')
            
            with open(prd_file, 'w', encoding='utf-8') as f:
                f.write(content)
            
            logger.info(f"[PRD] 保存: {project_id}/{safe_page_name}.md")
            self.send_json_response({'success': True, 'file': f'{safe_page_name}.md'})
            
        except Exception as e:
            logger.error(f"[PRD错误] 保存失败: {e}")
            import traceback
            traceback.print_exc()
            self.send_error_response(str(e))
    
    # ==================== 模型管理 API ====================
    
    def handle_get_models(self):
        """获取模型列表和当前选中模型"""
        try:
            data = load_models()
            self.send_json_response(data)
        except Exception as e:
            self.send_error_response(str(e))
    
    def handle_model_select(self):
        """切换选中的模型"""
        try:
            content_length = int(self.headers['Content-Length'])
            body = self.rfile.read(content_length)
            req = json.loads(body.decode('utf-8'))
            model_id = req.get('id', '')
            
            data = load_models()
            # 验证模型存在
            found = any(m['id'] == model_id for m in data.get('models', []))
            if not found:
                self.send_error_response("模型不存在")
                return
            
            data['selected_model_id'] = model_id
            save_models(data)
            
            selected = next(m for m in data['models'] if m['id'] == model_id)
            logger.info(f"[模型] 切换到: {selected.get('name', model_id)}")
            self.send_json_response({'success': True, 'selected': selected})
        except Exception as e:
            self.send_error_response(str(e))
    
    def handle_model_save(self):
        """添加或编辑模型"""
        try:
            content_length = int(self.headers['Content-Length'])
            body = self.rfile.read(content_length)
            model_info = json.loads(body.decode('utf-8'))
            
            if not model_info.get('id'):
                self.send_error_response("缺少模型 ID")
                return
            
            data = load_models()
            models = data.get('models', [])
            
            # 查找是否已存在
            existing_idx = next((i for i, m in enumerate(models) if m['id'] == model_info['id']), None)
            if existing_idx is not None:
                models[existing_idx] = model_info
                logger.info(f"[模型] 更新: {model_info.get('name', model_info['id'])}")
            else:
                models.append(model_info)
                logger.info(f"[模型] 新增: {model_info.get('name', model_info['id'])}")
            
            data['models'] = models
            save_models(data)
            self.send_json_response({'success': True, 'model': model_info})
        except Exception as e:
            self.send_error_response(str(e))
    
    def handle_model_delete(self):
        """删除模型"""
        try:
            content_length = int(self.headers['Content-Length'])
            body = self.rfile.read(content_length)
            req = json.loads(body.decode('utf-8'))
            model_id = req.get('id', '')
            
            data = load_models()
            models = data.get('models', [])
            
            if len(models) <= 1:
                self.send_error_response("至少保留一个模型")
                return
            
            data['models'] = [m for m in models if m['id'] != model_id]
            
            # 如果删除的是当前选中的，自动选第一个
            if data.get('selected_model_id') == model_id and data['models']:
                data['selected_model_id'] = data['models'][0]['id']
            
            save_models(data)
            logger.info(f"[模型] 删除: {model_id}")
            self.send_json_response({'success': True})
        except Exception as e:
            self.send_error_response(str(e))

    def handle_model_test(self):
        """测试模型连接：文本 / 多模态 / 工具调用"""
        try:
            content_length = int(self.headers['Content-Length'])
            body = self.rfile.read(content_length)
            req = json.loads(body.decode('utf-8'))

            test_type = req.get('test_type', 'text')  # text | multimodal | tools
            model_config = req.get('model_config', {})

            if not model_config.get('model') or not model_config.get('base_url') or not model_config.get('api_key'):
                self.send_error_response('缺少必填字段 (model / base_url / api_key)')
                return

            is_claude = self._is_claude_api(model_config)
            model_name = model_config.get('model')
            max_tokens = model_config.get('max_tokens') or 256
            temperature = model_config.get('temperature') or 0.7
            timeout = model_config.get('timeout') or 30

            results = {
                'format': 'claude' if is_claude else 'openai',
                'tests': {}
            }

            # ---------- 1. 文本测试 ----------
            messages = [
                {"role": "system", "content": "You are a helpful assistant. Reply in the user's language."},
                {"role": "user", "content": "请用一句话介绍你自己。"}
            ]
            url, headers, payload, _ = self._build_request_params(
                model_config, messages, model_name, max_tokens, temperature
            )
            text_ok, text_detail = self._execute_test_request(url, headers, payload, timeout, is_claude)
            results['tests']['text'] = {
                'success': text_ok,
                'detail': text_detail
            }

            # ---------- 2. 多模态测试 (仅当勾选) ----------
            if test_type == 'multimodal':
                # 生成 1x1 红色 PNG 作为测试图片
                import base64 as _b64
                try:
                    from PIL import Image
                    import io
                    img = Image.new('RGB', (16, 16), color='red')
                    buf = io.BytesIO()
                    img.save(buf, format='PNG')
                    test_img_b64 = _b64.b64encode(buf.getvalue()).decode('utf-8')
                    test_img_data_url = f"data:image/png;base64,{test_img_b64}"
                except ImportError:
                    # 没有 Pillow，用一段合法的最小 PNG
                    # 最小 1x1 红色 PNG
                    test_img_data_url = (
                        "data:image/png;base64,"
                        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
                        "/5+hHgAHggJ/PchI7wAAAABJRU5ErkJggg=="
                    )

                user_content = [
                    {"type": "text", "text": "请描述这张图片的颜色。"},
                    {"type": "image_url", "image_url": {"url": test_img_data_url}}
                ]
                mm_messages = [
                    {"role": "system", "content": "You are a helpful assistant. Reply concisely."},
                    {"role": "user", "content": user_content}
                ]
                mm_url, mm_headers, mm_payload, _ = self._build_request_params(
                    model_config, mm_messages, model_name, max_tokens, temperature
                )
                mm_ok, mm_detail = self._execute_test_request(mm_url, mm_headers, mm_payload, timeout, is_claude)
                results['tests']['multimodal'] = {
                    'success': mm_ok,
                    'detail': mm_detail
                }

            # ---------- 3. 工具调用测试 ----------
            if test_type == 'tools':
                tools_def = [{
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "获取指定城市的天气",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "city": {"type": "string", "description": "城市名"}
                            },
                            "required": ["city"]
                        }
                    }
                }]
                tool_messages = [
                    {"role": "system", "content": "You are a helpful assistant."},
                    {"role": "user", "content": "北京今天天气怎么样？"}
                ]
                tool_url, tool_headers, tool_payload, _ = self._build_request_params(
                    model_config, tool_messages, model_name, max_tokens, temperature,
                    tools=tools_def
                )
                tool_ok, tool_detail = self._execute_test_request(
                    tool_url, tool_headers, tool_payload, timeout, is_claude, expect_tools=True
                )
                results['tests']['tools'] = {
                    'success': tool_ok,
                    'detail': tool_detail
                }

            self.send_json_response({'success': True, 'results': results})

        except Exception as e:
            logger.error(f"[模型测试] 异常: {e}")
            self.send_error_response(str(e))

    def _execute_test_request(self, url, headers, payload, timeout, is_claude, expect_tools=False):
        """执行单次测试请求，返回 (success_bool, detail_dict)"""
        import time as _time
        start = _time.time()
        try:
            session = requests.Session()
            session.trust_env = False
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            resp = session.post(url, json=payload, headers=headers,
                                timeout=timeout, verify=False)
            elapsed = round(_time.time() - start, 2)
            resp.raise_for_status()
            result = resp.json()

            # 错误检查
            if 'error' in result:
                err = result['error']
                err_msg = err if isinstance(err, str) else err.get('message', str(err))
                return False, {'message': err_msg, 'elapsed': elapsed}

            # 解析响应
            if is_claude:
                content_text, finish_reason = self._parse_claude_non_streaming(result)
                tool_calls_info = None
                if expect_tools:
                    content_blocks = result.get('content', [])
                    tool_blocks = [b for b in content_blocks if b.get('type') == 'tool_use']
                    if tool_blocks:
                        tool_calls_info = [{'name': tb.get('name'), 'input': tb.get('input')} for tb in tool_blocks]
            else:
                choice = result.get('choices', [{}])[0]
                message = choice.get('message', {})
                content_text = message.get('content', '')
                finish_reason = choice.get('finish_reason', '')
                tool_calls_info = None
                if expect_tools:
                    raw_tcs = message.get('tool_calls')
                    if raw_tcs:
                        tool_calls_info = []
                        for tc in raw_tcs:
                            try:
                                args = json.loads(tc.get('function', {}).get('arguments', '{}'))
                            except json.JSONDecodeError:
                                args = {}
                            tool_calls_info.append({
                                'name': tc.get('function', {}).get('name'),
                                'input': args
                            })

            detail = {
                'elapsed': elapsed,
                'finish_reason': finish_reason,
                'content': content_text[:500] if content_text else ''
            }
            if expect_tools:
                detail['tool_calls'] = tool_calls_info
                detail['tools_found'] = len(tool_calls_info) if tool_calls_info else 0

            return True, detail

        except Exception as e:
            elapsed = round(_time.time() - start, 2)
            return False, {'message': str(e), 'elapsed': elapsed}
        """停止正在生成的任务（标记为已停止，保留项目）"""
        try:
            content_length = int(self.headers['Content-Length'])
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))
            project_id = data.get('id')

            if not project_id:
                self.send_error_response("Missing project ID")
                return

            # 尝试取消内存中的任务并中断 HTTP 请求
            active_session = None
            with tasks_lock:
                if project_id in generating_tasks:
                    task = generating_tasks[project_id]
                    if task.get('status') == 'generating':
                        task['status'] = 'cancelled'
                        active_session = task.get('session')
                        logger.info(f"[停止] 已取消生成任务: {project_id}")

            # 关闭活跃的 HTTP session，中断正在进行的请求
            if active_session:
                try:
                    active_session.close()
                    logger.info(f"[停止] 已中断 HTTP 请求: {project_id}")
                except Exception:
                    pass

            # 更新项目列表状态为 stopped
            projects = self.load_projects()
            project = next((p for p in projects if p['id'] == project_id), None)
            if project and project.get('status') in ('generating', 'pending_external'):
                project['status'] = 'stopped'
                self.save_projects(projects)
                logger.info(f"[停止] 项目状态已标记为已停止: {project_id}")
                self.send_json_response({'success': True, 'message': '任务已停止'})
            elif project and project.get('status') == 'stopped':
                self.send_json_response({'success': False, 'message': '任务已经处于停止状态'})
            else:
                self.send_json_response({'success': False, 'message': '未找到生成中的任务'})
        except Exception as e:
            self.send_error_response(str(e))

    def handle_resume_generation(self):
        """断点续传：利用对话上下文继续失败的生成"""
        try:
            content_length = int(self.headers['Content-Length'])
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))

            project_id = data.get('projectId')
            additional_instructions = data.get('instructions',
                '请继续完成上一轮未完成的HTML代码生成。从上次中断的地方继续，不要重复已生成的内容。')

            if not project_id:
                self.send_error_response("缺少 projectId")
                return

            project_folder = os.path.join(PROJECTS_DIR, project_id)
            if not os.path.exists(project_folder):
                self.send_error_response("项目不存在")
                return

            # 加载部分内容和原始 prompt
            partial_path = os.path.join(project_folder, 'partial_content.txt')
            prompt_path = os.path.join(project_folder, 'prompt.txt')

            if not os.path.exists(partial_path):
                self.send_error_response("未找到部分内容，无法续传")
                return

            with open(partial_path, 'r', encoding='utf-8') as f:
                partial_content = f.read()

            original_prompt = ''
            if os.path.exists(prompt_path):
                with open(prompt_path, 'r', encoding='utf-8') as f:
                    original_prompt = f.read()

            if not partial_content or len(partial_content) < 50:
                self.send_error_response("部分内容过短，无法续传")
                return

            # 构建多轮对话上下文
            system_prompt = AI_OPTIONS.get('system_prompt',
                'You are a professional UI/UX Developer. Generate complete, standalone HTML prototypes with realistic data. '
                'Critical layout rules: use min-height:100vh for page root, wrap tables in overflow-x:auto containers, '
                'never use max-width or container class on root elements, ensure content fills available space.')

            resume_messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": original_prompt or "生成原型"},
                {"role": "assistant", "content": partial_content},
                {"role": "user", "content": additional_instructions}
            ]

            # 注册异步任务
            with tasks_lock:
                generating_tasks[project_id] = {
                    'status': STATUS_GENERATING,
                    'progress': 10,
                    'error': '',
                    'accumulated_content': '',
                    'stream_chunks': [],
                    'stream_event': threading.Event(),
                    'stream_lock': threading.Lock(),
                }

            # 更新项目列表状态
            projects = self.load_projects()
            for p in projects:
                if p['id'] == project_id:
                    p['status'] = STATUS_GENERATING
                    break
            self.save_projects(projects)

            # 后台续传线程
            def resume_in_background():
                threading.current_thread()._project_id = project_id
                try:
                    logger.info(f"[续传] 开始后台续传: {project_id}")
                    accumulated = ""
                    gen = self.call_ai_model_streaming(
                            resume_messages, cancellable_project_id=project_id)
                    try:
                        for chunk_text, full_content, done, _tool_calls, *_rest in gen:
                            accumulated = full_content
                            with tasks_lock:
                                if project_id in generating_tasks:
                                    task = generating_tasks[project_id]
                                    task['accumulated_content'] = accumulated
                                    reasoning_text = _rest[0] if _rest else ''
                                    push_text = ''
                                    if chunk_text and not chunk_text.startswith('[think]'):
                                        push_text = chunk_text
                                    elif reasoning_text:
                                        push_text = '[think]' + reasoning_text
                                    if push_text:
                                        with task.get('stream_lock', threading.Lock()):
                                            task['stream_chunks'].append(push_text)
                                        evt = task.get('stream_event')
                                        if evt:
                                            evt.set()
                                        if not push_text.startswith('[think]'):
                                            task['progress'] = min(80, 20 + len(accumulated) // 100)
                            if done:
                                break
                    finally:
                        try:
                            gen.close()
                        except RuntimeError:
                            pass

                    # 提取 HTML
                    html_content = self.extract_html(accumulated) if accumulated else None
                    if not html_content:
                        raise Exception("续传未产生有效 HTML")

                    # 下载图片 + 注入导航
                    html_content = download_html_images(html_content, project_folder)
                    html_content = self.inject_page_navigation_listener(html_content)

                    # 保存结果
                    html_path = os.path.join(project_folder, 'index.html')
                    with open(html_path, 'w', encoding='utf-8') as f:
                        f.write(html_content)

                    # 清理部分内容
                    try:
                        os.remove(partial_path)
                    except Exception:
                        pass

                    # 更新状态
                    projects = self.load_projects()
                    for p in projects:
                        if p['id'] == project_id:
                            p['status'] = None
                            break
                    self.save_projects(projects)

                    record_path = os.path.join(project_folder, 'record.json')
                    if os.path.exists(record_path):
                        with open(record_path, 'r', encoding='utf-8') as f:
                            record = json.load(f)
                        record['status'] = STATUS_COMPLETED
                        with open(record_path, 'w', encoding='utf-8') as f:
                            json.dump(record, f, ensure_ascii=False, indent=2)

                    with tasks_lock:
                        if project_id in generating_tasks:
                            generating_tasks[project_id]['status'] = STATUS_COMPLETED
                            generating_tasks[project_id]['progress'] = 100
                            evt = generating_tasks[project_id].get('stream_event')
                            if evt:
                                evt.set()

                    logger.info(f"[续传] 完成: {project_id}")

                except Exception as e:
                    logger.error(f"[续传错误] {project_id}: {e}")
                    import traceback
                    traceback.print_exc()

                    # 保存部分内容
                    with tasks_lock:
                        if project_id in generating_tasks:
                            partial = generating_tasks[project_id].get('accumulated_content', '')
                            generating_tasks[project_id]['status'] = STATUS_FAILED
                            generating_tasks[project_id]['error'] = str(e)
                            evt = generating_tasks[project_id].get('stream_event')
                            if evt:
                                evt.set()

                    if partial and len(partial) > 100:
                        self._save_partial_content(project_id, partial, project_folder)

                    projects = self.load_projects()
                    for p in projects:
                        if p['id'] == project_id:
                            p['status'] = STATUS_FAILED
                            break
                    self.save_projects(projects)

            thread = threading.Thread(target=resume_in_background, daemon=True)
            thread.start()

            self.send_json_response({
                'success': True,
                'project': {'id': project_id},
                'resumed': True
            })

        except Exception as e:
            logger.error(f"[续传] 启动失败: {e}")
            self.send_error_response(str(e))

    # ==================== 模板解析 API ====================

    def handle_template_parse(self):
        """解析上传的 ZIP 文件，提取 HTML 文件列表"""
        try:
            content_length = int(self.headers['Content-Length'])
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))

            zip_base64 = data.get('zipData', '')
            if not zip_base64:
                self.send_json_response({'success': False, 'error': '未提供 ZIP 数据'})
                return

            # 去掉 data:application/zip;base64, 前缀
            if ',' in zip_base64:
                _, b64_data = zip_base64.split(',', 1)
            else:
                b64_data = zip_base64

            zip_bytes = base64.b64decode(b64_data)

            html_files = []
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                for name in zf.namelist():
                    # 跳过隐藏文件和目录
                    if name.startswith('__MACOSX') or name.startswith('.') or name.endswith('/'):
                        continue
                    if name.endswith(('.html', '.htm')):
                        try:
                            content = zf.read(name).decode('utf-8', errors='ignore')
                            # 提取 title
                            title_match = re.search(r'<title[^>]*>(.*?)</title>', content, re.IGNORECASE | re.DOTALL)
                            title = title_match.group(1).strip() if title_match else ''
                            html_files.append({
                                'name': os.path.basename(name),
                                'path': name,
                                'title': title,
                                'size': len(content)
                            })
                        except Exception as e:
                            logger.warning(f"[模板解析] 跳过文件 {name}: {e}")
                            continue

            logger.info(f"[模板解析] ZIP 中找到 {len(html_files)} 个 HTML 文件")
            self.send_json_response({'success': True, 'files': html_files})

        except zipfile.BadZipFile:
            self.send_json_response({'success': False, 'error': '无效的 ZIP 文件'})
        except Exception as e:
            logger.error(f"[模板解析] 失败: {e}")
            self.send_json_response({'success': False, 'error': str(e)})

    @staticmethod
    def extract_design_from_html(html_content, max_chars=8000):
        """从 HTML 中提取对 AI 最有价值的设计信息，控制在 max_chars 以内。
        专门处理 SingleFile 生成的大体积 HTML：剥离 data URL、script、svg 等。"""

        # ===== 第一步：预处理 — 合并清理 script/svg/noscript 为单次替换 =====
        cleaned = _RE_REMOVE_TAGS.sub('', html_content)

        # 4. 替换 img 标签中的 data URL 为占位符
        def replace_data_img(m):
            attrs = m.group(0)
            alt_m = _RE_IMG_ALT.search(attrs)
            width_m = _RE_IMG_WIDTH.search(attrs)
            height_m = _RE_IMG_HEIGHT.search(attrs)
            alt = alt_m.group(1) if alt_m else 'image'
            w = width_m.group(1) if width_m else ''
            h = height_m.group(1) if height_m else ''
            size = f' {w}x{h}' if w and h else ''
            return f'<img alt="{alt}{size}" src="[image]">'
        cleaned = _RE_DATA_IMG.sub(replace_data_img, cleaned)
        cleaned = _RE_EXT_IMG.sub('<!-- image -->', cleaned)

        # ===== 第二步：提取 CSS 样式（最高优先级） =====
        style_blocks = _RE_STYLE_BLOCKS.findall(cleaned)
        styles_text = '\n'.join(style_blocks)

        # CSS 中去掉 data URL 和 @font-face
        styles_text = _RE_CSS_DATA_URL.sub('url([image])', styles_text)
        styles_text = _RE_FONT_FACE.sub('', styles_text)

        # ===== 第三步：提取 body 布局结构 =====
        body_match = _RE_BODY.search(cleaned)
        body_html = body_match.group(1) if body_match else ''

        # 截断重复列表项和表格行（合并为单次遍历）
        body_html = re.sub(
            r'((<(li|tr)[^>]*>.*?</\3>\s*){3})',
            lambda m: m.group(1) + '<!-- ... more items -->',
            body_html,
            flags=re.DOTALL | re.IGNORECASE
        )
        # 截断重复 div 卡片/列表项
        for tag in ['div', 'article', 'section']:
            pattern = rf'((<{tag}[^>]*class=["\'][^"\']*["\'][^>]*>.*?</{tag}>\s*){{3}})'
            body_html = re.sub(
                pattern,
                lambda m, t=tag: m.group(1) + f'<!-- ... more {t}s -->',
                body_html,
                flags=re.DOTALL | re.IGNORECASE
            )

        # ===== 第四步：按优先级组装，控制在 max_chars 以内 =====
        result = ''

        # CSS 样式分配 1/3 预算
        if styles_text.strip():
            style_budget = max_chars // 3
            result += '<style>' + styles_text[:style_budget] + '</style>\n'

        # body 结构分配剩余预算
        remaining = max_chars - len(result)
        if len(body_html) > remaining:
            body_html = body_html[:remaining] + '\n<!-- ... truncated -->'
        result += '<body>\n' + body_html + '\n</body>'

        return result

    @staticmethod
    def split_singlefile_html(html_content):
        """将 SingleFile 生成的大体积 HTML 拆分为框架、内容、CSS 三部分。

        针对大型 SingleFile HTML（可能 30MB+）做了性能优化：
        - 先用字符串操作定位关键区域，再用正则处理小片段
        - 避免对整个 HTML 做全局正则替换

        返回 dict:
            css: str              — 提取并清洗后的 CSS 文本（去掉 @font-face、data URL）
            html_structure: str   — 清洗后的 body HTML 结构（去掉 script/svg/data URL）
            design_tokens: str    — 从 CSS 提取的设计令牌摘要（颜色、字体、组件样式）
            frame_html: str       — 外框架 HTML（精简版，用于 AI prompt）
            is_iframe_layout: bool — 是否为 iframe 布局
            raw_frame_html: str   — 原始框架 HTML（完整版，用于生成后组装）
        """
        total_len = len(html_content)
        parse_start = time.time()
        _t0 = parse_start
        logger.info(f'[模板] 开始解析 HTML: {total_len} 字符')

        # ===== 1. 快速检测 iframe srcdoc 并定位位置 =====
        # 使用字符串查找比正则快几个数量级
        frame_html = ''
        raw_frame_html = ''
        is_iframe_layout = False
        layout_type = 'plain'  # 'iframe' | 'sidebar' | 'plain'
        iframe_start = -1  # 初始化，避免后续引用时 NameError
        srcdoc_pos = -1

        # 快速定位 srcdoc
        srcdoc_idx = html_content.find('srcdoc=')
        if srcdoc_idx == -1:
            srcdoc_idx = html_content.find('srcDoc=')
        if srcdoc_idx == -1:
            srcdoc_idx = html_content.find('SRCDOC=')

        if srcdoc_idx >= 0:
            # 找到 iframe 标签的起始位置
            iframe_start = html_content.rfind('<iframe', 0, srcdoc_idx)
            if iframe_start >= 0:
                # 确定引号类型
                eq_pos = srcdoc_idx + len('srcdoc=')
                if eq_pos < total_len:
                    quote_char = html_content[eq_pos]
                    if quote_char in ('"', "'"):
                        # 找到 srcdoc 属性的结束位置（匹配引号）
                        content_start = eq_pos + 1
                        srcdoc_end = html_content.find(quote_char, content_start)
                        if srcdoc_end > content_start:
                            # 找到 iframe 标签的结束位置
                            iframe_end = html_content.find('>', srcdoc_end)
                            if iframe_end > 0:
                                is_iframe_layout = True
                                layout_type = 'iframe'
                                srcdoc_content_start = content_start
                                srcdoc_content_end = srcdoc_end
                                iframe_tag_end = iframe_end + 1
                                logger.info(
                                    f'[模板] 检测到 iframe srcdoc: '
                                    f'iframe 位置 {iframe_start}-{iframe_tag_end}, '
                                    f'srcdoc 内容 {srcdoc_content_start}-{srcdoc_content_end} '
                                    f'({srcdoc_content_end - srcdoc_content_start} 字符)'
                                )

        if is_iframe_layout:
            # ---- 构建原始框架 HTML（用字符串替换，不用正则）----
            # 在整个 HTML 中将 srcdoc 内容替换为占位符
            raw_frame_html = (
                html_content[:srcdoc_content_start]
                + '{{AI_GENERATED_CONTENT}}'
                + html_content[srcdoc_content_end:]
            )
            logger.info(f'[模板] 原始框架 HTML 构建: {len(raw_frame_html)} 字符')

            # ---- 提取清理后的框架 HTML（用于 AI prompt）----
            # 重要：只搜索 iframe 标签之前的范围，避免匹配到 srcdoc 内部的标签
            # srcdoc 内容中可能包含 <body>/<head> 等，会干扰定位
            pre_iframe_html = html_content[:iframe_start]

            # 在 iframe 之前的范围中找 <body>
            body_start = pre_iframe_html.rfind('<body')
            if body_start >= 0:
                body_content_start = pre_iframe_html.find('>', body_start) + 1
                # 取 body 开始到 iframe 标签之间的内容 + iframe 标签本身（替换 srcdoc）
                frame_part = pre_iframe_html[body_content_start:]
                iframe_tag_before_srcdoc = html_content[iframe_start:srcdoc_content_start]
                iframe_tag_after_srcdoc = html_content[srcdoc_content_end:iframe_tag_end]

                # 清理框架部分（只处理小片段，性能可接受）
                clean_frame = _RE_REMOVE_TAGS.sub('', frame_part)
                clean_frame = _RE_DATA_IMG.sub('<!-- img -->', clean_frame)
                # 截断重复菜单项（保留前10个）
                clean_frame = re.sub(
                    r'((<a[^>]*>.*?</a>\s*){10})',
                    lambda m: m.group(1) + '<!-- ... more menu items -->',
                    clean_frame,
                    flags=re.DOTALL | re.IGNORECASE
                )
                # 组装：框架内容 + 带占位符的 iframe 标签
                frame_html = clean_frame.strip() + '\n' + iframe_tag_before_srcdoc + '{{AI_GENERATED_CONTENT}}' + iframe_tag_after_srcdoc
                if len(frame_html) > 30000:
                    frame_html = frame_html[:30000] + '\n<!-- frame truncated -->'
                frame_html = frame_html.strip()
            else:
                # body 不在 iframe 之前，回退：用整个 pre_iframe 部分
                logger.warning('[模板] 未在 iframe 之前找到 <body>，使用 pre-iframe 范围')
                frame_part = pre_iframe_html
                iframe_tag_before_srcdoc = html_content[iframe_start:srcdoc_content_start]
                iframe_tag_after_srcdoc = html_content[srcdoc_content_end:iframe_tag_end]
                frame_html = (frame_part.strip() + '\n' + iframe_tag_before_srcdoc
                              + '{{AI_GENERATED_CONTENT}}' + iframe_tag_after_srcdoc)[:30000]

            logger.info(f'[模板] iframe 布局，框架 HTML: {len(frame_html)} 字符(精简), {len(raw_frame_html)} 字符(原始)')
        else:
            # ===== 非 iframe 布局：尝试检测「侧边栏+顶栏+内容区」布局 =====
            # 常见模式：mainLayout > mainHeader + mainContent > sideMenuWrapper + contentArea
            # 或 ant-pro-layout 等框架的 sidebar+header+content 结构
            is_sidebar_layout = False
            sidebar_content_boundary = -1  # 内容区域开始的字符位置

            body_start_tag = html_content.find('<body')
            if body_start_tag >= 0:
                body_inner_start = html_content.find('>', body_start_tag) + 1
                body_region = html_content[body_inner_start:]

                # 检测模式1：CSS class 名包含 sideMenuWrapper / sideMenu / sidebar 等
                sidebar_patterns = [
                    (r'class=[\'" ]?sideMenu', 'sideMenu 检测'),
                    (r'class=[\'" ]?sidebar', 'sidebar 检测'),
                    (r'class=[\'" ]?side-menu', 'side-menu 检测'),
                    (r'class="ant-layout-sider', 'Ant Design Sider 检测'),
                    (r'class="ant-pro-sider', 'Ant Design Pro Sider 检测'),
                ]

                detected_pattern = None
                for pattern, desc in sidebar_patterns:
                    match = re.search(pattern, body_region[:200000], re.IGNORECASE)
                    if match:
                        detected_pattern = (pattern, desc, match.start())
                        break

                if detected_pattern:
                    pattern, desc, sidebar_offset = detected_pattern
                    sidebar_pos = body_inner_start + sidebar_offset

                    # 在 sidebar 之后寻找内容区域
                    # 策略：找到 sidebar 容器的结束标签之后的第一个 div
                    # 简化方案：找到 sideMenuWrapper 结束后的同级 div
                    search_after_sidebar = body_region[sidebar_offset:]

                    # 寻找内容区域的开始标记
                    # 常见的内容区 class 名
                    content_markers = [
                        'class=pageContent',
                        'class="pageContent',
                        'class=contentArea',
                        'class="content-area',
                        'class=content-area',
                        'class=mainContent',
                        'class="main-content',
                        'class=main-content',
                        'class="ant-layout-content',
                        'class=ant-layout-content',
                    ]

                    content_start_offset = -1
                    content_marker_tag_end = -1  # 内容区 div 的 > 位置
                    for marker in content_markers:
                        idx = search_after_sidebar.find(marker)
                        if idx >= 0 and idx < 100000:  # 限制搜索范围
                            content_start_offset = idx
                            # 找到这个 div 标签的结束位置 (>), 内容从这里开始
                            tag_end = search_after_sidebar.find('>', idx)
                            if tag_end > idx:
                                content_marker_tag_end = tag_end + 1
                            break

                    if content_start_offset < 0:
                        # 回退：在 sidebar 之后找到第一个独立的 <div 同级元素
                        # 先尝试找到 sideMenuWrapper 的闭合 </div>
                        # 简化：找到 sidebar 开始后 500-50000 字符范围内的第一个顶级 div
                        for marker in content_markers:
                            idx = body_region.find(marker, sidebar_offset + 500)
                            if idx >= 0 and idx < sidebar_offset + 100000:
                                content_start_offset = idx
                                tag_end = body_region.find('>', idx)
                                if tag_end > idx:
                                    content_marker_tag_end = tag_end + 1
                                break

                    if content_start_offset >= 0:
                        is_sidebar_layout = True
                        # 边界包含内容区 div 的开始标签，AI 只替换 div 内部内容
                        if content_marker_tag_end > 0:
                            sidebar_content_boundary = body_inner_start + sidebar_offset + content_marker_tag_end
                        else:
                            sidebar_content_boundary = body_inner_start + sidebar_offset + content_start_offset
                        logger.info(f'[模板] 检测到侧边栏布局（{desc}），内容区域起始: {sidebar_content_boundary}')

            if is_sidebar_layout and sidebar_content_boundary > 0:
                # 将整个 body 分为两部分：
                # 1. 框架部分：从 body 开始到内容区域开始（含顶栏+侧边栏）
                # 2. 内容部分：从内容区域开始到 body 结束

                body_end_tag = html_content.rfind('</body>')
                if body_end_tag < 0:
                    body_end_tag = len(html_content)

                # 确定占位符后面的闭合内容
                after_placeholder = html_content[body_end_tag:]
                if not after_placeholder.strip():
                    # 没有 </body> 标签，手动添加闭合标签
                    # 动态计算需要闭合多少层 div
                    frame_part = html_content[body_start_tag:sidebar_content_boundary]
                    div_opens = len(re.findall(r'<div[\s>]', frame_part, re.IGNORECASE))
                    div_closes = len(re.findall(r'</div>', frame_part, re.IGNORECASE))
                    unclosed_divs = div_opens - div_closes
                    after_placeholder = '</div>' * unclosed_divs + '</body></html>'

                # 构建原始框架 HTML：保留头部+侧边栏，内容区域替换为占位符
                raw_frame_html = (
                    html_content[:sidebar_content_boundary]
                    + '{{AI_GENERATED_CONTENT}}'
                    + after_placeholder
                )
                logger.info(f'[模板] 侧边栏框架 HTML 构建: {len(raw_frame_html)} 字符')

                # 构建精简版框架 HTML（用于 AI prompt）
                # 清理框架中的大块 data:image
                frame_region = html_content[body_inner_start:sidebar_content_boundary]
                clean_frame = _RE_DATA_IMG.sub('<!-- img -->', frame_region)
                clean_frame = re.sub(
                    r'<(script|svg|noscript)[^>]*>.*?</\1>',
                    '', clean_frame, flags=re.DOTALL | re.IGNORECASE
                )
                # 截断过长的框架
                if len(clean_frame) > 30000:
                    clean_frame = clean_frame[:30000] + '\n<!-- ... frame truncated -->'
                frame_html = clean_frame.strip()

                # 覆盖 html_structure 为空（因为是框架模式，不需要单独的 body 结构）
                # 后面提取 HTML 结构时会跳过 iframe 布局，侧边栏布局也一样
                is_iframe_layout = True  # 复用 iframe 路径的处理逻辑
                layout_type = 'sidebar'
                logger.info(f'[模板] 侧边栏布局，框架 HTML: {len(frame_html)} 字符(精简), {len(raw_frame_html)} 字符(原始)')
            else:
                logger.info('[模板] 未检测到 iframe 布局或侧边栏布局，使用普通模板模式')

        logger.info(f'[模板] 步骤1 布局检测: {time.time() - _t0:.3f}s')
        _t0 = time.time()

        # ===== 2. 提取 CSS =====
        # 对于大型 HTML，只搜索外层 <head> 部分的 <style> 标签
        # 重要：如果 srcdoc 内容中有 </head>，find 会错误定位到那里
        # 所以限制搜索范围为 iframe 之前（如果存在 iframe）
        if is_iframe_layout and layout_type == 'iframe':
            css_search_limit = iframe_start
        else:
            head_end = html_content.find('</head>')
            css_search_limit = head_end if head_end > 0 else min(500000, total_len)
        head_section = html_content[:css_search_limit]

        style_blocks = _RE_STYLE_BLOCKS.findall(head_section)
        raw_css = '\n'.join(style_blocks)

        # 清洗 CSS：去掉 @font-face、data URL（使用预编译正则）
        clean_css = _RE_FONT_FACE.sub('', raw_css)
        clean_css = _RE_CSS_DATA_URL.sub('url()', clean_css)

        # 去掉第三方库 CSS（单次替换，替代原来的 9 次循环）
        clean_css = _RE_THIRD_PARTY_CSS.sub('', clean_css)

        # 去掉版权注释块
        clean_css = _RE_CSS_COMMENT.sub('', clean_css)

        # 如果 CSS 仍然超过 200KB，截断
        max_css_size = 200 * 1024
        if len(clean_css) > max_css_size:
            clean_css = clean_css[:max_css_size] + '\n/* ... CSS truncated */'

        # 去掉空行
        clean_css = _RE_BLANK_LINES.sub('\n', clean_css).strip()

        logger.info(f'[模板] 步骤2 CSS提取清洗: {time.time() - _t0:.3f}s')
        _t0 = time.time()

        # ===== 3. 提取 HTML 结构（用于 AI 参考布局）=====
        # 对于 iframe 布局，html_structure 只需要框架部分（已经在 frame_html 中了）
        # 对于非 iframe 布局，提取 body 结构但只处理前 50000 字符
        html_structure = ''
        if not is_iframe_layout:
            # 定位 body 内容
            body_start_tag = html_content.find('<body')
            body_end_tag = html_content.rfind('</body>')
            if body_start_tag >= 0 and body_end_tag > body_start_tag:
                body_inner_start = html_content.find('>', body_start_tag) + 1
                body_end = body_end_tag
            elif body_start_tag >= 0:
                # SingleFile 等工具可能不生成 </body>，此时取 body 开始到文件末尾
                body_inner_start = html_content.find('>', body_start_tag) + 1
                body_end = len(html_content)
                logger.info(f'[模板] 未找到 </body> 标签，从 body 起始位置提取到文件末尾')
            else:
                body_inner_start = -1
                body_end = -1

            if body_inner_start >= 0:
                # 跳过 body 开头的巨大 SVG 图标 sprite（SingleFile 常见，可达数十万字符）
                # 在原始 HTML 中搜索第一个有意义的标签，避免被截断的 chunk 限制
                search_region = html_content[body_inner_start:min(body_inner_start + 500000, body_end)]
                first_meaningful = re.search(
                    r'<(?:div|main|nav|table|form|section|header|article)[\s>]',
                    search_region
                )
                if first_meaningful and first_meaningful.start() > 500:
                    # 前面大段是 SVG sprite，跳过
                    skip_len = first_meaningful.start()
                    logger.info(f'[模板] 跳过 body 开头的 {skip_len} 字符（SVG 图标 sprite）')
                    body_inner_start += skip_len

                body_chunk = html_content[body_inner_start:min(body_inner_start + 50000, body_end)]

                # 清理（使用预编译正则，合并 script/svg/noscript/style 为单次替换）
                body_chunk = re.sub(
                    r'<(script|svg|noscript|style)[^>]*>.*?</\1>',
                    '', body_chunk, flags=re.DOTALL | re.IGNORECASE
                )
                # 处理未闭合的 SVG（如 SVG sprite 无 </svg>）
                body_chunk = re.sub(
                    r'<svg[^>]*>.*?(?=<div|<main|<nav|<table|<form|<section|$)',
                    '', body_chunk, flags=re.DOTALL | re.IGNORECASE
                )
                body_chunk = _RE_DATA_IMG.sub('<!-- img -->', body_chunk)
                # 合并 li 和 tr 的截断为单次正则
                body_chunk = re.sub(
                    r'((<(li|tr)[^>]*>.*?</\3>\s*){3})',
                    lambda m: m.group(1) + '<!-- ... more items -->',
                    body_chunk,
                    flags=re.DOTALL | re.IGNORECASE
                )
                for tag in ['div', 'article', 'section']:
                    body_chunk = re.sub(
                        rf'((<{tag}[^>]*class=["\'][^"\']*["\'][^>]*>.*?</{tag}>\s*){{3}})',
                        lambda m, t=tag: m.group(1) + f'<!-- ... more {t}s -->',
                        body_chunk,
                        flags=re.DOTALL | re.IGNORECASE
                    )
                html_structure = body_chunk[:12000]
        else:
            # iframe 布局时，html_structure 使用 frame_html 作为参考
            html_structure = frame_html[:12000]

        logger.info(f'[模板] 步骤3 HTML结构提取: {time.time() - _t0:.3f}s')
        _t0 = time.time()

        # ===== 4. 剥离 CSS-in-JS 作用域前缀 =====
        # Ant Design 5 等框架使用 :where(.css-xxxxx) 作用域前缀，
        # 这些前缀使 CSS 只在具有对应 hash class 的元素下生效，
        # AI 生成的页面中没有这些 hash class，所以 CSS 完全不生效。
        # 剥离后还原为标准 CSS 选择器，.ant-btn 等类名即可正常匹配。
        clean_css_before = len(clean_css)
        clean_css = CustomHandler._strip_css_scope_prefixes(clean_css)
        if len(clean_css) != clean_css_before:
            logger.info(f'[模板] CSS 作用域前缀剥离: {clean_css_before} → {len(clean_css)} 字符')

        logger.info(f'[模板] 步骤4 CSS作用域剥离: {time.time() - _t0:.3f}s')
        _t0 = time.time()

        # ===== 5. 从 CSS 提取设计令牌 =====
        design_tokens = CustomHandler._extract_design_tokens(clean_css)

        logger.info(f'[模板] 步骤5 设计令牌提取: {time.time() - _t0:.3f}s')
        _t0 = time.time()

        # ===== 6. 侧边栏元数据提取（通用化，不依赖特定 UI 框架）=====
        sidebar_meta = {}
        if layout_type == 'sidebar' and frame_html:
            sidebar_meta = CustomHandler._extract_sidebar_meta(frame_html, html_content)
            if sidebar_meta:
                logger.info(f'[模板] 侧边栏元数据: 框架={sidebar_meta.get("framework","?")}, '
                            f'菜单项数={sidebar_meta.get("menu_count",0)}, '
                            f'激活标记={sidebar_meta.get("active_classes","?")}, '
                            f'菜单列表={sidebar_meta.get("menu_items",[])}')

        logger.info(f'[模板] 步骤6 侧边栏元数据: {time.time() - _t0:.3f}s')
        logger.info(f'[模板] 解析完成: CSS {len(clean_css)} 字符, HTML结构 {len(html_structure)} 字符, 设计令牌 {len(design_tokens)} 字符, 耗时: {time.time() - parse_start:.2f}s')

        return {
            'css': clean_css,
            'html_structure': html_structure,
            'design_tokens': design_tokens,
            'frame_html': frame_html,
            'is_iframe_layout': is_iframe_layout,
            'layout_type': layout_type,  # 'iframe' | 'sidebar' | 'plain'
            'raw_frame_html': raw_frame_html,
            'sidebar_meta': sidebar_meta
        }

    @staticmethod
    def detect_and_split_srcdoc(html_content):
        """检测 srcdoc iframe 项目并拆分为外框架 + 解码后的内部内容。

        专用于编辑场景（微调/对话），与 split_singlefile_html 不同：
        - 返回解码后的内部 HTML（可读，可直接发给 AI）
        - 外框架保留 {{AI_GENERATED_CONTENT}} 占位符（用于后续重组）

        Returns:
            None — 非 srcdoc 项目
            dict — srcdoc 项目，包含:
                raw_frame_html: 外框架 HTML（含占位符）
                inner_html: 解码后的内部内容（真实 HTML）
        """
        total_len = len(html_content)

        # 定位 srcdoc（跳过 Vue 动态绑定 :srcdoc / v-bind:srcdoc）
        for keyword in ('srcdoc=', 'srcDoc=', 'SRCDOC='):
            search_start = 0
            while True:
                srcdoc_idx = html_content.find(keyword, search_start)
                if srcdoc_idx < 0:
                    break
                # 检查前面是否有 ':' 或 'v-bind:' → Vue 动态绑定，跳过
                pre = html_content[max(0, srcdoc_idx - 8):srcdoc_idx]
                if pre.endswith(':') or pre.rstrip().endswith('v-bind:'):
                    search_start = srcdoc_idx + len(keyword)
                    continue
                break
            if srcdoc_idx >= 0:
                break
        if srcdoc_idx < 0:
            return None

        iframe_start = html_content.rfind('<iframe', 0, srcdoc_idx)
        if iframe_start < 0:
            return None

        eq_pos = srcdoc_idx + len('srcdoc=')
        if eq_pos >= total_len:
            return None
        quote_char = html_content[eq_pos]
        if quote_char not in ('"', "'"):
            return None

        content_start = eq_pos + 1
        # srcdoc 内容中的引号已被编码为 &quot;，所以可以直接查找下一个同类型引号
        srcdoc_end = html_content.find(quote_char, content_start)
        if srcdoc_end <= content_start:
            return None

        # 提取编码后的 srcdoc 内容
        encoded_content = html_content[content_start:srcdoc_end]

        # 解码 HTML 实体：还原 srcdoc 属性中的编码
        import html as html_module
        inner_html = html_module.unescape(encoded_content)

        # 构建外框架（用占位符替换 srcdoc 内容）
        raw_frame_html = (
            html_content[:content_start]
            + '{{AI_GENERATED_CONTENT}}'
            + html_content[srcdoc_end:]
        )

        logger.info(f'[srcdoc拆分] 外框架 {len(raw_frame_html)} 字符, '
                    f'内部内容 {len(inner_html)} 字符')

        return {
            'raw_frame_html': raw_frame_html,
            'inner_html': inner_html,
        }

    @staticmethod
    def parse_srcdoc_ai_response(ai_response):
        """解析 AI 对 srcdoc 项目的结构化响应。

        AI 返回格式使用标记分隔：
        - 只改内部: ===INNER_START=== ... ===INNER_END===
        - 只改框架: ===FRAME_START=== ... ===FRAME_END===
        - 都改了: 两个 section 都返回

        Returns:
            dict: { 'inner_html': str|None, 'frame_html': str|None }
        """
        result = {'inner_html': None, 'frame_html': None}

        inner_match = re.search(
            r'===INNER_START===\s*\n(.*?)\n\s*===INNER_END===',
            ai_response, re.DOTALL)
        if inner_match:
            result['inner_html'] = inner_match.group(1).strip()

        frame_match = re.search(
            r'===FRAME_START===\s*\n(.*?)\n\s*===FRAME_END===',
            ai_response, re.DOTALL)
        if frame_match:
            result['frame_html'] = frame_match.group(1).strip()

        # 如果没有标记分隔，整体作为 inner_html（兼容未按格式返回的情况）
        if result['inner_html'] is None and result['frame_html'] is None:
            content = ai_response.strip()
            # 尝试提取 HTML 代码块
            html_block = re.search(r'```html\s*\n(.*?)\n\s*```', content, re.DOTALL)
            if html_block:
                content = html_block.group(1).strip()
            elif content.startswith('<!') or content.startswith('<html') or content.startswith('<HTML'):
                pass  # 已经是纯 HTML
            else:
                # 去掉 markdown 代码围栏包裹
                if content.startswith('```'):
                    first_nl = content.find('\n')
                    if first_nl > 0:
                        content = content[first_nl + 1:]
                stripped = content.rstrip()
                if stripped.endswith('```'):
                    last_fence = content.rfind('```')
                    content = content[:last_fence].rstrip()
            result['inner_html'] = content

        return result

    @staticmethod
    def _extract_sidebar_meta(frame_html, full_html=''):
        """从侧边栏框架 HTML 中自动提取菜单项元数据（通用，不依赖特定 UI 框架）。

        策略：在侧边栏区域扫描所有 HTML 标签，找到出现 >= 3 次且 class 一致的标签
        作为「菜单项」。从中提取文本、激活标记、克隆模板等。

        返回 dict 或 {}。
        """
        if not frame_html:
            return {}

        # 1. 定位侧边栏区域
        sidebar_start = -1
        for marker in ['sideMenuWrapper', 'sideMenu', 'sidebar', 'side-menu',
                        'ant-layout-sider', 'ant-pro-sider', 'nav-menu']:
            idx = frame_html.find(marker)
            if idx >= 0:
                sidebar_start = idx
                break
        if sidebar_start < 0:
            return {}

        # 重要：frame_html 可能包含 <style> 中的 CSS 规则（如 .sideMenuWrapper{...}）
        # 需要跳过 CSS 区域，只在 body HTML 中搜索
        body_tag_pos = frame_html.find('<body')
        if body_tag_pos >= 0 and sidebar_start < body_tag_pos:
            # 找到的 marker 在 CSS 中，跳到 body 后重新查找
            for marker in ['sideMenuWrapper', 'sideMenu', 'sidebar', 'side-menu',
                            'ant-layout-sider', 'ant-pro-sider', 'nav-menu']:
                idx = frame_html.find(marker, body_tag_pos)
                if idx >= 0:
                    sidebar_start = idx
                    break
            if sidebar_start < body_tag_pos:
                return {}

        # 只取侧边栏区域前 30KB（菜单项一定在前部，后面可能是内容区）
        sidebar_scan = frame_html[sidebar_start:sidebar_start + 30000]

        # 2. 找所有带 class 属性的标签，统计每个 class 出现次数
        tag_class_pattern = re.compile(
            r'<(\w+)\s[^>]*?class\s*=\s*["\']?([^\s"\'<>]+)["\']?[\s>]',
            re.DOTALL
        )
        class_counter = {}
        for m in tag_class_pattern.finditer(sidebar_scan):
            cls = m.group(2)
            # 跳过明显不是菜单项的 class
            skip = ('layout', 'container', 'wrapper', 'content', 'header',
                    'footer', 'menuContainer', 'scrollbar', 'icon', 'imgbox')
            if any(s.lower() in cls.lower() for s in skip):
                continue
            # 跳过纯 CSS class（css-xxxxx）
            if cls.startswith('css-') and len(cls) <= 12:
                continue
            class_counter[cls] = class_counter.get(cls, 0) + 1

        # 找出现 >= 3 次的最常见 class（代表重复菜单项）
        best_class = None
        best_count = 0
        for cls, cnt in sorted(class_counter.items(), key=lambda x: -x[1]):
            if cnt >= 3 and cnt > best_count:
                best_class = cls
                best_count = cnt
                break
        if not best_class or best_count < 3:
            return {}

        # 3. 收集该 class 的所有元素
        escaped_class = re.escape(best_class)
        # 匹配整个标签（从 <tag 到下一个同类标签之前或 >）
        item_pattern = re.compile(
            rf'<(\w+)\b[^>]*?\bclass\s*=\s*["\']?{escaped_class}["\']?\b[^>]*?>',
            re.DOTALL
        )
        item_matches = list(item_pattern.finditer(sidebar_scan))
        if len(item_matches) < 3:
            return {}

        menu_items_info = []
        for m in item_matches:
            tag_name = m.group(1)
            tag_start = m.start()
            # 取完整元素内容（到下一个同类标签或最多 1000 字符）
            tag_end = sidebar_scan.find(f'<{tag_name}', tag_start + 5)
            if tag_end < 0:
                tag_end = min(tag_start + 1000, len(sidebar_scan))
            item_html = sidebar_scan[tag_start:tag_end]

            # 提取文本内容
            text = ''
            # 方式1: <span ...>文本</span>
            tm = re.search(r'<span[^>]*>([^<]{1,80})</span>', item_html)
            if tm and len(tm.group(1).strip()) > 0:
                text = tm.group(1).strip()
            if not text:
                # 方式2: title="文本"
                tm = re.search(r'title=["\']([^"\']+)["\']', item_html)
                if tm:
                    text = tm.group(1).strip()
            if not text:
                # 方式3: >文本<
                tm = re.search(r'>([^<]{1,80})<', item_html)
                if tm:
                    text = tm.group(1).strip()

            # 获取完整 class 属性值（可能有多个 class）
            full_cls_match = re.search(r'class\s*=\s*["\']?([^"\'<>]+)["\']?', item_html[:300])
            full_classes = full_cls_match.group(1) if full_cls_match else best_class

            menu_items_info.append({
                'text': text,
                'start': tag_start,
                'html': item_html[:600],
                'full_classes': full_classes
            })

        # 4. 自动检测激活/选中标记
        # 在所有项的 class 中找包含 active/selected/current 等关键词的额外 class
        active_keywords = ['active', 'selected', 'current', 'is-active', 'on']
        active_classes = []
        active_item_text = ''

        base_classes = set(best_class.split())
        for info in menu_items_info:
            extra = set(info['full_classes'].split()) - base_classes
            for cls in extra:
                if any(kw in cls.lower() for kw in active_keywords):
                    if cls not in active_classes:
                        active_classes.append(cls)
                    if not active_item_text and info['text']:
                        active_item_text = info['text']

        # 5. 检测框架类型
        all_cls_str = ' '.join(info['full_classes'] for info in menu_items_info)
        if 'ant-menu' in all_cls_str:
            detected_framework = 'ant-design'
        elif 'el-menu' in all_cls_str:
            detected_framework = 'element-ui'
        elif 'van-sidebar' in all_cls_str:
            detected_framework = 'vant'
        else:
            detected_framework = 'custom'

        # 6. 选模板（第一个非激活项）
        item_template = ''
        for info in menu_items_info:
            is_active = any(ac in info['full_classes'] for ac in active_classes)
            if not is_active:
                item_template = info['html']
                break
        if not item_template:
            item_template = menu_items_info[0]['html']

        # 清理 base64
        item_template = re.sub(r'data:image/[^"\'\s)]+', '', item_template)

        menu_texts = [info['text'] for info in menu_items_info if info['text']]

        return {
            'framework': detected_framework,
            'menu_count': len(menu_items_info),
            'menu_items': menu_texts,
            'active_classes': active_classes,
            'item_template': item_template,
            'active_item_text': active_item_text,
        }

    @staticmethod
    def _strip_css_scope_prefixes(css_text):
        """剥离 CSS-in-JS 作用域前缀（如 Ant Design 5 的 :where(.css-xxxxx)）。

        Ant Design 5 使用 CSS-in-JS，所有选择器带有 :where(.css-HASH) 前缀，
        例如 `:where(.css-mncuj7).ant-btn-primary { background: #1677ff }`。
        这些前缀使 CSS 只在具有对应 hash class 的 DOM 子树中生效，
        导致 AI 生成的页面中使用 `ant-btn` 等 class 时样式完全不匹配。

        此方法将 `:where(.css-HASH)` 前缀剥离，还原为标准 CSS 选择器：
        `.ant-btn-primary { background: #1677ff }`
        """
        if not css_text:
            return css_text
        # 剥离 :where(.css-xxxxx) 前缀（含可能的空格）
        cleaned = re.sub(r':where\(\.css-[a-zA-Z0-9]+\)\s*', '', css_text)
        # 剥离独立出现的 .css-xxxxx 类选择器（作为复合选择器的一部分）
        cleaned = re.sub(r'\.css-[a-zA-Z0-9]+\s*', '', cleaned)
        return cleaned

    @staticmethod
    def _clean_frame_html_for_prompt(html, max_chars=15000):
        """清理框架 HTML 用于 AI prompt 注入：去除 base64 图片、保留结构信息。
        base64 图片数据极大（每个可达数千字符），会挤占 prompt 令牌预算，
        而 AI 只需了解 DOM 结构和 CSS class 命名即可参考样式。
        """
        if not html:
            return ''
        # 替换 <img src="data:image/...;base64,..."> 为简短占位符
        cleaned = re.sub(
            r'<img([^>]*?)\s+src\s*=\s*["\']data:image/[^"\']+["\']',
            r'<img\1 src="<!-- base64_image -->"',
            html,
            flags=re.IGNORECASE
        )
        # 替换 CSS 中的 base64 背景（url(data:image/...;base64,...)）
        cleaned = re.sub(
            r'url\(data:image/[^)]+\)',
            'url(<!-- base64_image -->)',
            cleaned,
            flags=re.IGNORECASE
        )
        # 替换 style 属性中的 base64 背景
        cleaned = re.sub(
            r'(style\s*=\s*["\'][^"\']*?)url\(data:image/[^)]+\)([^"\']*?["\'])',
            r'\1url(<!-- base64_image -->)\2',
            cleaned,
            flags=re.IGNORECASE
        )
        return cleaned[:max_chars]

    @staticmethod
    def _extract_design_tokens(css_text):
        """从 CSS 文本中提取关键设计令牌摘要，用于 AI prompt 注入。
        提取有语义的视觉属性：页面背景色、文字色、主色调、字体等。
        输出精简的设计规范描述，通常在 500-1000 字符以内。
        支持 Ant Design 5 (:where(.css-xxx) 前缀) 和 Element UI 等 CSS 框架。"""

        # Ant Design 5 使用 :where(.css-xxxxx) 前缀选择器，需先剥离以便匹配
        stripped_css = re.sub(r':where\(\.css-[a-zA-Z0-9]+\)', '', css_text)

        tokens = []

        # ===== 1. 提取有语义的关键样式 =====

        # 页面背景色（body、.main-container、Ant Design .ant-layout 等）
        page_bg = None
        for selector in ['body', '.main-container', '.app-wrapper', '.app-main', '.main-content',
                          '.ant-layout:not(.ant-layout-sider)']:
            m = re.search(re.escape(selector) + r'[^{]*\{[^}]*background(?:-color)?\s*:\s*([^;}{]+)',
                          stripped_css, re.IGNORECASE)
            if m:
                val = m.group(1).strip()
                if 'url(' not in val and 'data:' not in val and val != 'transparent':
                    page_bg = val
                    break
        # 回退：从 .ant-layout 提取（不带 :not 条件）
        if not page_bg:
            m = re.search(r'\.ant-layout[^-]*\{[^}]*background(?:-color)?\s*:\s*([^;}{]+)',
                          stripped_css, re.IGNORECASE)
            if m:
                val = m.group(1).strip()
                if 'url(' not in val and 'data:' not in val and val != 'transparent':
                    page_bg = val
        if page_bg:
            tokens.append(f"页面背景色: {page_bg}")

        # 内容区/卡片背景色
        content_bg = None
        for selector in ['.content-container', '.page-container', '.app-main', '.el-main',
                         '.main-content', '.card', '.el-card', '.panel',
                         '.ant-card', '.ant-layout-content']:
            m = re.search(re.escape(selector) + r'[^{]*\{[^}]*background(?:-color)?\s*:\s*([^;}{]+)',
                          stripped_css, re.IGNORECASE)
            if m:
                val = m.group(1).strip()
                if 'url(' not in val and 'data:' not in val and val != 'transparent':
                    content_bg = val
                    break
        if content_bg:
            tokens.append(f"内容区背景色: {content_bg}")

        # body 文字色（同时支持 #hex 和 rgba 格式）
        text_color = None
        # 先尝试 body 选择器
        m = re.search(r'body\s*\{[^}]*color\s*:\s*(#[0-9a-fA-F]{3,8})', stripped_css, re.IGNORECASE)
        if m:
            text_color = m.group(1)
        else:
            # 从 Element UI / Ant Design 常规文字选择器提取
            m = re.search(
                r'(?:\.el-menu-item|\.text-regular|p|span|\.ant-typography|\.ant-menu-item|\.ant-table)'
                r'[^{]*\{[^}]*color\s*:\s*(#[0-9a-fA-F]{3,8})',
                stripped_css, re.IGNORECASE)
            if m:
                text_color = m.group(1)
        # 回退：从 .ant-layout-content 提取（Ant Design 5 常用 rgba 格式）
        if not text_color:
            m = re.search(r'\.ant-layout-content[^{]*\{[^}]*color\s*:\s*(rgba?\([^)]+\)|#[0-9a-fA-F]{3,8})',
                          stripped_css, re.IGNORECASE)
            if m:
                val = m.group(1).strip()
                if 'rgba(0,0,0,0.88)' in val or 'rgba(0, 0, 0, 0.88)' in val:
                    text_color = '#000000d9'  # 近似值
                elif val.startswith('#'):
                    text_color = val
        if text_color:
            tokens.append(f"正文文字色: {text_color}")

        # 主色调（最常出现的 #1890ff 类颜色）
        primary_color = None
        # 优先从 active/primary 选择器的 color 属性提取主色调
        # 同时覆盖 Element UI (.el-xxx) 和 Ant Design (.ant-xxx)
        for selector in ['.el-button--primary', '.ant-btn-primary',
                         '.el-menu-item.is-active', '.ant-menu-item-selected',
                         '.primary', '.el-link--primary', '.ant-link-primary',
                         '.active', '.el-pagination button:hover']:
            # 先找 color（文字色=主色调）
            m = re.search(re.escape(selector) + r'[^{]*\{[^}]*\bcolor\s*:\s*(#[0-9a-fA-F]{3,8})',
                          stripped_css, re.IGNORECASE)
            if m:
                val = m.group(1).lower()
                if val not in ('#fff', '#ffffff', '#333', '#000', '#303133'):
                    primary_color = m.group(1)
                    break
            # 再找 background-color
            m = re.search(re.escape(selector) + r'[^{]*\{[^}]*background(?:-color)?\s*:\s*(#[0-9a-fA-F]{3,8})',
                          stripped_css, re.IGNORECASE)
            if m:
                val = m.group(1).lower()
                if val not in ('#fff', '#ffffff', '#f5f5f5', '#ededed'):
                    primary_color = m.group(1)
                    break
        if not primary_color:
            # 回退：找常见的蓝色系
            m = re.search(r'(?:color|background)\s*:\s*(#1890ff|#409eff|#1677ff|#0958d9|#eb4b4b|#f56c6c)',
                          stripped_css, re.IGNORECASE)
            if m:
                primary_color = m.group(1)
        if primary_color:
            tokens.append(f"主色调: {primary_color}")

        # 字体（同时从 body、.ant-layout、通用声明提取）
        fonts = set()
        for m in re.finditer(r'(?:body|:root|html|\.ant-layout[^-{]*)\s*\{[^}]*font-family\s*:\s*([^;}{]+)',
                             stripped_css, re.IGNORECASE):
            font_val = m.group(1).strip().strip('"\'')
            if 'icon' not in font_val.lower():
                fonts.add(font_val)
        # 回退：从通用 font-family 声明提取（Ant Design 5 可能在组件级别定义）
        if not fonts:
            for m in re.finditer(r'font-family\s*:\s*([^;}{]+)', stripped_css[:20000], re.IGNORECASE):
                font_val = m.group(1).strip().strip('"\'')
                if ('icon' not in font_val.lower() and 'emoji' not in font_val.lower()
                        and len(font_val) > 10):
                    fonts.add(font_val)
                    if len(fonts) >= 2:
                        break
        if fonts:
            tokens.append(f"字体: {', '.join(sorted(fonts))}")

        # 基础字号（同时从 body、.ant-layout 提取）
        base_size = None
        m = re.search(r'(?:body|:root|html|\.ant-layout[^-{]*)\s*\{[^}]*font-size\s*:\s*([^;}{]+)',
                       stripped_css, re.IGNORECASE)
        if m:
            base_size = m.group(1).strip()
            tokens.append(f"基础字号: {base_size}")

        # ===== 2. 补充颜色参考 =====
        colors = set()
        for m in re.finditer(r'(?:color|background|border-color)\s*:[^;]*'
                             r'(#[0-9a-fA-F]{3,8})', stripped_css, re.IGNORECASE):
            colors.add(m.group(1).lower())
        if colors:
            sorted_colors = sorted(colors, key=lambda c: c)
            if len(sorted_colors) > 12:
                sorted_colors = sorted_colors[:12]
            tokens.append(f"其他颜色参考: {', '.join(sorted_colors)}")

        # 圆角
        radii = set()
        for m in re.finditer(r'border-radius\s*:\s*([^;}{]+)', stripped_css, re.IGNORECASE):
            val = m.group(1).strip()
            if val != '0' and val != '0px':
                radii.add(val)
        if radii:
            tokens.append(f"圆角: {', '.join(sorted(radii)[:5])}")

        # 阴影
        shadows = set()
        for m in re.finditer(r'box-shadow\s*:\s*([^;}{]+)', stripped_css, re.IGNORECASE):
            val = m.group(1).strip()
            if val != 'none' and len(val) < 80:
                shadows.add(val)
        if shadows:
            sorted_shadows = sorted(shadows)[:3]
            tokens.append(f"阴影: {', '.join(sorted_shadows)}")

        # ===== 3. Ant Design 5 CSS 变量提取 =====
        # Ant Design 5 使用 --ant-color-primary 等 CSS 变量
        css_var_map = {
            '--ant-color-primary': '主色调',
            '--ant-color-success': '成功色',
            '--ant-color-warning': '警告色',
            '--ant-color-error': '错误色',
            '--ant-color-bg-base': '页面背景色',
            '--ant-color-bg-container': '容器背景色',
            '--ant-color-text': '正文文字色',
            '--ant-color-text-secondary': '次要文字色',
            '--ant-font-family': '字体',
            '--ant-font-size': '基础字号',
            '--ant-border-radius': '圆角',
        }
        for var_name, label in css_var_map.items():
            # 避免与已提取的令牌重复
            existing_labels = [t.split(':')[0] for t in tokens]
            if label in existing_labels:
                continue
            m = re.search(re.escape(var_name) + r'\s*:\s*([^;}{]+)', stripped_css, re.IGNORECASE)
            if m:
                val = m.group(1).strip().strip('"\'')
                if val and val != 'transparent':
                    tokens.append(f"{label}: {val}")

        if not tokens:
            return '(未提取到设计令牌)'

        return '\n'.join(tokens)

    # ==================== 对话编辑解析与应用 ====================

    def _strip_code_fence(self, text):
        """去除文本外层的 ```html ... ``` 或 ``` ... ``` 代码围栏"""
        text = text.strip()
        # 匹配 ```html\n...\n``` 或 ```\n...\n```
        if text.startswith('```'):
            # 找第一个换行（跳过语言标记行）
            first_nl = text.find('\n')
            if first_nl != -1:
                # 找结尾的 ```
                if text.rstrip().endswith('```'):
                    inner = text[first_nl + 1:]
                    # 去掉末尾的 ```
                    inner = inner.rstrip()
                    if inner.endswith('```'):
                        inner = inner[:-3].rstrip()
                    return inner
        return text

    def parse_edits(self, content):
        """从 AI 响应中解析定向编辑块

        支持多种格式变体:
            ### 编辑 [页面名]        (带方括号)
            ### 编辑 页面名          (不带方括号)
            搜索: / 替换为: 后的内容可能被 ```html 包裹

        Returns:
            list[dict]: [{'page': str, 'search': str, 'replace': str}, ...]
        """
        edits = []

        # 匹配 "### 编辑" 开头，支持 [页面名] 和 页面名 两种格式
        # 使用正则 finditer 而非 split，更灵活
        pattern = re.compile(
            r'### 编辑\s*[\[`]?\s*([^\]`\n]+)\s*[\]`]?\s*\n'
        )
        matches = list(pattern.finditer(content))

        for idx, match in enumerate(matches):
            page_name = match.group(1).strip()
            # 编辑体 = 从当前位置到下一个 ### 编辑 或文本末尾
            body_start = match.end()
            body_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(content)
            edit_body = content[body_start:body_end]

            search_text = ''
            replace_text = ''

            # 解析 搜索: ... 替换为: ...
            # 两种格式：直接文本 或 被 ```html 包裹
            search_match = re.search(
                r'搜索[：:]\s*\n([\s\S]*?)\n(?=替换为[：:])',
                edit_body
            )
            if search_match:
                search_text = self._strip_code_fence(search_match.group(1))
            else:
                # 宽松匹配：搜索后面到替换为之间的所有内容
                search_match = re.search(
                    r'搜索[：:]\s*([\s\S]*?)(?=替换为[：:])',
                    edit_body
                )
                if search_match:
                    search_text = self._strip_code_fence(search_match.group(1).strip())

            replace_match = re.search(
                r'替换为[：:]\s*\n([\s\S]*?)$',
                edit_body
            )
            if replace_match:
                replace_text = self._strip_code_fence(replace_match.group(1).rstrip())
            else:
                replace_match = re.search(
                    r'替换为[：:]\s*([\s\S]*?)$',
                    edit_body
                )
                if replace_match:
                    replace_text = self._strip_code_fence(replace_match.group(1).strip())

            if search_text:
                edits.append({
                    'page': page_name,
                    'search': search_text.strip('\n'),
                    'replace': replace_text.strip('\n')
                })

        logger.info(f"[编辑解析] 找到 {len(matches)} 个 ### 编辑 标记, "
                    f"成功解析 {len(edits)} 个编辑块")
        for i, e in enumerate(edits):
            logger.info(f"  编辑 {i+1}: 页面='{e['page']}', "
                        f"搜索长度={len(e['search'])}, 替换长度={len(e['replace'])}")

        return edits

    def apply_edits(self, pages_html, edits):
        """将编辑应用到页面 HTML

        参考 Claude Code 的 findActualString 策略：
        1. 精确匹配
        2. 精确匹配失败 → 空白归一化匹配
        3. 多匹配 → 拒绝，要求更多上下文

        Args:
            pages_html: dict {page_name: html_string}
            edits: list[dict] from parse_edits()

        Returns:
            tuple: (updated_pages_html, results_list)
        """
        updated_pages = dict(pages_html)
        results = []

        for edit in edits:
            page_name = edit['page']
            search_text = edit['search']
            replace_text = edit['replace']

            if page_name not in updated_pages:
                results.append({
                    'applied': False,
                    'page': page_name,
                    'search_snippet': search_text[:80],
                    'error': 'page_not_found'
                })
                continue

            current_html = updated_pages[page_name]

            # 1. 精确匹配
            idx = current_html.find(search_text)

            if idx != -1:
                # 检查唯一性
                second_idx = current_html.find(search_text, idx + 1)
                if second_idx != -1:
                    results.append({
                        'applied': False,
                        'page': page_name,
                        'search_snippet': search_text[:80],
                        'error': 'multiple_matches'
                    })
                    continue

                updated_pages[page_name] = (
                    current_html[:idx] + replace_text + current_html[idx + len(search_text):]
                )
                results.append({
                    'applied': True,
                    'page': page_name,
                    'search_snippet': search_text[:80],
                    'error': None
                })
                continue

            # 2. 空白归一化匹配
            normalized_html = re.sub(r'\s+', ' ', current_html)
            normalized_search = re.sub(r'\s+', ' ', search_text)

            nidx = normalized_html.find(normalized_search)
            if nidx != -1:
                # 检查唯一性
                nidx2 = normalized_html.find(normalized_search, nidx + 1)
                if nidx2 != -1:
                    results.append({
                        'applied': False,
                        'page': page_name,
                        'search_snippet': search_text[:80],
                        'error': 'multiple_matches_fuzzy'
                    })
                    continue

                # 映射回原始位置
                orig_start = self._norm_to_orig(current_html, nidx)
                orig_end = self._norm_to_orig(current_html, nidx + len(normalized_search))
                if orig_start is not None and orig_end is not None:
                    updated_pages[page_name] = (
                        current_html[:orig_start] + replace_text + current_html[orig_end:]
                    )
                    results.append({
                        'applied': True,
                        'page': page_name,
                        'search_snippet': search_text[:80],
                        'error': None
                    })
                    continue

            # 3. 匹配失败
            results.append({
                'applied': False,
                'page': page_name,
                'search_snippet': search_text[:80],
                'error': 'not_found'
            })

        return updated_pages, results

    def _norm_to_orig(self, original, norm_idx):
        """将空白归一化字符串的索引映射回原始字符串索引"""
        orig_pos = 0
        norm_pos = 0
        prev_was_space = False

        while orig_pos < len(original) and norm_pos < norm_idx:
            ch = original[orig_pos]
            if ch in ' \t\n\r':
                if not prev_was_space:
                    norm_pos += 1
                    prev_was_space = True
                orig_pos += 1
            else:
                norm_pos += 1
                orig_pos += 1
                prev_was_space = False

        return orig_pos if norm_pos >= norm_idx else None

    # ----------------------------------------------------------------
    # _match_return_block — 使用括号匹配提取完整的 return {...}
    # ----------------------------------------------------------------
    @staticmethod
    def _match_return_block(script_text):
        """使用括号匹配算法提取完整的 return {...} 块。

        比 ``re.search(r'return\\s*\\{([\\s\\S]*?)\\}')`` 更可靠，
        因为非贪婪正则在 return 体内含嵌套大括号时会提前截断，
        导致后面暴露的变量被误报为 "未定义"。

        Returns:
            _ReturnMatch (兼容 re.Match), or None
        """
        import re as _re

        # 定位 'return {' 的起始位置
        pattern = _re.compile(r'return\s*\{')
        m = pattern.search(script_text)
        if not m:
            return None

        start = m.end() - 1  # 指向 '{'
        depth = 0
        i = start
        in_str = False
        str_ch = None
        in_line_cmt = False
        in_block_cmt = False

        while i < len(script_text):
            ch = script_text[i]

            # 注释处理（高优先级）
            if in_line_cmt:
                if ch == '\n':
                    in_line_cmt = False
                i += 1
                continue
            if in_block_cmt:
                if ch == '*' and i + 1 < len(script_text) and script_text[i + 1] == '/':
                    in_block_cmt = False
                    i += 1
                i += 1
                continue

            # 字符串处理
            if in_str:
                if ch == '\\':
                    i += 2  # 跳过转义字符
                    continue
                if ch == str_ch:
                    in_str = False
                i += 1
                continue

            # 进入字符串 / 注释
            if ch in ('"', "'", '`'):
                in_str = True
                str_ch = ch
                i += 1
                continue
            if ch == '/' and i + 1 < len(script_text):
                nxt = script_text[i + 1]
                if nxt == '/':
                    in_line_cmt = True
                    i += 2
                    continue
                if nxt == '*':
                    in_block_cmt = True
                    i += 2
                    continue

            # 括号计数
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    body = script_text[start + 1:i]
                    return _ReturnMatch(
                        m.start(), m.end(), m.group(0), body)

            i += 1

        return None  # 未找到匹配的 }

    def _run_code_review(self, html_content, page_name, project_id,
                         project_folder, prompt_summary='',
                         sidebar_context='', push_event_fn=None,
                         page_spec=''):
        """页面审查 + 自动修复（确保页面能正常打开、无报错）

        流程：静态诊断 → 自动修复标签 → AI agentic 审查

        Args:
            html_content: 待审查的 HTML
            page_name: 页面名称（用于审查标题一致性）
            project_id: 项目 ID（用于 SSE 推送和取消检测）
            project_folder: 项目目录
            prompt_summary: 用户原始需求摘要
            sidebar_context: 侧边栏上下文信息
            push_event_fn: SSE 推送函数，签名为 fn(data_dict)，None 则静默

        Returns:
            (fixed_html, total_edits, review_summary)
        """
        current_html = html_content
        total_edits = 0

        def _push(data):
            if push_event_fn:
                push_event_fn(data)

        # === 1. 静态诊断 ===
        static_diagnostics = self._diagnose_html(current_html)
        static_errors = [d for d in static_diagnostics if d['severity'] == 'error']
        static_warnings = [d for d in static_diagnostics if d['severity'] == 'warning']

        diag_summary = ''
        if static_errors:
            diag_summary += '\n### 静态诊断发现以下错误（必须修复）：\n'
            for d in static_errors[:10]:
                line_info = f" (行 {d.get('line', '?')})" if d.get('line') else ''
                diag_summary += f"- [{d['rule']}] {d['message']}{line_info}\n"
            if len(static_errors) > 10:
                diag_summary += f"... 还有 {len(static_errors) - 10} 个错误\n"
        if static_warnings:
            diag_summary += '\n### 静态诊断警告（建议修复）：\n'
            for d in static_warnings[:5]:
                line_info = f" (行 {d.get('line', '?')})" if d.get('line') else ''
                diag_summary += f"- [{d['rule']}] {d['message']}{line_info}\n"

        # === 2. 快速验证 + 自动修复标签 ===
        verify_result = self._quick_verify_html(current_html)
        if verify_result is not True:
            # 尝试自动修复标签不平衡
            fixed_html, did_fix = self._auto_fix_tag_imbalances(current_html)
            if did_fix:
                current_html = fixed_html
                total_edits += 1
                logger.info(f"[审查] 自动修复标签不平衡")
                _push({
                    'status': 'fixing',
                    'action': 'auto_fix_tags',
                    'message': f'自动修复标签不平衡'
                })

        # === 3. 判断是否需要 AI 审查 ===
        need_ai_review = bool(static_errors) or verify_result is not True

        if not need_ai_review:
            # 静态检查全部通过，无需 AI 审查
            logger.info(f"[审查] 页面 {page_name} 静态检查通过，跳过 AI 审查")
            _push({
                'status': 'passed',
                'message': f'页面检查通过（自动修复 {total_edits} 处）',
                'total_edits': total_edits
            })
            return current_html, total_edits, '静态检查通过'

        # === 4. AI agentic 审查 ===
        _push({
            'status': 'reviewing',
            'message': f'AI 审查页面 {page_name}...',
            'checks': ['结构完整性', '变量定义', '交互元素', '静态诊断']
        })

        # 审查工具定义
        review_tools = [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": (
                        "读取 HTML 页面源代码。"
                        "在用 edit_file 之前先 read_file 查看精确代码，"
                        "确保 old_string 与页面中完全一致。"
                        "可以指定 start_line 和 end_line 分段读取大文件。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "start_line": {
                                "type": "integer",
                                "description": "起始行号（从1开始），不指定则从第1行开始"
                            },
                            "end_line": {
                                "type": "integer",
                                "description": "结束行号（包含），不指定则读取到末尾"
                            }
                        },
                        "required": []
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "edit_file",
                    "description": (
                        "对 HTML 页面进行精确的搜索替换编辑。\n"
                        "1. old_string 必须从 read_file 返回内容中逐字复制。\n"
                        "2. old_string 应包含 2-5 行，足以唯一匹配。\n"
                        "3. 如果匹配失败，重新 read_file 获取最新内容再重试。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "old_string": {
                                "type": "string",
                                "description": "要搜索的精确文本片段（2-5行）"
                            },
                            "new_string": {
                                "type": "string",
                                "description": "替换后的新文本"
                            }
                        },
                        "required": ["old_string", "new_string"]
                    }
                }
            }
        ]

        # 构建审查系统提示（聚焦页面无报错）
        # 检测是否存在截断（缺 <script> 区域）
        has_script_section = bool(re.search(r'<script[^>]*>(?!.*src=)', current_html, re.IGNORECASE))

        review_system_prompt = (
            "你是一个专业的 HTML 原型页面审查助手。你的任务是检查并修复页面中的错误，"
            "确保页面能正常打开、无 JS 报错。\n\n"
            "## 页面代码已在消息中提供\n"
            "代码关键段已直接放在下方的用户消息中，你无需再用 read_file 读取。直接开始审查和修复。\n\n"
            "## 审查检查清单（按优先级排序）\n\n"
            "### 高优先级（必须修复，否则页面无法正常运行）\n\n"
            "### 1. 页面结构完整性\n"
            "- HTML 标签闭合正确，script/style 标签完整\n"
            "- CSS/JS 资源路径正确\n"
            "- 无明显的语法错误\n\n"
            "### 2. 静态诊断问题\n"
            "- 修复所有静态诊断发现的错误（变量未定义、标签未闭合、括号不匹配等）\n"
            "- 这些错误会直接导致页面白屏或 Vue 无法挂载\n\n"
            "### 3. Vue 初始化完整性\n"
            "- 如果页面使用了 Vue 模板语法（{{ }}、v-model、@click 等），必须有 <script> 中的 createApp 初始化\n"
            "- 如果缺少 <script> 区域，必须根据页面规格（features、dataStructure、interaction）补全 Vue 初始化代码\n"
            "- 补全的代码应包含：createApp、setup()、所有模板引用的 ref/reactive 数据、所有事件处理方法\n\n"
            "### 4. 交互元素\n"
            "- 按钮的点击事件应该有对应处理函数\n"
            "- 表单元素应有合理的默认值和交互\n"
            "- 分页、搜索、筛选等控件应有事件绑定\n\n"
            "### 中优先级（建议修复）\n\n"
            "### 5. 页面标题\n"
            f"- 页面 <title> 标签应该包含「{page_name}」\n"
            "- 页面主标题（h1/h2）应该与项目名一致\n\n"
            "### 6. 侧边栏/导航状态\n"
            "- 如果页面有侧边栏导航，当前页面应该处于选中/激活状态\n"
            "- 选中的菜单项文字应该是当前页面名\n\n"
            "### 7. 跨页面导航链接\n"
            "- 检查页面中是否实现了必需的跨页跳转\n"
            "- 跳转按钮的点击事件应调用 navigateTo() 或修改 currentPage\n\n"
            "### 8. 运行时安全\n"
            "- 检查是否存在会导致页面白屏的 JS 错误\n"
            "- CDN 资源路径有效（不 404）\n\n"
            "## 工作方式\n"
            "1. 页面代码已在用户消息中提供，直接开始审查\n"
            "2. 发现问题立即用 edit_file 修复，一次性修复所有发现的问题\n"
            "3. 如果页面所有检查项都通过，回复「页面审查通过」\n\n"
            "## 重要原则\n"
            "- 只修复真正的问题，不要重构或美化代码\n"
            "- edit_file 的 old_string 必须精确匹配页面中的文本\n"
            "- 如果 old_string 匹配失败，可用 read_file 获取最新代码再重试\n"
            "- 发现所有问题后一次性用 edit_file 修复，不要一轮只读不改"
        )

        # 构建首次用户消息：预读代码 + 页面规格
        lines = current_html.split('\n')
        total_lines = len(lines)

        # ---- 代码上下文构建 ----
        FULL_HTML_THRESHOLD = 200000  # 200K 字符

        if len(current_html) <= FULL_HTML_THRESHOLD:
            # 小页面：全量传入，零遗漏
            code_context = (
                f"### 完整页面代码（{len(current_html)} 字符，{total_lines} 行）\n"
                f"```html\n{current_html}\n```\n"
            )
        else:
            # 大页面：分段提取关键区域
            code_context = ""

            # 1. <head> 区域
            head_end = next((i for i, l in enumerate(lines) if '</head>' in l.lower()), min(50, total_lines))
            code_context += f"### <head> (行 1-{head_end+1})\n```\n" + '\n'.join(lines[:head_end+1]) + "\n```\n\n"

            # 2. <script> 区域
            if has_script_section:
                for i, l in enumerate(lines):
                    if '<script>' in l.lower() and 'src=' not in l.lower():
                        script_start = i
                        script_end = next(
                            (j for j, l2 in enumerate(lines[script_start:], script_start)
                             if '</script>' in l2.lower()), None)
                        if script_end:
                            code_context += (
                                f"### <script> (行 {script_start+1}-{script_end+1})\n"
                                f"```\n" + '\n'.join(lines[script_start:script_end+1]) + "\n```\n\n"
                            )
                        break
            else:
                code_context += "### ⚠️ 缺少 <script> 区域！页面缺少 Vue 初始化代码。\n\n"

            # 3. 尾部（检查闭合）
            tail_start = max(0, total_lines - 50)
            code_context += (
                f"### 尾部 (行 {tail_start+1}-{total_lines})\n"
                f"```\n" + '\n'.join(lines[tail_start:]) + "\n```\n\n"
            )

            # 4. 全文 Vue 变量引用汇总
            vue_refs = re.findall(r'\{\{\s*([^}]+)\s*\}\}', current_html)
            if vue_refs:
                unique_refs = list(dict.fromkeys(v.strip() for v in vue_refs))[:30]
                code_context += (
                    f"### 模板中引用的全部变量（{len(unique_refs)} 个）\n"
                    + '\n'.join(f"- {r}" for r in unique_refs) + "\n\n"
                )

        # ---- 组装用户消息 ----
        review_user_msg = (
            f"请审查这个 HTML 原型页面（共 {len(current_html)} 字符，"
            f"约 {total_lines} 行）。\n"
            f"\n项目名称/页面名：{page_name}"
        )
        if prompt_summary:
            review_user_msg += prompt_summary
        if page_spec:
            review_user_msg += f"\n\n### 页面规格\n{page_spec}\n"
        if sidebar_context:
            review_user_msg += sidebar_context
        if diag_summary:
            review_user_msg += diag_summary

        review_user_msg += (
            "\n\n## 页面代码（已为你预读，无需 read_file）\n\n"
            + code_context +
            "\n\n## 审查要求\n"
            "发现的问题直接用 edit_file 修复。\n"
        )
        if not has_script_section:
            review_user_msg += (
                "⚠️ 页面缺少 <script> 区域！请根据上方「页面规格」中的 features、dataStructure、interaction "
                "补全 Vue 初始化代码（createApp + setup + ref/reactive 数据 + methods）。\n"
            )
        review_user_msg += "如果所有检查项都通过，回复「页面审查通过」。"

        # ---- 防止 prompt 超过 API 输入长度限制 ----
        # 大多数 API 限制在 ~983K 字符；留出 system prompt + tools 的余量
        MAX_REVIEW_INPUT_CHARS = 900000
        total_msg_chars = len(review_system_prompt) + len(review_user_msg)
        if total_msg_chars > MAX_REVIEW_INPUT_CHARS:
            # 截断 code_context 部分，保留其余上下文
            overhead = len(review_system_prompt) + len(review_user_msg) - len(code_context)
            allowed_code = MAX_REVIEW_INPUT_CHARS - overhead - 2000  # 2K 安全余量
            if allowed_code > 10000:
                code_context = code_context[:allowed_code] + '\n\n... (代码已截断，可用 read_file 读取其余部分)'
                # 重新组装用户消息
                review_user_msg = (
                    f"请审查这个 HTML 原型页面（共 {len(current_html)} 字符，"
                    f"约 {total_lines} 行，代码已截断展示）。\n"
                    f"\n项目名称/页面名：{page_name}"
                )
                if prompt_summary:
                    review_user_msg += prompt_summary
                if page_spec:
                    review_user_msg += f"\n\n### 页面规格\n{page_spec}\n"
                if sidebar_context:
                    review_user_msg += sidebar_context
                if diag_summary:
                    review_user_msg += diag_summary
                review_user_msg += (
                    "\n\n## 页面代码（代码过长已截断，可用 read_file 读取）\n\n"
                    + code_context +
                    "\n\n## 审查要求\n"
                    "发现的问题直接用 edit_file 修复。\n"
                )
                if not has_script_section:
                    review_user_msg += (
                        "⚠️ 页面缺少 <script> 区域！请根据上方「页面规格」补全 Vue 初始化代码。\n"
                    )
                review_user_msg += "如果所有检查项都通过，回复「页面审查通过」。"
                logger.info(f"[审查] 代码已截断: {total_msg_chars} → ~{MAX_REVIEW_INPUT_CHARS} 字符")
            else:
                # 连截断后的余量都不够 → 跳过 AI 审查，只依赖静态诊断
                logger.warning(f"[审查] 页面过大 ({total_msg_chars} 字符)，跳过 AI 审查")
                _push({
                    'status': 'passed',
                    'message': f'页面过大跳过 AI 审查（自动修复 {total_edits} 处）',
                    'total_edits': total_edits
                })
                return current_html, total_edits, '页面过大跳过 AI 审查'

        review_messages = [
            {"role": "system", "content": review_system_prompt},
            {"role": "user", "content": review_user_msg}
        ]

        # ===== Feature Flag: Agent Loop 代码审查 =====
        if AI_OPTIONS.get('USE_AGENT_LOOP_REVIEW', False):
            return self._run_code_review_agent(
                current_html, page_name, project_id, project_folder,
                total_edits, review_messages, review_tools, push_event_fn)

        MAX_REVIEW_ROUNDS = 3
        is_verify_round = False

        for review_round in range(MAX_REVIEW_ROUNDS):
            phase_label = '验证' if is_verify_round else '分析+修复'
            logger.info(f"[审查] AI 审查轮次 {review_round + 1}/{MAX_REVIEW_ROUNDS} [{phase_label}]")

            _push({
                'status': 'reviewing',
                'message': f'审查轮次 {review_round + 1}（{phase_label}），已修复 {total_edits} 处问题...',
                'round': review_round + 1,
                'total_edits': total_edits
            })

            accumulated = ""
            tool_calls_result = None
            try:
                review_gen = self.call_ai_model_streaming(
                    review_messages, [],
                    tools=review_tools,
                    cancellable_project_id=project_id
                )
                for chunk_text, full_content, done, tc, *_ in review_gen:
                    accumulated = full_content
                    if done:
                        tool_calls_result = tc
                        break
            except Exception as review_ex:
                logger.warning(f"[审查] AI 审查调用失败: {review_ex}")
                break

            # 推送 AI 分析文本
            if accumulated and len(accumulated.strip()) > 20:
                _push({
                    'status': 'reviewing',
                    'action': 'analysis',
                    'message': accumulated.strip()[:500]
                })

            if not tool_calls_result:
                # AI 没有调用工具，说明它认为页面OK
                logger.info(f"[审查] AI 回复: {accumulated[:200]}")
                _push({
                    'status': 'passed',
                    'message': f'AI 审查完成（共修复 {total_edits} 处问题）',
                    'total_edits': total_edits
                })
                break

            # 处理 tool_calls
            has_edit = False
            has_read = False
            tool_calls_list = []
            if isinstance(tool_calls_result, dict):
                tool_calls_list = list(tool_calls_result.values())
            elif isinstance(tool_calls_result, list):
                tool_calls_list = tool_calls_result

            for tc_data in tool_calls_list:
                if not isinstance(tc_data, dict):
                    continue
                name = tc_data.get('name', '')
                raw_args = tc_data.get('arguments', {})
                if isinstance(raw_args, str):
                    try:
                        args = json.loads(raw_args)
                        if not isinstance(args, dict):
                            args = {}
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                elif isinstance(raw_args, dict):
                    args = raw_args
                else:
                    args = {}
                tc_id = tc_data.get('id', '')

                if name == 'read_file':
                    has_read = True
                    lines = current_html.split('\n')
                    start_line = args.get('start_line', 1)
                    end_line = args.get('end_line', len(lines))
                    content = '\n'.join(lines[start_line - 1:end_line])
                    if len(content) > 300000:
                        content = content[:300000] + f'\n... (内容已截断，请用 start_line/end_line 分段读取)'
                    numbered_header = f"[页面 {start_line}-{end_line} 行 / 共 {len(lines)} 行]\n"
                    review_messages.append({
                        'role': 'tool',
                        'tool_call_id': tc_id,
                        'name': 'read_file',
                        'content': numbered_header + content
                    })
                    logger.info(f"[审查] read_file: 行 {start_line}-{end_line}")
                    _push({
                        'status': 'reviewing',
                        'action': 'read',
                        'message': f'读取代码第 {start_line}-{end_line} 行'
                    })

                elif name == 'edit_file':
                    old_string = args.get('old_string', '')
                    new_string = args.get('new_string', '')

                    if not old_string or not new_string:
                        review_messages.append({
                            'role': 'tool',
                            'tool_call_id': tc_id,
                            'name': 'edit_file',
                            'content': '错误：old_string 和 new_string 不能为空'
                        })
                        continue

                    idx = current_html.find(old_string)
                    if idx >= 0:
                        current_html = (current_html[:idx] + new_string
                                        + current_html[idx + len(old_string):])
                        has_edit = True
                        total_edits += 1
                        old_line = current_html[:idx].count('\n') + 1
                        review_messages.append({
                            'role': 'tool',
                            'tool_call_id': tc_id,
                            'name': 'edit_file',
                            'content': (f'成功：已替换 {len(old_string)} 字符'
                                        f'（约第 {old_line} 行附近）')
                        })
                        logger.info(
                            f"[审查] edit_file 成功: 替换 {len(old_string)}"
                            f" → {len(new_string)} 字符 (第{old_line}行)")
                        _push({
                            'status': 'fixing',
                            'action': 'edit',
                            'message': f'修复第 {total_edits} 处: 替换 {len(old_string)} 字符',
                            'edit_count': total_edits,
                            'success': True
                        })
                    else:
                        first_line = old_string.split('\n')[0][:80]
                        hint = (
                            f"匹配失败：未找到 old_string。\n"
                            f"首行 '{first_line}' "
                            f"{'存在' if first_line in current_html else '不存在'}。\n"
                            f"请重新 read_file 获取最新代码。"
                        )
                        review_messages.append({
                            'role': 'tool',
                            'tool_call_id': tc_id,
                            'name': 'edit_file',
                            'content': hint
                        })
                        logger.warning(f"[审查] edit_file 匹配失败")
                        _push({
                            'status': 'reviewing',
                            'action': 'edit_fail',
                            'message': '编辑匹配失败，重新读取代码...'
                        })

            if has_edit:
                # 保存修复后的版本
                current_html = self._fix_page_data_script_escaping(current_html)
                html_path = os.path.join(project_folder, 'index.html')
                with open(html_path, 'w', encoding='utf-8') as f:
                    f.write(current_html)
                logger.info(f"[审查] 已保存修复后页面 (累计 {total_edits} 处修改)")
                # 有编辑 → 下一轮进入验证模式
                is_verify_round = True
                review_messages.append({
                    "role": "user",
                    "content": (
                        "修复已应用。请验证修复结果，检查是否还有遗漏问题。"
                        "如果没有，回复「页面审查通过」。"
                    )
                })
            elif has_read and not has_edit:
                if is_verify_round:
                    # 验证轮只读不改 = 通过
                    logger.info(f"[审查] 验证轮无新修改，审查通过")
                    break
                else:
                    # 首轮只读不改 → 追问
                    review_messages.append({
                        "role": "user",
                        "content": (
                            "你读取了代码但没有修复。如果发现请立即用 edit_file 修复问题；"
                            "如果没有问题回复「页面审查通过」。"
                        )
                    })
            else:
                break
        else:
            # 达到最大轮次
            logger.info(f"[审查] 达到最大审查轮次 ({MAX_REVIEW_ROUNDS})")
            _push({
                'status': 'passed',
                'message': f'AI 审查完成（{MAX_REVIEW_ROUNDS} 轮，共修复 {total_edits} 处问题）',
                'total_edits': total_edits
            })

        return current_html, total_edits, f'审查完成，修复 {total_edits} 处'

    def _run_code_review_agent(self, current_html, page_name, project_id,
                               project_folder, total_edits,
                               review_messages, review_tools, push_event_fn):
        """使用 AgentLoop 引擎执行 AI 代码审查

        由 _run_code_review 通过 feature flag 调用。
        复用已构建的 review_messages 和静态诊断结果。
        """
        from agent_loop import (AgentLoop, AgentLoopConfig, DiskOverflowManager,
                                 HookManager, HookType)
        from agent_integration import AICallAdapter
        from agent_tools import ToolContext, create_review_registry

        def _push(data):
            if push_event_fn:
                push_event_fn(data)

        _push({
            'status': 'reviewing',
            'message': f'Agent Loop 审查页面 {page_name}...',
        })

        # 创建最小化 session 代理（EditFileTool 需要 session.pages_html）
        class _ReviewSessionProxy:
            def __init__(self, html):
                self.pages_html = {'review_target': html}
                self.page_order = ['review_target']
                self.generated_html = html

        session_proxy = _ReviewSessionProxy(current_html)

        tool_ctx = ToolContext(
            project_id=project_id,
            project_folder=project_folder,
            server=self,
            session=session_proxy,
            extra={'file_content': current_html},
        )

        # SSE 回调 — 将 AgentLoop 事件转为 push_event_fn 格式
        def _sse_callback(event_type, data):
            if event_type == 'context_compacted':
                logger.info(f"[审查 Agent] 上下文已压缩: {data}")

        config = AgentLoopConfig(
            max_turns=3,
            max_consecutive_empty=1,
            tool_names=['read_file', 'edit_file'],
            disk_overflow=True,
            proactive_compaction=False,
        )

        loop = AgentLoop(
            config=config,
            tool_registry=create_review_registry(),
        )
        loop.disk_overflow = DiskOverflowManager()

        ai_call = AICallAdapter(self)

        try:
            result = loop.run(
                initial_messages=review_messages,
                tool_context=tool_ctx,
                streaming_callback=_sse_callback,
                ai_call_fn=ai_call,
            )
        finally:
            if loop.disk_overflow:
                loop.disk_overflow.cleanup_all()

        # 提取编辑后的 HTML
        edited_html = session_proxy.pages_html.get('review_target', current_html)
        edit_count = sum(1 for er in result.state.edit_results if er.get('applied'))
        total_edits += edit_count

        logger.info(
            f"[审查 Agent] 完成: {result.terminal_reason.value}, "
            f"{result.total_turns} 轮, {result.total_tool_calls} 工具调用, "
            f"{edit_count} 处修复"
        )

        _push({
            'status': 'passed',
            'message': f'Agent Loop 审查完成（修复 {total_edits} 处）',
            'total_edits': total_edits,
        })

        return edited_html, total_edits, f'Agent Loop 审查完成，修复 {total_edits} 处'

    def _check_js_safety(self, html):
        """兼容旧调用方 — 委托给 _diagnose_html"""
        result = self._diagnose_html(html)
        errors = [d['message'] for d in result if d['severity'] == 'error']
        return {'safe': len(errors) == 0, 'errors': errors}

    def _diagnose_html(self, html):
        """对生成的 HTML 进行静态诊断（不依赖 AI）

        模拟 Claude Code 的 LSP 诊断，覆盖原型编辑中最常见的错误：
        1. Vue return{} 变量引用 vs 声明不匹配
        2. Vue 模板中的变量引用 vs return{} 暴露不匹配
        3. JavaScript 括号/花括号不匹配
        4. HTML 标签未闭合
        5. 常见 Vue 模板错误（v-for 缺少 :key）

        返回: list of { severity, rule, message, line? }
        """
        diagnostics = []

        # 提取 script 内容
        scripts = re.findall(r'<script[^>]*>([\s\S]*?)</script>', html,
                             re.IGNORECASE)
        script_text = '\n'.join(scripts)

        # 提取 template 内容（<div id="app"> 到 </div> 之前的最后一个 </div>）
        template_html = html
        app_match = re.search(
            r'<div\s+id=["\']app["\'][^>]*>([\s\S]+)', html)
        if app_match:
            # 取到 <script> 之前
            script_pos = html.find('<script', app_match.start())
            if script_pos > app_match.start():
                template_html = html[app_match.start():script_pos]

        # ========== 检查 1: Vue return{} 引用 vs 声明 ==========
        # 使用括号匹配算法提取完整的 return{} 块（正确处理嵌套大括号）
        return_match = self._match_return_block(script_text)
        if return_match:
            return_body = return_match.group(1)
            # 提取 return 中引用的标识符
            refs = re.findall(
                r'(?<!["\'/\w])([a-zA-Z_$]\w*)(?!\s*[:(])', return_body)
            js_keywords = {
                'true', 'false', 'null', 'undefined', 'this',
                'function', 'if', 'else', 'return', 'const', 'let',
                'var', 'new', 'typeof', 'instanceof', 'of', 'in',
                'Object', 'Array', 'String', 'Number', 'Boolean',
                'Math', 'Date', 'JSON', 'console', 'document',
                'window', 'localStorage', 'Promise', 'Map', 'Set',
                'Error', 'parseInt', 'parseFloat', 'isNaN',
                'setTimeout', 'setInterval', 'clearTimeout',
                'clearInterval', 'fetch', 'alert', 'confirm',
            }
            refs = set(refs) - js_keywords

            # 声明的变量名
            declared = set(re.findall(
                r'(?:const|let|var)\s+([a-zA-Z_$]\w*)', script_text))
            declared |= set(re.findall(
                r'function\s+([a-zA-Z_$]\w*)', script_text))

            for ref in sorted(refs):
                if ref not in declared:
                    line_num = self._find_line_number(
                        script_text, ref, return_match.start())
                    diagnostics.append({
                        'severity': 'error',
                        'rule': 'vue-undefined-ref',
                        'message': f"ReferenceError: '{ref}' is not defined"
                                   f" (在 return{{}} 中引用但未声明)",
                        'line': line_num
                    })

        # ========== 检查 2: Vue 模板中的变量 vs return{} 暴露 ==========
        if return_match:
            return_body = return_match.group(1)
            # 提取 return{} 暴露的变量名
            exposed = set(re.findall(
                r'([a-zA-Z_$]\w*)\s*[,:}]', return_body))
            exposed -= {'true', 'false', 'null', 'undefined'}

            # 从模板中提取变量引用
            # v-model="xxx"
            vmodel_refs = set(re.findall(r'v-model="([^".]+)', template_html))
            # v-for="item in xxx" / v-for="(item, i) in xxx"
            vfor_refs = set(re.findall(
                r'v-for="[^"]*\bin\s+([a-zA-Z_$]\w*)', template_html))
            # @click="handlerName" / @click="handlerName()"
            event_refs = set(re.findall(
                r'@\w+="([a-zA-Z_$]\w*)', template_html))
            # :class="xxx" / :style="xxx"（简单标识符）
            colon_refs = set(re.findall(
                r':[a-z-]+="([a-zA-Z_$]\w*)', template_html))
            # {{ xxx }} 模板插值
            interp_refs = set(re.findall(
                r'\{\{\s*([a-zA-Z_$]\w*)', template_html))
            # v-if="xxx" / v-show="xxx"
            conditional_refs = set(re.findall(
                r'v-(?:if|show)="([a-zA-Z_$]\w*)', template_html))

            template_refs = (vmodel_refs | vfor_refs | event_refs |
                             colon_refs | interp_refs | conditional_refs)

            # 过滤掉 JS 关键字和全局对象
            global_refs = {
                'Math', 'Date', 'JSON', 'console', 'window',
                'document', 'localStorage', 'true', 'false', 'null',
                'undefined', 'parseInt', 'parseFloat', 'isNaN',
                'String', 'Number', 'Boolean', 'Array', 'Object',
            }
            template_refs -= global_refs

            # 检查模板中引用但未在 return{} 暴露的变量
            unexposed = template_refs - exposed
            for ref in sorted(unexposed):
                # 也检查是否在 setup() 外部声明（全局变量）
                # 如果变量在 script 中声明了但没在 return 中暴露
                if ref in declared:
                    diagnostics.append({
                        'severity': 'warning',
                        'rule': 'vue-unexposed-ref',
                        'message': f"'{ref}' 已声明但未在 return{{}} 中暴露，"
                                   f"模板中无法访问",
                    })

        # ========== 检查 3: JavaScript 括号匹配 ==========
        brace_stack = []
        brace_pairs = {'(': ')', '[': ']', '{': '}'}
        openers = set(brace_pairs.keys())
        closers = set(brace_pairs.values())
        in_string = False
        string_char = None
        in_comment = False
        in_line_comment = False
        i = 0
        while i < len(script_text):
            ch = script_text[i]

            # 处理字符串
            if not in_comment and not in_line_comment:
                if ch in ('"', "'", '`') and not in_string:
                    in_string = True
                    string_char = ch
                elif in_string and ch == string_char:
                    # 检查是否被转义
                    if i > 0 and script_text[i - 1] == '\\':
                        # 可能是转义的，检查前面有几个反斜杠
                        backslashes = 0
                        j = i - 1
                        while j >= 0 and script_text[j] == '\\':
                            backslashes += 1
                            j -= 1
                        if backslashes % 2 == 0:
                            in_string = False
                    else:
                        in_string = False
                elif not in_string:
                    # 处理注释
                    if ch == '/' and i + 1 < len(script_text):
                        if script_text[i + 1] == '/':
                            in_line_comment = True
                        elif script_text[i + 1] == '*':
                            in_comment = True
                            i += 1
                    elif ch == '\n' and in_line_comment:
                        in_line_comment = False
                    elif ch == '*' and i + 1 < len(script_text) and \
                            script_text[i + 1] == '/':
                        in_comment = False
                        i += 1
                    elif ch in openers:
                        brace_stack.append((ch, i))
                    elif ch in closers:
                        if brace_stack:
                            last_open, _ = brace_stack[-1]
                            if brace_pairs.get(last_open) == ch:
                                brace_stack.pop()
                            else:
                                line_num = script_text[:i].count('\n') + 1
                                diagnostics.append({
                                    'severity': 'error',
                                    'rule': 'js-brace-mismatch',
                                    'message': f"括号不匹配: 期望关闭 "
                                               f"'{brace_pairs.get(last_open)}' "
                                               f"但遇到 '{ch}'",
                                    'line': line_num
                                })
                                brace_stack.pop()
                        else:
                            line_num = script_text[:i].count('\n') + 1
                            diagnostics.append({
                                'severity': 'error',
                                'rule': 'js-brace-mismatch',
                                'message': f"多余的关闭括号 '{ch}'",
                                'line': line_num
                            })
            elif in_line_comment and ch == '\n':
                in_line_comment = False
            elif in_comment and ch == '*' and i + 1 < len(script_text) \
                    and script_text[i + 1] == '/':
                in_comment = False
                i += 1

            i += 1

        for open_ch, pos in brace_stack:
            line_num = script_text[:pos].count('\n') + 1
            diagnostics.append({
                'severity': 'error',
                'rule': 'js-unclosed-brace',
                'message': f"未关闭的 '{open_ch}' (第 {line_num} 行)",
            })

        # ========== 检查 4: HTML 关键标签闭合 ==========
        # 只检查最常被 AI 编辑破坏的结构性标签
        structural_tags = ['header', 'nav', 'main', 'section', 'aside',
                           'footer', 'table', 'thead', 'tbody', 'tr',
                           'form', 'dialog']
        for tag in structural_tags:
            # 统计开闭标签数量
            open_pattern = f'<{tag}[\\s>]'
            close_pattern = f'</{tag}>'
            open_count = len(re.findall(open_pattern, html, re.IGNORECASE))
            close_count = len(re.findall(close_pattern, html, re.IGNORECASE))
            if open_count != close_count:
                diagnostics.append({
                    'severity': 'warning',
                    'rule': 'html-unclosed-tag',
                    'message': f"<{tag}> 标签未闭合 "
                               f"(开 {open_count} 个, 闭 {close_count} 个)",
                })

        # ========== 检查 5: v-for 缺少 :key ==========
        vfor_tags = re.findall(
            r'<([a-zA-Z][a-zA-Z0-9-]*)\s[^>]*v-for="[^"]*"[^>]*>',
            template_html)
        for vfor_tag_match in re.finditer(
                r'v-for="[^"]*"[^>]*>', template_html):
            tag_content = vfor_tag_match.group(0)
            if ':key' not in tag_content and 'v-bind:key' not in tag_content:
                line_num = template_html[:vfor_tag_match.start()].count('\n')
                diagnostics.append({
                    'severity': 'warning',
                    'rule': 'vue-missing-key',
                    'message': "v-for 缺少 :key 绑定",
                    'line': line_num
                })

        # ========== 检查 6: ref/reactive 使用检查 ==========
        # 检查 .value 是否在模板中使用（不应该在模板中用）
        value_in_template = re.findall(
            r'\{\{[^}]*\b\w+\.value\b', template_html)
        if value_in_template:
            diagnostics.append({
                'severity': 'warning',
                'rule': 'vue-value-in-template',
                'message': f"模板中使用了 .value "
                           f"({len(value_in_template)} 处)，"
                           f"Vue 3 模板会自动解包 ref",
            })

        # ========== 检查 7: 声明但未暴露的变量（编辑不完整）==========
        if return_match:
            return_body = return_match.group(1)
            exposed = set(re.findall(
                r'([a-zA-Z_$]\w*)\s*[,:}]', return_body))
            exposed -= {'true', 'false', 'null', 'undefined'}

            all_declared = set(re.findall(
                r'(?:const|let|var)\s+([a-zA-Z_$]\w*)', script_text))
            all_declared |= set(re.findall(
                r'function\s+([a-zA-Z_$]\w*)', script_text))

            # 找出声明了但没在 return{} 中暴露的变量
            unexposed_vars = all_declared - exposed
            # 过滤掉 setup() 内部使用的辅助变量和 API 方法
            internal_vars = {
                'ref', 'reactive', 'computed', 'watch', 'onMounted',
                'onUnmounted', 'nextTick', 'toRefs',
            }
            # 过滤掉 v-for 循环变量（如 item, row, cat 等）
            # v-for="item in list" 或 v-for="(item, index) in list"
            vfor_loop_vars = set()
            for vm in re.finditer(
                    r'v-for="([^"]+)\s+in\s+\w+', template_html):
                decl = vm.group(1).strip()
                if decl.startswith('(') and decl.endswith(')'):
                    # (item, index) → 拆分
                    for part in decl[1:-1].split(','):
                        vfor_loop_vars.add(part.strip())
                else:
                    vfor_loop_vars.add(decl)
            unexposed_vars -= vfor_loop_vars
            # 过滤掉只含单字符和下划线的临时变量
            likely_internal = set()
            for v in unexposed_vars:
                # 以 _ 开头的是内部变量
                if v.startswith('_'):
                    likely_internal.add(v)
                # ref/reactive 的结果变量通常需要暴露
                # 但 API 方法（如 fetch、addEventListener）不需要
            unexposed_vars -= likely_internal

            # 检查这些变量是否在模板中被引用
            for v in sorted(unexposed_vars):
                # 在模板中搜索这个变量名
                # 排除在 script 中已经被使用的情况（内部变量）
                # 只关注在模板 HTML 中出现的
                pattern = re.compile(
                    r'(?:v-model|v-if|v-show|v-for|@click|:class|:style|'
                    r'\{\{\s*)["\s]*\b' + re.escape(v) + r'\b',
                    re.IGNORECASE
                )
                if pattern.search(template_html):
                    diagnostics.append({
                        'severity': 'error',
                        'rule': 'vue-incomplete-edit',
                        'message': f"'{v}' 已声明且在模板中使用，"
                                   f"但未在 return{{}} 中暴露 — "
                                   f"编辑可能不完整",
                    })

        # ========== 检查 8: 重复 const/let/function 声明 ==========
        # 多页组装或 AI 编辑后常见问题：同一作用域内重复声明同名变量
        # 导致 SyntaxError: Identifier 'xxx' has already been declared
        seen_decls = {}  # name -> [(line, keyword)]
        for m in re.finditer(
                r'^([ \t]*)(const|let|var|function)\s+([a-zA-Z_$]\w*)',
                script_text, re.MULTILINE):
            indent = m.group(1)
            keyword = m.group(2)
            name = m.group(3)
            line_num = script_text[:m.start()].count('\n') + 1
            # 只检查顶层声明的重复（缩进 <= 4 空格 / 1 tab）
            # 跳过函数内部的局部变量（缩进 >= 6 空格）
            indent_len = len(indent.replace('\t', '    '))
            if indent_len > 4:
                continue
            if name not in seen_decls:
                seen_decls[name] = []
            seen_decls[name].append((line_num, keyword))

        for name, locations in seen_decls.items():
            if len(locations) > 1:
                lines_str = ', '.join(
                    f'L{l[0]}' for l in locations)
                keywords = set(l[1] for l in locations)
                # const/let 重复声明一定会报错
                # var 允许重复，function 也会提升但可能与 const 冲突
                has_block_scope = any(
                    k in ('const', 'let') for k in keywords)
                if has_block_scope:
                    severity = 'error'
                else:
                    severity = 'warning'
                diagnostics.append({
                    'severity': severity,
                    'rule': 'js-duplicate-declaration',
                    'message': (
                        f"'{name}' 重复声明 "
                        f"({len(locations)} 次: {lines_str}) "
                        f"— SyntaxError: Identifier "
                        f"'{name}' has already been declared"
                    ),
                    'line': locations[1][0]  # 指向第二次声明的行
                })

        return diagnostics

    def _find_line_number(self, text, target, offset=0):
        """在 text 中从 offset 位置查找 target 的行号"""
        pos = text.find(target, offset)
        if pos == -1:
            return None
        return text[:pos].count('\n') + 1

    # ==================== 对话调整 API ====================

    # ---------- 多对话管理 API ----------

    def handle_conversations_list(self):
        """GET /api/conversations?projectId=xxx"""
        try:
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            project_id = params.get('projectId', [''])[0]
            if not project_id:
                self.send_json_response(
                    {'success': False, 'error': '缺少 projectId'})
                return
            project_folder = os.path.join(PROJECTS_DIR, project_id)
            if not os.path.exists(project_folder):
                self.send_json_response(
                    {'success': False, 'error': '项目不存在'})
                return
            from server_conversations import (
                load_conversations_index)
            index, _ = load_conversations_index(project_folder)
            self.send_json_response({
                'success': True,
                'active_conversation_id':
                    index.get('active_conversation_id'),
                'conversations': index.get('conversations', []),
            })
        except Exception as e:
            logger.error(f"[对话] 列表失败: {e}")
            self.send_json_response(
                {'success': False, 'error': str(e)})

    def handle_conversations_search(self):
        """GET /api/conversations/search?projectId=xxx&q=keyword"""
        try:
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            project_id = params.get('projectId', [''])[0]
            query = params.get('q', [''])[0]
            if not project_id:
                self.send_json_response(
                    {'success': False, 'error': '缺少 projectId'})
                return
            project_folder = os.path.join(PROJECTS_DIR, project_id)
            from server_conversations import search_conversations
            results = search_conversations(project_folder, query)
            self.send_json_response({
                'success': True,
                'conversations': results,
            })
        except Exception as e:
            logger.error(f"[对话] 搜索失败: {e}")
            self.send_json_response(
                {'success': False, 'error': str(e)})

    def handle_conversation_export(self):
        """GET /api/conversations/export?projectId=xxx&conversationId=xxx"""
        try:
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            project_id = params.get('projectId', [''])[0]
            conv_id = params.get('conversationId', [''])[0]
            if not project_id:
                self.send_json_response(
                    {'success': False, 'error': '缺少 projectId'})
                return
            project_folder = os.path.join(PROJECTS_DIR, project_id)
            from server_conversations import get_conversation_dir
            conv_dir = get_conversation_dir(project_folder, conv_id)
            html_path = os.path.join(conv_dir, 'index.html')
            if not os.path.exists(html_path):
                self.send_json_response(
                    {'success': False, 'error': '对话 HTML 不存在'})
                return
            filename = f"{conv_id}.html"
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header(
                'Content-Disposition',
                f'attachment; filename="{filename}"')
            with open(html_path, 'rb') as f:
                data = f.read()
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            logger.error(f"[对话] 导出失败: {e}")
            self.send_json_response(
                {'success': False, 'error': str(e)})

    def _read_post_json(self):
        """读取 POST 请求体并解析 JSON"""
        content_length = int(
            self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)
        return json.loads(body.decode('utf-8')) if body else {}

    def handle_conversation_create(self):
        """POST /api/conversations/create"""
        try:
            data = self._read_post_json()
            project_id = data.get('projectId', '')
            title = data.get('title', '')
            clone_from = data.get('cloneFrom', '')
            if not project_id:
                self.send_json_response(
                    {'success': False, 'error': '缺少 projectId'})
                return
            project_folder = os.path.join(PROJECTS_DIR, project_id)
            from server_conversations import create_conversation
            conv = create_conversation(
                project_folder, title=title or None,
                clone_from_id=clone_from or None)
            self.send_json_response({
                'success': True,
                'conversation': conv,
            })
        except Exception as e:
            logger.error(f"[对话] 创建失败: {e}")
            self.send_json_response(
                {'success': False, 'error': str(e)})

    def handle_conversation_switch(self):
        """POST /api/conversations/switch"""
        try:
            data = self._read_post_json()
            project_id = data.get('projectId', '')
            conv_id = data.get('conversationId', '')
            if not project_id or not conv_id:
                self.send_json_response(
                    {'success': False, 'error': '参数不完整'})
                return
            project_folder = os.path.join(PROJECTS_DIR, project_id)
            from server_conversations import switch_conversation
            ok, result = switch_conversation(
                project_folder, conv_id)
            if ok:
                self.send_json_response({
                    'success': True,
                    'conversation': result,
                })
            else:
                self.send_json_response(
                    {'success': False, 'error': result})
        except Exception as e:
            logger.error(f"[对话] 切换失败: {e}")
            self.send_json_response(
                {'success': False, 'error': str(e)})

    def handle_conversation_delete(self):
        """POST /api/conversations/delete"""
        try:
            data = self._read_post_json()
            project_id = data.get('projectId', '')
            conv_id = data.get('conversationId', '')
            if not project_id or not conv_id:
                self.send_json_response(
                    {'success': False, 'error': '参数不完整'})
                return
            project_folder = os.path.join(PROJECTS_DIR, project_id)
            from server_conversations import delete_conversation
            ok, err = delete_conversation(project_folder, conv_id)
            if ok:
                self.send_json_response({'success': True})
            else:
                self.send_json_response(
                    {'success': False, 'error': err})
        except Exception as e:
            logger.error(f"[对话] 删除失败: {e}")
            self.send_json_response(
                {'success': False, 'error': str(e)})

    def handle_conversation_rename(self):
        """POST /api/conversations/rename"""
        try:
            data = self._read_post_json()
            project_id = data.get('projectId', '')
            conv_id = data.get('conversationId', '')
            title = data.get('title', '')
            if not project_id or not conv_id or not title:
                self.send_json_response(
                    {'success': False, 'error': '参数不完整'})
                return
            project_folder = os.path.join(PROJECTS_DIR, project_id)
            from server_conversations import rename_conversation
            rename_conversation(project_folder, conv_id, title)
            self.send_json_response({'success': True})
        except Exception as e:
            logger.error(f"[对话] 重命名失败: {e}")
            self.send_json_response(
                {'success': False, 'error': str(e)})

    def handle_conversation_pin(self):
        """POST /api/conversations/pin"""
        try:
            data = self._read_post_json()
            project_id = data.get('projectId', '')
            conv_id = data.get('conversationId', '')
            pinned = data.get('pinned', False)
            if not project_id or not conv_id:
                self.send_json_response(
                    {'success': False, 'error': '参数不完整'})
                return
            project_folder = os.path.join(PROJECTS_DIR, project_id)
            from server_conversations import toggle_pin
            toggle_pin(project_folder, conv_id, pinned)
            self.send_json_response({'success': True})
        except Exception as e:
            logger.error(f"[对话] 置顶失败: {e}")
            self.send_json_response(
                {'success': False, 'error': str(e)})

    def handle_conversation_undo(self):
        """POST /api/conversations/undo — 撤回指定消息"""
        try:
            data = self._read_post_json()
            project_id = data.get('projectId', '')
            conv_id = data.get('conversationId', '')
            msg_index = data.get('messageIndex', -1)
            if not project_id or not conv_id or msg_index < 0:
                self.send_json_response(
                    {'success': False, 'error': '参数不完整'})
                return

            project_folder = os.path.join(PROJECTS_DIR, project_id)
            import shutil
            from server_conversations import (
                load_snapshot, get_session_path,
                get_conversation_dir,
                update_conversation_meta,
                _activate_conversation_html)

            # 1. 加载快照
            snapshot = load_snapshot(
                project_folder, conv_id, msg_index)
            if not snapshot:
                self.send_json_response({
                    'success': False,
                    'error': '找不到该消息的快照，无法撤回',
                })
                return

            # 2. 恢复页面状态到 session
            session_path = get_session_path(
                project_folder, conv_id)
            from server_session import GenerationSession
            session = _ensure_session_attrs(
                GenerationSession.load(
                    project_id, project_folder, session_path))
            session.pages_html = snapshot.get('pages_html', {})
            session.page_order = snapshot.get('page_order', [])
            session.generated_html = snapshot.get(
                'generated_html', '')
            session.srcdoc_frame_html = snapshot.get(
                'srcdoc_frame_html', '')
            session._chat_head_html = snapshot.get(
                '_chat_head_html', '')

            # 3. 截断消息：移除该条及之后所有
            session.messages = session.messages[:msg_index]

            # 4. 保存 session
            session.save(save_path=session_path)

            # 5. 写 index.html
            html_to_write = session.generated_html
            if session.srcdoc_frame_html:
                html_to_write = self.assemble_iframe_html(
                    html_to_write, session.srcdoc_frame_html)
            if session._chat_head_html:
                html_to_write = _merge_head_body(
                    session._chat_head_html, html_to_write)
            # 保存到对话目录
            conv_dir = get_conversation_dir(
                project_folder, conv_id)
            conv_html = os.path.join(conv_dir, 'index.html')
            with open(conv_html, 'w', encoding='utf-8') as f:
                f.write(html_to_write)
            # 同步到项目根
            root_html = os.path.join(
                project_folder, 'index.html')
            shutil.copy2(conv_html, root_html)

            # 6. 更新索引元数据
            update_conversation_meta(project_folder, conv_id)

            # 7. 返回更新后的消息
            messages = []
            for msg in session.messages:
                role = msg.get('role', '')
                if role in ('user', 'assistant'):
                    entry = {
                        'role': role,
                        'content': msg.get('content', '')[:2000],
                    }
                    if msg.get('html_changes'):
                        entry['html_changes'] = msg['html_changes']
                    messages.append(entry)

            self.send_json_response({
                'success': True,
                'messages': messages,
            })
        except Exception as e:
            logger.error(f"[对话] 撤回失败: {e}")
            self.send_json_response(
                {'success': False, 'error': str(e)})

    # ---------- 对话调整原有 API ----------

    def handle_chat_rollback(self):
        """回滚到编辑前的 HTML — POST /api/chat-rollback"""
        try:
            data = self._read_post_json()
            project_id = data.get('projectId', '')
            conv_id = data.get('conversationId', '')

            if not project_id:
                self.send_json_response({'success': False, 'error': '缺少 projectId'})
                return

            project_folder = os.path.join(PROJECTS_DIR, project_id)

            # 确定回滚的文件路径
            if conv_id:
                from server_conversations import get_conversation_dir
                conv_dir = get_conversation_dir(
                    project_folder, conv_id)
                html_path = os.path.join(conv_dir, 'index.html')
                bak_path = html_path + '.bak'
            else:
                html_path = os.path.join(
                    project_folder, 'index.html')
                bak_path = html_path + '.bak'

            if not os.path.exists(bak_path):
                self.send_json_response({'success': False, 'error': '没有备份可回滚'})
                return

            import shutil
            shutil.copy2(bak_path, html_path)
            # 同步到项目根
            root_html = os.path.join(
                project_folder, 'index.html')
            shutil.copy2(html_path, root_html)

            # 恢复 session 数据
            from server_conversations import get_session_path
            session_path = get_session_path(
                project_folder, conv_id or None)
            from server_session import GenerationSession
            session = _ensure_session_attrs(
                GenerationSession.load(
                    project_id, project_folder, session_path))
            with open(root_html, 'r', encoding='utf-8') as f:
                session.generated_html = f.read()
            session.pages_html = {}
            session.srcdoc_frame_html = ''
            session.save(save_path=session_path)

            self.send_json_response({
                'success': True,
                'message': '已回滚到编辑前版本'
            })

        except Exception as e:
            logger.error(f"[对话] 回滚失败: {e}")
            self.send_json_response({'success': False, 'error': str(e)})

    def handle_chat_history(self):
        """获取对话历史 — GET /api/chat-history?projectId=xxx[&conversationId=xxx]"""
        try:
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            project_id = params.get('projectId', [''])[0]
            conv_id = params.get('conversationId', [''])[0]

            if not project_id:
                self.send_json_response({'success': False, 'error': '缺少 projectId'})
                return

            project_folder = os.path.join(PROJECTS_DIR, project_id)
            if not os.path.exists(project_folder):
                self.send_json_response({'success': False, 'error': '项目不存在'})
                return

            from server_conversations import get_session_path
            session_path = get_session_path(
                project_folder, conv_id or None)
            from server_session import GenerationSession
            session = _ensure_session_attrs(
                GenerationSession.load(
                    project_id, project_folder, session_path))

            # 返回对话消息（包含 html_changes 用于展示编辑记录，
            # 包含 thinking 用于展示 AI 思考过程）
            messages = []
            for msg in session.messages:
                role = msg.get('role', '')
                if role in ('user', 'assistant'):
                    entry = {
                        'role': role,
                        'content': msg.get('content', '')
                    }
                    html_changes = msg.get('html_changes')
                    if html_changes:
                        entry['html_changes'] = html_changes
                    tool_calls_log = msg.get('tool_calls_log')
                    if tool_calls_log:
                        entry['tool_calls_log'] = tool_calls_log
                    thinking = msg.get('thinking', '')
                    if thinking:
                        entry['thinking'] = thinking
                    messages.append(entry)

            self.send_json_response({
                'success': True,
                'messages': messages,
                'pages_html_keys': list(session.pages_html.keys()),
                'has_session': bool(session.messages)
            })

        except Exception as e:
            logger.error(f"[对话历史] 获取失败: {e}")
            self.send_json_response({'success': False, 'error': str(e)})

    def handle_chat(self):
        """对话式原型调整端点 — POST /api/chat

        用户通过自然语言描述修改需求，AI 理解上下文并返回修改后的 HTML。
        支持流式响应（SSE）。
        """
        try:
            data = self._read_post_json()
            project_id = data.get('projectId', '')
            message = data.get('message', '')
            target_page = data.get('targetPage', '')  # 可选：指定调整哪个页面
            conversation_id = data.get('conversationId', '')  # 可选：多对话
            selected_elements = data.get('selectedElements', [])  # 可选：用户选中的元素

            if not project_id or not message:
                self.send_error_response("缺少 projectId 或 message")
                return

            project_folder = os.path.join(PROJECTS_DIR, project_id)
            if not os.path.exists(project_folder):
                self.send_error_response("项目不存在")
                return

            # 加载或创建会话（支持多对话路径）
            from server_conversations import get_session_path
            session_path = get_session_path(
                project_folder, conversation_id or None)
            from server_session import GenerationSession
            session = _ensure_session_attrs(
                GenerationSession.load(
                    project_id, project_folder, session_path))

            # 首次对话：初始化页面数据和元数据
            # 读取 index.html（完整页面）用于判断项目类型
            index_html_path = os.path.join(project_folder, 'index.html')
            index_html = ''
            if os.path.exists(index_html_path):
                with open(index_html_path, 'r', encoding='utf-8') as f:
                    index_html = f.read()

            # 多文件架构检测：从 pages/ 目录加载页面
            pages_dir = os.path.join(project_folder, 'pages')
            is_multi_file_project = os.path.isdir(pages_dir)
            if is_multi_file_project and not session.pages_html:
                page_files = sorted([f for f in os.listdir(pages_dir) if f.endswith('.html')])
                for filename in page_files:
                    match = re.match(r'page_(\d+)_(.+)\.html', filename)
                    if match:
                        page_name = match.group(2)
                        filepath = os.path.join(pages_dir, filename)
                        with open(filepath, 'r', encoding='utf-8') as f:
                            session.pages_html[page_name] = f.read()
                session.page_order = list(session.pages_html.keys())
                if session.page_order:
                    logger.info(f"[对话] 多文件项目: 从 pages/ 加载 {len(session.page_order)} 个页面")
                session._is_multi_file = True

            state_path = os.path.join(project_folder, 'multi_round_state.json')
            # 判断是否为 srcdoc 项目（排除 Vue 动态绑定 :srcdoc / v-bind:srcdoc）
            _check_html = index_html or session.generated_html or ''
            is_srcdoc_project = False
            if 'srcdoc=' in _check_html:
                import re as _re
                # Vue 绑定 :srcdoc 或 v-bind:srcdoc 不是真正的 srcdoc 项目
                _static_srcdoc = _re.sub(r'[:\w-]*:srcdoc\s*=', '', _check_html)
                is_srcdoc_project = 'srcdoc=' in _static_srcdoc

            # 判断是否为侧边栏嵌入项目：
            # 非 srcdoc + index.html 远大于 multi_round_state 片段 = 框架+内容已拼好
            use_full_index = False
            if not is_srcdoc_project and index_html and os.path.exists(state_path):
                try:
                    with open(state_path, 'r', encoding='utf-8') as f:
                        state = json.load(f)
                    fragments = state.get('page_fragments', [])
                    if fragments:
                        max_frag = max(len(f) for f in fragments)
                        # 完整页面是纯内容的 3 倍以上 → 有框架
                        if len(index_html) > max_frag * 3:
                            use_full_index = True
                            logger.info(
                                f"[对话] 侧边栏嵌入项目: index.html "
                                f"{len(index_html)} >> 片段 {max_frag}，"
                                f"使用完整页面")
                except Exception:
                    pass

            if use_full_index:
                # 侧边栏嵌入项目：拆分 head/body
                # head（CSS）不需要 AI 编辑，单独存储
                # body（可编辑内容）存入 pages_html
                page_name = '主页面'
                title_match = re.search(r'<title>([^<]+)</title>',
                                         index_html)
                if title_match:
                    page_name = title_match.group(1).strip()

                head_html, body_html = _split_head_body(index_html)
                if head_html:
                    # 存储 head 框架（类似 srcdoc_frame_html）
                    session._chat_head_html = head_html
                    body_normalized = _normalize_html_lines(body_html)
                    session.pages_html = {page_name: body_normalized}
                    session.page_order = [page_name]
                    session.generated_html = body_normalized
                    logger.info(
                        f"[对话] 拆分加载: head {len(head_html)}"
                        f" + body {len(body_normalized)}"
                        f" ({body_normalized.count(chr(10))} 行)")
                else:
                    # 无法拆分，整个文件 normalize
                    normalized = _normalize_html_lines(index_html)
                    session.pages_html = {page_name: normalized}
                    session.page_order = [page_name]
                    session.generated_html = normalized
                    logger.info(f"[对话] 整体加载: {len(normalized)} 字符")

            elif os.path.exists(state_path) and not session.pages_html:
                # srcdoc 或纯内容项目：从 multi_round_state 加载原始片段
                try:
                    with open(state_path, 'r', encoding='utf-8') as f:
                        state = json.load(f)
                    page_fragments = state.get('page_fragments', [])
                    page_names = state.get('page_names', [])
                    if page_fragments and page_names:
                        for name, fragment in zip(page_names, page_fragments):
                            session.pages_html[name] = fragment
                        session.page_order = list(page_names)
                        logger.info(f"[对话] 首次初始化: 从 multi_round_state 加载 "
                                    f"{len(page_names)} 个页面")
                    # 设计系统
                    ds = state.get('design_system', {})
                    if isinstance(ds, dict):
                        session.design_system = ds.get('css_variables', '')
                    elif isinstance(ds, str):
                        session.design_system = ds
                    # 跨页规格
                    cross_page_spec = state.get('cross_page_spec', {})
                    if cross_page_spec:
                        session.cross_page_spec = cross_page_spec
                except Exception as e:
                    logger.warning(f"[对话] 加载 multi_round_state 失败: {e}")

            # 回退：从生成的 HTML 提取页面
            if not session.pages_html and session.generated_html:
                pages = session.extract_pages_from_html(session.generated_html)
                if pages:
                    session.pages_html = pages
                    session.page_order = list(pages.keys())
                    logger.info(f"[对话] 首次初始化: 从 HTML 提取 {len(pages)} 个页面")

            # 最终兜底：单页面项目（没有多页面结构），整个 HTML 作为单一页面
            if not session.pages_html and session.generated_html:
                page_name = '主页面'
                # 尝试从 title 提取页面名
                title_match = re.search(r'<title>([^<]+)</title>',
                                         session.generated_html)
                if title_match:
                    page_name = title_match.group(1).strip()
                session.pages_html = {page_name: session.generated_html}
                session.page_order = [page_name]
                logger.info(f"[对话] 单页面模式: 以 '{page_name}' 作为单一页面")

            # ===== srcdoc iframe 项目检测 =====
            # 如果 session 中没有 srcdoc_frame_html 但 HTML 是 srcdoc 项目，
            # 拆分并存储框架，用解码后的内部内容替代页面 HTML
            if not session.srcdoc_frame_html and session.generated_html:
                srcdoc_split = self.detect_and_split_srcdoc(
                    session.generated_html)
                if srcdoc_split:
                    session.srcdoc_frame_html = srcdoc_split['raw_frame_html']
                    # 用解码后的内部内容替代 pages_html
                    inner_html = srcdoc_split['inner_html']
                    first_page = next(iter(session.pages_html), None)
                    if first_page:
                        session.pages_html[first_page] = inner_html
                    logger.info(
                        f"[对话] 检测到 srcdoc iframe 项目，"
                        f"已拆分: 框架 {len(session.srcdoc_frame_html)} 字符, "
                        f"内部内容 {len(inner_html)} 字符")
                    session.save(save_path=session_path)

            if not session.page_order and session.pages_html:
                session.page_order = list(session.pages_html.keys())

            # 加载全局配置
            if not session.global_config:
                record_path = os.path.join(project_folder, 'record.json')
                if os.path.exists(record_path):
                    try:
                        with open(record_path, 'r', encoding='utf-8') as f:
                            record = json.load(f)
                        session.global_config = record.get('globalConfig', {})
                        if not session.design_system:
                            session.design_system = record.get('designSystem', '')
                    except Exception:
                        pass

            # 添加用户消息
            session.add_message('user', message)

            # 自动生成对话标题（首条消息时）
            if session.conversation_id and len(session.messages) == 1:
                auto_title = message.strip()[:30]
                if not auto_title:
                    auto_title = '新对话'
                from server_conversations import rename_conversation, update_conversation_meta
                rename_conversation(project_folder, session.conversation_id, auto_title)
                session.title = auto_title

            # 构建 AI 上下文
            ai_messages = session.get_ai_context()

            # System prompt — 定向编辑模式
            system_prompt = AI_OPTIONS.get('system_prompt', '') + """

## 对话调整模式 — 定向编辑

你是一个 UI 原型精调助手。用户用自然语言描述对原型的修改需求。

### 历史对话处理（非常重要！）
对话中包含历史消息。**只处理最后一条用户消息的要求**。
之前的用户消息和助手回复是已完成的操作，仅作为上下文参考。
绝对不要重复执行历史中的旧要求。

### 工作方式
1. 理解用户的修改意图
2. 分析提供的当前页面 HTML 代码
3. 使用 edit_file 工具进行精确的搜索替换编辑

### 修改范围判断（非常重要！）

**小范围修改**（修改 1-2 个位置）：使用 edit_file 工具的 old_string/new_string
- 颜色、文字、布局微调、单个组件修改
- old_string 必须从 read_page 返回内容中逐字精确复制（包括空格、缩进、换行）
- 绝对不要缩短、省略或修改 old_string 中的任何字符
- 应包含 2-5 行上下文确保唯一性
- 重命名变量时使用 replace_all=true

**大范围修改**（新增功能、多区域联动、结构调整）：**必须使用 write_page 工具**
- 新增弹窗、表单、Tab 页、功能模块
- 修改涉及 HTML 模板 + Vue 数据声明 + return{} 暴露 + 事件处理 等多处联动
- 任何需要 3 处以上搜索替换的修改，都应该用 write_page
- 使用 write_page 时，完整输出修改后的整个页面 HTML

### 为什么大范围修改要用 write_page？
搜索替换方式容易遗漏：只改了变量声明但忘了改 return{}，只加了 HTML 但忘了绑定事件。
write_page 虽然输出较长，但能保证代码完整性，不会遗漏任何部分。

### 注意事项
- 保持已有的 CSS 变量和 Tailwind 类确保风格一致
- 页面名称在「## 当前页面：」标题中给出
- 如果用户的需求不明确，先询问确认

### 编辑后自检（重要！）
每次使用 write_page 重写或进行大范围编辑后，必须自行检查：
1. 所有在模板中使用的变量（v-model、@click、{{ }}等）都已声明且在 return{} 中暴露
2. JavaScript 括号/花括号正确闭合，没有遗漏
3. 新增的 HTML 标签都已正确闭合
如果不确定，使用 read_page 工具查看编辑结果确认代码正确。
"""
            ai_messages.insert(0, {"role": "system", "content": system_prompt})

            # 生成页面结构摘要（替代完整 HTML 注入，节省 ~80% tokens）
            pages_summary = self._build_pages_summary(session)
            if pages_summary:
                ai_messages.append({
                    "role": "system",
                    "content": pages_summary
                })

            # 设计系统上下文
            if session.design_system:
                ai_messages.append({
                    "role": "system",
                    "content": f"设计系统 CSS 变量:\n```css\n{session.design_system}\n```"
                })

            # 用户选中的元素上下文（从对话模式元素选择功能传入）
            if selected_elements:
                # 验证和限制
                if not isinstance(selected_elements, list):
                    selected_elements = []
                elif len(selected_elements) > 20:
                    logger.warning(
                        f"[对话] 选中元素过多 ({len(selected_elements)})，截断为 20")
                    selected_elements = selected_elements[:20]
                elements_desc_parts = []
                for i, el in enumerate(selected_elements):
                    desc = f"### 元素 {i + 1}"
                    selector = el.get('selector', '')
                    summary = el.get('summary', '')
                    html_snippet = el.get('html', '')
                    if selector:
                        desc += f"\n- CSS 选择器: `{selector}`"
                    if summary:
                        desc += f"\n- 概要: `{summary}`"
                    if html_snippet:
                        # 截断过长的 HTML（保留核心结构）
                        if len(html_snippet) > 2000:
                            html_snippet = html_snippet[:2000] + '\n<!-- ... 截断 -->'
                        desc += f"\n- HTML:\n```html\n{html_snippet}\n```"
                    elements_desc_parts.append(desc)

                elements_context = (
                    f"## 用户选中的页面元素\n\n"
                    f"用户在页面上选中了 {len(selected_elements)} 个元素，"
                    f"修改应聚焦于这些元素。\n\n"
                    + "\n\n".join(elements_desc_parts) +
                    "\n\n请优先使用 edit_file 工具，"
                    "通过搜索选中元素的 HTML 片段来定位并精确修改。"
                    "不要修改选中元素之外的代码（除非用户明确要求）。"
                )
                ai_messages.append({
                    "role": "system",
                    "content": elements_context
                })
                logger.info(
                    f"[对话] 注入 {len(selected_elements)} 个选中元素上下文")

            # 设置 SSE 响应（流式）
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Connection', 'keep-alive')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()

            # 定义 function calling tools（参考 Claude Code FileEditTool）
            edit_tools = [
                {
                    "type": "function",
                    "function": {
                        "name": "read_page",
                        "description": (
                            "Read the HTML source of a page. "
                            "You MUST call this before using edit_file or write_page.\n"
                            "The output uses line numbers (e.g. '  123→<div>'). "
                            "When copying content for edit_file's old_string, "
                            "copy ONLY the content after the arrow, NOT the line number prefix.\n"
                            "For large pages, use start_line and end_line to read "
                            "only the section you need to modify (typically 20-50 lines). "
                            "For small pages (<1000 lines), you can read the entire page."
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "page": {
                                    "type": "string",
                                    "description": "Page name (must match exactly a name from list_pages)"
                                },
                                "start_line": {
                                    "type": "integer",
                                    "description": "Line number to start reading from (1-indexed). Only provide if the page is too large to read at once"
                                },
                                "end_line": {
                                    "type": "integer",
                                    "description": "Line number to end reading at (inclusive). Only provide if the page is too large to read at once."
                                }
                            },
                            "required": []
                        }
                    }
                },
                {
                    "type": "function",
                    "function": {
                        "name": "edit_file",
                        "description": (
                            "Performs exact string replacements in a page.\n"
                            "Usage:\n"
                            "1. You MUST call read_page at least once before editing."
                            " The old_string must be copied verbatim from read_page output"
                            " (including all whitespace and indentation).\n"
                            "2. old_string should be 2-5 lines to ensure unique match."
                            " The edit will fail if old_string is not unique in the page.\n"
                            "3. Use replace_all=true to replace all occurrences of old_string"
                            " (e.g. renaming a variable across the page).\n"
                            "4. If the edit fails, re-read the page with read_page"
                            " to get the current content, then retry."
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "page": {
                                    "type": "string",
                                    "description": "Page name (must match exactly a name from list_pages)"
                                },
                                "old_string": {
                                    "type": "string",
                                    "description": "The text to replace (2-5 lines, must be unique in the page)"
                                },
                                "new_string": {
                                    "type": "string",
                                    "description": "The text to replace it with (must be different from old_string)"
                                },
                                "replace_all": {
                                    "type": "boolean",
                                    "description": "Replace all occurrences of old_string (default false)",
                                    "default": False
                                }
                            },
                            "required": ["page", "old_string", "new_string"]
                        }
                    }
                },
                {
                    "type": "function",
                    "function": {
                        "name": "write_page",
                        "description": (
                            "Write the complete HTML content for a page,"
                            " overwriting the existing content."
                            " Use this for large-scale refactoring that would"
                            " require many individual edits."
                            " You MUST call read_page first to see the current content"
                            " before overwriting."
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "page": {
                                    "type": "string",
                                    "description": "Page name (must match exactly a name from list_pages)"
                                },
                                "html": {
                                    "type": "string",
                                    "description": "The complete HTML content to write for the page"
                                }
                            },
                            "required": ["page", "html"]
                        }
                    }
                },
                {
                    "type": "function",
                    "function": {
                        "name": "list_pages",
                        "description": (
                            "List all pages in the project with summary info. "
                            "Returns page name, line count, char count, "
                            "key HTML elements and Vue variables for each page. "
                            "Call this before using read_page/edit_file/write_page "
                            "to identify which page to work on."
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {},
                            "required": []
                        }
                    }
                }
            ]

            # ========== Agentic Loop (类 Claude Code while not done) ==========
            # 统一循环：AI 调用 → 处理 tool_calls(read/edit) → 反馈结果 → 继续
            # 诊断错误、编辑失败、空响应都在循环内处理，不再走独立降级路径

            # ===== Feature Flag: Agent Loop 对话调整 =====
            _use_agent_loop = AI_OPTIONS.get('USE_AGENT_LOOP_CHAT', False)
            _agent_loop_done = False

            # 提前初始化变量（Agent Loop 和旧循环都需要）
            pre_existing_imbalances = {}
            pre_existing_console_errors = []
            if _use_agent_loop:
                try:
                    _html_path = os.path.join(project_folder, 'index.html')
                    if os.path.exists(_html_path):
                        with open(_html_path, 'r', encoding='utf-8') as _f:
                            pre_existing_imbalances = (
                                self._get_tag_imbalances(_f.read()))
                except Exception:
                    pass

            if _use_agent_loop:
                agent_loop_result = self._chat_run_agent_loop(
                    ai_messages, session, project_folder,
                    pre_existing_imbalances, pre_existing_console_errors,
                    edit_tools, project_id)

                accumulated = agent_loop_result.get('accumulated', '')
                accumulated_reasoning = agent_loop_result.get('accumulated_reasoning', '')
                edit_results = agent_loop_result.get('edit_results', [])
                tool_calls_log = agent_loop_result.get('tool_calls_log', [])
                has_update = agent_loop_result.get('has_update', False)
                js_warnings = agent_loop_result.get('js_warnings')
                all_diagnostics = agent_loop_result.get('all_diagnostics', [])
                _agent_loop_done = True

            MAX_AGENT_ROUNDS = 50  # 安全上限，防止无限循环
            consecutive_empty = 0  # 连续空响应计数，2 次即退出
            loop_messages = list(ai_messages)
            if not _agent_loop_done:
                has_update = False
            pending_verify_fix = False  # 验证失败后阻止 AI 以文字总结退出
            consecutive_reads = 0  # 连续只读不编辑的轮数（空转检测）
            MAX_CONSECUTIVE_READS = 3  # 编辑后超过此数则强制干预
            MAX_EXPLORE_READS = 6  # 编辑前探索性读取上限（防止无限探索）
            has_ever_edited = False  # 是否已进行过至少一次编辑尝试
            verify_fail_count = 0  # 连续验证失败次数
            MAX_VERIFY_RETRIES = 3  # 验证失败最大重试次数

            # 捕获编辑前已有的标签不平衡，避免将原有问题归咎于编辑
            pre_existing_imbalances = {}
            try:
                current_html_path = os.path.join(
                    project_folder, 'index.html')
                if os.path.exists(current_html_path):
                    with open(current_html_path, 'r',
                              encoding='utf-8') as f:
                        pre_existing_imbalances = (
                            self._get_tag_imbalances(f.read()))
                    if pre_existing_imbalances:
                        logger.info(
                            f"[Agent] 原有标签不平衡: "
                            + ", ".join(
                                f"<{t}> 差{v['diff']}"
                                for t, v
                                in pre_existing_imbalances.items()))
            except Exception:
                pass

            # 捕获编辑前浏览器控制台错误基线（排除编辑前已有的错误）
            pre_existing_console_errors = []
            try:
                server_port = AI_OPTIONS.get('port', 8080)
                current_html_path = os.path.join(
                    project_folder, 'index.html')
                if os.path.exists(current_html_path):
                    pre_existing_console_errors = (
                        self._capture_browser_console_errors(
                            current_html_path, server_port))
                    if pre_existing_console_errors:
                        logger.info(
                            f"[Agent] 原有控制台错误 ({len(pre_existing_console_errors)}): "
                            + "; ".join(pre_existing_console_errors[:3]))
            except Exception:
                pass

            if not _agent_loop_done:
                edit_results = []
                tool_calls_log = []  # 记录所有工具调用（含 read_page），用于历史回放
                js_warnings = None
                all_diagnostics = []
                accumulated = ""
            injected_html_retry = False  # 是否已注入过完整 HTML 重试

            # 上下文字符上限（保守估计：模型 context window 通常 128K tokens，
            # × 2.5 字符/token ≈ 320K，留余量给 system prompt + 输出，设为 240K）
            MAX_CONTEXT_CHARS = 240000

            for agent_round in range(MAX_AGENT_ROUNDS):
                # Agent Loop 已完成则跳过旧循环
                if _agent_loop_done:
                    break

                logger.info(f"[Agent] 轮次 {agent_round}, 消息 {len(loop_messages)} 条")

                # ======== Step 0: 上下文截断 ========
                # 在每轮调用前检查总字符数，超限时截断旧的 read_page 结果
                total_chars = sum(
                    len(m.get('content', ''))
                    if isinstance(m.get('content'), str)
                    else len(str(m.get('content', '')))
                    for m in loop_messages
                )
                if total_chars > MAX_CONTEXT_CHARS:
                    logger.info(
                        f"[Agent] 上下文 {total_chars} 字符超限"
                        f"（上限 {MAX_CONTEXT_CHARS}），开始截断")
                    # 找到最后一个 read_page tool 消息的索引
                    # 当 pending_verify_fix 时保护它不被截断
                    last_read_page_idx = -1
                    for i, m in enumerate(loop_messages):
                        if (m.get('role') == 'tool'
                                and m.get('name') == 'read_page'):
                            last_read_page_idx = i
                    protect_last_read = (
                        pending_verify_fix
                        and last_read_page_idx >= 0)

                    # 策略：压缩所有非最新的 read_page tool 结果
                    new_msgs = []
                    for i, m in enumerate(loop_messages):
                        content = m.get('content', '')
                        is_protected = (
                            protect_last_read
                            and i == last_read_page_idx)
                        if (m.get('role') == 'tool'
                                and m.get('name') == 'read_page'
                                and isinstance(content, str)
                                and len(content) > 5000
                                and not is_protected):
                            # 智能摘要：保留行号骨架而非硬替换
                            page_match = re.search(
                                r'\[([^\]]+)\]', content)
                            page_name = (page_match.group(1)
                                         if page_match else 'unknown')
                            # 从原始内容提取行号骨架
                            lines_in_content = content.split('\n')
                            skeleton_lines = []
                            for cl in lines_in_content:
                                # 保留行号标记 (L数字 格式)
                                if re.match(r'\s*\d+→', cl):
                                    skeleton_lines.append(cl)
                            if len(skeleton_lines) > 30:
                                skeleton_lines = skeleton_lines[:30]
                            skeleton = '\n'.join(skeleton_lines)
                            summary = (
                                f'([{page_name}] content '
                                f'({len(content)} chars) '
                                f'compressed to skeleton.\n'
                                f'{skeleton}\n'
                                f'Use read_page(start_line, end_line) '
                                f'to read specific line ranges.)'
                            )
                            new_msgs.append({
                                **m,
                                'content': summary
                            })
                        else:
                            new_msgs.append(m)
                    loop_messages = new_msgs
                    total_chars = sum(
                        len(m.get('content', ''))
                        if isinstance(m.get('content'), str)
                        else len(str(m.get('content', '')))
                        for m in loop_messages
                    )
                    logger.info(
                        f"[Agent] 截断后上下文: {total_chars} 字符")

                    # 如果截断 read_page 后仍然超限，截断长消息
                    # 但保护最后一个 read_page（验证修复时 AI 需要精确内容）
                    if total_chars > MAX_CONTEXT_CHARS:
                        new_msgs = []
                        for i, m in enumerate(loop_messages):
                            # 不截断受保护的最后一个 read_page
                            is_protected = (
                                protect_last_read
                                and i == last_read_page_idx)
                            if is_protected:
                                new_msgs.append(m)
                                continue
                            content = m.get('content', '')
                            # 截断 assistant 的长文本
                            if (m.get('role') == 'assistant'
                                    and isinstance(content, str)
                                    and len(content) > 3000):
                                new_msgs.append({
                                    **m,
                                    'content': content[:2000]
                                              + '\n...(内容已截断)'
                                })
                            # 截断 user 消息中的超长 HTML（注入重试场景）
                            elif (m.get('role') == 'user'
                                  and isinstance(content, str)
                                  and len(content) > 10000):
                                # 提取页面名
                                pm = re.search(
                                    r'页面\s*\[([^\]]+)\]', content)
                                pn = pm.group(1) if pm else '页面'
                                new_msgs.append({
                                    **m,
                                    'content': (
                                        f'(页面 [{pn}] 的完整 HTML'
                                        f'（{len(content)} 字符）已省略。'
                                        f'请使用 read_page 工具重新读取。)'
                                    )
                                })
                            else:
                                new_msgs.append(m)
                        loop_messages = new_msgs
                        total_chars = sum(
                            len(m.get('content', ''))
                            if isinstance(m.get('content'), str)
                            else len(str(m.get('content', '')))
                            for m in loop_messages
                        )
                        logger.info(
                            f"[Agent] 二次截断后: {total_chars} 字符")

                # ======== Step 1: 流式调用 AI ========
                accumulated = ""
                tool_calls_result = None
                seen_tool_calls = set()

                gen = self.call_ai_model_streaming(
                    loop_messages, [], tools=edit_tools)
                accumulated_reasoning = ""  # 累积 reasoning 内容
                try:
                    for stream_item in gen:
                        # 解包流式响应：(chunk_text, full_content, done, tc[, reasoning])
                        chunk_text = stream_item[0] if len(stream_item) > 0 else ''
                        full_content = stream_item[1] if len(stream_item) > 1 else ''
                        done = stream_item[2] if len(stream_item) > 2 else False
                        tc = stream_item[3] if len(stream_item) > 3 else None
                        reasoning = stream_item[4] if len(stream_item) > 4 else ''

                        accumulated = full_content
                        if reasoning:
                            accumulated_reasoning += reasoning
                        if chunk_text:
                            # 直接流式发送 chat 文本
                            self._send_sse_data(json.dumps({
                                'type': 'chat',
                                'data': {'role': 'assistant',
                                         'content': chunk_text}
                            }, ensure_ascii=False))

                        if reasoning:
                            # 流式发送推理/思考内容
                            self._send_sse_data(json.dumps({
                                'type': 'chat',
                                'data': {'role': 'assistant',
                                         'reasoning': reasoning}
                            }, ensure_ascii=False))

                        # 实时检测 tool call 进度
                        if tc and isinstance(tc, dict):
                            for key, tc_data in tc.items():
                                name = tc_data.get('name', '')
                                if key not in seen_tool_calls:
                                    if name == 'read_page':
                                        self._send_sse_data(json.dumps({
                                            'type': 'tool_call_progress',
                                            'data': {
                                                'tool_call_id': f'r{agent_round}-' + key,
                                                'status': 'running', 'page': '',
                                                'tool_name': 'read_page'
                                            }
                                        }, ensure_ascii=False))
                                        seen_tool_calls.add(key)
                                    elif name == 'edit_file':
                                        self._send_sse_data(json.dumps({
                                            'type': 'tool_call_progress',
                                            'data': {
                                                'tool_call_id': f'e{agent_round}-' + key,
                                                'status': 'running', 'page': '',
                                                'old_string': '',
                                                'replace_all': False
                                            }
                                        }, ensure_ascii=False))
                                        seen_tool_calls.add(key)
                        if done:
                            tool_calls_result = tc
                            break
                finally:
                    try:
                        gen.close()
                    except RuntimeError:
                        pass

                # ======== Step 2: 无 tool_calls → 纯文本响应 ========
                if not tool_calls_result:
                    html_result = self.extract_html(
                        accumulated, fallback_error_page=False)
                    text_edits = self.parse_edits(accumulated)

                    if html_result and len(html_result) > 100:
                        # 完整 HTML 响应 → 直接应用
                        has_update = True
                        if target_page and target_page in session.pages_html:
                            session.update_page(target_page, html_result)
                        # 多页项目：更新指定页面后重新组装以确保转义正确
                        _multi_file_saved = False
                        if (target_page and target_page in session.pages_html
                                and len(session.page_order) > 1):
                            from server_context_engineering import (
                                assemble_multi_page_html)
                            page_fragments = [
                                session.pages_html[n]
                                for n in session.page_order
                                if n in session.pages_html
                            ]
                            page_names = [
                                n for n in session.page_order
                                if n in session.pages_html
                            ]
                            if page_fragments:
                                session.generated_html = (
                                    assemble_multi_page_html(
                                        page_fragments=page_fragments,
                                        design_system_css=(
                                            session.design_system),
                                        page_names=page_names,
                                        global_config=(
                                            session.global_config),
                                        project_dir=project_folder,
                                        output_format='dual',
                                        cross_page_spec=getattr(
                                            session, 'cross_page_spec', None)
                                    )
                                )
                                # output_format='dual' + project_dir 时，
                                # save_multi_file_output 已在内部保存了
                                # 正确的多文件 index.html（iframe 导航）
                                _multi_file_saved = True
                            else:
                                session.generated_html = html_result
                        else:
                            session.generated_html = html_result
                        # 多文件已保存时不再重复写入 index.html
                        if not _multi_file_saved:
                            html_path = os.path.join(project_folder, 'index.html')
                            bak_path = html_path + '.bak'
                            if os.path.exists(html_path):
                                try:
                                    import shutil
                                    shutil.copy2(html_path, bak_path)
                                except Exception:
                                    pass
                            # 合并 head（如果有拆分）
                            html_to_write = html_result
                            # srcdoc 项目：重组内部内容与外框架
                            if session.srcdoc_frame_html:
                                html_to_write = self.assemble_iframe_html(
                                    html_result, session.srcdoc_frame_html)
                            if getattr(session, '_chat_head_html', ''):
                                html_to_write = _merge_head_body(
                                    session._chat_head_html, html_to_write)
                            with open(html_path, 'w', encoding='utf-8') as f:
                                f.write(html_to_write)
                            # 多文件项目：同步保存各页面到 pages/ 目录
                            if getattr(session, '_is_multi_file', False) and session.pages_html:
                                mf_pages_dir = os.path.join(project_folder, 'pages')
                                if os.path.isdir(mf_pages_dir):
                                    for pn, ph in session.pages_html.items():
                                        for pf in os.listdir(mf_pages_dir):
                                            if pf.endswith(f'_{pn}.html'):
                                                with open(os.path.join(mf_pages_dir, pf), 'w', encoding='utf-8') as pf_f:
                                                    pf_f.write(ph)
                                                break
                            # 同步到对话目录
                            if session.conversation_id:
                                from server_conversations import (
                                    get_conversation_dir)
                                conv_dir = get_conversation_dir(
                                    project_folder,
                                    session.conversation_id)
                                with open(os.path.join(
                                        conv_dir, 'index.html'), 'w',
                                        encoding='utf-8') as f:
                                    f.write(html_to_write)
                        session.save(save_path=session_path)
                        # 页面审查 + 自动修复
                        try:
                            def _push_chat_review(data):
                                self._send_sse_data(json.dumps({
                                    'type': 'code_review',
                                    'data': data
                                }, ensure_ascii=False))
                            reviewed_html, chat_review_edits, _ = self._run_code_review(
                                html_to_write, target_page or 'all',
                                getattr(session, 'project_id', ''),
                                project_folder,
                                push_event_fn=_push_chat_review
                            )
                            if chat_review_edits > 0:
                                html_to_write = reviewed_html
                                session.generated_html = reviewed_html
                                with open(html_path, 'w', encoding='utf-8') as rf:
                                    rf.write(html_to_write)
                                logger.info(f"[Chat] 编辑后审查修复 {chat_review_edits} 处")
                        except Exception as review_ex:
                            logger.warning(f"[Chat] 编辑后审查异常（不影响结果）: {review_ex}")
                        # 编辑后基础验证
                        verify_ok = self._quick_verify_html(
                            html_to_write,
                            pre_existing=pre_existing_imbalances)
                        edit_results.append({
                            'applied': True,
                            'page': target_page or 'all',
                            'search_snippet': '完整页面重生成',
                            'error': (None if verify_ok is True
                                      else str(verify_ok)),
                            'old_text': session.generated_html or '',
                            'new_text': html_result
                        })
                        self._send_sse_data(json.dumps({
                            'type': 'edit_result',
                            'data': {
                                'applied': 1, 'failed': 0,
                                'edit_results': edit_results,
                                'mode': 'full_html'
                            }
                        }, ensure_ascii=False))
                        self._send_sse_data(json.dumps({
                            'type': 'preview_update',
                            'data': {'page': target_page or 'all',
                                     'mode': 'full_html'}
                        }, ensure_ascii=False))
                        break

                    elif text_edits and session.pages_html:
                        # 文本解析的编辑 → 应用
                        updated_pages, results = self.apply_edits(
                            session.pages_html, text_edits)
                        applied_count = sum(1 for r in results if r['applied'])
                        edit_results.extend(results)

                        if applied_count > 0:
                            has_update = True
                            session.pages_html = updated_pages
                            if len(session.page_order) <= 1:
                                pn = (session.page_order[0]
                                      if session.page_order
                                      else list(updated_pages.keys())[0])
                                session.generated_html = updated_pages[pn]
                            else:
                                # 多页项目：重新组装以确保 pageData 转义正确
                                from server_context_engineering import (
                                    assemble_multi_page_html)
                                page_fragments = [
                                    session.pages_html[n]
                                    for n in session.page_order
                                    if n in session.pages_html
                                ]
                                page_names = [
                                    n for n in session.page_order
                                    if n in session.pages_html
                                ]
                                if page_fragments:
                                    session.generated_html = (
                                        assemble_multi_page_html(
                                            page_fragments=page_fragments,
                                            design_system_css=(
                                                session.design_system),
                                            page_names=page_names,
                                            global_config=(
                                                session.global_config),
                                            project_dir=project_folder,
                                            output_format='dual',
                                            cross_page_spec=getattr(
                                                session, 'cross_page_spec', None)
                                        )
                                    )
                                    # output_format='dual' + project_dir 时，
                                    # save_multi_file_output 已保存了正确的
                                    # 多文件 index.html，不再重复写入
                            # srcdoc 项目：重组内部内容与外框架
                            html_to_write = session.generated_html
                            if session.srcdoc_frame_html:
                                inner = session.generated_html
                                html_to_write = self.assemble_iframe_html(
                                    inner, session.srcdoc_frame_html)
                                logger.info(
                                    f"[Agent] srcdoc 重组: "
                                    f"内部 {len(inner)} + 框架 "
                                    f"{len(session.srcdoc_frame_html)} "
                                    f"→ {len(html_to_write)} 字符")
                            # 侧边栏项目：合并 head + body
                            if getattr(session, '_chat_head_html', ''):
                                html_to_write = _merge_head_body(
                                    session._chat_head_html,
                                    html_to_write)
                            html_path = os.path.join(
                                project_folder, 'index.html')
                            with open(html_path, 'w',
                                      encoding='utf-8') as f:
                                f.write(html_to_write)
                            # 多文件项目：同步保存各页面到 pages/ 目录
                            if getattr(session, '_is_multi_file', False) and session.pages_html:
                                pages_dir = os.path.join(project_folder, 'pages')
                                if os.path.isdir(pages_dir):
                                    for page_name, page_html in session.pages_html.items():
                                        # 查找对应的页面文件
                                        for pf in os.listdir(pages_dir):
                                            if pf.endswith(f'_{page_name}.html'):
                                                pf_path = os.path.join(pages_dir, pf)
                                                with open(pf_path, 'w', encoding='utf-8') as pf_f:
                                                    pf_f.write(page_html)
                                                break
                            # 同步到对话目录
                            if session.conversation_id:
                                from server_conversations import (
                                    get_conversation_dir)
                                conv_dir = get_conversation_dir(
                                    project_folder,
                                    session.conversation_id)
                                with open(os.path.join(
                                        conv_dir, 'index.html'), 'w',
                                        encoding='utf-8') as f:
                                    f.write(html_to_write)
                            session.save(save_path=session_path)
                            # 页面审查 + 自动修复
                            try:
                                def _push_text_review(data):
                                    self._send_sse_data(json.dumps({
                                        'type': 'code_review',
                                        'data': data
                                    }, ensure_ascii=False))
                                reviewed_text_html, text_review_edits, _ = self._run_code_review(
                                    html_to_write, target_page or 'all',
                                    getattr(session, 'project_id', ''),
                                    project_folder,
                                    push_event_fn=_push_text_review
                                )
                                if text_review_edits > 0:
                                    html_to_write = reviewed_text_html
                                    with open(html_path, 'w', encoding='utf-8') as rf:
                                        rf.write(html_to_write)
                                    logger.info(f"[Chat] text_edits 审查修复 {text_review_edits} 处")
                            except Exception as review_ex:
                                logger.warning(f"[Chat] text_edits 审查异常（不影响结果）: {review_ex}")
                            # 基础验证（日志记录）
                            v = self._quick_verify_html(
                                html_to_write,
                                pre_existing=pre_existing_imbalances)
                            if v is not True:
                                logger.warning(
                                    f"[Agent] text_edits 验证: {v}")
                        break

                    else:
                        # 无 HTML、无编辑 — 可能是 AI 总结性回复
                        if has_update and not pending_verify_fix:
                            # 之前已有成功编辑，这是 AI 的完成总结
                            logger.info(
                                f"[Agent] AI 总结回复（已有编辑成功），"
                                f"结束循环")
                            break

                        # 无有效内容且之前无编辑
                        # 检查是否上下文溢出（模型返回空响应因为 token 超限）
                        est_context_tokens = int(total_chars * 0.4)
                        if not accumulated and est_context_tokens > 80000:
                            # 上下文已超过 ~80K tokens，模型无法处理
                            logger.warning(
                                f"[Agent] 上下文溢出退出: "
                                f"约 {est_context_tokens} tokens "
                                f"({total_chars} 字符)，"
                                f"模型无法响应")
                            self._send_sse_data(json.dumps({
                                'type': 'chat',
                                'data': {
                                    'role': 'system',
                                    'content': (
                                        '对话上下文过长，AI 无法继续处理。'
                                        '请开启新对话或切换模型。'
                                    )
                                }
                            }, ensure_ascii=False))
                            break

                        if (not injected_html_retry
                                and (session.pages_html
                                     or session.generated_html)):
                            # 注入完整 HTML 重试
                            pages = session.pages_html or {}
                            if not pages and session.generated_html:
                                pages = {'主页面': session.generated_html}
                            first_page_name = list(pages.keys())[0]
                            first_page_html = pages[first_page_name]

                            logger.info(f"[Agent] 注入完整 HTML "
                                        f"({len(first_page_html)} 字符) 重试")
                            self._send_sse_data(json.dumps({
                                'type': 'chat',
                                'data': {
                                    'role': 'system',
                                    'content': '正在重新加载页面代码...'
                                }
                            }, ensure_ascii=False))

                            loop_messages.append({
                                "role": "assistant",
                                "content": accumulated[:300]
                                if accumulated else "[重新加载页面...]"
                            })
                            loop_messages.append({
                                "role": "user",
                                "content": (
                                    f"以下是页面 [{first_page_name}] "
                                    f"的完整 HTML 代码：\n"
                                    f"```\n{first_page_html}\n```\n\n"
                                    f"请根据之前的用户需求，"
                                    f"使用 edit_file 工具进行修改。"
                                )
                            })
                            injected_html_retry = True
                            continue

                        logger.warning(f"[Agent] 轮次 {agent_round}: "
                                       f"无法提取有效内容 "
                                       f"(长度={len(accumulated)})")
                        consecutive_empty += 1
                        if consecutive_empty >= 2:
                            break  # 连续 2 次空响应，退出
                        # 还有重试机会，跳过 Step 3 继续下一轮
                        continue

                # ======== Step 3: 处理 tool_calls ========
                consecutive_empty = 0  # AI 有动作，重置空响应计数
                # 构建 assistant 消息（含 tool_calls）
                assistant_tc_list = [
                    {
                        "id": tc['id'],
                        "type": "function",
                        "function": {
                            "name": tc['name'],
                            "arguments": json.dumps(
                                tc['arguments'], ensure_ascii=False)
                        }
                    }
                    for tc in tool_calls_result
                ]
                loop_messages.append({
                    "role": "assistant",
                    "content": (accumulated[:500] if accumulated else None),
                    "tool_calls": assistant_tc_list
                })

                round_has_edit = False

                for tc_idx, tc in enumerate(tool_calls_result):
                    tc_id = tc.get('id', f'tc-{tc_idx}')

                    # ---- list_pages ----
                    if tc['name'] == 'list_pages':
                        pages = session.pages_html or {}
                        if not pages and session.generated_html:
                            pages = {'主页面': session.generated_html}

                        list_parts = [
                            f"项目共 {len(pages)} 个页面：\n"
                        ]
                        for pname, phtml in pages.items():
                            lines = phtml.split('\n')
                            total_lines = len(lines)
                            total_chars = len(phtml)
                            # 提取关键标签概要
                            key_tags = []
                            for line in lines:
                                s = line.strip()
                                if (re.match(
                                    r'<\w+[^>]*(id|class)\s*=',
                                    s
                                )):
                                    m = re.match(
                                        r'(<\w+[^>]*(?:id|class)'
                                        r'\s*=\s*["\'][^"\']*["\'])',
                                        s)
                                    if m:
                                        key_tags.append(
                                            m.group(1)[:80])
                                    if len(key_tags) >= 8:
                                        break
                            # Vue 变量
                            vue_vars = re.findall(
                                r'(?:const|let|var)\s+(\w+)'
                                r'\s*=\s*(?:ref\(|reactive\()',
                                phtml)
                            detail = (
                                f"- {pname}: "
                                f"{total_lines} 行, "
                                f"{total_chars:,} 字符"
                            )
                            if vue_vars:
                                detail += (
                                    f"\n  Vue变量: "
                                    f"{', '.join(vue_vars[:15])}"
                                )
                            if key_tags:
                                detail += (
                                    "\n  关键元素: "
                                    + "; ".join(key_tags[:5])
                                )
                            list_parts.append(detail)

                        content = '\n'.join(list_parts)
                        logger.info(
                            f"[Agent] list_pages: "
                            f"返回 {len(pages)} 个页面概要")
                        loop_messages.append({
                            'role': 'tool',
                            'tool_call_id': tc_id,
                            'name': 'list_pages',
                            'content': content
                        })
                        self._send_sse_data(json.dumps({
                            'type': 'tool_call_progress',
                            'data': {
                                'tool_call_id': (
                                    f'l{agent_round}-{tc_idx}'),
                                'status': 'applied',
                                'tool_name': 'list_pages',
                                'page_count': len(pages)
                            }
                        }, ensure_ascii=False))
                        tool_calls_log.append({
                            'tool': 'list_pages',
                            'page_count': len(pages),
                            'status': 'applied'
                        })
                        continue

                    # ---- read_page ----
                    if tc['name'] == 'read_page':
                        args = tc['arguments']
                        req_page = args.get('page', '')
                        start_line = args.get('start_line')
                        end_line = args.get('end_line')

                        pages = session.pages_html or {}
                        if not pages and session.generated_html:
                            pages = {'主页面': session.generated_html}

                        # 特殊处理: "index.html" 是组装页面，不能直接读取
                        # 引导 AI 读取正确的子页面
                        if req_page.lower().endswith('index.html') \
                                and len(session.page_order) > 1:
                            # 根据错误行号推断属于哪个子页面
                            guess_page = self._guess_sub_page(
                                session, start_line)
                            guide = (
                                f"'index.html' is an assembled file "
                                f"({len(session.generated_html):,} chars). "
                                f"Reading it directly would exceed the context limit.\n\n"
                                f"Use one of these page names instead:\n"
                            )
                            for pn in session.page_order:
                                if pn in session.pages_html:
                                    p_len = len(session.pages_html[pn])
                                    guide += (
                                        f"- read_page(page=\"{pn}\") "
                                        f"({p_len:,} chars)\n"
                                    )
                            if guess_page:
                                guide += (
                                    f"\nHint: based on the line number, "
                                    f"the target code is likely in page "
                                    f"\"{guess_page}\"."
                                )
                            content = guide
                            logger.info(
                                f"[Agent] read_page index.html: "
                                f"引导读取子页面")
                            loop_messages.append({
                                'role': 'tool',
                                'tool_call_id': tc_id,
                                'name': 'read_page',
                                'content': content
                            })
                            tool_calls_log.append({
                                'tool': 'read_page',
                                'page': 'index.html → 引导',
                                'status': 'redirected',
                                'content_length': len(content)
                            })
                            continue

                        target_html = None
                        target_name = req_page
                        # 精确匹配：先尝试完全相等
                        if req_page in pages:
                            target_html = pages[req_page]
                            target_name = req_page
                        else:
                            # 不做模糊匹配。返回可用页面列表让 AI 修正
                            available = list(pages.keys())
                            content = (
                                f"Error: page '{req_page}' not found. "
                                f"Available pages: {available}. "
                                f"Please use the exact page name."
                            )

                        if target_html:
                            # read_page 返回内容的字符上限
                            # 约 60K tokens，配合 MAX_CONTEXT_CHARS=240K
                            # 确保有足够空间给其他消息和输出
                            MAX_READ_CHARS = 150000

                            if start_line and end_line:
                                lines = target_html.split('\n')
                                s = max(0, start_line - 1)
                                e = min(len(lines), end_line)
                                content_lines = lines[s:e]
                                # 格式化为 cat -n 风格的行号格式
                                # "  行号→内容"，AI 知道箭头后面才是实际内容
                                numbered_lines = []
                                for i, line in enumerate(content_lines):
                                    line_no = start_line + i
                                    numbered_lines.append(
                                        f'{line_no:>6}→{line}'
                                    )
                                content_text = '\n'.join(numbered_lines)
                                content = (
                                    f"[{target_name}] "
                                    f"lines {start_line}-{end_line} "
                                    f"({e - s + 1} lines):\n"
                                    f"{content_text}"
                                )
                            else:
                                # 无行号范围：返回完整页面
                                all_lines = target_html.split('\n')
                                total_lines_count = len(all_lines)

                                if len(target_html) <= MAX_READ_CHARS:
                                    # 页面足够小，直接返回全部（带行号）
                                    numbered_all = []
                                    for i, line in enumerate(all_lines):
                                        numbered_all.append(
                                            f'{i + 1:>6}→{line}'
                                        )
                                    content = (
                                        f"[{target_name}] "
                                        f"full content "
                                        f"({total_lines_count} lines, "
                                        f"{len(target_html)} chars):\n"
                                        + '\n'.join(numbered_all)
                                    )
                                else:
                                    # 超出上限：按行收集到预算用完
                                    result_lines = []
                                    used = 0
                                    for i, line in enumerate(
                                            all_lines):
                                        if used + len(line) + 1 > MAX_READ_CHARS:
                                            result_lines.append(
                                                f"\n[truncated] page has "
                                                f"{total_lines_count} lines, "
                                                f"returned first {i}. "
                                                f"Use start_line={i+1}"
                                                f" to continue reading."
                                            )
                                            break
                                        result_lines.append(line)
                                        used += len(line) + 1

                                    # 带行号的截断内容
                                    numbered_result = []
                                    for i, line in enumerate(result_lines):
                                        if line.startswith('[truncated]'):
                                            numbered_result.append(line)
                                        else:
                                            numbered_result.append(
                                                f'{i + 1:>6}→{line}'
                                            )
                                    content = (
                                        f"[{target_name}] "
                                        f"full content "
                                        f"({total_lines_count} lines, "
                                        f"{len(target_html)} chars):\n"
                                        + '\n'.join(numbered_result)
                                    )

                            logger.info(
                                f"[Agent] read_page: {target_name}"
                                f"{f' L{start_line}-{end_line}' if start_line else ''}"
                                f" -> {len(content)} chars")
                        # content already set above (either page data or error)

                        # SSE 状态 + tool 结果
                        stream_key = str(tc_idx)
                        if target_html:
                            _total_lines = len(
                                target_html.split('\n'))
                            self._send_sse_data(json.dumps({
                                'type': 'tool_call_progress',
                                'data': {
                                    'tool_call_id': (
                                        f'r{agent_round}-'
                                        + stream_key),
                                    'status': 'applied',
                                    'page': req_page or target_name,
                                    'tool_name': 'read_page',
                                    'line_start': start_line,
                                    'line_end': end_line,
                                    'total_lines': _total_lines,
                                    'content_length': len(content),
                                }
                            }, ensure_ascii=False))

                            # 记录到工具调用日志（用于历史回放）
                            tool_calls_log.append({
                                'tool': 'read_page',
                                'page': req_page or target_name,
                                'line_start': start_line,
                                'line_end': end_line,
                                'total_lines': _total_lines,
                                'status': 'applied',
                                'content_length': len(content)
                            })
                        else:
                            self._send_sse_data(json.dumps({
                                'type': 'tool_call_progress',
                                'data': {
                                    'tool_call_id': (
                                        f'r{agent_round}-'
                                        + stream_key),
                                    'status': 'failed',
                                    'page': req_page,
                                    'tool_name': 'read_page',
                                    'error': (
                                        f"page '{req_page}' "
                                        f"not found"),
                                }
                            }, ensure_ascii=False))

                        # tool 结果
                        loop_messages.append({
                            'role': 'tool',
                            'tool_call_id': tc_id,
                            'name': 'read_page',
                            'content': content
                        })

                    # ---- edit_file ----
                    elif tc['name'] == 'edit_file':
                        args = tc['arguments']
                        page_name = args.get('page', '')
                        old_string = args.get('old_string', '')
                        new_string = args.get('new_string', '')
                        replace_all = args.get('replace_all', False)
                        tc_id_sse = f'e{agent_round}-' + str(tc_idx)

                        # 容错：AI 未传 page 时自动选择目标页或首个页面
                        if not page_name:
                            pages = session.pages_html or {}
                            if len(pages) == 1:
                                page_name = next(iter(pages))
                            elif target_page and target_page in pages:
                                page_name = target_page
                            elif session.page_order:
                                page_name = session.page_order[0]

                        # SSE: running
                        self._send_sse_data(json.dumps({
                            'type': 'tool_call_progress',
                            'data': {
                                'tool_call_id': tc_id_sse,
                                'status': 'running',
                                'tool_name': 'edit_file',
                                'page': page_name,
                                'old_string': (old_string[:100]
                                               if old_string else ''),
                                'replace_all': bool(replace_all)
                            }
                        }, ensure_ascii=False))

                        # 查找页面
                        pages = session.pages_html or {}
                        if not pages and session.generated_html:
                            pages = {'主页面': session.generated_html}

                        # 特殊处理: "index.html" 是组装页面，不能直接编辑
                        # 引导 AI 编辑正确的子页面
                        if page_name.lower().endswith('index.html') \
                                and len(session.page_order) > 1:
                            guide = (
                                f"'index.html' is an assembled file. "
                                f"Edit the specific sub-pages instead:\n"
                            )
                            for pn in session.page_order:
                                if pn in session.pages_html:
                                    guide += (
                                        f"- edit_file(page=\"{pn}\", ...)\n"
                                    )
                            edit_results.append({
                                'applied': False,
                                'page': page_name,
                                'search_snippet': '',
                                'error': 'index.html is assembled, edit sub-pages'
                            })
                            loop_messages.append({
                                'role': 'tool',
                                'tool_call_id': tc_id,
                                'name': 'edit_file',
                                'content': guide
                            })
                            self._send_sse_data(json.dumps({
                                'type': 'tool_call_progress',
                                'data': {
                                    'tool_call_id': tc_id_sse,
                                    'status': 'failed',
                                    'page': page_name,
                                    'error': 'edit sub-pages instead of index.html'
                                }
                            }, ensure_ascii=False))
                            continue

                        current_html = pages.get(page_name)
                        if not current_html:
                            # 精确匹配失败，返回可用页面列表
                            available = list(pages.keys())

                        if not current_html:
                            edit_results.append({
                                'applied': False,
                                'page': page_name,
                                'search_snippet': '',
                                'error': '页面不存在'
                            })
                            loop_messages.append({
                                'role': 'tool',
                                'tool_call_id': tc_id,
                                'name': 'edit_file',
                                'content': (
                                    f"Error: page '{page_name}' not found. "
                                    f"Available pages: {available}. "
                                    f"Use the exact page name."
                                )
                            })
                            self._send_sse_data(json.dumps({
                                'type': 'tool_call_progress',
                                'data': {
                                    'tool_call_id': tc_id_sse,
                                    'status': 'failed',
                                    'page': page_name,
                                    'error': '页面不存在'
                                }
                            }, ensure_ascii=False))
                            continue

                        # 辅助函数：计算编辑在页面中的行号范围
                        def _edit_line_range(html, text):
                            if not html or not text:
                                return None, None
                            idx = html.find(text)
                            if idx == -1:
                                return None, None
                            start = html[:idx].count('\n') + 1
                            end = start + text.count('\n')
                            return start, end

                        # 应用编辑
                        applied = False

                        if old_string:
                            # 搜索替换：精确匹配
                            idx = current_html.find(old_string)
                            if idx != -1:
                                second_idx = current_html.find(
                                    old_string, idx + 1)
                                if second_idx != -1 and not replace_all:
                                    edit_results.append({
                                        'applied': False,
                                        'page': page_name,
                                        'search_snippet': old_string[:80],
                                        'error': 'multiple_matches'
                                    })
                                    loop_messages.append({
                                        'role': 'tool',
                                        'tool_call_id': tc_id,
                                        'name': 'edit_file',
                                        'content': (
                                            "Error: old_string matches "
                                            "multiple locations in the page. "
                                            "Either provide more context to "
                                            "make it unique, or set "
                                            "replace_all=true to replace all "
                                            "occurrences."
                                        )
                                    })
                                    self._send_sse_data(json.dumps({
                                        'type': 'tool_call_progress',
                                        'data': {
                                            'tool_call_id': tc_id_sse,
                                            'status': 'failed',
                                            'tool_name': 'edit_file',
                                            'page': page_name,
                                            'error': 'multiple matches, need more context or replace_all=true'
                                        }
                                    }, ensure_ascii=False))
                                    continue

                                if replace_all:
                                    # Replace all occurrences
                                    session.pages_html[page_name] = (
                                        current_html.replace(
                                            old_string, new_string)
                                    )
                                else:
                                    # Replace first occurrence only
                                    session.pages_html[page_name] = (
                                        current_html[:idx] + new_string
                                        + current_html[idx + len(old_string):]
                                    )
                                edit_results.append({
                                    'applied': True,
                                    'page': page_name,
                                    'search_snippet': old_string[:80],
                                    'error': None,
                                    'old_text': old_string,
                                    'new_text': new_string
                                })
                                applied = True
                                old_snip, new_snip = self._diff_snippet(
                                    old_string, new_string)
                                old_full, new_full = self._diff_text(
                                    old_string, new_string)
                                _ls, _le = (
                                    _edit_line_range(
                                        current_html, old_string)
                                    if not replace_all
                                    else (None, None))
                                _sse_data = {
                                        'tool_call_id': tc_id_sse,
                                        'status': 'applied',
                                        'tool_name': 'edit_file',
                                        'page': page_name,
                                        'old_snippet': old_snip,
                                        'new_snippet': new_snip,
                                        'old_text': old_full,
                                        'new_text': new_full,
                                    }
                                if _ls is not None:
                                    _sse_data['line_start'] = _ls
                                    _sse_data['line_end'] = _le
                                self._send_sse_data(json.dumps({
                                    'type': 'tool_call_progress',
                                    'data': _sse_data
                                }, ensure_ascii=False))

                            else:
                                # 归一化匹配
                                norm_search = _norm_re.sub(
                                    ' ', old_string)
                                norm_html = _norm_re.sub(
                                    ' ', current_html)
                                norm_idx = norm_html.find(norm_search)
                                if norm_idx != -1:
                                    # 映射归一化位置到原始位置
                                    abs_start = self._norm_to_orig(
                                        current_html, norm_idx)
                                    abs_end = self._norm_to_orig(
                                        current_html,
                                        norm_idx + len(norm_search))
                                    if (abs_start is not None
                                            and abs_end is not None):
                                        session.pages_html[page_name] = (
                                            current_html[:abs_start]
                                            + new_string
                                            + current_html[abs_end:]
                                        )
                                        edit_results.append({
                                            'applied': True,
                                            'page': page_name,
                                            'search_snippet':
                                                old_string[:80],
                                            'error': None,
                                            'old_text': old_string,
                                            'new_text': new_string
                                        })
                                        applied = True
                                        old_snip, new_snip = (
                                            self._diff_snippet(
                                                old_string, new_string))
                                        old_full, new_full = (
                                            self._diff_text(
                                                old_string, new_string))
                                        self._send_sse_data(
                                            json.dumps({
                                                'type': 'tool_call_progress',
                                                'data': {
                                                    'tool_call_id': tc_id_sse,
                                                    'status': 'applied',
                                                    'tool_name': 'edit_file',
                                                    'page': page_name,
                                                    'old_snippet': old_snip,
                                                    'new_snippet': new_snip,
                                                    'old_text': old_full,
                                                    'new_text': new_full,
                                                    'line_start': current_html[:abs_start].count('\n') + 1,
                                                    'line_end': current_html[:abs_end].count('\n') + 1,
                                                }
                                            }, ensure_ascii=False))

                                # 第三层：逐行 strip 后匹配
                                # 处理 AI 复制时多了/少了缩进的情况
                                if not applied and old_string.strip():
                                    old_lines = old_string.split('\n')
                                    html_lines = current_html.split('\n')
                                    stripped_old = [
                                        l.strip() for l in old_lines
                                    ]
                                    stripped_html = [
                                        l.strip() for l in html_lines
                                    ]
                                    # 找连续匹配的起始位置
                                    match_start = -1
                                    for i in range(
                                        len(stripped_html)
                                        - len(stripped_old) + 1
                                    ):
                                        if (stripped_html[i:i + len(stripped_old)]
                                                == stripped_old):
                                            # 检查是否唯一匹配
                                            next_match = -1
                                            for j in range(
                                                i + 1,
                                                len(stripped_html)
                                                - len(stripped_old) + 1
                                            ):
                                                if (stripped_html[j:j + len(stripped_old)]
                                                        == stripped_old):
                                                    next_match = j
                                                    break
                                            if next_match == -1:
                                                match_start = i
                                            break

                                    if match_start != -1:
                                        # 用页面中实际的内容做替换
                                        match_end = (
                                            match_start + len(old_lines))
                                        actual_old = '\n'.join(
                                            html_lines[match_start:match_end])
                                        new_html = (
                                            '\n'.join(html_lines[:match_start])
                                            + '\n' + new_string
                                            + '\n' + '\n'.join(
                                                html_lines[match_end:])
                                        )
                                        session.pages_html[page_name] = (
                                            new_html)
                                        edit_results.append({
                                            'applied': True,
                                            'page': page_name,
                                            'search_snippet':
                                                actual_old[:80],
                                            'error': None,
                                            'old_text': actual_old,
                                            'new_text': new_string
                                        })
                                        applied = True
                                        old_snip, new_snip = (
                                            self._diff_snippet(
                                                actual_old, new_string))
                                        old_full, new_full = (
                                            self._diff_text(
                                                actual_old, new_string))
                                        self._send_sse_data(
                                            json.dumps({
                                                'type': 'tool_call_progress',
                                                'data': {
                                                    'tool_call_id': tc_id_sse,
                                                    'status': 'applied',
                                                    'tool_name': 'edit_file',
                                                    'page': page_name,
                                                    'old_snippet': old_snip,
                                                    'new_snippet': new_snip,
                                                    'old_text': old_full,
                                                    'new_text': new_full,
                                                    'line_start': match_start + 1,
                                                    'line_end': match_end,
                                                }
                                            }, ensure_ascii=False))

                                if not applied:
                                    diag_parts = [
                                        f"Error: old_string not found "
                                        f"in page [{page_name}]. "
                                    ]
                                    old_lines = old_string.strip().split('\n')
                                    if old_lines:
                                        first_line = old_lines[0].strip()
                                        last_line = old_lines[-1].strip()
                                        first_found = (
                                            first_line in current_html)
                                        last_found = (
                                            last_line in current_html)
                                        if first_found and last_found:
                                            diag_parts.append(
                                                "First and last lines exist "
                                                "but middle content does not "
                                                "match (whitespace/indent "
                                                "difference likely)."
                                            )
                                        elif first_found:
                                            diag_parts.append(
                                                f"First line '{first_line[:60]}'"
                                                f" found but overall match failed."
                                            )
                                        elif last_found:
                                            diag_parts.append(
                                                f"Last line '{last_line[:60]}'"
                                                f" found but overall match failed."
                                            )
                                        else:
                                            diag_parts.append(
                                                f"First line '{first_line[:60]}'"
                                                f" not found. Content may have"
                                                f" been modified by a previous"
                                                f" edit. Re-read with read_page."
                                            )
                                    diag_parts.append(
                                        " Re-read the page with read_page"
                                        " to get current content, then retry."
                                    )
                                    diag_msg = ''.join(diag_parts)

                                    edit_results.append({
                                        'applied': False,
                                        'page': page_name,
                                        'search_snippet': old_string[:80],
                                        'error': 'not_found'
                                    })
                                    loop_messages.append({
                                        'role': 'tool',
                                        'tool_call_id': tc_id,
                                        'name': 'edit_file',
                                        'content': diag_msg
                                    })
                                    self._send_sse_data(json.dumps({
                                        'type': 'tool_call_progress',
                                        'data': {
                                            'tool_call_id': tc_id_sse,
                                            'status': 'failed',
                                            'tool_name': 'edit_file',
                                            'page': page_name,
                                            'old_string': old_string[:100],
                                            'error': 'not found'
                                        }
                                    }, ensure_ascii=False))
                                    continue

                        if applied:
                            round_has_edit = True
                            has_ever_edited = True
                            loop_messages.append({
                                'role': 'tool',
                                'tool_call_id': tc_id,
                                'name': 'edit_file',
                                'content': (
                                    f"The page [{page_name}] "
                                    f"has been updated."
                                )
                            })

                    # ---- write_page ----
                    elif tc['name'] == 'write_page':
                        args = tc['arguments']
                        page_name = args.get('page', '')
                        new_html = args.get('html', '')
                        tc_id_sse = f'w{agent_round}-' + str(tc_idx)

                        # 容错：AI 未传 page 时自动选择目标页或首个页面
                        if not page_name:
                            pages = session.pages_html or {}
                            if len(pages) == 1:
                                page_name = next(iter(pages))
                            elif target_page and target_page in pages:
                                page_name = target_page
                            elif session.page_order:
                                page_name = session.page_order[0]

                        self._send_sse_data(json.dumps({
                            'type': 'tool_call_progress',
                            'data': {
                                'tool_call_id': tc_id_sse,
                                'status': 'running',
                                'page': page_name,
                                'tool_name': 'write_page'
                            }
                        }, ensure_ascii=False))

                        pages = session.pages_html or {}
                        if not pages and session.generated_html:
                            pages = {'主页面': session.generated_html}

                        # 精确匹配
                        current_html = pages.get(page_name)
                        if not current_html:
                            available = list(pages.keys())
                            loop_messages.append({
                                'role': 'tool',
                                'tool_call_id': tc_id,
                                'name': 'write_page',
                                'content': (
                                    f"Error: page '{page_name}' not found. "
                                    f"Available pages: {available}. "
                                    f"Use the exact page name."
                                )
                            })
                            self._send_sse_data(json.dumps({
                                'type': 'tool_call_progress',
                                'data': {
                                    'tool_call_id': tc_id_sse,
                                    'status': 'failed',
                                    'tool_name': 'write_page',
                                    'page': page_name,
                                    'error': 'page not found'
                                }
                            }, ensure_ascii=False))
                            continue

                        old_full_page = current_html
                        session.pages_html[page_name] = new_html
                        edit_results.append({
                            'applied': True, 'page': page_name,
                            'search_snippet': 'full page write',
                            'error': None,
                            'old_text': old_full_page,
                            'new_text': new_html
                        })
                        round_has_edit = True
                        has_ever_edited = True

                        old_snip, new_snip = self._diff_snippet(
                            old_full_page, new_html)
                        old_full, new_full = self._diff_text(
                            old_full_page, new_html)
                        self._send_sse_data(json.dumps({
                            'type': 'tool_call_progress',
                            'data': {
                                'tool_call_id': tc_id_sse,
                                'status': 'applied',
                                'page': page_name,
                                'tool_name': 'write_page',
                                'old_snippet': old_snip,
                                'new_snippet': new_snip,
                                'old_text': old_full,
                                'new_text': new_full
                            }
                        }, ensure_ascii=False))

                        loop_messages.append({
                            'role': 'tool',
                            'tool_call_id': tc_id,
                            'name': 'write_page',
                            'content': (
                                f"The page [{page_name}] "
                                f"has been written successfully."
                            )
                        })

                # ======== Step 4: 编辑后保存 ========
                if round_has_edit:
                    has_update = True

                    # 更新 generated_html
                    is_single_page = (
                        len(session.page_order) <= 1
                        and len(session.pages_html) <= 1
                    )
                    if is_single_page:
                        pn = (session.page_order[0]
                              if session.page_order
                              else list(session.pages_html.keys())[0])
                        session.generated_html = session.pages_html[pn]
                    else:
                        from server_context_engineering import (
                            assemble_multi_page_html)
                        page_fragments = [
                            session.pages_html[n]
                            for n in session.page_order
                            if n in session.pages_html
                        ]
                        page_names = [
                            n for n in session.page_order
                            if n in session.pages_html
                        ]
                        if page_fragments:
                            session.generated_html = (
                                assemble_multi_page_html(
                                    page_fragments=page_fragments,
                                    design_system_css=(
                                        session.design_system),
                                    page_names=page_names,
                                    global_config=session.global_config,
                                    project_dir=project_folder,
                                    output_format='dual',
                                    cross_page_spec=getattr(
                                        session, 'cross_page_spec', None)
                                )
                            )

                    # 备份并保存
                    html_path = os.path.join(
                        project_folder, 'index.html')
                    bak_path = html_path + '.bak'
                    if os.path.exists(html_path):
                        try:
                            import shutil
                            shutil.copy2(html_path, bak_path)
                        except Exception:
                            pass
                    # srcdoc 项目：重组内部内容与外框架
                    html_to_write = session.generated_html
                    # 多页项目且非 srcdoc/head 拆分模式：
                    # save_multi_file_output 已保存正确的多文件 index.html，
                    # 不再用 Vue SPA 覆盖
                    _skip_write = (
                        not is_single_page
                        and not session.srcdoc_frame_html
                        and not getattr(session, '_chat_head_html', '')
                    )
                    if _skip_write:
                        # 多文件已保存，仅同步到对话目录
                        logger.info("[Chat] 多文件项目已保存，跳过 index.html 覆盖")
                    else:
                        # 安全网：确保 pageData 中 </script> 转义正确
                        html_to_write = self._fix_page_data_script_escaping(
                            html_to_write)
                        if session.srcdoc_frame_html:
                            inner = session.generated_html
                            html_to_write = self.assemble_iframe_html(
                                inner, session.srcdoc_frame_html)
                            logger.info(
                                f"[Agent] srcdoc 重组: 内部 {len(inner)} "
                                f"+ 框架 {len(session.srcdoc_frame_html)} "
                                f"→ {len(html_to_write)} 字符")
                        # 侧边栏项目：合并 head + body
                        if getattr(session, '_chat_head_html', ''):
                            html_to_write = _merge_head_body(
                                session._chat_head_html,
                                html_to_write)
                            logger.info(
                                f"[Agent] head+body 合并: "
                                f"→ {len(html_to_write)} 字符")
                        with open(html_path, 'w', encoding='utf-8') as f:
                            f.write(html_to_write)
                    # 保存编辑前快照（用于撤回）
                    if session.conversation_id:
                        from server_conversations import save_snapshot
                        save_snapshot(
                            project_folder,
                            session.conversation_id,
                            len(session.messages),
                            session.pages_html,
                            session.page_order,
                            session.generated_html,
                            session.srcdoc_frame_html,
                            session._chat_head_html,
                        )
                    # 同步到对话目录的 index.html
                    if session.conversation_id:
                        from server_conversations import (
                            get_conversation_dir)
                        conv_dir = get_conversation_dir(
                            project_folder,
                            session.conversation_id)
                        with open(os.path.join(
                                conv_dir, 'index.html'), 'w',
                                encoding='utf-8') as f:
                            f.write(html_to_write)
                    session.save(save_path=session_path)

                    # 编辑后验证（对标 Claude Code 的 build 验证）
                    # 1) 静态检查：标签平衡（排除编辑前已存在的问题）
                    verify_result = self._quick_verify_html(
                        html_to_write,
                        pre_existing=pre_existing_imbalances)

                    # 2) 验证失败时尝试自动修复简单的不平衡
                    if verify_result is not True:
                        auto_fixed_html, did_auto_fix = (
                            self._auto_fix_tag_imbalances(
                                html_to_write,
                                pre_existing=pre_existing_imbalances))
                        if did_auto_fix:
                            html_to_write = auto_fixed_html
                            # 同步更新 session 数据，
                            # 否则下一轮 read_page 会读到旧版
                            if is_single_page:
                                pn = (
                                    session.page_order[0]
                                    if session.page_order
                                    else list(
                                        session.pages_html.keys())[0])
                                session.pages_html[pn] = (
                                    auto_fixed_html)
                                session.generated_html = (
                                    auto_fixed_html)
                            elif getattr(
                                    session, '_chat_head_html', ''):
                                # 侧边栏项目：从合并结果中
                                # 提取 body 部分更新
                                _, fixed_body = _split_head_body(
                                    auto_fixed_html)
                                if fixed_body:
                                    for pn in (
                                            session.pages_html):
                                        session.pages_html[pn] = (
                                            fixed_body)
                                    session.generated_html = (
                                        fixed_body)
                            with open(html_path, 'w',
                                      encoding='utf-8') as f:
                                f.write(html_to_write)
                            session.save(save_path=session_path)
                            verify_result = True
                            logger.info(
                                "[Agent] 自动修复标签不平衡，"
                                "验证通过")

                    # 3) 浏览器检查：控制台错误（需要 Playwright）
                    if verify_result is True:
                        server_port = AI_OPTIONS.get('port', 8080)
                        browser_result = self._verify_page_in_browser(
                            html_path, server_port,
                            pre_existing=pre_existing_console_errors)
                        if browser_result is not True:
                            verify_result = browser_result

                    if verify_result is not True:
                        verify_fail_count += 1
                        logger.warning(
                            f"[Agent] 编辑后验证失败 ({verify_fail_count}"
                            f"/{MAX_VERIFY_RETRIES}): {verify_result}")

                        if verify_fail_count > MAX_VERIFY_RETRIES:
                            # 验证失败次数超限，放弃修复
                            logger.warning(
                                f"[Agent] 验证失败重试已达上限"
                                f" {MAX_VERIFY_RETRIES} 次，停止修复")
                            pending_verify_fix = False
                            break

                        pending_verify_fix = True
                        # 重置空转计数：AI 确实在编辑，
                        # 不应因验证失败导致的后续读取而惩罚
                        consecutive_reads = 0

                        # 构建包含实际代码片段的修复指导
                        # 让 AI 能直接修复而无需再 read_page
                        fix_hint_parts = [
                            f"编辑已应用，但页面验证发现问题：",
                            f"{verify_result}",
                            f"",
                        ]
                        # 提取定位信息中的行号，
                        # 直接附加相关代码行
                        lines = html_to_write.split('\n')
                        hint_lines = re.findall(
                            r'L(\d+)', verify_result)
                        if hint_lines:
                            fix_hint_parts.append(
                                "相关代码区域：")
                            seen = set()
                            for ln_str in hint_lines[:4]:
                                ln = int(ln_str)
                                if ln in seen:
                                    continue
                                seen.add(ln)
                                start = max(0, ln - 3)
                                end = min(len(lines), ln + 3)
                                for i in range(start, end):
                                    marker = (
                                        " >>>" if i == ln - 1
                                        else "    ")
                                    fix_hint_parts.append(
                                        f"{marker} L{i+1}: "
                                        f"{lines[i][:120]}")
                                fix_hint_parts.append(
                                    "    ...")
                        fix_hint_parts.extend([
                            "",
                            "请直接用 edit_file 修复上述标签问题，"
                            "不要再 read_page。",
                        ])
                        fix_hint = '\n'.join(fix_hint_parts)

                        loop_messages.append({
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "name": tc['name'],
                            "content": fix_hint
                        })
                        loop_messages.append({
                            "role": "assistant",
                            "content": "正在检查编辑结果..."
                        })
                        continue  # 继续循环让 AI 修复

                    # 验证通过，清除标记
                    pending_verify_fix = False
                    verify_fail_count = 0  # 验证通过，重置失败计数
                    # 更新标签不平衡基线，后续编辑以此为基准
                    pre_existing_imbalances = (
                        self._get_tag_imbalances(html_to_write))

                    has_more_reads = any(
                        tc['name'] == 'read_page'
                        for tc in tool_calls_result
                    )
                    if not has_more_reads:
                        break  # AI 只做了编辑，没有更多读取 → 完成
                    # 否则 AI 可能还有后续操作，继续循环

                # 只有 read_page → 循环自然继续
                # 空转检测：连续只读不编辑超过阈值时干预
                if not round_has_edit:
                    consecutive_reads += 1
                    limit = MAX_CONSECUTIVE_READS if has_ever_edited else MAX_EXPLORE_READS
                    if consecutive_reads >= limit:
                        logger.warning(
                            f"[Agent] 连续 {consecutive_reads} 轮只读不编辑"
                            f"（{'空转' if has_ever_edited else '探索超限'}），强制退出")
                        self._send_sse_data(json.dumps({
                            'type': 'chat',
                            'data': {
                                'role': 'system',
                                'content': (
                                    'AI 多次读取页面但未能完成修改，'
                                    '可能是因为页面过大或问题过于复杂。'
                                    '请尝试更具体的描述，或切换模型重试。'
                                )
                            }
                        }, ensure_ascii=False))
                        break
                    elif consecutive_reads >= limit - 1:
                        # 倒数第二轮，给 AI 最后一次机会
                        loop_messages.append({
                            'role': 'user',
                            'content': (
                                '你已经连续读取多次但没有编辑。'
                                '请现在直接用 edit_file 修复问题，'
                                '或者说明你发现了什么。'
                                '不要再读取。'
                            )
                        })
                else:
                    consecutive_reads = 0  # 编辑成功，重置计数

                logger.info(f"[Agent] 轮次 {agent_round} 完成"
                            f"（空转={consecutive_reads}），继续下一轮")
                # 中间保存：每轮结束后将当前进度写入磁盘
                # 防止崩溃丢失已完成的工具调用和页面变更
                session._in_progress = {
                    'accumulated': accumulated,
                    'accumulated_reasoning': accumulated_reasoning,
                    'tool_calls_log': tool_calls_log or [],
                    'edit_results': edit_results,
                    'agent_round': agent_round,
                }
                session._flush_to_disk()

            else:
                # 循环达到上限
                logger.warning(
                    f"[Agent] 达到最大轮次 {MAX_AGENT_ROUNDS}")
                if pending_verify_fix:
                    logger.warning(
                        "[Agent] 注意：验证问题未修复，"
                        "页面可能存在标签不平衡等问题")

            # ======== 最终结果 ========
            # 为已应用的编辑注入 diff 数据（用于历史记录展示）
            for er in edit_results:
                if er['applied']:
                    er.setdefault('old_text', '')
                    er.setdefault('new_text', '')

            applied_count = sum(1 for r in edit_results if r['applied'])

            # 构建保存用的 assistant content：完整保存，不截断
            save_content = accumulated if accumulated.strip() else ''
            save_thinking = accumulated_reasoning if accumulated_reasoning.strip() else None

            if has_update:
                session.add_message('assistant', save_content,
                                    html_changes=edit_results,
                                    tool_calls_log=tool_calls_log or None,
                                    thinking_content=save_thinking)
            else:
                # 编辑全部失败：
                # 移除本轮的 user 消息，避免下次对话携带失败的上下文
                # 这样下次提问时，AI 会像新对话一样重新处理
                if session.messages and session.messages[-1].get('role') == 'user':
                    removed = session.messages.pop()
                    logger.info(f"[对话] 编辑失败，移除用户消息: "
                                f"{removed.get('content', '')[:50]}...")
                    # 同步落盘：移除的消息也要持久化
                    session._flush_to_disk()
                # 不保存 assistant 消息（因为没有有效编辑）
            # 清除中间保存标记（对话已完成）
            session._in_progress = None

            # 发送编辑结果
            mode = 'agent_loop'
            message = None
            if not edit_results and not has_update:
                mode = 'none'
                est_input_tokens = int(sum(
                    len(m.get('content', ''))
                    if isinstance(m.get('content'), str)
                    else len(str(m.get('content', '')))
                    for m in ai_messages
                ) * 0.4)
                if not accumulated and est_input_tokens > 30000:
                    message = ('AI 返回空响应，可能是因为输入内容过长'
                               f'（约 {est_input_tokens} tokens）。'
                               '请尝试缩短对话历史或切换模型。')
                elif not accumulated:
                    message = ('AI 返回空响应，当前模型可能不支持 '
                               'function calling。'
                               '请尝试切换模型。')
                else:
                    message = ('AI 响应中未包含可执行的修改指令，'
                                '请尝试更具体地描述需要修改的内容。')

            self._send_sse_data(json.dumps({
                'type': 'edit_result',
                'data': {
                    'applied': applied_count,
                    'failed': len(edit_results) - applied_count,
                    'edit_results': edit_results,
                    'mode': mode,
                    'js_warnings': js_warnings,
                    'diagnostics': all_diagnostics,
                    'message': message,
                    'can_rollback': os.path.exists(
                        os.path.join(project_folder, 'index.html.bak'))
                }
            }, ensure_ascii=False))

            if has_update:
                self._send_sse_data(json.dumps({
                    'type': 'preview_update',
                    'data': {'page': 'all', 'mode': 'edit'}
                }, ensure_ascii=False))

            # 保存会话
            session.save(save_path=session_path)

            # 更新对话索引元数据
            if session.conversation_id:
                from server_conversations import (
                    update_conversation_meta)
                update_conversation_meta(
                    project_folder, session.conversation_id)

            # 发送完成事件
            self._send_sse_event('status', json.dumps({
                'status': 'completed',
                'has_html_update': has_update,
            }, ensure_ascii=False))

            # SSE 结束
            try:
                self.wfile.write(b'data: [DONE]\n\n')
                self.wfile.flush()
                self.close_connection = True
            except Exception:
                pass

        except Exception as e:
            logger.error(f"[对话] 调整失败: {e}")
            import traceback
            logger.error(traceback.format_exc())
            try:
                self._send_sse_event('status', json.dumps({
                    'status': 'failed',
                    'error': str(e),
                }, ensure_ascii=False))
                self.wfile.write(b'data: [DONE]\n\n')
                self.wfile.flush()
                self.close_connection = True
            except Exception:
                pass

    def _chat_run_agent_loop(self, ai_messages, session, project_folder,
                             pre_existing_imbalances, pre_existing_console_errors,
                             edit_tools, project_id):
        """使用 AgentLoop 引擎执行对话式调整

        由 handle_chat 通过 feature flag 调用。
        返回包含 accumulated, edit_results 等字段的字典，
        供 handle_chat 后处理使用。
        """
        import json as _json
        from agent_loop import (AgentLoop, AgentLoopConfig, DiskOverflowManager,
                                 HookManager, HookType)
        from agent_tools import (ToolContext, create_inspector_registry,
                                  verify_after_edit_hook, anti_spin_post_hook)

        # 创建 SSE 回调 — 将 AgentLoop 事件转为 server.py 的 SSE 格式
        def _sse_callback(event_type, data):
            try:
                if event_type == 'text_delta':
                    self._send_sse_data(_json.dumps({
                        'type': 'chat',
                        'data': {'role': 'assistant', 'content': data.get('text', '')}
                    }, ensure_ascii=False))
                elif event_type == 'reasoning_delta':
                    self._send_sse_data(_json.dumps({
                        'type': 'chat',
                        'data': {'role': 'assistant', 'reasoning': data.get('text', '')}
                    }, ensure_ascii=False))
                elif event_type == 'tool_call_started':
                    tc_id = data.get('tool_call_id', '')
                    tool_name = data.get('tool_name', '')
                    self._send_sse_data(_json.dumps({
                        'type': 'tool_call_progress',
                        'data': {
                            'tool_call_id': f'r0-{tc_id}' if tool_name == 'read_page' else f'e0-{tc_id}',
                            'status': 'running',
                            'tool_name': tool_name,
                            'page': '',
                        }
                    }, ensure_ascii=False))
                elif event_type == 'context_compacted':
                    logger.info(f"[Chat Agent] 上下文已压缩: {data}")
                elif event_type == 'tool_call_completed':
                    # 每次工具调用完成后实时落盘，防止中途停止丢失进度
                    # 将当前工具调用信息追加到 _in_progress 并刷盘
                    progress = getattr(session, '_in_progress', None) or {}
                    tool_log = progress.get('tool_calls_log', [])
                    edit_res = progress.get('edit_results', [])
                    tool_name = data.get('tool_name', '')
                    page = data.get('page', '')
                    success = data.get('success', False)
                    # 记录工具调用
                    tool_log.append({
                        'tool': tool_name,
                        'page': page,
                        'success': success,
                        'content_length': data.get('content_length', 0),
                    })
                    # 如果是编辑操作且成功，记录编辑结果
                    if tool_name in ('edit_file', 'write_page', 'edit_page') and success:
                        edit_res.append({
                            'tool': tool_name,
                            'page': page,
                            'applied': True,
                            'old_text': data.get('old_text', ''),
                            'new_text': data.get('new_text', ''),
                        })
                    session._in_progress = {
                        'tool_calls_log': tool_log,
                        'edit_results': edit_res,
                    }
                    session._flush_to_disk()
                    # UI 层：发送 diff 数据给前端渲染
                    tc_id = data.get('tool_call_id', '')
                    tool_name = data.get('tool_name', '')
                    page = data.get('page', '')
                    old_text = data.get('old_text', '')
                    new_text = data.get('new_text', '')
                    is_write = data.get('write_page', False)

                    if data.get('success'):
                        prefix = 'w0' if is_write else (
                            'r0' if tool_name == 'read_page' else 'e0')
                        sse_data = {
                            'tool_call_id': f'{prefix}-{tc_id}',
                            'status': 'applied',
                            'tool_name': tool_name,
                            'page': page,
                        }
                        # 透传行号信息给前端
                        for k in ('line_start', 'line_end',
                                  'total_lines', 'content_length'):
                            if data.get(k):
                                sse_data[k] = data[k]
                        if old_text or new_text:
                            # 生成 diff 摘要
                            old_snip, new_snip = self._diff_snippet(
                                old_text, new_text)
                            old_full, new_full = self._diff_text(
                                old_text, new_text)
                            sse_data['old_snippet'] = old_snip
                            sse_data['new_snippet'] = new_snip
                            sse_data['old_text'] = old_full
                            sse_data['new_text'] = new_full
                        self._send_sse_data(_json.dumps({
                            'type': 'tool_call_progress',
                            'data': sse_data,
                        }, ensure_ascii=False))
                    else:
                        prefix = 'r0' if tool_name == 'read_page' else 'e0'
                        self._send_sse_data(_json.dumps({
                            'type': 'tool_call_progress',
                            'data': {
                                'tool_call_id': f'{prefix}-{tc_id}',
                                'status': 'failed',
                                'tool_name': tool_name,
                                'page': page,
                                'error': 'edit failed',
                            }
                        }, ensure_ascii=False))
            except Exception:
                pass

        # 创建取消检查
        def _cancel_check():
            return (project_id in generating_tasks
                    and generating_tasks[project_id].get('status') == 'cancelled')

        # 构建 ToolContext
        tool_ctx = ToolContext(
            project_id=project_id,
            project_folder=project_folder,
            server=self,
            session=session,
            extra={
                'pre_existing_imbalances': pre_existing_imbalances,
                'pre_existing_console_errors': pre_existing_console_errors,
            },
        )

        # 创建 Hook Manager
        hook_mgr = HookManager()
        hook_mgr.register(HookType.STOP, verify_after_edit_hook)
        hook_mgr.register(HookType.POST_TOOL_USE, anti_spin_post_hook)

        # 创建 Loop
        config = AgentLoopConfig(
            max_turns=50,
            max_consecutive_empty=2,
            max_consecutive_reads=3,
            max_explore_reads=6,
            max_verify_retries=3,
            tool_names=['read_page', 'edit_file', 'write_page',
                         'list_pages', 'read_framework'],
            extract_html=True,
            parallel_execution=False,
            disk_overflow=True,
            proactive_compaction=True,
        )

        loop = AgentLoop(
            config=config,
            tool_registry=create_inspector_registry(),
            hook_manager=hook_mgr,
        )
        loop.disk_overflow = DiskOverflowManager()

        try:
            result = loop.run(
                initial_messages=ai_messages,
                tool_context=tool_ctx,
                streaming_callback=_sse_callback,
                cancel_check=_cancel_check,
            )
        finally:
            if loop.disk_overflow:
                loop.disk_overflow.cleanup_all()

        # 循环后处理：保存编辑到磁盘
        edit_results = []
        for er in result.state.edit_results:
            er_dict = {
                'tool': er.get('tool', ''),
                'page': er.get('page', er.get('page_key', '')),
                'applied': er.get('applied', False),
                'error': er.get('error'),
            }
            edit_results.append(er_dict)

        # 保存到磁盘（如果有编辑）
        has_update = any(er.get('applied', False) for er in edit_results)
        logger.info(f"[Chat Agent] edit_results={edit_results}, has_update={has_update}")
        if has_update:
            try:
                # 组装并保存页面
                pages = session.pages_html or {}
                if pages:
                    # 多页面项目：保存到 pages/ 目录
                    pages_dir = os.path.join(project_folder, 'pages')
                    if os.path.isdir(pages_dir) or len(pages) > 1:
                        os.makedirs(pages_dir, exist_ok=True)
                        page_order = session.page_order or list(pages.keys())
                        for idx, pg_name in enumerate(page_order):
                            pg_html = pages.get(pg_name, '')
                            if pg_html:
                                safe_name = re.sub(r'[^\w\u4e00-\u9fff-]', '_', pg_name)
                                pg_path = os.path.join(pages_dir, f'page_{idx}_{safe_name}.html')
                                with open(pg_path, 'w', encoding='utf-8') as f:
                                    f.write(pg_html)

                    # 组装 index.html
                    from server_context_engineering import (
                        assemble_multi_page_html)
                    page_fragments = [
                        session.pages_html[n]
                        for n in session.page_order
                        if n in session.pages_html
                    ]
                    page_names = [
                        n for n in session.page_order
                        if n in session.pages_html
                    ]
                    if page_fragments:
                        assembled = assemble_multi_page_html(
                            page_fragments=page_fragments,
                            design_system_css=getattr(
                                session, 'design_system', ''),
                            page_names=page_names,
                            global_config=getattr(
                                session, 'global_config', None),
                            project_dir=project_folder,
                            output_format='dual',
                            cross_page_spec=getattr(
                                session, 'cross_page_spec', None)
                        )
                        # output_format='dual' + project_dir 时，
                        # save_multi_file_output 已在 assemble_multi_page_html
                        # 内部保存了正确的多文件 index.html（iframe 导航）。
                        # 不再用 Vue SPA 覆盖它，避免 id="app" 冲突和交互异常。
                        session.generated_html = assembled
                    else:
                        assembled = None
                    if not assembled:
                        # 单页项目或无片段：直接保存当前页面内容
                        import shutil
                        html_path = os.path.join(project_folder, 'index.html')
                        single_page = next(iter(pages.values()), '')
                        if single_page:
                            if os.path.exists(html_path):
                                shutil.copy2(html_path, html_path + '.bak')
                            with open(html_path, 'w', encoding='utf-8') as f:
                                f.write(single_page)

                    # 同步到对话目录的 index.html
                    if session.conversation_id and session.generated_html:
                        from server_conversations import get_conversation_dir
                        conv_dir = get_conversation_dir(
                            project_folder, session.conversation_id)
                        conv_index_path = os.path.join(
                            conv_dir, 'index.html')
                        html_to_sync = session.generated_html
                        # 多页项目且非 srcdoc 模式：对话 index.html 用组装的单文件版本
                        is_single = (len(session.page_order) <= 1
                                     and len(pages) <= 1)
                        if not is_single:
                            # assembled 已经是 SPA 版本（包含侧边栏导航）
                            # multi-file index.html 是 iframe 版本，对话需要 SPA
                            pass  # assembled 即为正确的 SPA HTML
                        with open(conv_index_path, 'w',
                                  encoding='utf-8') as f:
                            f.write(html_to_sync)
                        logger.info(
                            f"[Chat Agent] 已同步到对话 index.html "
                            f"({len(html_to_sync)} 字符)")
            except Exception as e:
                logger.error(f"[Chat Agent] 保存编辑失败: {e}")

        logger.info(
            f"[Chat Agent] 循环结束: {result.terminal_reason.value}, "
            f"{result.total_turns} 轮, {result.total_tool_calls} 工具调用"
        )

        return {
            'accumulated': result.final_text,
            'accumulated_reasoning': result.state.accumulated_reasoning,
            'edit_results': edit_results,
            'tool_calls_log': result.state.tool_calls_log,
            'has_update': has_update,
            'js_warnings': None,
            'all_diagnostics': [],
        }

    def handle_inspector_apply(self):
        """处理微调模式的 AI 修改请求

        支持 srcdoc iframe 项目：自动检测并拆分，分别发送给 AI，再重组。
        """

    def handle_migrate_multifile(self):
        """将单文件项目迁移为多文件架构"""
        try:
            data = self._read_post_json()
            project_id = data.get('projectId', '')
            if not project_id:
                self.send_error_response("缺少 projectId")
                return

            project_dir = os.path.join(PROJECTS_DIR, project_id)
            if not os.path.exists(project_dir):
                self.send_error_response("项目不存在")
                return

            from server_context_engineering import migrate_single_to_multifile
            success = migrate_single_to_multifile(project_dir)
            if success:
                self.send_json_response({'success': True, 'message': '迁移成功'})
            else:
                self.send_error_response("迁移失败：未检测到多页面结构")
        except Exception as e:
            logger.error(f"[迁移] 迁移失败: {e}")
            self.send_error_response(str(e))


        try:
            content_length = int(self.headers['Content-Length'])
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))

            project_id = data.get('projectId')
            user_request = data.get('userRequest', '')
            elements = data.get('elements', [])
            prompt = data.get('prompt', '')

            if not project_id:
                self.send_error_response("缺少 projectId")
                return

            if not user_request:
                self.send_error_response("缺少修改需求")
                return

            if not elements:
                self.send_error_response("未选中任何元素")
                return

            # 读取当前 HTML
            html_file = os.path.join(PROJECTS_DIR, project_id, 'index.html')
            if not os.path.exists(html_file):
                self.send_error_response("项目不存在")
                return

            with open(html_file, 'r', encoding='utf-8') as f:
                current_html = f.read()

            logger.info(f"[Inspector] 收到微调请求: {project_id}")
            logger.info(f"[Inspector] 选中元素数: {len(elements)}")
            logger.info(f"[Inspector] 用户需求: {user_request}")

            # 构建 AI Prompt
            elements_desc = "\n".join([
                f"元素 {i+1}:\n- 选择器: {el.get('selector', 'unknown')}\n- HTML:\n```html\n{el.get('html', '')}\n```"
                for i, el in enumerate(elements)
            ])

            # ===== 检测 srcdoc iframe 项目 =====
            srcdoc_split = self.detect_and_split_srcdoc(current_html)

            if srcdoc_split:
                # srcdoc 项目：分别发送外框架和内部内容
                logger.info("[Inspector] 检测到 srcdoc iframe 项目，使用拆分模式")

                # 框架精简版（用于 AI 参考）
                frame_display = srcdoc_split['raw_frame_html']
                if len(frame_display) > 8000:
                    frame_display = frame_display[:8000] + '\n<!-- ... 外框架截断 ... -->'

                ai_prompt = f"""你是一个精准的 HTML 修改专家。请根据用户的需求，精确修改指定的 HTML 元素。

## 项目结构说明
这是一个 iframe 布局项目。外框架（侧边栏/导航）包裹着内部页面内容。

## 外框架 HTML（侧边栏/导航，仅供参考和修改框架时使用）
```html
{frame_display}
```

## 内部页面内容（iframe srcdoc 中的实际页面）
```html
{srcdoc_split['inner_html']}
```

## 需要修改的元素
{elements_desc}

## 用户修改需求
{user_request}

## 修改规则
1. 只修改上述指定的元素，不要修改其他任何代码
2. 保持页面整体风格和结构不变
3. 如果涉及样式修改，优先使用内联 style 或 Tailwind CSS 类

## 返回格式
请用以下格式返回修改结果：
- 如果只修改了内部页面内容：
```
===INNER_START===
（修改后的完整内部页面 HTML）
===INNER_END===
```
- 如果只修改了外框架：
```
===FRAME_START===
（修改后的外框架 HTML，保留 {{{{AI_GENERATED_CONTENT}}}} 占位符）
===FRAME_END===
```
- 如果两者都修改了，返回两个 section。"""

                # 调用 AI
                try:
                    ai_response = self.call_ai_model(ai_prompt, [])

                    if not ai_response or len(ai_response) < 50:
                        self.send_error_response("AI 返回内容无效")
                        return

                    # 解析 AI 响应
                    parsed = self.parse_srcdoc_ai_response(ai_response)

                    # 确定最终内容
                    final_inner = parsed['inner_html'] or srcdoc_split['inner_html']
                    final_frame = parsed['frame_html'] or srcdoc_split['raw_frame_html']

                    # 重组
                    modified_html = self.assemble_iframe_html(
                        final_inner, final_frame)

                    if not modified_html or len(modified_html) < 100:
                        self.send_error_response("重组后的 HTML 无效")
                        return

                    # 备份原文件
                    backup_file = os.path.join(
                        PROJECTS_DIR, project_id, 'index.html.bak')
                    with open(backup_file, 'w', encoding='utf-8') as f:
                        f.write(current_html)
                    logger.info(
                        f"[Inspector] 备份已创建: {backup_file}")

                    # 保存修改后的 HTML
                    with open(html_file, 'w', encoding='utf-8') as f:
                        f.write(modified_html)

                    logger.info(
                        f"[Inspector] srcdoc HTML 已更新: {html_file}")
                    self.send_json_response({
                        'success': True,
                        'message': '修改成功',
                        'backupFile': 'index.html.bak'
                    })

                except Exception as ai_error:
                    logger.info(
                        f"[Inspector] AI 调用失败: {ai_error}")
                    import traceback
                    traceback.print_exc()
                    self.send_error_response(
                        f"AI 调用失败: {str(ai_error)}")
            else:
                # ===== 非 srcdoc 项目：原有逻辑 =====
                ai_prompt = f"""你是一个精准的 HTML 修改专家。请根据用户的需求，精确修改指定的 HTML 元素。

## 当前完整 HTML
```html
{current_html}
```

## 需要修改的元素
{elements_desc}

## 用户修改需求
{user_request}

## 修改规则
1. 只修改上述指定的元素，不要修改其他任何代码
2. 保持页面整体风格和结构不变
3. 如果涉及样式修改，优先使用内联 style 或 Tailwind CSS 类
4. 返回修改后的完整 HTML 文档

请直接返回修改后的完整 HTML 代码（从 <!DOCTYPE html> 开始到 </html> 结束），不要有任何额外说明。"""

                # 调用 AI
                try:
                    modified_html = self.call_ai_model(ai_prompt, [])

                    if not modified_html or len(modified_html) < 100:
                        self.send_error_response("AI 返回内容无效")
                        return

                    # 备份原文件
                    backup_file = os.path.join(
                        PROJECTS_DIR, project_id, 'index.html.bak')
                    with open(backup_file, 'w', encoding='utf-8') as f:
                        f.write(current_html)
                    logger.info(
                        f"[Inspector] 备份已创建: {backup_file}")

                    # 保存修改后的 HTML
                    with open(html_file, 'w', encoding='utf-8') as f:
                        f.write(modified_html)

                    logger.info(
                        f"[Inspector] HTML 已更新: {html_file}")
                    self.send_json_response({
                        'success': True,
                        'message': '修改成功',
                        'backupFile': 'index.html.bak'
                    })

                except Exception as ai_error:
                    logger.info(
                        f"[Inspector] AI 调用失败: {ai_error}")
                    import traceback
                    traceback.print_exc()
                    self.send_error_response(
                        f"AI 调用失败: {str(ai_error)}")

        except Exception as e:
            logger.error(f"[Inspector错误] {e}")
            import traceback
            traceback.print_exc()
            self.send_error_response(str(e))
    
    def handle_prd_load(self, query):
        """加载 PRD 文档"""
        try:
            project_id = query.get('projectId', [''])[0]
            page_name = query.get('pageName', ['default'])[0]
            
            if not project_id:
                self.send_error_response("缺少 projectId")
                return
            
            # 清理页面名称
            safe_page_name = re.sub(r'[^\w\u4e00-\u9fff-]', '_', page_name)
            prd_file = os.path.join(PROJECTS_DIR, project_id, 'prd', f'{safe_page_name}.md')
            
            content = ''
            if os.path.exists(prd_file):
                with open(prd_file, 'r', encoding='utf-8') as f:
                    content = f.read()
            
            self.send_json_response({'content': content, 'pageName': safe_page_name})
            
        except Exception as e:
            logger.error(f"[PRD错误] 加载失败: {e}")
            self.send_error_response(str(e))
    
    def handle_get_pages(self, query):
        """获取项目的页面列表（支持多文件和单文件两种架构）"""
        try:
            project_id = query.get('projectId', [''])[0]

            if not project_id:
                self.send_error_response("缺少 projectId")
                return

            project_dir = os.path.join(PROJECTS_DIR, project_id)
            if not os.path.exists(project_dir):
                self.send_error_response("项目不存在")
                return

            # 多文件架构：从 pages/ 目录读取页面列表
            pages_dir = os.path.join(project_dir, 'pages')
            if os.path.isdir(pages_dir):
                pages = []
                page_files = sorted([f for f in os.listdir(pages_dir) if f.endswith('.html')])
                for filename in page_files:
                    # 从文件名提取页面名: page_0_数据汇聚.html -> 数据汇聚
                    match = re.match(r'page_(\d+)_(.+)\.html', filename)
                    if match:
                        page_idx = int(match.group(1))
                        page_name = match.group(2)
                        pages.append({
                            'name': page_name,
                            'label': self.get_page_label(page_name),
                            'type': 'multi-file',
                            'filename': f'pages/{filename}',
                            'index': page_idx,
                        })
                if pages:
                    self.send_json_response({'pages': pages, 'mode': 'multi-file'})
                    return

            # 回退：单文件架构，解析 index.html
            html_file = os.path.join(project_dir, 'index.html')
            if not os.path.exists(html_file):
                self.send_error_response("项目不存在")
                return

            with open(html_file, 'r', encoding='utf-8') as f:
                html_content = f.read()

            pages = self.extract_pages_from_html(html_content)

            # 单文件项目如果没有提取到多页结构，仍返回 index.html 作为默认页面
            if not pages:
                pages = [{
                    'name': '主页面',
                    'label': '主页面',
                    'type': 'single-file',
                    'filename': 'index.html',
                    'index': 0,
                }]

            self.send_json_response({'pages': pages, 'mode': 'single-file'})

        except Exception as e:
            logger.error(f"[Pages错误] {e}")
            self.send_error_response(str(e))
    
    def handle_get_flowchart(self, query):
        """生成流程图（支持多文件和单文件两种架构）"""
        try:
            project_id = query.get('projectId', [''])[0]

            if not project_id:
                self.send_error_response("缺少 projectId")
                return

            project_dir = os.path.join(PROJECTS_DIR, project_id)
            if not os.path.exists(project_dir):
                self.send_error_response("项目不存在")
                return

            # 多文件架构：从各页面 HTML 中提取跳转关系
            pages_dir = os.path.join(project_dir, 'pages')
            if os.path.isdir(pages_dir):
                page_files = sorted([f for f in os.listdir(pages_dir) if f.endswith('.html')])
                if page_files:
                    # 收集所有页面的 HTML 内容
                    all_html = ''
                    page_names = []
                    for filename in page_files:
                        match = re.match(r'page_(\d+)_(.+)\.html', filename)
                        if match:
                            page_name = match.group(2)
                            page_names.append(page_name)
                            filepath = os.path.join(pages_dir, filename)
                            with open(filepath, 'r', encoding='utf-8') as f:
                                all_html += f.read() + '\n'
                    flowchart = self.generate_flowchart_from_html(all_html)
                    self.send_json_response(flowchart)
                    return

            # 回退：单文件架构
            html_file = os.path.join(project_dir, 'index.html')
            if not os.path.exists(html_file):
                self.send_error_response("项目不存在")
                return

            with open(html_file, 'r', encoding='utf-8') as f:
                html_content = f.read()

            flowchart = self.generate_flowchart_from_html(html_content)
            self.send_json_response(flowchart)

        except Exception as e:
            logger.error(f"[Flowchart错误] {e}")
            import traceback
            traceback.print_exc()
            self.send_error_response(str(e))
    
    def extract_pages_from_html(self, html_content):
        """从 HTML 中提取页面列表"""
        pages = []
        seen_names = set()
        
        # ===== 模式A: Vue currentPage 相关的页面定义 =====
        # 模式1: v-if="currentPage === 'xxx'"
        pattern1 = r'v-if=["\']currentPage\s*===?\s*["\']([^"\']+)["\']'
        matches1 = re.findall(pattern1, html_content)
        
        # 模式2: currentPage = 'xxx' 或 currentPage.value = 'xxx'
        pattern2 = r'currentPage(?:\.value)?\s*=\s*["\']([^"\']+)["\']'
        matches2 = re.findall(pattern2, html_content)
        
        for page in set(matches1 + matches2):
            if page and len(page) < 50 and not page.startswith('!') and page not in seen_names:
                seen_names.add(page)
                pages.append({
                    'name': page,
                    'label': self.get_page_label(page),
                    'type': 'currentPage'
                })
        
        # ===== 模式B: Vue Router 路由定义 =====
        # 匹配 { path: '/xxx', component: YyyPage } 模式
        router_pattern = r'\{\s*path:\s*["\'](/[^"\']*)["\']'
        router_matches = re.findall(router_pattern, html_content)
        
        if router_matches and not pages:
            # 仅当没有 currentPage 模式时才使用 Router 模式（避免重复）
            for route_path in router_matches:
                # 将路径转换为页面名称: '/scan-result' -> 'scan-result', '/' -> 'home'
                page_name = route_path.strip('/')
                if not page_name:
                    page_name = 'home'
                
                if page_name and len(page_name) < 50 and page_name not in seen_names:
                    seen_names.add(page_name)
                    pages.append({
                        'name': page_name,
                        'label': self.get_page_label(page_name),
                        'type': 'router',
                        'routePath': route_path
                    })
        
        # 按名称排序（home 排在最前面）
        pages.sort(key=lambda x: (0 if x['name'] == 'home' else 1, x['name']))
        
        return pages
    
    def get_page_label(self, page_name):
        """获取页面的中文标签"""
        label_map = {
            'home': '首页',
            'scan': '扫描页',
            'scan-result': '扫描结果',
            'result': '结果页',
            'analysis': '解析页',
            'aiTutor': 'AI讲题',
            'ai-explain': 'AI讲解',
            'ai-qa': 'AI答疑',
            'wrongBookHome': '错题本首页',
            'wrongBookList': '错题列表',
            'wrongBookDetail': '错题详情',
            'mistakes': '错题本',
            'mistakes-list': '错题列表',
            'login': '登录',
            'register': '注册',
            'profile': '个人中心',
            'settings': '设置',
            'detail': '详情页',
            'list': '列表页',
            'learning-report': '学情报告',
            'homework-list': '作业列表',
            'homework-report': '作业报告',
            'online-answer': '在线作答',
            'photo-correction': '拍照批改',
            'writing-guidance': '写作指导',
            'english-translation': '英文翻译',
            'speaking-practice': '口语练习',
        }
        return label_map.get(page_name, page_name)
    
    def generate_flowchart_from_html(self, html_content):
        """从 HTML 生成 Mermaid 流程图"""
        pages = []
        transitions = []
        modals = []
        
        # 1. 提取所有页面
        page_patterns = [
            r'v-if=["\']currentPage\s*===?\s*["\']([^"\']+)["\']',
            r'currentPage(?:\.value)?\s*=\s*["\']([^"\']+)["\']',
            r':class="[^"]*currentPage\s*===?\s*["\']([^"\']+)["\']',
        ]
        
        all_pages = set()
        for pattern in page_patterns:
            matches = re.findall(pattern, html_content)
            for m in matches:
                if m and len(m) < 50 and not m.startswith('!'):
                    all_pages.add(m)
        
        pages = list(all_pages)
        
        # 2. 提取页面跳转关系 - 改进算法
        # 分割成页面区块来分析
        page_block_pattern = r'(v-if=["\']currentPage\s*===?\s*["\'][^"\']+["\'])'
        blocks = re.split(page_block_pattern, html_content)
        
        current_page = None
        for i, block in enumerate(blocks):
            # 检查是否是页面标识块
            page_match = re.search(r'v-if=["\']currentPage\s*===?\s*["\']([^"\']+)["\']', block)
            if page_match:
                current_page = page_match.group(1)
                continue
            
            # 如果有当前页面，分析这个块中的跳转
            if current_page and current_page in pages:
                # 模式1: currentPage = 'xxx' 或 currentPage.value = 'xxx'
                jump_matches = re.findall(r'currentPage(?:\.value)?\s*=\s*["\']([^"\']+)["\']', block)
                for target in jump_matches:
                    if target in pages and target != current_page:
                        transitions.append({
                            'from': current_page,
                            'to': target,
                            'type': 'direct'
                        })
                
                # 模式2: goToXxx 或 goTo('xxx')
                method_matches = re.findall(r'@click=["\'][^"\']*go(?:To)?([A-Z][a-zA-Z]*)', block)
                for target in method_matches:
                    target_lower = target[0].lower() + target[1:] if target else ''
                    if target_lower in pages and target_lower != current_page:
                        transitions.append({
                            'from': current_page,
                            'to': target_lower,
                            'type': 'method'
                        })
                
                # 模式3: navigateTo('xxx')
                nav_matches = re.findall(r'navigateTo\(["\']([^"\']+)["\']\)', block)
                for target in nav_matches:
                    if target in pages and target != current_page:
                        transitions.append({
                            'from': current_page,
                            'to': target,
                            'type': 'navigate'
                        })
        
        # 3. 提取弹窗/模态框/交互组件
        modal_patterns = [
            (r'v-if=["\']show(\w+)["\']', 'show'),
            (r'(\w+Modal)\s*=\s*ref\(', 'modal'),
            (r'(\w+Dialog)\s*=\s*ref\(', 'dialog'),
            (r'(\w+Popup)\s*=\s*ref\(', 'popup'),
            (r'const\s+(show\w+)\s*=\s*ref\(', 'ref'),
        ]
        
        modal_set = set()
        for pattern, ptype in modal_patterns:
            matches = re.findall(pattern, html_content)
            for modal in matches:
                # 清理名称
                clean_name = modal.replace('show', '').replace('Show', '')
                clean_name = clean_name.replace('Modal', '').replace('Dialog', '').replace('Popup', '')
                if clean_name and len(clean_name) < 30 and clean_name.lower() not in ['loading', 'error', 'success']:
                    modal_set.add((modal, f'{clean_name}弹窗'))
        
        modals = [{'name': m[0], 'label': m[1]} for m in modal_set]
        
        # 4. 去重转换
        unique_transitions = {}
        for t in transitions:
            key = f"{t['from']}->{t['to']}"
            if key not in unique_transitions:
                unique_transitions[key] = t
        transitions = list(unique_transitions.values())
        
        # 5. 确保没有孤立页面 - 如果页面没有入边和出边，尝试推断
        connected_pages = set()
        for t in transitions:
            connected_pages.add(t['from'])
            connected_pages.add(t['to'])
        
        isolated_pages = set(pages) - connected_pages
        
        # 如果有 home 页面，将孤立页面连接到 home
        if 'home' in pages and isolated_pages:
            for page in isolated_pages:
                if page != 'home':
                    transitions.append({
                        'from': 'home',
                        'to': page,
                        'type': 'inferred'
                    })
        
        # 6. 生成 Mermaid 代码
        mermaid_lines = ['flowchart TD']
        
        # 添加页面节点
        for page in sorted(pages):
            label = self.get_page_label(page)
            # 使用安全的节点ID (移除特殊字符)
            safe_id = re.sub(r'[^a-zA-Z0-9]', '_', page)
            mermaid_lines.append(f'    {safe_id}["{label}"]')
        
        # 添加弹窗节点（圆角矩形）
        for modal in modals[:8]:  # 限制数量
            safe_id = re.sub(r'[^a-zA-Z0-9]', '_', modal['name'])
            mermaid_lines.append(f'    {safe_id}("{modal["label"]}")')
        
        # 添加跳转连接
        added_transitions = set()
        for t in transitions:
            safe_from = re.sub(r'[^a-zA-Z0-9]', '_', t['from'])
            safe_to = re.sub(r'[^a-zA-Z0-9]', '_', t['to'])
            key = f"{safe_from}->{safe_to}"
            if key not in added_transitions:
                if t.get('type') == 'inferred':
                    mermaid_lines.append(f'    {safe_from} -.-> {safe_to}')
                else:
                    mermaid_lines.append(f'    {safe_from} --> {safe_to}')
                added_transitions.add(key)
        
        # 添加样式类定义（必须在节点和边之后）
        mermaid_lines.append('    classDef pageNode fill:#e0e7ff,stroke:#6366f1,stroke-width:2px')
        mermaid_lines.append('    classDef modalNode fill:#fef3c7,stroke:#f59e0b,stroke-width:1px,stroke-dasharray:5 5')
        
        # 应用样式（必须在 classDef 之后，节点名用逗号分隔）
        if pages:
            page_ids = ','.join([re.sub(r'[^a-zA-Z0-9]', '_', p) for p in pages])
            mermaid_lines.append(f'    class {page_ids} pageNode')
        if modals:
            modal_ids = ','.join([re.sub(r'[^a-zA-Z0-9]', '_', m['name']) for m in modals[:8]])
            mermaid_lines.append(f'    class {modal_ids} modalNode')
        
        mermaid_code = '\n'.join(mermaid_lines)
        
        return {
            'pages': [{'name': p, 'label': self.get_page_label(p)} for p in sorted(pages)],
            'transitions': transitions,
            'modals': modals,
            'mermaid': mermaid_code,
            'stats': {
                'pageCount': len(pages),
                'transitionCount': len(transitions),
                'modalCount': len(modals)
            }
        }

    def send_json_response(self, data):
        """发送JSON响应"""
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))

    def send_error_response(self, message):
        """发送错误响应"""
        self.send_response(500)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({'error': message}, ensure_ascii=False).encode('utf-8'))

    def handle_generation_status(self, query):
        """查询项目生成状态"""
        try:
            project_id = query.get('id', [''])[0]
            if not project_id:
                self.send_error_response("缺少project_id")
                return
            
            # 检查任务状态
            with tasks_lock:
                if project_id in generating_tasks:
                    task_info = generating_tasks[project_id]
                    # 已取消的任务，返回 cancelled 并清理
                    if task_info.get('status') == 'cancelled':
                        del generating_tasks[project_id]
                        self.send_json_response({'status': 'cancelled', 'progress': 0})
                        return
                    self.send_json_response({
                        'status': task_info['status'],
                        'progress': task_info.get('progress', 0),
                        'error': task_info.get('error', ''),
                        # 多轮生成扩展字段
                        'round': task_info.get('round', 0),
                        'phase_description': task_info.get('phase_description', ''),
                        'page_progress': task_info.get('page_progress'),
                        'strategy': task_info.get('strategy', 'single'),
                    })
                    return
            
            # 检查是否已完成
            html_path = os.path.join(PROJECTS_DIR, project_id, 'index.html')
            if os.path.exists(html_path):
                self.send_json_response({'status': STATUS_COMPLETED, 'progress': 100})
            else:
                self.send_json_response({'status': 'not_found', 'progress': 0})
                
        except Exception as e:
            logger.error(f"[错误] 查询状态失败: {e}")
            self.send_error_response(str(e))

    def handle_stop_generation(self):
        """停止后台生成任务"""
        try:
            content_length = int(self.headers['Content-Length'])
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))
            project_id = data.get('project_id')

            if not project_id:
                self.send_error_response("缺少 project_id")
                return

            active_session = None
            with tasks_lock:
                if project_id in generating_tasks:
                    generating_tasks[project_id]['status'] = 'cancelled'
                    active_session = generating_tasks[project_id].get('session')
                    logger.info(f"[停止] 已取消生成任务: {project_id}")
                else:
                    self.send_error_response("项目没有正在进行的生成任务")
                    return

            if active_session:
                try:
                    active_session.close()
                except Exception:
                    pass

            self.send_json_response({'success': True})

        except Exception as e:
            logger.error(f"[错误] 停止生成失败: {e}")
            self.send_error_response(str(e))

    def handle_generation_stream(self, query):
        """SSE 端点：流式推送 AI 生成内容到前端"""
        project_id = query.get('id', [''])[0]
        if not project_id:
            self.send_error_response("缺少 project_id")
            return

        # 验证任务存在
        with tasks_lock:
            if project_id not in generating_tasks:
                self.send_error_response("项目不存在")
                return
            task = generating_tasks[project_id]
            stream_event = task.get('stream_event')

        # 设置 SSE 响应头
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'keep-alive')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('X-Accel-Buffering', 'no')
        self.end_headers()
        self.wfile.flush()  # 立即刷出响应头，确保浏览器建立 SSE 连接

        # 发送初始连接确认
        self._send_sse_data(json.dumps({'type': 'connected'}, ensure_ascii=False))
        logger.info(f"[SSE] 连接已建立: {project_id[:30]}...")

        last_chunk_index = 0

        try:
            while True:
                # 检查任务是否已结束
                with tasks_lock:
                    if project_id not in generating_tasks:
                        break
                    task = generating_tasks[project_id]
                    current_status = task.get('status')

                # 任务终态：发送剩余数据后关闭
                if current_status in (STATUS_COMPLETED, STATUS_FAILED, 'cancelled'):
                    with task.get('stream_lock', threading.Lock()):
                        remaining = task.get('stream_chunks', [])[last_chunk_index:]
                        for chunk in remaining:
                            self._send_smart_sse(chunk)
                        last_chunk_index = len(task.get('stream_chunks', []))

                    # 发送最终状态事件（含多轮元数据）
                    self._send_sse_event('status', json.dumps({
                        'status': current_status,
                        'error': task.get('error', ''),
                        'progress': task.get('progress', 0),
                        'round': task.get('round', 0),
                        'phase_description': task.get('phase_description', ''),
                        'page_progress': task.get('page_progress'),
                        'strategy': task.get('strategy', 'single'),
                    }, ensure_ascii=False))
                    break

                # 排空新数据块
                with task.get('stream_lock', threading.Lock()):
                    chunks = task.get('stream_chunks', [])
                    new_chunks = chunks[last_chunk_index:]
                    last_chunk_index = len(chunks)

                for chunk in new_chunks:
                    self._send_smart_sse(chunk)

                if new_chunks:
                    logger.debug(f"[SSE] 推送 {len(new_chunks)} 个数据块 (总 {last_chunk_index})")

                # 等待新数据（100ms 超时，避免忙等）
                if stream_event:
                    stream_event.wait(timeout=0.1)
                    stream_event.clear()

        except (ConnectionResetError, BrokenPipeError, OSError):
            pass  # 客户端断开连接
        except Exception as e:
            logger.error(f"[SSE 错误] {e}")
        finally:
            try:
                self.wfile.write(b'data: [DONE]\n\n')
                self.wfile.flush()
            except Exception:
                pass

    def _guess_sub_page(self, session, target_line=None):
        """根据行号推测属于哪个子页面。

        在组装后的 index.html 中，各子页面按 page_order 顺序排列。
        通过估算每个子页面的行数范围来推断目标页面。
        """
        if not target_line or not session.generated_html:
            return None

        assembled_lines = session.generated_html.split('\n')
        # 在组装的 HTML 中搜索子页面特征标记
        # 多页组装通常用注释标记页面边界
        page_markers = []
        for i, line in enumerate(assembled_lines):
            # 寻找页面分隔标记
            if 'page_' in line and (
                    'data-page' in line
                    or '@click' in line
                    or "navigateTo('page_" in line):
                for pn in session.page_order:
                    if pn in line:
                        page_markers.append((i + 1, pn))

        if page_markers:
            # 根据标记位置推断
            for idx, (marker_line, pname) in enumerate(page_markers):
                next_line = (page_markers[idx + 1][0]
                             if idx + 1 < len(page_markers)
                             else len(assembled_lines))
                if marker_line <= target_line <= next_line:
                    return pname

        # 回退：按行数比例估算
        total_lines = len(assembled_lines)
        if total_lines == 0:
            return None

        # 估算每个子页面的行数
        cumulative = 0
        for pname in session.page_order:
            if pname not in session.pages_html:
                continue
            page_lines = session.pages_html[pname].count('\n') + 1
            if cumulative + page_lines >= target_line:
                return pname
            cumulative += page_lines

        return list(session.page_order)[0] if session.page_order else None

    def _sync_edit_to_sub_pages(self, session, old_string, new_string):
        """将 index.html 的搜索替换编辑同步到子页面

        当 AI 直接编辑 index.html（组装页面）时，
        尝试将相同的 old_string → new_string 替换
        应用到包含该文本的子页面中，保持子页面同步。
        """
        for pname in session.page_order:
            if pname not in session.pages_html:
                continue
            sub_html = session.pages_html[pname]
            idx = sub_html.find(old_string)
            if idx != -1:
                # 只替换第一个匹配（与主编辑逻辑一致）
                session.pages_html[pname] = (
                    sub_html[:idx] + new_string
                    + sub_html[idx + len(old_string):]
                )
                logger.info(
                    f"[Agent] 同步编辑到子页面: {pname}")

    def _diff_snippet(self, old_text, new_text, max_lines=8):
        """提取 old/new 文本的前 N 行用于 diff 展示"""
        def _trim(text, n):
            lines = text.strip().splitlines()
            return '\n'.join(lines[:n])
        return _trim(old_text or '', max_lines), _trim(new_text or '', max_lines)

    def _diff_text(self, old_text, new_text, max_chars=15000):
        """返回完整 old/new 文本用于客户端 unified diff 渲染（带字符限制）"""
        def _clamp(text, limit):
            if not text:
                return ''
            text = text.strip()
            if len(text) > limit:
                text = text[:limit] + '\n... (truncated)'
            return text
        return _clamp(old_text, max_chars), _clamp(new_text, max_chars)

    def _get_tag_imbalances(self, html):
        """返回标签不平衡的详细信息。

        Returns:
            dict: {tag_name: {'open': count, 'close': count, 'diff': diff}}
            只包含不平衡的标签。
        """
        if not html or len(html) < 50:
            return {}

        low = html.lower()
        check_tags = ['div', 'table', 'ul', 'ol', 'section',
                      'main', 'nav', 'header', 'footer',
                      'aside', 'form']
        imbalances = {}
        for tag in check_tags:
            open_pattern = f'<{tag}[^a-z]'
            close_pattern = f'</{tag}'
            open_count = len(re.findall(open_pattern, low))
            close_count = low.count(close_pattern)
            diff = open_count - close_count
            if diff != 0:
                imbalances[tag] = {
                    'open': open_count,
                    'close': close_count,
                    'diff': diff,
                }
        return imbalances

    def _fix_page_data_script_escaping(self, html):
        """修复组装后 HTML 中 pageData 内未转义的 </script>。

        多页项目的 pageData 是 JavaScript 字符串字面量，位于 <script> 标签内。
        如果 AI 审查或编辑引入了未转义的 </script>，HTML 解析器会提前关闭
        script 块，导致 Vue 无法初始化。

        此函数定位 pageData {...} 区域，将其中的 </script> 转义为 <\\/script>。
        """
        if 'pageData' not in html:
            return html

        # 定位 pageData 对象的起始位置
        pd_marker = 'const pageData'
        pd_idx = html.find(pd_marker)
        if pd_idx < 0:
            # 尝试其他变体
            pd_marker = 'pageData = {'
            pd_idx = html.find(pd_marker)
        if pd_idx < 0:
            return html

        # 找到 pageData 的 { 开始
        brace_start = html.find('{', pd_idx)
        if brace_start < 0:
            return html

        # 找到 pageData 的 } 结束（匹配花括号）
        brace_count = 1
        pos = brace_start + 1
        while pos < len(html) and brace_count > 0:
            ch = html[pos]
            if ch == '{':
                brace_count += 1
            elif ch == '}':
                brace_count -= 1
            elif ch == "'":
                # 跳过字符串内容（避免字符串内的 { } 干扰计数）
                pos += 1
                while pos < len(html):
                    if html[pos] == '\\' and pos + 1 < len(html):
                        pos += 2  # 跳过转义字符
                        continue
                    if html[pos] == "'":
                        break
                    pos += 1
            pos += 1

        pd_end = pos  # } 之后的位置

        # 在 pageData 区域内修复未转义的 </script>
        # 注意: <\/script> (已转义) 不匹配 </script> (未转义)，
        # 所以 .replace 只会修复未转义的实例
        pd_section = html[brace_start:pd_end]
        fixed = pd_section.replace('</script>', '<\\/script>')

        if fixed != pd_section:
            result = html[:brace_start] + fixed + html[pd_end:]
            unesc = pd_section.count('</script>')
            esc = pd_section.count('<' + chr(92) + '/script>')
            logger.info(
                f"[转义修复] pageData 内 </script> 转义修复: "
                f"{unesc - esc} 处")
            return result
        return html

    def _quick_verify_html(self, html, pre_existing=None):
        """编辑后验证：检查 HTML 结构完整性。

        类似 Claude Code 的 build 验证：
        1. 基本闭合标签（html/body）
        2. 关键容器标签平衡（div/table/ul/ol/section/main/nav/header/footer）
        3. 没有明显的截断痕迹

        Args:
            html: 要验证的 HTML 字符串
            pre_existing: 编辑前已有的标签不平衡（dict），
                         用于区分编辑引入的新问题 vs 原已存在的问题。
                         如果某个标签的不平衡在编辑前就已存在且未恶化，
                         则不报告为错误。

        Returns:
            True 如果通过，否则返回错误描述字符串（含行号定位）
        """
        if not html or len(html) < 50:
            return "页面内容为空或过短"

        low = html.lower()

        # 检查基本闭合标签
        if '<html' in low and '</html>' not in low:
            return "缺少 </html> 闭合标签"
        if '<body' in low and '</body>' not in low:
            return "缺少 </body> 闭合标签"

        # 检查明显的截断（代码块未闭合）
        code_blocks = low.count('```')
        if code_blocks % 2 != 0:
            return "存在未闭合的 ``` 代码块"

        # 标签平衡检查：统计开标签和闭标签数量
        # 对于这些容器标签，开闭数量应该相等
        check_tags = ['div', 'table', 'ul', 'ol', 'section',
                      'main', 'nav', 'header', 'footer',
                      'aside', 'form']
        errors = []
        # 同时收集定位信息（行号），帮助 AI 精准修复
        location_hints = []
        lines = html.split('\n')

        for tag in check_tags:
            # 统计 <tag (开标签，排除 </tag 闭标签)
            open_pattern = f'<{tag}[^a-z]'
            close_pattern = f'</{tag}'
            open_count = len(re.findall(open_pattern, low))
            close_count = low.count(close_pattern)
            diff = open_count - close_count
            if diff != 0:
                # 如果编辑前就存在相同或更严重的不平衡，跳过
                if pre_existing and tag in pre_existing:
                    pre_diff = pre_existing[tag]['diff']
                    if abs(diff) <= abs(pre_diff):
                        # 不平衡没有恶化，视为编辑前遗留问题
                        continue

                errors.append(
                    f"<{tag}> 开 {open_count} 个 / "
                    f"闭 {close_count} 个，"
                    f"差 {diff}")
                # 定位前几个开/闭标签的行号
                tag_locs = []
                for li, line in enumerate(lines):
                    ll = line.lower()
                    if re.search(open_pattern, ll):
                        tag_locs.append(f"L{li+1}开")
                    if close_pattern in ll:
                        tag_locs.append(f"L{li+1}闭")
                    if len(tag_locs) >= 6:
                        break
                if tag_locs:
                    location_hints.append(
                        f"<{tag}> 位置: {', '.join(tag_locs)}")

        if errors:
            # 只报告前 3 个不平衡的标签
            msg = "标签不平衡: " + "; ".join(errors[:3])
            if location_hints:
                msg += "\n定位: " + "; ".join(location_hints[:3])
            return msg

        return True

    def _auto_fix_tag_imbalances(self, html, pre_existing=None):
        """自动修复简单的标签不平衡（缺失闭标签）。

        策略：对于「开多闭少」的标签，在 </body> 前自动补上缺失的闭标签。
        对于「闭多开少」的标签，移除多余的闭标签。
        修复后重新验证，通过则返回修复后的 HTML，否则返回原始 HTML。

        Args:
            html: 待修复的 HTML
            pre_existing: 编辑前已有的不平衡（排除已有问题）

        Returns:
            (fixed_html, did_fix) 元组
        """
        if not html:
            return html, False

        imbalances = self._get_tag_imbalances(html)
        if not imbalances:
            return html, False

        # 过滤掉编辑前已存在的相同/更严重的不平衡
        fixable = {}
        for tag, info in imbalances.items():
            if pre_existing and tag in pre_existing:
                if abs(info['diff']) <= abs(pre_existing[tag]['diff']):
                    continue  # 原有问题，不修
            fixable[tag] = info

        if not fixable:
            return html, False

        fixed = html
        lines = fixed.split('\n')

        # 找到 </body> 所在行，用于插入闭标签
        body_close_line = -1
        for i, line in enumerate(lines):
            if '</body>' in line.lower():
                body_close_line = i
                break

        insertions = []  # (line_index, text_to_insert)
        removals = []    # (line_index, text_to_remove_in_line)

        for tag, info in fixable.items():
            diff = info['diff']
            if diff > 0:
                # 开多闭少：补闭标签（最多补 3 个，防止异常）
                close_tag = f'</{tag}>'
                if body_close_line >= 0:
                    insertions.append(
                        (body_close_line, close_tag))
                else:
                    # 无 </body>，追加到末尾
                    insertions.append(
                        (len(lines), close_tag))
            elif diff < 0:
                # 闭多开少：移除多余的闭标签（最多移 3 个）
                count = min(-diff, 3)
                close_pattern = f'</{tag}>'
                removed = 0
                for i in range(len(lines) - 1, -1, -1):
                    if removed >= count:
                        break
                    ll = lines[i].lower()
                    if close_pattern in ll:
                        removals.append((i, close_pattern))
                        removed += 1

        if not insertions and not removals:
            return html, False

        # 应用修复（从后往前处理行号，避免偏移）
        lines = fixed.split('\n')

        # 先处理移除（从后往前）
        for line_idx, close_tag in sorted(removals, reverse=True):
            lines[line_idx] = re.sub(
                re.escape(close_tag), '', lines[line_idx],
                count=1, flags=re.IGNORECASE)

        # 再处理插入（从后往前）
        for line_idx, close_tag in sorted(insertions, reverse=True):
            if line_idx < len(lines):
                lines[line_idx] = close_tag + '\n' + lines[line_idx]
            else:
                lines.append(close_tag)

        fixed = '\n'.join(lines)

        # 验证修复结果
        verify = self._quick_verify_html(fixed, pre_existing=pre_existing)
        if verify is True:
            logger.info(
                f"[Agent] 自动修复标签不平衡成功: "
                + ", ".join(
                    f"<{t}> 差{info['diff']}"
                    for t, info in fixable.items()))
            return fixed, True
        else:
            logger.warning(
                f"[Agent] 自动修复后仍不通过: {verify}，放弃自动修复")
            return html, False

    def _verify_page_in_browser(self, html_path, server_port,
                                  pre_existing=None):
        """在无头浏览器中打开页面，捕获控制台错误。

        可选功能：需要 pip install playwright && playwright install chromium
        如果 Playwright 未安装则跳过，不影响正常流程。

        Args:
            html_path: HTML 文件路径
            server_port: 服务器端口
            pre_existing: 编辑前已有的控制台错误列表，
                         这些错误将被过滤掉，不报告为编辑引入的问题。

        Returns:
            True 如果通过或未安装 Playwright
            错误描述字符串 如果检测到新的控制台报错
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return True  # Playwright 未安装，跳过

        # 构建页面 URL
        project_name = os.path.basename(os.path.dirname(html_path))
        url = (f'http://localhost:{server_port}'
               f'/projects/{project_name}/index.html')

        console_errors = []
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()

                # 捕获页面 JS 错误
                page.on('pageerror',
                        lambda err: console_errors.append(
                            f'JS Error: {err}'))
                # 捕获控制台错误
                page.on('console',
                        lambda msg: console_errors.append(
                            f'Console {msg.type}: {msg.text}')
                        if msg.type == 'error' else None)

                page.goto(url, wait_until='networkidle',
                          timeout=15000)
                browser.close()
        except Exception as e:
            logger.warning(f"[验证] 浏览器验证异常: {e}")
            return True  # 浏览器启动失败，跳过

        if console_errors:
            # 过滤编辑前已存在的错误
            if pre_existing:
                new_errors = [err for err in console_errors
                              if err not in pre_existing]
                if not new_errors:
                    logger.info(
                        f"[验证] {len(console_errors)} 个控制台错误"
                        "均为编辑前已有，跳过")
                    return True
                if len(new_errors) < len(console_errors):
                    logger.info(
                        f"[验证] 过滤 {len(console_errors) - len(new_errors)}"
                        f" 个原有错误，剩余 {len(new_errors)} 个新错误")
                console_errors = new_errors

            return ("浏览器控制台报错:\n"
                    + "\n".join(console_errors[:5]))
        return True

    def _capture_browser_console_errors(self, html_path, server_port):
        """捕获页面的控制台错误列表（用于建立基线）。

        Returns:
            list: 控制台错误字符串列表
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return []

        project_name = os.path.basename(os.path.dirname(html_path))
        url = (f'http://localhost:{server_port}'
               f'/projects/{project_name}/index.html')

        console_errors = []
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.on('pageerror',
                        lambda err: console_errors.append(
                            f'JS Error: {err}'))
                page.on('console',
                        lambda msg: console_errors.append(
                            f'Console {msg.type}: {msg.text}')
                        if msg.type == 'error' else None)
                page.goto(url, wait_until='networkidle',
                          timeout=15000)
                browser.close()
        except Exception:
            return []

        return console_errors

    def _build_pages_summary(self, session):
        """生成页面结构摘要（替代完整 HTML 注入，节省 ~80% tokens）

        只发送页面骨架：HTML 标签树 + Vue 组件结构 + 样式变量。
        AI 通过 read_page 工具按需获取完整内容。
        """
        if not session.pages_html and not session.generated_html:
            return None

        pages = session.pages_html or {}
        if not pages and session.generated_html:
            pages = {'主页面': session.generated_html}

        # 多页项目: 添加行号映射帮助 AI 定位错误
        page_line_map = ''
        if len(session.page_order) > 1 and session.generated_html:
            assembled_lines = session.generated_html.split('\n')
            total_assembled = len(assembled_lines)
            # 估算每个子页面在组装文件中的行号范围
            cumulative = 1
            page_ranges = []
            for pn in session.page_order:
                if pn not in pages:
                    continue
                page_lines = pages[pn].count('\n') + 1
                end = min(cumulative + page_lines - 1, total_assembled)
                page_ranges.append(
                    f"  L{cumulative}-L{end}: \"{pn}\"")
                cumulative = end + 1
            if page_ranges:
                page_line_map = (
                    f"\n### 组装文件 index.html 行号映射\n"
                    f"index.html 共 {total_assembled} 行，"
                    f"由以下页面按顺序组装：\n"
                    + '\n'.join(page_ranges)
                    + "\n当用户报告 'index.html:行号' 错误时，"
                    "请根据此映射读取对应的子页面。\n"
                )

        # 根据页面大小决定读取策略提示
        max_page_chars = max(
            len(p) for p in pages.values()) if pages else 0
        if max_page_chars > 50000:
            read_hint = (
                "页面较大，请使用 read_page(start_line, end_line) "
                "分段读取需要修改的区域，"
                "不要一次读取全文（会超出上下文限制）。\n"
                "参考上方行号定位，只读取目标区域即可。\n"
            )
        else:
            read_hint = (
                "编辑前请调用 read_page 工具读取完整页面代码，"
                "然后用 edit_file 进行修改。\n"
            )

        parts = [
            "以下是当前项目的页面结构摘要。",
            read_hint,
            page_line_map
        ]

        for page_name, page_html in pages.items():
            lines = page_html.split('\n')
            total_lines = len(lines)
            total_chars = len(page_html)

            # 提取关键结构信息
            summary = f"\n## 页面: {page_name} ({total_lines} 行, {total_chars:,} 字符)\n"

            # 提取 HTML 结构骨架（标签层级，不含文本内容）
            # 保留: <div id/class=...>, <section>, <header>, <nav>, <main>,
            #        <template>, <script>, <style>, Vue 组件
            structure_lines = []
            indent_stack = [0]
            for i, line in enumerate(lines):
                stripped = line.strip()
                if not stripped:
                    continue

                # 保留带 id 或 class 的标签
                if re.match(r'<\w+[^>]*(id|class)\s*=', stripped):
                    # 简化：只保留标签开头
                    tag_match = re.match(r'(<\w+[^>]*(?:id|class)\s*=\s*["\'][^"\']*["\'])', stripped)
                    if tag_match:
                        indent = len(line) - len(line.lstrip())
                        structure_lines.append(f"  L{i+1}: {tag_match.group(1)}>")
                    continue

                # 保留关键标签
                if re.match(r'<(/?)(template|script|style|section|header|nav|main|footer|form|table|dialog|modal|button|svg)', stripped):
                    indent = len(line) - len(line.lstrip())
                    tag_match = re.match(r'<[^>]+>', stripped)
                    if tag_match:
                        structure_lines.append(f"  L{i+1}: {tag_match.group(0)}")
                    continue

                # 保留 Vue 指令
                if 'v-if' in stripped or 'v-for' in stripped or 'v-model' in stripped or '@click' in stripped:
                    tag_match = re.match(r'<[^>]+>', stripped)
                    if tag_match and len(tag_match.group(0)) < 200:
                        structure_lines.append(f"  L{i+1}: {tag_match.group(0)}")

            # 限制结构行数：大页面给更多结构信息，减少探索性读取
            max_struct_lines = 120 if total_lines > 500 else 60
            if len(structure_lines) > max_struct_lines:
                structure_lines = structure_lines[:max_struct_lines]
                structure_lines.append(f"  ... ({total_lines - max_struct_lines} more lines)")

            summary += '\n'.join(structure_lines)

            # 提取 Vue setup 中的 ref/reactive 声明
            setup_vars = re.findall(
                r'(?:const|let|var)\s+(\w+)\s*=\s*(?:ref\(|reactive\(|computed\()', page_html)
            if setup_vars:
                summary += f"\n\nVue 响应式变量: {', '.join(setup_vars[:30])}"

            # 提取 return {} 中的暴露变量
            return_match = re.search(r'return\s*\{([^}]+)\}', page_html, re.DOTALL)
            if return_match:
                return_vars = [v.strip().split(':')[0].split(',')[0].strip()
                              for v in return_match.group(1).split('\n')
                              if v.strip() and not v.strip().startswith('//')]
                return_vars = [v for v in return_vars if v and re.match(r'^\w+$', v)]
                if return_vars:
                    summary += f"\nreturn 暴露: {', '.join(return_vars[:30])}"

            parts.append(summary)

        return '\n'.join(parts)

    def _send_sse_data(self, data):
        """发送 SSE data 行"""
        self.wfile.write(f'data: {data}\n\n'.encode('utf-8'))
        self.wfile.flush()

    def _send_smart_sse(self, chunk):
        """智能 SSE 发送：区分原始文本和结构化事件

        - 如果 chunk 是结构化 JSON 事件（含 type 字段），直接透传
        - 否则包装为原有 {content: ...} 格式
        """
        # 尝试检测结构化事件
        is_structured = False
        try:
            if chunk.startswith('{') and '"type"' in chunk[:100]:
                parsed = json.loads(chunk)
                if 'type' in parsed and 'data' in parsed:
                    is_structured = True
        except (json.JSONDecodeError, ValueError):
            pass

        if is_structured:
            # 结构化事件直接透传
            self._send_sse_data(chunk)
        else:
            # 原始文本包装为原有格式
            self._send_sse_data(json.dumps({'content': chunk}, ensure_ascii=False))

    def _send_sse_event(self, event_type, data):
        """发送 SSE 命名事件"""
        self.wfile.write(f'event: {event_type}\ndata: {data}\n\n'.encode('utf-8'))
        self.wfile.flush()

    def handle_create_placeholder(self):
        """创建占位项目（不调用AI，用于复制Prompt功能）"""
        try:
            content_length = int(self.headers['Content-Length'])
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))
            
            project_id = data.get('projectId', '')
            project_name = data.get('projectName', '未命名项目')
            form_data = data.get('formData', {})
            image_files = data.get('imageFiles', {})  # {pageIndex: [base64...]}
            
            if not project_id:
                self.send_error_response("缺少projectId")
                return
            
            logger.info(f"[占位] 创建项目: {project_name} ({project_id})")
            
            # 创建项目文件夹
            project_folder = os.path.join(PROJECTS_DIR, project_id)
            os.makedirs(project_folder, exist_ok=True)
            
            # 保存参考图片
            ref_images_folder = os.path.join(project_folder, 'reference')
            os.makedirs(ref_images_folder, exist_ok=True)
            
            saved_image_names = []
            for page_index_str, images in image_files.items():
                for i, img_base64 in enumerate(images):
                    filename = f"ref_{page_index_str}_{i+1}"
                    saved = save_base64_image(img_base64, ref_images_folder, filename)
                    if saved:
                        saved_image_names.append(saved)
            
            # 构建record.json
            record = {
                'global': form_data.get('global', {}),
                'pages': form_data.get('pages', []),
                'status': 'pending_external',
                'createdAt': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            
            record_path = os.path.join(project_folder, 'record.json')
            with open(record_path, 'w', encoding='utf-8') as f:
                json.dump(record, f, ensure_ascii=False, indent=2)
            
            # 获取当前选中的模型名称
            current_model = get_selected_model()
            current_model_name = current_model.get('name', '') if current_model else ''
            
            # 更新项目列表
            projects = self.load_projects()
            new_project = {
                'id': project_id,
                'name': project_name + ' (待外部生成)',
                'model_name': current_model_name,
                'status': 'pending_external',
                'url': f'/projects/{project_id}/record.json',  # 暂无HTML
                'date': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            projects.insert(0, new_project)
            self.save_projects(projects)
            
            logger.info(f"[完成] 占位项目已创建: {project_folder}")
            self.send_json_response({'success': True, 'project': new_project})
            
        except Exception as e:
            logger.error(f"[错误] 创建占位项目失败: {e}")
            import traceback
            traceback.print_exc()
            self.send_error_response(str(e))

    # ==================== GitHub 列表页生成 ====================

    def generate_index_html(self, manifest, username, repo):
        """生成 projects/index.html 列表页 HTML"""
        mode_labels = {'preview': '纯净版', 'dev': '研发版', 'embedded': '内嵌版'}
        mode_colors = {'preview': '#3b82f6', 'dev': '#8b5cf6', 'embedded': '#f59e0b'}

        cards_html = ''
        sorted_items = sorted(manifest, key=lambda x: x.get('publishedAt', ''), reverse=True)
        for item in sorted_items:
            name = item.get('name', '未命名项目')
            url = item.get('url', '#')
            published_at = item.get('publishedAt', '')[:10]
            mode = item.get('mode', 'preview')
            mode_label = mode_labels.get(mode, mode)
            mode_color = mode_colors.get(mode, '#6b7280')
            cards_html += f'''
            <div class="card" onclick="window.open('{url}','_blank')">
                <div class="card-header">
                    <div class="project-icon">🎨</div>
                </div>
                <div class="card-body">
                    <h3 class="project-name" title="{name}">{name}</h3>
                    <div class="project-meta">
                        <span class="mode-badge" style="background:{mode_color}20;color:{mode_color};border-color:{mode_color}40">{mode_label}</span>
                        <span class="published-date">{published_at}</span>
                    </div>
                </div>
                <div class="card-footer">
                    <button class="btn-open" onclick="event.stopPropagation();window.open('{url}','_blank')">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 13v6a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
                        打开
                    </button>
                    <button class="btn-copy" onclick="event.stopPropagation();copyLink('{url}',this)">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>
                        复制链接
                    </button>
                </div>
            </div>'''

        count = len(manifest)
        pages_root = f'https://{username}.github.io/{repo}'
        return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>原型作品库 · {username}</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f0f13;color:#e2e8f0;min-height:100vh}}
  .header{{background:linear-gradient(135deg,#1e1b4b 0%,#312e81 50%,#1e1b4b 100%);padding:48px 24px 40px;text-align:center;border-bottom:1px solid #ffffff12}}
  .header-icon{{font-size:40px;margin-bottom:12px}}
  .header h1{{font-size:28px;font-weight:700;color:#fff;margin-bottom:6px;letter-spacing:-0.5px}}
  .header p{{color:#a5b4fc;font-size:14px}}
  .header .count{{display:inline-block;background:#4f46e5;color:#fff;font-size:12px;font-weight:600;padding:3px 10px;border-radius:20px;margin-top:10px}}
  .container{{max-width:1100px;margin:0 auto;padding:32px 24px}}
  .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:20px}}
  .card{{background:#1a1a24;border:1px solid #ffffff0f;border-radius:16px;overflow:hidden;cursor:pointer;transition:all .2s;display:flex;flex-direction:column}}
  .card:hover{{border-color:#6366f140;transform:translateY(-3px);box-shadow:0 12px 40px #6366f120}}
  .card-header{{background:linear-gradient(135deg,#1e1b4b,#312e81);padding:28px 20px;display:flex;align-items:center;justify-content:center}}
  .project-icon{{font-size:36px;filter:drop-shadow(0 4px 8px rgba(0,0,0,.3))}}
  .card-body{{padding:16px 18px;flex:1}}
  .project-name{{font-size:15px;font-weight:600;color:#f1f5f9;margin-bottom:8px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
  .project-meta{{display:flex;align-items:center;gap:8px}}
  .mode-badge{{font-size:11px;font-weight:500;padding:2px 8px;border-radius:6px;border:1px solid;flex-shrink:0}}
  .published-date{{font-size:12px;color:#64748b}}
  .card-footer{{padding:12px 18px;border-top:1px solid #ffffff08;display:flex;gap:8px}}
  .btn-open,.btn-copy{{flex:1;display:flex;align-items:center;justify-content:center;gap:6px;padding:8px;border-radius:8px;font-size:12px;font-weight:500;border:none;cursor:pointer;transition:all .15s}}
  .btn-open{{background:#4f46e5;color:#fff}}.btn-open:hover{{background:#4338ca}}
  .btn-copy{{background:#ffffff0a;color:#94a3b8;border:1px solid #ffffff12}}.btn-copy:hover{{background:#ffffff14;color:#fff}}
  .btn-copy.copied{{background:#059669;color:#fff;border-color:#059669}}
  .empty{{text-align:center;padding:80px 24px;color:#475569}}
  .empty-icon{{font-size:48px;margin-bottom:16px}}
  .footer{{text-align:center;padding:32px;color:#334155;font-size:12px;border-top:1px solid #ffffff08;margin-top:32px}}
  .footer a{{color:#6366f1;text-decoration:none}}
</style>
</head>
<body>
<div class="header">
  <div class="header-icon">🎨</div>
  <h1>原型作品库</h1>
  <p>{username} · {repo}</p>
  <span class="count">共 {count} 个原型</span>
</div>
<div class="container">
  {'<div class="grid">' + cards_html + '</div>' if manifest else '<div class="empty"><div class="empty-icon">📭</div><p>暂无已发布的原型</p></div>'}
</div>
<div class="footer">由 <a href="{pages_root}" target="_blank">AI 原型生成器</a> 发布 · GitHub Pages 托管</div>
<script>
function copyLink(url, btn) {{
  navigator.clipboard.writeText(url).then(() => {{
    const orig = btn.innerHTML;
    btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 12 4 10"/></svg> 已复制';
    btn.classList.add('copied');
    setTimeout(() => {{ btn.innerHTML = orig; btn.classList.remove('copied'); }}, 2000);
  }});
}}
</script>
</body>
</html>'''

    def update_github_listing(self, session, api_base, username, repo, default_branch,
                              project_id, project_name, project_url, mode, remove=False):
        """更新 GitHub 上的 projects/manifest.json 和 projects/index.html"""
        manifest_api_url = f"{api_base}/repos/{username}/{repo}/contents/projects/manifest.json"

        # 读取现有 manifest
        existing = session.get(manifest_api_url, timeout=15)
        manifest = []
        manifest_sha = None
        if existing.status_code == 200:
            import base64 as b64
            raw = b64.b64decode(existing.json().get('content', '')).decode('utf-8')
            try:
                manifest = json.loads(raw)
            except Exception:
                manifest = []
            manifest_sha = existing.json().get('sha', '')

        if remove:
            # 移除项目
            manifest = [m for m in manifest if m.get('id') != project_id]
        else:
            # 更新或添加项目
            found = False
            for m in manifest:
                if m.get('id') == project_id:
                    m['name'] = project_name
                    m['url'] = project_url
                    m['mode'] = mode
                    m['publishedAt'] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    found = True
                    break
            if not found:
                manifest.append({
                    'id': project_id,
                    'name': project_name,
                    'url': project_url,
                    'mode': mode,
                    'publishedAt': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                })

        # 上传 manifest.json
        manifest_b64 = base64.b64encode(json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8')).decode()
        manifest_payload = {
            'message': 'Update projects manifest',
            'content': manifest_b64,
            'branch': default_branch
        }
        if manifest_sha:
            manifest_payload['sha'] = manifest_sha
        session.put(manifest_api_url, json=manifest_payload, timeout=20)
        logger.info(f"[GitHub] manifest.json 已更新 ({len(manifest)} 个项目)")

        # 生成并上传 projects/index.html
        index_html = self.generate_index_html(manifest, username, repo)
        index_api_url = f"{api_base}/repos/{username}/{repo}/contents/projects/index.html"
        existing_index = session.get(index_api_url, timeout=10)
        index_payload = {
            'message': 'Update projects listing page',
            'content': base64.b64encode(index_html.encode('utf-8')).decode(),
            'branch': default_branch
        }
        if existing_index.status_code == 200:
            index_payload['sha'] = existing_index.json().get('sha', '')
        session.put(index_api_url, json=index_payload, timeout=30)
        logger.info(f"[GitHub] projects/index.html 已更新")

    # ==================== 取消发布 API ====================

    def handle_github_unpublish(self):
        """取消发布：删除 GitHub 上的项目文件，更新列表页"""
        try:
            content_length = int(self.headers['Content-Length'])
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))

            project_id = data.get('projectId', '')
            if not project_id:
                self.send_error_response("缺少 projectId")
                return

            config = load_config()
            gh = config.get('github', {})
            token = gh.get('token', '')
            username = gh.get('username', '')
            repo = gh.get('repo', 'my-prototypes')

            if not token or not username:
                self.send_error_response("请先配置 GitHub Token")
                return

            from requests.adapters import HTTPAdapter
            from urllib3.util.retry import Retry
            retry_strategy = Retry(total=3, backoff_factor=1, connect=3, read=3,
                                   status_forcelist=[500, 502, 503, 504],
                                   allowed_methods=["GET", "PUT", "POST", "DELETE"])
            session = requests.Session()
            session.mount('https://', HTTPAdapter(max_retries=retry_strategy))
            session.headers.update({
                'Authorization': f'token {token}',
                'Accept': 'application/vnd.github.v3+json',
                'Content-Type': 'application/json'
            })

            api_base = 'https://api.github.com'

            # 获取默认分支
            repo_info = session.get(f"{api_base}/repos/{username}/{repo}", timeout=15).json()
            default_branch = repo_info.get('default_branch', 'main')

            # 列出 projects/{id}/ 下的所有文件并逐一删除
            logger.info(f"[GitHub] 取消发布: {project_id}")
            folder_url = f"{api_base}/repos/{username}/{repo}/contents/projects/{project_id}"
            files_resp = session.get(folder_url, timeout=15)
            if files_resp.status_code == 200:
                files = files_resp.json()
                # 如果有子目录（如 images/），需要递归列出
                all_files = []
                for f in files:
                    if f.get('type') == 'file':
                        all_files.append(f)
                    elif f.get('type') == 'dir':
                        sub_resp = session.get(f['url'], timeout=15)
                        if sub_resp.status_code == 200:
                            all_files.extend([sf for sf in sub_resp.json() if sf.get('type') == 'file'])

                for f in all_files:
                    del_resp = session.delete(f['url'], json={
                        'message': f'Remove prototype: {project_id}',
                        'sha': f['sha'],
                        'branch': default_branch
                    }, timeout=20)
                    if del_resp.status_code in (200, 201):
                        logger.info(f"[GitHub] 已删除: {f['path']}")
                    else:
                        logger.info(f"[GitHub] 删除失败: {f['path']} ({del_resp.status_code})")

            # 更新列表页（传 remove=True）
            self.update_github_listing(
                session, api_base, username, repo, default_branch,
                project_id=project_id, project_name='', project_url='', mode='', remove=True
            )

            # 清除本地 record.json 中的 github_url
            project_dir = os.path.join(PROJECTS_DIR, project_id)
            record_path = os.path.join(project_dir, 'record.json')
            if os.path.exists(record_path):
                with open(record_path, 'r', encoding='utf-8') as f:
                    record = json.load(f)
                record.pop('github_url', None)
                record.pop('github_published_at', None)
                record.pop('github_mode', None)
                with open(record_path, 'w', encoding='utf-8') as f:
                    json.dump(record, f, ensure_ascii=False, indent=2)
                logger.info(f"[GitHub] 本地 record.json 已清除 github_url")

            self.send_json_response({'success': True, 'message': '已取消发布，GitHub 文件已删除'})

        except Exception as e:
            logger.error(f"[错误] 取消发布失败: {e}")
            import traceback
            traceback.print_exc()
            self.send_error_response(f"取消发布失败: {str(e)}")

    # ==================== 画布布局 API ====================

    def handle_get_canvas_layout(self, query):
        """GET /api/canvas-layout?projectId=xxx"""
        try:
            project_id = query.get('projectId', [''])[0]
            if not project_id:
                self.send_json_response({'positions': {}, 'connections': [], 'viewport': {}})
                return

            layout_path = os.path.join(PROJECTS_DIR, project_id, 'canvas_layout.json')
            if os.path.exists(layout_path):
                with open(layout_path, 'r', encoding='utf-8') as f:
                    self.send_json_response(json.load(f))
            else:
                self.send_json_response({'positions': {}, 'connections': [], 'viewport': {}})
        except Exception as e:
            self.send_json_response({'positions': {}, 'connections': [], 'viewport': {}})

    def handle_save_canvas_layout(self):
        """POST /api/canvas-layout"""
        try:
            content_length = int(self.headers['Content-Length'])
            body = json.loads(self.rfile.read(content_length).decode('utf-8'))
            project_id = body.get('projectId', '')
            layout = body.get('layout', {})

            if not project_id:
                self.send_error_response('缺少 projectId')
                return

            layout_path = os.path.join(PROJECTS_DIR, project_id, 'canvas_layout.json')
            with open(layout_path, 'w', encoding='utf-8') as f:
                json.dump(layout, f, ensure_ascii=False, indent=2)

            self.send_json_response({'success': True})
        except Exception as e:
            self.send_error_response(str(e))

    # ==================== 导出 API ====================

    def handle_export(self):
        """触发本地导出，支持四种模式: preview / embedded / dev / figma"""
        try:
            content_length = int(self.headers['Content-Length'])
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))

            project_id = data.get('projectId', '')
            mode = data.get('mode', 'preview')  # preview | embedded | dev | figma

            if not project_id:
                self.send_error_response("缺少 projectId")
                return

            project_dir = os.path.join(PROJECTS_DIR, project_id)
            if not os.path.exists(project_dir):
                self.send_error_response(f"项目不存在: {project_id}")
                return

            logger.info(f"[导出] 项目: {project_id}, 模式: {mode}")

            # 动态导入 export_project 模块
            import importlib.util
            ep_path = os.path.join(get_base_path(), 'export_project.py')
            spec = importlib.util.spec_from_file_location("export_project", ep_path)
            ep = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(ep)

            export_path = ep.export_project(project_id, mode=mode)

            self.send_json_response({
                'success': True,
                'downloadUrl': f'/api/download-export?project={project_id}&mode={mode}'
            })

        except Exception as e:
            logger.error(f"[错误] 导出失败: {e}")
            import traceback
            traceback.print_exc()
            self.send_error_response(str(e))

    def handle_download_export(self):
        """下载导出的文件（支持单文件和目录打包为 ZIP）"""
        try:
            from urllib.parse import urlparse, parse_qs
            query = parse_qs(urlparse(self.path).query)
            project_id = query.get('project', [''])[0]
            mode = query.get('mode', ['preview'])[0]

            logger.info(f"[下载导出] project_id={project_id}, mode={mode}")

            if not project_id:
                self.send_error_response("缺少 project 参数")
                return

            # 构建导出路径（根据 export_project.py 的实际输出规则）
            export_base = os.path.join(get_base_path(), 'exports')

            if mode == 'preview':
                export_path = os.path.join(export_base, f'{project_id}_预览版')
                download_filename = f"{project_id}_preview.zip"
            elif mode == 'embedded':
                export_path = os.path.join(export_base, f'{project_id}_内嵌版')
                download_filename = f"{project_id}_embedded.zip"
            elif mode == 'figma':
                export_path = os.path.join(export_base, 'figma', f'{project_id}.json')
                download_filename = f"{project_id}_figma.json"
            else:  # dev mode
                export_path = os.path.join(export_base, project_id)
                download_filename = f"{project_id}_dev.zip"

            logger.info(f"[下载导出] export_path={export_path}")

            if not os.path.exists(export_path):
                self.send_error_response(f"导出文件不存在")
                return

            # 判断是文件还是目录
            is_directory = os.path.isdir(export_path)

            if is_directory:
                # 目录模式：打包成 ZIP
                import zipfile
                import io

                logger.info(f"[下载导出] 打包目录为 ZIP...")
                zip_buffer = io.BytesIO()
                with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zipf:
                    for root, dirs, files in os.walk(export_path):
                        for file in files:
                            file_path = os.path.join(root, file)
                            arcname = os.path.relpath(file_path, os.path.dirname(export_path))
                            zipf.write(file_path, arcname)

                zip_buffer.seek(0)
                content = zip_buffer.read()
                content_type = 'application/zip'
            else:
                # 单文件模式：直接读取
                with open(export_path, 'rb') as f:
                    content = f.read()

                # 根据 mode 确定文件类型
                if mode == 'figma':
                    content_type = 'application/json'
                else:
                    content_type = 'text/html; charset=utf-8'

            # 发送文件响应
            self.send_response(200)
            self.send_header('Content-Type', content_type)

            # 文件名编码：处理中文等非 ASCII 字符
            from urllib.parse import quote
            encoded_filename = quote(download_filename, safe='')
            # 使用 RFC 5987 格式：filename*=utf-8''<url-encoded-filename>
            disposition = f"attachment; filename*=utf-8''{encoded_filename}"
            self.send_header('Content-Disposition', disposition)

            self.send_header('Content-Length', str(len(content)))
            self.end_headers()

            self.wfile.write(content)
            logger.info(f"[下载导出] 完成，文件大小: {len(content)} 字节")

        except Exception as e:
            logger.error(f"[错误] 下载导出文件失败: {e}")
            import traceback
            traceback.print_exc()
            self.send_error_response(str(e))

        except Exception as e:
            logger.error(f"[错误] 下载导出文件失败: {e}")
            import traceback
            traceback.print_exc()
            self.send_error_response(str(e))

    # ==================== GitHub 配置 API ====================

    def handle_github_config_get(self):
        """读取 GitHub 配置（Token 打码）"""
        try:
            config = load_config()
            gh = config.get('github', {'token': '', 'username': '', 'repo': 'my-prototypes'})
            token = gh.get('token', '')
            # 打码显示
            masked_token = (token[:6] + '****' + token[-4:]) if len(token) > 10 else ('****' if token else '')
            self.send_json_response({
                'success': True,
                'username': gh.get('username', ''),
                'repo': gh.get('repo', 'my-prototypes'),
                'tokenMasked': masked_token,
                'hasToken': bool(token)
            })
        except Exception as e:
            self.send_error_response(str(e))

    def handle_github_config_save(self):
        """保存 GitHub 配置到 config.json"""
        try:
            content_length = int(self.headers['Content-Length'])
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))

            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)

            if 'github' not in config:
                config['github'] = {}

            # 只更新非空字段（Token 若用户没改则保持旧值）
            if data.get('token'):
                config['github']['token'] = data['token']
            if 'username' in data:
                config['github']['username'] = data['username']
            if 'repo' in data:
                config['github']['repo'] = data['repo'] or 'my-prototypes'

            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(config, f, ensure_ascii=False, indent=4)

            logger.info(f"[GitHub] 配置已保存: {config['github']['username']}/{config['github']['repo']}")
            self.send_json_response({'success': True, 'message': '配置已保存'})

        except Exception as e:
            self.send_error_response(str(e))

    def handle_github_test(self):
        """验证 GitHub Token 有效性"""
        try:
            content_length = int(self.headers['Content-Length'])
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))

            token = data.get('token', '')
            if not token:
                # 从配置读取
                config = load_config()
                token = config.get('github', {}).get('token', '')

            if not token:
                self.send_error_response("请先填写 Personal Access Token")
                return

            resp = requests.get(
                'https://api.github.com/user',
                headers={
                    'Authorization': f'token {token}',
                    'Accept': 'application/vnd.github.v3+json'
                },
                timeout=15
            )

            if resp.status_code == 200:
                user = resp.json()
                self.send_json_response({
                    'success': True,
                    'username': user.get('login', ''),
                    'name': user.get('name', ''),
                    'message': f"✅ 验证通过，用户：{user.get('login', '')}"
                })
            else:
                self.send_json_response({
                    'success': False,
                    'message': f"Token 无效（HTTP {resp.status_code}）"
                })

        except Exception as e:
            self.send_error_response(f"连接失败：{str(e)}")

    # ==================== GitHub 发布 API ====================

    def handle_github_publish(self):
        """将项目发布到 GitHub Pages"""
        try:
            content_length = int(self.headers['Content-Length'])
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))

            project_id = data.get('projectId', '')
            mode = data.get('mode', 'preview')  # 导出模式: dev / preview / embedded
            if not project_id:
                self.send_error_response("缺少 projectId")
                return

            config = load_config()
            gh = config.get('github', {})
            token = gh.get('token', '')
            username = gh.get('username', '')
            repo = gh.get('repo', 'my-prototypes')

            if not token or not username:
                self.send_error_response("请先在设置中配置 GitHub Token 和用户名")
                return

            project_dir = os.path.join(PROJECTS_DIR, project_id)
            if not os.path.exists(project_dir):
                self.send_error_response(f"项目不存在: {project_id}")
                return

            api_base = 'https://api.github.com'
            
            # 用带自动重试的 Session，解决连接池里的"僵尸连接"被 GitHub 关闭后引发的 ConnectionResetError
            from requests.adapters import HTTPAdapter
            from urllib3.util.retry import Retry
            retry_strategy = Retry(
                total=3,          # 最多重试 3 次
                backoff_factor=1, # 重试间隔: 0s, 1s, 2s
                connect=3,        # 连接失败（ConnectionResetError）也重试
                read=3,
                status_forcelist=[500, 502, 503, 504],
                allowed_methods=["GET", "PUT", "POST", "DELETE"]
            )
            adapter = HTTPAdapter(max_retries=retry_strategy)
            session = requests.Session()
            session.mount('https://', adapter)
            session.headers.update({
                'Authorization': f'token {token}',
                'Accept': 'application/vnd.github.v3+json',
                'Content-Type': 'application/json'
            })

            logger.info(f"[GitHub] 开始发布项目 '{project_id}' 到仓库 '{username}/{repo}'")

            # ---- 1. 检查/创建仓库 ----
            logger.info(f"[GitHub] 步骤 1/7: 检查或创建 GitHub 仓库 '{username}/{repo}'...")
            repo_url = f"{api_base}/repos/{username}/{repo}"
            try:
                r = session.get(repo_url, timeout=15)
                if r.status_code == 404:
                    logger.info(f"[GitHub] 仓库 '{repo}' 不存在，尝试创建...")
                    create_resp = session.post(
                        f"{api_base}/user/repos",
                        json={'name': repo, 'private': False, 'auto_init': True},
                        timeout=20
                    )
                    if create_resp.status_code not in (200, 201):
                        raise Exception(f"创建仓库失败: {create_resp.json().get('message', create_resp.text)}")
                    logger.info(f"[GitHub] 仓库 '{repo}' 已成功创建。")
                    import time
                    time.sleep(2)  # 等待仓库初始化
                elif r.status_code != 200:
                    raise Exception(f"访问仓库失败（HTTP {r.status_code}）: {r.json().get('message', r.text)}")
                else:
                    logger.info(f"[GitHub] 仓库 '{repo}' 已存在。")
            except requests.exceptions.RequestException as req_e:
                raise Exception(f"连接 GitHub API 失败（检查网络或Token）: {req_e}")

            # ---- 2. 获取默认分支 ----
            logger.info(f"[GitHub] 步骤 2/7: 获取仓库默认分支...")
            repo_info = session.get(repo_url, timeout=15).json()
            default_branch = repo_info.get('default_branch', 'main')
            logger.info(f"[GitHub] 默认分支为: '{default_branch}'。")

            # ---- 3. 确保 index.html 根文件存在（GitHub Pages 需要） ----
            logger.info(f"[GitHub] 步骤 3/7: 检查并创建根目录重定向文件 'index.html'...")
            root_index_path = f"{api_base}/repos/{username}/{repo}/contents/index.html"
            r_root = session.get(root_index_path, timeout=10)
            if r_root.status_code == 404:
                root_content = base64.b64encode(b'<meta http-equiv="refresh" content="0;url=projects/">').decode()
                put_resp = session.put(root_index_path, json={
                    'message': 'Add root redirect for GitHub Pages',
                    'content': root_content,
                    'branch': default_branch
                }, timeout=15)
                if put_resp.status_code not in (200, 201):
                    raise Exception(f"创建根目录 'index.html' 失败: {put_resp.json().get('message', put_resp.text)}")
                logger.info(f"[GitHub] 根目录 'index.html' 已创建。")
            else:
                logger.info(f"[GitHub] 根目录 'index.html' 已存在。")

            # ---- 4. 启用 GitHub Pages ----
            logger.info(f"[GitHub] 步骤 4/7: 检查并启用 GitHub Pages...")
            pages_api_url = f"{api_base}/repos/{username}/{repo}/pages"
            # Pages API 需要特殊的 Accept header，临时覆盖
            pages_headers = {'Accept': 'application/vnd.github+json'}
            pages_resp = session.get(pages_api_url, headers=pages_headers, timeout=10)
            if pages_resp.status_code == 404:
                post_resp = session.post(pages_api_url, headers=pages_headers, json={
                    'source': {'branch': default_branch, 'path': '/'}
                }, timeout=15)
                if post_resp.status_code not in (200, 201):
                    raise Exception(f"启用 GitHub Pages 失败: {post_resp.json().get('message', post_resp.text)}")
                logger.info(f"[GitHub] GitHub Pages 已成功启用。")
            elif pages_resp.status_code == 200:
                logger.info(f"[GitHub] GitHub Pages 已存在，跳过。")
            else:
                raise Exception(f"检查 GitHub Pages 状态失败（HTTP {pages_resp.status_code}）: {pages_resp.json().get('message', pages_resp.text)}")

            # ---- 4.5. 执行本地导出 ----
            logger.info(f"[GitHub] 步骤 4.5/7: 按模式 '{mode}' 执行本地导出...")
            import importlib.util
            ep_path = os.path.join(get_base_path(), 'export_project.py')
            spec = importlib.util.spec_from_file_location("export_project", ep_path)
            ep = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(ep)
            
            export_dir = ep.export_project(project_id, mode=mode)
            logger.info(f"[GitHub] 导出目录: {export_dir}")

            # ---- 5. 上传项目文件 ----
            logger.info(f"[GitHub] 步骤 5/7: 上传项目文件到 'projects/{project_id}/' 目录...")
            def upload_file(local_path, remote_path):
                with open(local_path, 'rb') as f:
                    content_b64 = base64.b64encode(f.read()).decode()

                file_api_url = f"{api_base}/repos/{username}/{repo}/contents/{remote_path}"
                
                # Check if file exists to get SHA for update
                existing = session.get(file_api_url, timeout=10)
                payload = {
                    'message': f'Update prototype: {project_id}',
                    'content': content_b64,
                    'branch': default_branch
                }
                if existing.status_code == 200:
                    payload['sha'] = existing.json().get('sha', '')
                    logger.info(f"[GitHub] 更新文件: {remote_path}")
                else:
                    logger.info(f"[GitHub] 创建文件: {remote_path}")

                put_resp = session.put(file_api_url, json=payload, timeout=30)
                if put_resp.status_code not in (200, 201):
                    raise Exception(f"上传文件失败 {remote_path}: {put_resp.json().get('message', put_resp.text)}")
                logger.info(f"[GitHub] 文件 '{remote_path}' 上传成功。")

            # 遍历 export_dir 下的所有文件并上传
            if os.path.exists(export_dir):
                for root_dir, _, files in os.walk(export_dir):
                    for file_name in files:
                        local_path = os.path.join(root_dir, file_name)
                        rel_path = os.path.relpath(local_path, export_dir).replace('\\', '/')
                        remote_path = f"projects/{project_id}/{rel_path}"
                        upload_file(local_path, remote_path)
            else:
                raise Exception(f"导出目录不存在: {export_dir}")
                
            logger.info(f"[GitHub] 项目文件上传完成。")

            # ---- 6. 生成 Pages URL ----
            logger.info(f"[GitHub] 步骤 6/7: 生成 GitHub Pages URL...")
            pages_url_result = f"https://{username}.github.io/{repo}/projects/{project_id}/"
            logger.info(f"[GitHub] 预计发布链接: {pages_url_result}")

            # ---- 7. 更新 record.json + 列表页 ----
            logger.info(f"[GitHub] 步骤 7/7: 更新本地记录 + GitHub 列表页...")
            record_path = os.path.join(project_dir, 'record.json')
            project_name = project_id  # 默认用 ID
            try:
                record = {}
                if os.path.exists(record_path):
                    with open(record_path, 'r', encoding='utf-8') as f:
                        record = json.load(f)
                project_name = record.get('title', record.get('name', project_id))
                record['github_url'] = pages_url_result
                record['github_published_at'] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                record['github_mode'] = mode
                with open(record_path, 'w', encoding='utf-8') as f:
                    json.dump(record, f, ensure_ascii=False, indent=2)
                logger.info(f"[GitHub] 'record.json' 已更新。")
            except Exception as e:
                logger.warning(f"[GitHub] 警告: 更新 'record.json' 失败（非致命错误）: {e}")

            # 更新 GitHub 列表页
            try:
                self.update_github_listing(
                    session, api_base, username, repo, default_branch,
                    project_id=project_id,
                    project_name=project_name,
                    project_url=pages_url_result,
                    mode=mode,
                    remove=False
                )
            except Exception as e:
                logger.warning(f"[GitHub] 警告: 更新列表页失败（非致命错误）: {e}")

            logger.info(f"[GitHub] 项目 '{project_id}' 发布流程完成。")
            self.send_json_response({
                'success': True,
                'url': pages_url_result,
                'mode': mode,
                'message': f'发布成功！约 1-3 分钟后链接生效: {pages_url_result}'
            })

        except Exception as e:
            logger.error(f"[错误] GitHub 发布失败: {e}")
            import traceback
            traceback.print_exc()
            self.send_error_response(f"GitHub 发布失败: {str(e)}")

    # ==================== 需求文档导入 ====================

    def handle_requirements_import(self):
        """异步处理需求文档导入：立即返回 task_id，后台线程完成解析和AI提取"""
        try:
            content_type = self.headers['Content-Type']
            if not content_type.startswith('multipart/form-data'):
                self.send_error_response("Expected multipart/form-data")
                return

            boundary_match = re.search(r'boundary=([^;]+)', content_type)
            if not boundary_match:
                self.send_error_response("Missing boundary")
                return

            boundary = boundary_match.group(1).strip('"').encode()
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length == 0:
                self.send_error_response("Missing Content-Length")
                return

            if content_length > 60 * 1024 * 1024:
                self.send_error_response("文件过大，请上传小于 60MB 的文件")
                return

            body = self.rfile.read(content_length)

            # 提取上传的文件
            parts = body.split(b'--' + boundary)
            file_data = None
            filename = None
            file_type = None

            for part in parts:
                if not part or part == b'--\r\n' or part == b'--':
                    continue
                if part.startswith(b'\r\n'):
                    part = part[2:]
                if part.endswith(b'\r\n'):
                    part = part[:-2]

                header_end = part.find(b'\r\n\r\n')
                if header_end == -1:
                    continue

                headers = part[:header_end].decode('utf-8', errors='ignore')
                file_content = part[header_end + 4:]

                filename_match = re.search(r'filename="([^"]+)"', headers)
                if filename_match:
                    filename = os.path.basename(filename_match.group(1))
                    file_data = file_content

                    if filename.endswith('.docx'):
                        file_type = 'docx'
                    elif filename.endswith('.md'):
                        file_type = 'md'
                    elif filename.endswith('.txt'):
                        file_type = 'txt'
                    else:
                        self.send_error_response(f"不支持的文件类型: {filename}，请上传 .docx、.md 或 .txt 文件")
                        return

            if not file_data or not filename:
                self.send_error_response("未找到上传的文件")
                return

            logger.info(f"[需求导入] 收到文件: {filename}, 类型: {file_type}, 大小: {len(file_data)} 字节")

            # 保存临时文件（后台线程用完后清理）
            with tempfile.NamedTemporaryFile(delete=False, suffix=f'.{file_type}') as tmp_file:
                tmp_file.write(file_data)
                tmp_file_path = tmp_file.name

            # 生成任务 ID
            task_id = f"import_{int(time.time() * 1000)}"

            # 注册异步任务（含流式传输字段）
            with tasks_lock:
                import_tasks[task_id] = {
                    'status': STATUS_GENERATING,
                    'progress': 0,
                    'error': '',
                    'data': None,
                    'metadata': None,
                    'accumulated_content': '',
                    'stream_chunks': [],
                    'stream_event': threading.Event(),
                    'stream_lock': threading.Lock(),
                }

            # 后台线程执行解析和AI提取
            handler = self  # 闭包引用

            def process_import():
                try:
                    logger.info(f"[需求导入] 后台开始处理: {task_id}")

                    with tasks_lock:
                        import_tasks[task_id]['progress'] = 10

                    # 解析文档（含图片提取）
                    doc_result = parse_document(tmp_file_path, file_type)
                    document_text = doc_result['text']
                    doc_images = doc_result.get('images', [])
                    logger.info(f"[需求导入] 解析完成，文本: {len(document_text)} 字符，图片: {len(doc_images)} 张")

                    with tasks_lock:
                        import_tasks[task_id]['progress'] = 30

                    if not document_text.strip() and not doc_images:
                        with tasks_lock:
                            import_tasks[task_id]['status'] = STATUS_FAILED
                            import_tasks[task_id]['error'] = '文档内容为空'
                        return

                    # 调用 AI 提取结构化数据
                    extracted_data = handler.call_ai_for_requirements(document_text)
                    extracted_pages = extracted_data.get('pages', [])
                    logger.info(f"[需求导入] AI 提取完成: {len(extracted_pages)} 个页面")

                    with tasks_lock:
                        import_tasks[task_id]['progress'] = 80

                    # 将图片按文档位置分配到各页面
                    if doc_images and extracted_pages:
                        handler._assign_images_to_pages(extracted_data, doc_images)

                    # 保存结果
                    metadata = {
                        'filename': filename,
                        'fileType': file_type,
                        'extractedAt': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        'contentLength': len(document_text),
                        'imageCount': len(doc_images)
                    }

                    with tasks_lock:
                        import_tasks[task_id]['status'] = STATUS_COMPLETED
                        import_tasks[task_id]['progress'] = 100
                        import_tasks[task_id]['data'] = extracted_data
                        import_tasks[task_id]['metadata'] = metadata

                    logger.info(f"[需求导入] 处理完成: {task_id}")

                except Exception as e:
                    logger.error(f"[需求导入错误] {task_id}: {e}")
                    import traceback
                    traceback.print_exc()
                    with tasks_lock:
                        import_tasks[task_id]['status'] = STATUS_FAILED
                        import_tasks[task_id]['error'] = str(e)

                finally:
                    try:
                        os.remove(tmp_file_path)
                    except Exception:
                        pass

            thread = threading.Thread(target=process_import, daemon=True)
            thread.start()

            logger.info(f"[需求导入] 任务已创建，后台处理中: {task_id}")
            self.send_json_response({
                'success': True,
                'taskId': task_id,
                'async': True
            })

        except Exception as e:
            logger.error(f"[需求导入错误] {e}")
            import traceback
            traceback.print_exc()
            self.send_error_response(str(e))

    def handle_requirements_import_status(self):
        """查询需求导入任务状态"""
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        task_id = query.get('id', [''])[0]
        if not task_id:
            self.send_error_response("缺少 id 参数")
            return

        with tasks_lock:
            if task_id in import_tasks:
                task = import_tasks[task_id]
                response = {
                    'status': task['status'],
                    'progress': task.get('progress', 0),
                    'error': task.get('error', '')
                }
                # 完成或失败时附带数据，保留 30 分钟后清除
                if task['status'] == STATUS_COMPLETED and task['data']:
                    response['data'] = task['data']
                    response['metadata'] = task['metadata']
                    # 记录完成时间，30 分钟后清理
                    if 'completed_at' not in task:
                        task['completed_at'] = time.time()
                    if time.time() - task['completed_at'] > 1800:
                        del import_tasks[task_id]
                elif task['status'] == STATUS_FAILED:
                    if 'completed_at' not in task:
                        task['completed_at'] = time.time()
                    if time.time() - task['completed_at'] > 600:
                        del import_tasks[task_id]
                self.send_json_response(response)
                return

        self.send_json_response({'status': 'not_found', 'progress': 0})

    def handle_requirements_stream(self, query):
        """SSE 端点：流式推送需求导入 AI 内容到前端"""
        task_id = query.get('id', [''])[0]
        if not task_id:
            self.send_error_response("缺少 id 参数")
            return

        with tasks_lock:
            if task_id not in import_tasks:
                self.send_error_response("任务不存在")
                return
            task = import_tasks[task_id]
            stream_event = task.get('stream_event')

        # 设置 SSE 响应头
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'keep-alive')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()

        last_chunk_index = 0

        try:
            while True:
                with tasks_lock:
                    if task_id not in import_tasks:
                        break
                    task = import_tasks[task_id]
                    current_status = task.get('status')

                if current_status in (STATUS_COMPLETED, STATUS_FAILED):
                    with task.get('stream_lock', threading.Lock()):
                        remaining = task.get('stream_chunks', [])[last_chunk_index:]
                        for chunk in remaining:
                            self._send_sse_data(json.dumps({'content': chunk}, ensure_ascii=False))
                        last_chunk_index = len(task.get('stream_chunks', []))

                    self._send_sse_event('status', json.dumps({
                        'status': current_status,
                        'error': task.get('error', ''),
                        'progress': task.get('progress', 0),
                        'data': task.get('data') if current_status == STATUS_COMPLETED else None
                    }, ensure_ascii=False))
                    break

                with task.get('stream_lock', threading.Lock()):
                    chunks = task.get('stream_chunks', [])
                    new_chunks = chunks[last_chunk_index:]
                    last_chunk_index = len(chunks)

                for chunk in new_chunks:
                    self._send_sse_data(json.dumps({'content': chunk}, ensure_ascii=False))

                if stream_event:
                    stream_event.wait(timeout=0.1)
                    stream_event.clear()

        except (ConnectionResetError, BrokenPipeError, OSError):
            pass
        except Exception as e:
            logger.error(f"[需求SSE错误] {e}")
        finally:
            try:
                self.wfile.write(b'data: [DONE]\n\n')
                self.wfile.flush()
            except Exception:
                pass

    def call_ai_for_requirements(self, document_content):
        """调用 AI 从文档中提取结构化需求"""
        system_prompt = """你是一个资深的产品需求分析师和UI/UX设计师，擅长将产品需求文档转化为高保真原型设计规范。
你的任务是从需求规格说明书中深度提取结构化信息，目标是让前端工程师仅凭提取结果就能生成高质量的原型页面。
提取时要站在"如何实现这个页面"的角度，不仅要描述功能，还要描述界面长什么样、数据如何展示、用户如何操作。"""

        user_prompt = f"""请仔细分析以下需求规格说明书，深度提取结构化的原型设计信息。

# 原始文档内容
{document_content}

# 提取要求

## 1. 全局设计规范（global）
从文档中提取或推断以下设计参数（未提及的保持默认值）：
- primaryColor: 主色调（如 #004fff）
- secondaryColor: 强调色（如 #10B981）
- backgroundMode: 背景模式（light/dark）
- componentStyle: 组件风格（Ant Design/Material Design/Tailwind UI）
- fontFamily: 字体偏好（如 "Inter, system-ui, sans-serif"）
- designStyle: 整体风格描述（如"简洁商务风"、"活泼社交风"、"科技感数据大屏"等，帮助AI把握整体调性）

## 2. 导航结构（navigation）
提取系统的整体导航和页面层级关系：
- type: 导航类型（"sidebar"左侧导航 / "topbar"顶部导航 / "hybrid"混合导航）
- items: 导航菜单项列表，每项包含 name（菜单名）和 icon（建议的 FontAwesome 图标类名，如 "fa-home"、"fa-users"）

## 3. 页面详情（pages）
对于文档中描述的每个页面/界面，请深度提取以下信息：

### name: 页面名称（如"用户管理"、"数据仪表盘"）

### description: 页面用途简述（一句话说明这个页面用来做什么）

### layout: 详细布局描述
不要只写"上下布局"这种笼统描述。请具体描述：
- 页面整体布局结构（如"顶部标题栏 + 左侧筛选面板 + 右侧主内容区域"）
- 各区域的大小比例（如"左侧占1/4宽度，右侧占3/4"）
- 关键元素的位置（如"右上角有搜索框和操作按钮组"）
- 响应式行为（如"移动端左侧面板折叠为下拉"）

### components: 组件级描述
详细列出页面中包含的UI组件及其配置：
- 顶部区域：标题、面包屑、操作按钮等
- 筛选/搜索区：搜索框、筛选下拉、日期选择等，具体说明有哪些筛选项
- 数据展示区：使用表格还是卡片列表，具体有哪些列/字段（列出列名），是否支持排序、筛选
- 表单区：包含哪些字段，字段类型（文本/下拉/日期/数字等），哪些必填
- 弹窗/抽屉：触发条件和内容
- 统计/图表：使用什么类型的图表，展示什么指标
- 分页：是否需要分页，每页多少条

### dataStructure: 数据字段说明
列出页面涉及的核心数据对象及其字段，例如：
- 用户对象：id、姓名、邮箱、角色、状态、创建时间
- 订单对象：订单号、客户名、金额、状态、日期
这有助于生成真实的示例数据。

### interactions: 交互行为描述
详细描述用户的操作流程和页面响应：
- 按钮点击后的行为（如"点击'新建'按钮弹出表单弹窗"）
- 列表操作（如"每行有编辑、删除操作，删除前需确认"）
- 数据联动（如"选择部门后自动过滤该部门下的用户"）
- 状态变化（如"审核通过后状态标签变绿，操作列隐藏审核按钮"）
- 搜索和筛选行为（如"输入关键词实时搜索，300ms防抖"）

### userFlow: 用户操作流程（可选）
描述典型用户在这个页面上的操作步骤，如：
1. 进入页面看到数据列表
2. 使用顶部筛选条件缩小范围
3. 点击某条数据查看详情
4. 在详情中执行编辑操作

## 4. 枚举值提取（重要）

从文档中提取所有结构化的枚举值、常量列表、选项集合。这些通常以以下形式出现：
- 表格中的分类列表（如"分类包含：A/B/C"）
- 状态流转说明（如"草稿→审核中→已发布"）
- 类型定义（如"类型分为 structured/textual 两种"）
- 角色权限表中的角色列表
- 下拉选项、筛选条件的选项列表
- 数据字段的可选值说明（如 status 字段取值为 draft/reviewing/published）

### 枚举组织原则
1. **全局枚举**（放在 global.enums 中）：跨多个页面共享的值，如用户角色、通用状态码、通用类型等
2. **页面级枚举**（放在页面对象的 enums 字段中）：特定页面使用的选项，如知识分类、特定筛选器选项等

### 输出格式
在 global.enums 中存放全局枚举，在页面对象中添加 enums 字段存放页面级枚举：
- 枚举名使用英文 camelCase（如 userRoles, knowledgeStatus）
- 值数组保持原文档中的语言（中文文档提取中文值，英文文档提取英文值）
- 格式支持两种：`{{"enumName": ["值1", "值2"]}}` 或 `{{"enumName": {{"values": ["值1", "值2"], "description": "简短说明"}}}}`
- 枚举字段为可选，文档中未出现的枚举不需要编造

### 提取示例
如果文档中有如下内容：
"知识分类体系包含：业务术语表（文本类）、指标口径库（结构化）、领域业务知识（文本类）、FAQ（文本类）"
应提取为：`{{"knowledgeCategories": ["业务术语表", "指标口径库", "领域业务知识", "FAQ"]}}`

如果文档中有如下内容：
"状态流转：草稿 → 审核中 → 已发布，支持驳回回到草稿"
应提取为：`{{"knowledgeStatus": ["草稿", "审核中", "已发布", "已驳回"]}}`

# 输出格式
请直接返回JSON格式，不要有任何额外说明、markdown标记或其他文字：
{{"global":{{"primaryColor":"","secondaryColor":"","backgroundMode":"","componentStyle":"","fontFamily":"","designStyle":"","enums":{{}}}},"navigation":{{"type":"","items":[{{"name":"","icon":""}}]}},"pages":[{{"name":"","description":"","layout":"","components":"","dataStructure":"","enums":{{}},"interactions":"","userFlow":""}}]}}

如果某个字段在文档中未提及，请根据上下文合理推断。完全无法推断的使用空字符串""或空对象{{}}。"""

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]

        # 获取模型配置
        selected_model = get_selected_model()
        model_name = selected_model.get('model', 'gpt-4')
        base_url = selected_model.get('base_url', '')
        api_key = selected_model.get('api_key', '')

        # 优先使用模型配置，fallback 到全局配置或默认值
        max_tokens = selected_model.get('max_tokens') or AI_OPTIONS.get('max_tokens', 8000)
        temperature = selected_model.get('temperature') or 0.3
        payload = {
            "model": model_name,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature
        }

        url = f"{base_url}/chat/completions"
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {api_key}'
        }

        logger.info(f"[需求提取] 调用AI模型: {selected_model.get('name', model_name)}")

        # 使用流式调用 AI
        content = ""
        gen = self.call_ai_model_streaming(messages, cancellable_project_id=None)
        try:
            for chunk_text, full_content, done, _tool_calls, *_rest in gen:
                content = full_content
                # 推送数据块到 import_tasks（如果有关联任务）
                with tasks_lock:
                    for tid, task in import_tasks.items():
                        if task.get('status') == STATUS_GENERATING and task.get('progress', 0) >= 30:
                            task['accumulated_content'] = full_content
                            reasoning_text = _rest[0] if _rest else ''
                            push_text = ''
                            if chunk_text and not chunk_text.startswith('[think]'):
                                push_text = chunk_text
                            elif reasoning_text:
                                push_text = '[think]' + reasoning_text
                            if push_text:
                                stream_lock = task.get('stream_lock')
                                if stream_lock:
                                    with stream_lock:
                                        task['stream_chunks'].append(push_text)
                                stream_event = task.get('stream_event')
                                if stream_event:
                                    stream_event.set()
                            break
                if done:
                    break
        finally:
            try:
                gen.close()
            except RuntimeError:
                pass

        logger.info(f"[需求提取] AI 响应长度: {len(content)} 字符")

        # 提取 JSON
        json_data = self._extract_json_from_ai_response(content)

        # 验证和补充默认值
        json_data = self._validate_requirements_data(json_data)

        return json_data

    @staticmethod
    def _extract_json_from_ai_response(content):
        """从 AI 响应中提取 JSON"""
        # 尝试直接解析
        try:
            return json.loads(content)
        except (json.JSONDecodeError, ValueError):
            pass

        # 尝试提取 markdown 代码块
        json_match = re.search(r'```(?:json)?\s*\n([\s\S]*?)\n```', content)
        if json_match:
            try:
                return json.loads(json_match.group(1))
            except (json.JSONDecodeError, ValueError):
                pass

        # 尝试查找 JSON 对象
        json_start = content.find('{')
        json_end = content.rfind('}')
        if json_start != -1 and json_end != -1 and json_end > json_start:
            try:
                return json.loads(content[json_start:json_end + 1])
            except (json.JSONDecodeError, ValueError):
                pass

        raise Exception("无法从 AI 响应中提取有效的 JSON 数据")

    @staticmethod
    def _validate_requirements_data(data):
        """验证和标准化需求数据"""
        if not isinstance(data, dict):
            return {'global': {}, 'pages': []}

        # 确保 global 字段完整
        if 'global' not in data:
            data['global'] = {}

        global_defaults = {
            'primaryColor': '#004fff',
            'secondaryColor': '#10B981',
            'backgroundMode': 'light',
            'componentStyle': 'Ant Design',
            'fontFamily': '',
            'designStyle': ''
        }

        for key, default_value in global_defaults.items():
            if key not in data['global'] or not data['global'][key]:
                data['global'][key] = default_value

        # 验证 global.enums（可选字段）
        if 'enums' in data['global']:
            if not isinstance(data['global']['enums'], dict):
                data['global']['enums'] = {}
            else:
                # 确保每个枚举格式正确：列表 或 {"values": [...]} 对象
                for enum_name in list(data['global']['enums'].keys()):
                    enum_data = data['global']['enums'][enum_name]
                    if not (isinstance(enum_data, list) or
                            (isinstance(enum_data, dict) and 'values' in enum_data)):
                        del data['global']['enums'][enum_name]

        # 确保 navigation 字段存在
        if 'navigation' not in data or not isinstance(data['navigation'], dict):
            data['navigation'] = {'type': '', 'items': []}
        nav = data['navigation']
        if 'type' not in nav:
            nav['type'] = ''
        if 'items' not in nav or not isinstance(nav['items'], list):
            nav['items'] = []

        # 确保 pages 字段存在且为列表
        if 'pages' not in data or not isinstance(data['pages'], list):
            data['pages'] = []

        # 验证每个页面字段（兼容新旧格式）
        for page in data['pages']:
            if not isinstance(page, dict):
                continue
            # 新格式字段
            for key in ['name', 'description', 'layout', 'components', 'dataStructure', 'interactions', 'userFlow']:
                if key not in page:
                    page[key] = ''
            # 兼容旧格式：将旧的 features/interaction 迁移到新字段
            if page.get('features') and not page.get('components'):
                page['components'] = page['features']
            if page.get('interaction') and not page.get('interactions'):
                page['interactions'] = page['interaction']
            # 验证页面级 enums（可选字段）
            if 'enums' in page:
                if not isinstance(page['enums'], dict):
                    page['enums'] = {}
                else:
                    for enum_name in list(page['enums'].keys()):
                        enum_data = page['enums'][enum_name]
                        if not (isinstance(enum_data, list) or
                                (isinstance(enum_data, dict) and 'values' in enum_data)):
                            del page['enums'][enum_name]

        return data

    @staticmethod
    def _assign_images_to_pages(extracted_data, doc_images):
        """将文档中提取的图片按段落位置分配到 AI 识别的各页面。

        策略：图片按 para_index 排序，根据其在文档中的相对位置
        分配到对应的页面段落区间。
        """
        pages = extracted_data.get('pages', [])
        if not pages or not doc_images:
            return

        # 按段落位置排序所有图片
        sorted_images = sorted(doc_images, key=lambda img: img['para_index'])
        total_images = len(sorted_images)
        total_pages = len(pages)

        if total_pages == 1:
            # 只有一个页面，所有图片归它
            pages[0]['images'] = [
                {'name': img['name'], 'base64': img['base64']}
                for img in sorted_images
            ]
        else:
            # 多个页面：按段落位置均匀分配
            # 找到所有图片的最大段落索引，建立 [0, max_para] 区间
            max_para = max((img['para_index'] for img in sorted_images), default=0)
            if max_para == 0:
                max_para = 1  # 避免除零

            for page in pages:
                page['images'] = []

            for img in sorted_images:
                # 按相对位置决定属于哪个页面
                ratio = img['para_index'] / max_para
                page_idx = min(int(ratio * total_pages), total_pages - 1)
                pages[page_idx]['images'].append({
                    'name': img['name'],
                    'base64': img['base64']
                })


    # ==================== PRD 需求讨论 ====================

    def _get_prd_disc_context(self, data_or_query):
        """根据请求参数确定讨论存储目录。
        projectId 可选 — 有则项目内讨论，无则全局讨论（创建项目前）。
        Returns: (disc_folder, project_folder_or_None)
        """
        from server_prd_discussions import get_disc_folder_for_request
        project_id = ''
        if isinstance(data_or_query, dict):
            project_id = data_or_query.get('projectId', '')
        return get_disc_folder_for_request(project_id or None)

    def _process_discussion_attachments(self, raw_attachments, disc_folder, disc_id):
        """处理前端传来的附件列表，提取文档文本，保存图片。

        Args:
            raw_attachments: 前端传来的附件列表 [{type, name, base64, ext}]
            disc_folder: 讨论存储目录
            disc_id: 讨论 ID

        Returns:
            处理后的附件列表，可直接传给 add_message()
        """
        from server_prd_discussions import extract_text_from_base64

        if not raw_attachments:
            return None

        processed = []
        uploads_dir = os.path.join(disc_folder, disc_id, 'uploads')
        os.makedirs(uploads_dir, exist_ok=True)

        for att in raw_attachments:
            att_type = att.get('type', '')
            att_name = att.get('name', 'unknown')
            att_b64 = att.get('base64', '')
            att_ext = att.get('ext', '')

            if att_type == 'image' and att_b64:
                # 保存图片到 uploads 目录
                try:
                    b64_data = att_b64
                    if ',' in b64_data:
                        b64_data = b64_data.split(',', 1)[1]
                    img_bytes = base64.b64decode(b64_data)

                    safe_name = re.sub(r'[^\w\-.]', '_', att_name)
                    if not safe_name:
                        safe_name = f'image_{int(time.time())}.png'
                    img_path = os.path.join(uploads_dir, safe_name)

                    counter = 1
                    base_path = img_path
                    while os.path.exists(img_path):
                        name_part, ext_part = os.path.splitext(base_path)
                        img_path = f"{name_part}_{counter}{ext_part}"
                        counter += 1

                    with open(img_path, 'wb') as f:
                        f.write(img_bytes)

                    processed.append({
                        'type': 'image',
                        'name': att_name,
                        'ext': att_ext or os.path.splitext(att_name)[1].lower(),
                        'base64': att_b64,
                        'url': f'/api/prd/discussion/file/{disc_id}/{os.path.basename(img_path)}'
                    })
                except Exception as e:
                    logger.warning(f'[PRD讨论] 保存图片失败 {att_name}: {e}')

            elif att_type == 'file' and att_b64:
                # 提取文档文本
                try:
                    extracted = extract_text_from_base64(att_b64, att_name)

                    b64_data = att_b64
                    if ',' in b64_data:
                        b64_data = b64_data.split(',', 1)[1]
                    file_bytes = base64.b64decode(b64_data)

                    safe_name = re.sub(r'[^\w\-.]', '_', att_name)
                    file_path = os.path.join(uploads_dir, safe_name)
                    counter = 1
                    base_path = file_path
                    while os.path.exists(file_path):
                        name_part, ext_part = os.path.splitext(base_path)
                        file_path = f"{name_part}_{counter}{ext_part}"
                        counter += 1

                    with open(file_path, 'wb') as f:
                        f.write(file_bytes)

                    processed.append({
                        'type': 'file',
                        'name': att_name,
                        'ext': att_ext or os.path.splitext(att_name)[1].lower(),
                        'extracted_text': extracted,
                        'url': f'/api/prd/discussion/file/{disc_id}/{os.path.basename(file_path)}'
                    })
                except Exception as e:
                    logger.warning(f'[PRD讨论] 处理文件失败 {att_name}: {e}')

        return processed if processed else None

    def _load_prd_session(self, disc_folder, discussion_id):
        """加载 PRD 讨论会话"""
        from server_prd_discussions import PRDDiscussionSession
        return PRDDiscussionSession.load(disc_folder, discussion_id)

    def _send_prd_sse_headers(self):
        """发送 SSE 响应头"""
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()

    def _stream_prd_discussion(self, disc_folder, discussion_id, system_prompt, session):
        """通用的 PRD 讨论流式处理逻辑"""
        from server_prd_discussions import update_discussion_meta

        messages = [{'role': 'system', 'content': system_prompt}]
        messages.extend(session.get_ai_context())

        self._send_prd_sse_headers()

        accumulated = ''
        gen = self.call_ai_model_streaming(messages)
        for chunk_text, acc, done, tool_calls, *_ in gen:
            if chunk_text:
                accumulated += chunk_text
                self._send_sse_data(json.dumps({
                    'type': 'chat',
                    'data': {'role': 'assistant', 'content': chunk_text}
                }, ensure_ascii=False))

        session.add_message('assistant', accumulated)
        session.update_spec_from_ai_response(accumulated)
        session.save(disc_folder)
        update_discussion_meta(disc_folder, discussion_id)

        self._send_sse_data(json.dumps({
            'type': 'spec_card_update',
            'data': session.get_spec_card()
        }, ensure_ascii=False))
        self._send_sse_data(json.dumps({
            'type': 'done', 'data': {}
        }, ensure_ascii=False))

    def handle_prd_discussions_list(self):
        """GET /api/prd/discussions — 列出所有讨论（projectId 可选）"""
        try:
            from server_prd_discussions import list_discussions
            query = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(query)
            project_id = params.get('projectId', [''])[0]
            disc_folder, _ = self._get_prd_disc_context({'projectId': project_id})
            index = list_discussions(disc_folder)
            self.send_json_response({
                'success': True,
                'active_discussion_id': index.get('active_discussion_id'),
                'discussions': index.get('discussions', []),
            })
        except Exception as e:
            logger.error(f"[PRD讨论] 列出讨论失败: {e}")
            self.send_error_response(str(e))

    def handle_prd_discussion_status(self, query):
        """GET /api/prd/discussion/status?discussionId=xxx"""
        try:
            from server_prd_discussions import (
                PRDDiscussionSession, get_active_discussion_id)
            project_id = query.get('projectId', [''])[0]
            discussion_id = query.get('discussionId', [''])[0]
            disc_folder, _ = self._get_prd_disc_context(query)

            if not discussion_id:
                discussion_id = get_active_discussion_id(disc_folder) or ''

            if not discussion_id:
                self.send_json_response({
                    'success': True,
                    'has_discussion': False,
                    'maturity_level': 'RA0',
                    'spec_card': None,
                })
                return

            session = PRDDiscussionSession.load(disc_folder, discussion_id)
            spec_card = session.get_spec_card()

            self.send_json_response({
                'success': True,
                'has_discussion': True,
                'discussion_id': discussion_id,
                'title': session.title,
                'maturity_level': session.maturity_level,
                'current_layer': session.current_layer,
                'message_count': len(session.messages),
                'spec_card': spec_card,
            })
        except Exception as e:
            logger.error(f"[PRD讨论] 获取状态失败: {e}")
            self.send_error_response(str(e))

    def handle_prd_discussion_start(self):
        """POST /api/prd/discussion/start — 开始新的需求讨论（projectId 可选）"""
        try:
            from server_prd_discussions import (
                create_discussion, PRDDiscussionSession,
                build_discussion_system_prompt, update_discussion_meta,
                extract_text_from_base64)
            data = self._read_post_json()
            project_id = data.get('projectId', '')
            title = data.get('title', '')
            initial_idea = data.get('initialIdea', '')
            raw_attachments = data.get('attachments', [])

            disc_folder, project_folder = self._get_prd_disc_context(data)

            meta = create_discussion(disc_folder, title=title)
            disc_id = meta['id']

            session = PRDDiscussionSession.load(disc_folder, disc_id)
            session.project_id = project_id or ''

            # 处理附件
            processed_attachments = self._process_discussion_attachments(
                raw_attachments, disc_folder, disc_id)

            # 读取项目上下文（仅项目内讨论）
            project_context = ''
            if project_folder and os.path.exists(project_folder):
                prompt_path = os.path.join(project_folder, 'prompt.txt')
                if os.path.exists(prompt_path):
                    with open(prompt_path, 'r', encoding='utf-8') as f:
                        prompt_text = f.read()
                    project_context = prompt_text[:2000] + '...' if len(prompt_text) > 2000 else prompt_text

            if initial_idea or processed_attachments:
                session.add_message('user', initial_idea or '(上传了附件)', processed_attachments)

            system_prompt = build_discussion_system_prompt(
                session, project_context)

            self._send_prd_sse_headers()

            self._send_sse_data(json.dumps({
                'type': 'discussion_start',
                'data': {'discussionId': disc_id, 'title': session.title}
            }, ensure_ascii=False))

            accumulated = ''
            # 构建完整的 AI 消息列表（system + 上下文）
            ai_messages = [{'role': 'system', 'content': system_prompt}]
            ai_messages.extend(session.get_ai_context())
            gen = self.call_ai_model_streaming(ai_messages)
            for chunk_text, acc, done, tool_calls, *_rest in gen:
                if chunk_text:
                    accumulated += chunk_text
                    self._send_sse_data(json.dumps({
                        'type': 'chat',
                        'data': {'role': 'assistant', 'content': chunk_text}
                    }, ensure_ascii=False))

            session.add_message('assistant', accumulated)
            session.update_spec_from_ai_response(accumulated)
            session.save(disc_folder)
            update_discussion_meta(disc_folder, disc_id)

            self._send_sse_data(json.dumps({
                'type': 'spec_card_update',
                'data': session.get_spec_card()
            }, ensure_ascii=False))
            self._send_sse_data(json.dumps({
                'type': 'done', 'data': {}
            }, ensure_ascii=False))

        except Exception as e:
            logger.error(f"[PRD讨论] 开始讨论失败: {e}")
            try:
                self._send_sse_data(json.dumps({
                    'type': 'error', 'data': {'message': str(e)}
                }, ensure_ascii=False))
            except Exception:
                pass

    def handle_prd_discussion_message(self):
        """POST /api/prd/discussion/message — 发送讨论消息（SSE 流式）"""
        try:
            from server_prd_discussions import (
                PRDDiscussionSession, build_discussion_system_prompt,
                update_discussion_meta)
            data = self._read_post_json()
            discussion_id = data.get('discussionId', '')
            message = data.get('message', '')
            raw_attachments = data.get('attachments', [])

            if not discussion_id:
                self.send_error_response("缺少 discussionId")
                return

            disc_folder, project_folder = self._get_prd_disc_context(data)
            session = PRDDiscussionSession.load(disc_folder, discussion_id)

            # 处理附件
            processed_attachments = self._process_discussion_attachments(
                raw_attachments, disc_folder, discussion_id)

            session.add_message('user', message, processed_attachments)

            project_context = ''
            if project_folder and os.path.exists(project_folder):
                prompt_path = os.path.join(project_folder, 'prompt.txt')
                if os.path.exists(prompt_path):
                    with open(prompt_path, 'r', encoding='utf-8') as f:
                        prompt_text = f.read()
                    project_context = prompt_text[:2000] + '...' if len(prompt_text) > 2000 else prompt_text

            system_prompt = build_discussion_system_prompt(
                session, project_context)

            self._send_prd_sse_headers()

            accumulated = ''
            ai_messages = [{'role': 'system', 'content': system_prompt}]
            ai_messages.extend(session.get_ai_context())
            gen = self.call_ai_model_streaming(ai_messages)
            for chunk_text, acc, done, tool_calls, *_rest in gen:
                if chunk_text:
                    accumulated += chunk_text
                    self._send_sse_data(json.dumps({
                        'type': 'chat',
                        'data': {'role': 'assistant', 'content': chunk_text}
                    }, ensure_ascii=False))

            session.add_message('assistant', accumulated)
            session.update_spec_from_ai_response(accumulated)
            session.save(disc_folder)
            update_discussion_meta(disc_folder, discussion_id)

            self._send_sse_data(json.dumps({
                'type': 'spec_card_update',
                'data': session.get_spec_card()
            }, ensure_ascii=False))
            self._send_sse_data(json.dumps({
                'type': 'done', 'data': {}
            }, ensure_ascii=False))

        except Exception as e:
            logger.error(f"[PRD讨论] 发送消息失败: {e}")
            try:
                self._send_sse_data(json.dumps({
                    'type': 'error', 'data': {'message': str(e)}
                }, ensure_ascii=False))
            except Exception:
                pass

    def handle_prd_discussion_generate(self):
        """POST /api/prd/discussion/generate — 生成 PRD 文档"""
        try:
            from server_prd_discussions import (
                PRDDiscussionSession, build_prd_generation_prompt)
            data = self._read_post_json()
            discussion_id = data.get('discussionId', '')
            mode = data.get('mode', 'preview')

            if not discussion_id:
                self.send_error_response("缺少 discussionId")
                return

            disc_folder, _ = self._get_prd_disc_context(data)
            session = PRDDiscussionSession.load(disc_folder, discussion_id)

            if len(session.messages) < 2:
                self.send_error_response("讨论内容不足，请先进行需求讨论")
                return

            prd_prompt = build_prd_generation_prompt(session, mode=mode)

            self._send_prd_sse_headers()

            accumulated = ''
            gen = self.call_ai_model_streaming(prd_prompt)
            for chunk_text, acc, done, tool_calls, *_rest in gen:
                if chunk_text:
                    accumulated += chunk_text
                    self._send_sse_data(json.dumps({
                        'type': 'prd_content',
                        'data': {'content': chunk_text}
                    }, ensure_ascii=False))

            # 清理 markdown 代码块标记
            prd_content = accumulated.strip()
            if prd_content.startswith('```markdown'):
                prd_content = prd_content[len('```markdown'):].strip()
            if prd_content.startswith('```'):
                prd_content = prd_content[3:].strip()
            if prd_content.endswith('```'):
                prd_content = prd_content[:-3].strip()

            # 保存到讨论目录
            prd_filename = 'prd_delivery.md' if mode == 'delivery' else 'prd_preview.md'
            disc_dir = os.path.join(disc_folder, discussion_id)
            os.makedirs(disc_dir, exist_ok=True)
            prd_path = os.path.join(disc_dir, prd_filename)
            with open(prd_path, 'w', encoding='utf-8') as f:
                f.write(prd_content)

            logger.info(
                f"[PRD讨论] 生成{'交付版' if mode == 'delivery' else '预览版'} "
                f"PRD: {len(prd_content)} 字符 → {prd_filename}")

            self._send_sse_data(json.dumps({
                'type': 'prd_done',
                'data': {
                    'mode': mode,
                    'filename': prd_filename,
                    'length': len(prd_content),
                }
            }, ensure_ascii=False))

        except Exception as e:
            logger.error(f"[PRD讨论] 生成 PRD 失败: {e}")
            try:
                self._send_sse_data(json.dumps({
                    'type': 'error', 'data': {'message': str(e)}
                }, ensure_ascii=False))
            except Exception:
                pass

    def handle_prd_discussion_apply(self):
        """POST /api/prd/discussion/apply — 将讨论结果提取为结构化需求"""
        try:
            from server_prd_discussions import (
                PRDDiscussionSession, build_requirements_extraction_prompt)
            data = self._read_post_json()
            discussion_id = data.get('discussionId', '')
            mode = data.get('mode', 'delivery')

            if not discussion_id:
                self.send_error_response("缺少 discussionId")
                return

            disc_folder, _ = self._get_prd_disc_context(data)
            session = PRDDiscussionSession.load(disc_folder, discussion_id)

            # 尝试读取已生成的 PRD 文件
            disc_dir = os.path.join(disc_folder, discussion_id)
            prd_filename = 'prd_delivery.md' if mode == 'delivery' else 'prd_preview.md'
            prd_path = os.path.join(disc_dir, prd_filename)
            prd_markdown = ''  # PRD 原文，用于回填到项目

            if not os.path.exists(prd_path):
                alt = 'prd_preview.md' if mode == 'delivery' else 'prd_delivery.md'
                alt_path = os.path.join(disc_dir, alt)
                if os.path.exists(alt_path):
                    prd_path = alt_path
                    prd_filename = alt
                else:
                    # 没有 PRD 文件，直接从讨论历史生成结构化需求
                    logger.info("[PRD讨论] 无 PRD 文件，直接从讨论历史提取需求")
                    extract_prompt = build_requirements_extraction_prompt(
                        '\n'.join(
                            f"{'用户' if m['role'] == 'user' else '助手'}: {m['content'][:200]}"
                            for m in session.messages[-20:]
                        )
                    )
            else:
                with open(prd_path, 'r', encoding='utf-8') as f:
                    prd_markdown = f.read()
                extract_prompt = build_requirements_extraction_prompt(prd_markdown)

            self._send_prd_sse_headers()

            self._send_sse_data(json.dumps({
                'type': 'extract_start',
                'data': {'message': '正在从讨论中提取结构化需求...'}
            }, ensure_ascii=False))

            accumulated = ''
            gen = self.call_ai_model_streaming(extract_prompt)
            for chunk_text, acc, done, tool_calls, *_rest in gen:
                if chunk_text:
                    accumulated += chunk_text

            extracted = self._extract_json_from_ai_response(accumulated)

            if extracted:
                extracted = self._validate_requirements_data(extracted)
                self._send_sse_data(json.dumps({
                    'type': 'extract_done',
                    'data': {
                        'success': True,
                        'requirements': extracted,
                        'prd_filename': prd_filename,
                        'prd_markdown': prd_markdown,
                    }
                }, ensure_ascii=False))
            else:
                self._send_sse_data(json.dumps({
                    'type': 'extract_done',
                    'data': {
                        'success': False,
                        'error': '无法从 AI 响应中提取结构化数据',
                    }
                }, ensure_ascii=False))

        except Exception as e:
            logger.error(f"[PRD讨论] 应用到项目失败: {e}")
            try:
                self._send_sse_data(json.dumps({
                    'type': 'error', 'data': {'message': str(e)}
                }, ensure_ascii=False))
            except Exception:
                pass

    def handle_prd_discussion_file(self):
        """GET /api/prd/discussion/file/{disc_id}/{filename} — 服务讨论上传的文件"""
        try:
            import mimetypes
            path = urllib.parse.urlparse(self.path).path
            # /api/prd/discussion/file/{disc_id}/{filename}
            parts = path.split('/')
            # ['', 'api', 'prd', 'discussion', 'file', disc_id, filename, ...]
            if len(parts) < 7:
                self.send_error(404, '文件路径无效')
                return

            disc_id = parts[5]
            filename = '/'.join(parts[6:])  # 支持文件名含子路径

            # 在全局讨论目录和项目目录中搜索
            search_dirs = [
                os.path.join('prd_discussions_global', disc_id, 'uploads'),
            ]
            # 也搜索项目内讨论目录
            if os.path.exists('projects'):
                for proj_dir in os.listdir('projects'):
                    uploads = os.path.join('projects', proj_dir, 'prd_discussions', disc_id, 'uploads')
                    if os.path.isdir(uploads):
                        search_dirs.append(uploads)

            file_path = None
            for d in search_dirs:
                candidate = os.path.join(d, filename)
                if os.path.isfile(candidate):
                    file_path = candidate
                    break

            if not file_path:
                self.send_error(404, '文件不存在')
                return

            # 安全检查：防止路径遍历
            abs_path = os.path.abspath(file_path)
            if '..' in filename or not abs_path.startswith(os.path.abspath('.')):
                self.send_error(403, '禁止访问')
                return

            mime_type, _ = mimetypes.guess_type(filename)
            if not mime_type:
                mime_type = 'application/octet-stream'

            with open(file_path, 'rb') as f:
                data = f.read()

            self.send_response(200)
            self.send_header('Content-Type', mime_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'public, max-age=86400')
            self.end_headers()
            self.wfile.write(data)

        except Exception as e:
            logger.error(f"[PRD讨论] 文件服务失败: {e}")
            self.send_error(500, str(e))


logger.info(f"=" * 50)
logger.info(f"原型生成器服务启动")
logger.info(f"地址: http://localhost:{PORT}/src/")
logger.info(f"项目目录: {os.path.abspath(PROJECTS_DIR)}")
logger.info(f"=" * 50)

socketserver.TCPServer.allow_reuse_address = True

try:
    with socketserver.ThreadingTCPServer(("", PORT), CustomHandler) as httpd:
        httpd.serve_forever()
except KeyboardInterrupt:
    logger.info("\n服务已停止")
except Exception as e:
    logger.error(f"\n服务错误: {e}")
