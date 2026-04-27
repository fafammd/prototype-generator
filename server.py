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

# ==================== 日志配置 ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger('prototype')

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


def extract_title_from_html(html_content):
    """从HTML中提取title标签的内容"""
    match = re.search(r'<title[^>]*>([^<]+)</title>', html_content, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return None


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
    """下载HTML中的所有外部图片并替换URL"""
    # 创建images子目录
    images_folder = os.path.join(save_folder, 'images')
    os.makedirs(images_folder, exist_ok=True)
    
    # 匹配图片URL（src="https://..."）
    img_pattern = r'src=["\']?(https?://[^"\'>\s]+\.(jpg|jpeg|png|gif|webp|svg)[^"\'>\s]*)["\']?'
    matches = re.findall(img_pattern, html_content, re.IGNORECASE)
    
    url_map = {}
    for url, ext in matches:
        if url not in url_map:
            filename = download_image(url, images_folder)
            if filename:
                url_map[url] = f"images/{filename}"
                logger.info(f"[下载] {url} -> {filename}")
    
    # 替换URL
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
        elif path == '/api/github/config':
            self.handle_github_config_get()
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
        elif self.path == '/api/stop-generation':
            self.handle_stop_generation()
        elif self.path == '/api/models/select':
            self.handle_model_select()
        elif self.path == '/api/models/save':
            self.handle_model_save()
        elif self.path == '/api/models/delete':
            self.handle_model_delete()
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
            template_css_path = None       # 保存的 CSS 文件相对路径
            template_design_tokens = ''    # 设计令牌摘要
            template_html_summary = ''     # HTML 结构摘要
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
            if has_template_info:
                # iframe 框架模式：保留外框架，只替换内容区
                if template_is_iframe and template_frame_html:
                    template_section = "\n\n# 现有系统框架（侧边栏+顶栏由系统自动保留）\n\n"
                    template_section += "用户的现有系统采用「侧边栏 + 顶栏 + iframe 内容区」的布局。\n"
                    template_section += "你生成的页面将嵌入到 iframe 中作为主内容区域。\n\n"
                    template_section += "## 外框架侧边栏菜单（供参考，当前激活项用★标记）\n"
                    template_section += "```\n"
                    sidebar_items = re.findall(r'<span>([^<]+)</span>', template_frame_html[:8000])
                    active_match = re.search(r'is-active[^>]*>.*?<span>([^<]+)</span>', template_frame_html[:8000], re.DOTALL)
                    active_text = active_match.group(1) if active_match else ''
                    for item in sidebar_items:
                        marker = ' ★ (当前激活)' if item == active_text else ''
                        template_section += f"  - {item}{marker}\n"
                    template_section += "```\n\n"
                    if template_design_tokens:
                        template_section += f"## 视觉规范（必须严格遵循，确保与外框架风格一致）\n{template_design_tokens}\n\n"
                        template_section += """### 样式一致性要求（非常重要）
你的页面将嵌入到现有系统的 iframe 中，视觉必须与外框架完全融合：
- 页面背景色必须与「页面背景色」一致，不能是白色如果外框架是灰色
- 字体和字号必须与外框架一致（通常是微软雅黑 15px）
- 正文文字色、主色调必须与规范一致
- 组件（按钮、输入框、表格、下拉框）使用 Element UI 风格，与外框架的 Element UI 组件保持一致
- 不要引入与现有系统不协调的配色方案
"""
                    template_section += """## 重要：生成要求

### 内容生成
1. 你只需要生成 iframe 内部的页面内容（即主内容区域的 HTML）
2. 不要生成侧边栏、顶栏、导航等外框架元素
3. 配色方案、字体、组件样式必须与模板设计令牌一致
4. 引用模板 CSS: `<link rel="stylesheet" href="template/template.css">`
5. 生成的 HTML 应该是一个完整的独立页面（有 <!DOCTYPE html>、<head>、<body>）
6. 页面视觉风格必须与现有系统保持一致

### 布局要求（非常重要）
- 生成的内容必须是**单个连续页面**，不要使用 Tab 标签页分页
- 页面应是可垂直滚动的长表单/长页面，所有内容在一个视图中
- 如果需要多个状态（如列表/编辑），使用按钮跳转而不是 Tab 切换
- 参考 Element UI 或 Ant Design 的表单页面风格

### 侧边栏菜单修改（可选）
如果用户需求中提到要在侧边栏添加新菜单项或修改现有菜单项，请在 HTML 代码的最后添加以下格式的注释：
```html
<!-- SIDEBAR_ADD: 菜单名称 -->
```
例如：`<!-- SIDEBAR_ADD: 模型蒸馏 -->`
系统会自动将该菜单项添加到侧边栏中。
"""
                    final_prompt += template_section
                else:
                    # 普通模板模式：整体参考
                    template_section = "\n\n# 现有系统设计模板（必须严格遵循其视觉风格）\n\n"
                    if template_design_tokens:
                        template_section += f"## 设计令牌（从模板 CSS 提取）\n{template_design_tokens}\n\n"
                    if template_html_summary:
                        template_section += f"## 页面结构参考\n```html\n{template_html_summary}\n```\n\n"
                    if template_css_path:
                        template_section += f"## 模板 CSS 文件\n已保存到 `{template_css_path}`，请在生成的 HTML 中通过 `<link rel=\"stylesheet\" href=\"{template_css_path}\">` 引用它。\n\n"
                    template_section += """## 模板还原要求
1. 配色方案必须与模板设计令牌一致
2. 组件样式（按钮、表格、表单、卡片）必须与模板一致
3. 布局结构参考模板的页面结构
4. 字体、字号、间距与模板保持统一
5. 在 HTML <head> 中添加 <link> 引用模板 CSS 文件
6. 你可以在模板基础上添加新功能，但视觉风格不能偏离
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

            # iframe 框架拼接：如果模板是 iframe 布局，将 AI 生成的内容嵌入框架
            if template_is_iframe and (template_raw_frame_html or template_frame_html):
                logger.info("[组装] 检测到 iframe 框架布局，拼接框架+内容...")
                # 优先使用原始框架 HTML（保留完整样式），如果不存在则用精简版
                frame_to_use = template_raw_frame_html or template_frame_html
                html_content = self.assemble_iframe_html(html_content, frame_to_use, template_css_path)

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
            
            # 保存 prompt (用于调试)
            prompt_path = os.path.join(project_folder, 'prompt.txt')
            with open(prompt_path, 'w', encoding='utf-8') as f:
                f.write(prompt)
            
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

            # 解析模板 ZIP：拆分为 CSS 文件 + HTML 结构 + 设计令牌
            template_css_path = None
            template_design_tokens = ''
            template_html_summary = ''
            template_frame_html = ''
            template_raw_frame_html = ''
            template_is_iframe = False
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
            
            # 保存 prompt
            prompt_path = os.path.join(project_folder, 'prompt.txt')
            with open(prompt_path, 'w', encoding='utf-8') as f:
                f.write(prompt)
            
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
            
            # 注册异步任务（含流式传输字段）
            with tasks_lock:
                generating_tasks[project_id] = {
                    'status': STATUS_GENERATING,
                    'progress': 0,
                    'error': '',
                    'accumulated_content': '',   # 流式累积的完整文本
                    'stream_chunks': [],         # SSE 待推送的数据块
                    'stream_event': threading.Event(),  # 通知有新数据
                    'stream_lock': threading.Lock(),     # 保护 stream_chunks
                }
            
            # 启动后台线程
            def generate_in_background():
                # 将 project_id 绑定到线程对象，供 AI 调用时使用
                threading.current_thread()._project_id = project_id
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
                        # iframe 框架模式：保留外框架，只替换内容区
                        if template_is_iframe and template_frame_html:
                            template_section = "\n\n# 现有系统框架（侧边栏+顶栏由系统自动保留）\n\n"
                            template_section += "用户的现有系统采用「侧边栏 + 顶栏 + iframe 内容区」的布局。\n"
                            template_section += "你生成的页面将嵌入到 iframe 中作为主内容区域。\n\n"
                            template_section += "## 外框架侧边栏菜单（供参考，当前激活项用★标记）\n"
                            template_section += "```\n"
                            # 提取侧边栏菜单项文本
                            sidebar_items = re.findall(r'<span>([^<]+)</span>', template_frame_html[:8000])
                            active_match = re.search(r'is-active[^>]*>.*?<span>([^<]+)</span>', template_frame_html[:8000], re.DOTALL)
                            active_text = active_match.group(1) if active_match else ''
                            for item in sidebar_items:
                                marker = ' ★ (当前激活)' if item == active_text else ''
                                template_section += f"  - {item}{marker}\n"
                            template_section += "```\n\n"
                            if template_design_tokens:
                                template_section += f"## 视觉规范（必须严格遵循，确保与外框架风格一致）\n{template_design_tokens}\n\n"
                                template_section += """### 样式一致性要求（非常重要）
你的页面将嵌入到现有系统的 iframe 中，视觉必须与外框架完全融合：
- 页面背景色必须与「页面背景色」一致，不能是白色如果外框架是灰色
- 字体和字号必须与外框架一致（通常是微软雅黑 15px）
- 正文文字色、主色调必须与规范一致
- 组件（按钮、输入框、表格、下拉框）使用 Element UI 风格，与外框架的 Element UI 组件保持一致
- 不要引入与现有系统不协调的配色方案
"""
                            template_section += """## 重要：生成要求

### 内容生成
1. 你只需要生成 iframe 内部的页面内容（即主内容区域的 HTML）
2. 不要生成侧边栏、顶栏、导航等外框架元素
3. 配色方案、字体、组件样式必须与模板设计令牌一致
4. 引用模板 CSS: `<link rel="stylesheet" href="template/template.css">`
5. 生成的 HTML 应该是一个完整的独立页面（有 <!DOCTYPE html>、<head>、<body>）
6. 页面视觉风格必须与现有系统保持一致

### 布局要求（非常重要）
- 生成的内容必须是**单个连续页面**，不要使用 Tab 标签页分页
- 页面应是可垂直滚动的长表单/长页面，所有内容在一个视图中
- 如果需要多个状态（如列表/编辑），使用按钮跳转而不是 Tab 切换
- 参考 Element UI 或 Ant Design 的表单页面风格

### 侧边栏菜单修改（可选）
如果用户需求中提到要在侧边栏添加新菜单项或修改现有菜单项，请在 HTML 代码的最后添加以下格式的注释：
```html
<!-- SIDEBAR_ADD: 菜单名称 -->
```
例如：`<!-- SIDEBAR_ADD: 模型蒸馏 -->`
系统会自动将该菜单项添加到侧边栏中。
"""
                            enhanced_prompt += template_section
                        else:
                            # 普通模板模式：整体参考
                            template_section = "\n\n# 现有系统设计模板（必须严格遵循其视觉风格）\n\n"
                            if template_design_tokens:
                                template_section += f"## 设计令牌（从模板 CSS 提取）\n{template_design_tokens}\n\n"
                            if template_html_summary:
                                template_section += f"## 页面结构参考\n```html\n{template_html_summary}\n```\n\n"
                            if template_css_path:
                                template_section += f"## 模板 CSS 文件\n已保存到 `{template_css_path}`，请在生成的 HTML 中通过 `<link rel=\"stylesheet\" href=\"{template_css_path}\">` 引用它。\n\n"
                            template_section += """## 模板还原要求
1. 配色方案必须与模板设计令牌一致
2. 组件样式（按钮、表格、表单、卡片）必须与模板一致
3. 布局结构参考模板的页面结构
4. 字体、字号、间距与模板保持统一
5. 在 HTML <head> 中添加 <link> 引用模板 CSS 文件
6. 你可以在模板基础上添加新功能，但视觉风格不能偏离
"""
                            enhanced_prompt += template_section

                    if is_incremental and source_html_content and reused_pages > 0:
                        incremental_hint = f"\n\n# 增量更新上下文（重要）\n这是一个增量更新任务。原项目中有{reused_pages}个页面内容未变化。请保持整体风格一致。"
                        if source_html_content:
                            html_preview = source_html_content[:15000]
                            incremental_hint += f"\n\n## 原项目HTML代码（供参考，请保持风格一致）\n```html\n{html_preview}\n```\n\n## 增量更新要求\n1. 保持原项目的整体设计风格、配色方案、组件风格\n2. 新增页面必须与已有页面风格一致\n3. 不要重新设计已有页面，除非用户明确要求"
                        enhanced_prompt += incremental_hint
                    
                    # 使用类似 call_ai_model 的逻辑
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
                    
                    # 下载图片
                    html_content = download_html_images(html_content, project_folder)

                    # iframe 框架拼接：如果模板是 iframe 布局，将 AI 生成的内容嵌入框架
                    if template_is_iframe and (template_raw_frame_html or template_frame_html):
                        logger.info("[异步组装] 检测到 iframe 框架布局，拼接框架+内容...")
                        frame_to_use = template_raw_frame_html or template_frame_html
                        html_content = self.assemble_iframe_html(html_content, frame_to_use, template_css_path)

                    # 注入导航监听器
                    html_content = self.inject_page_navigation_listener(html_content)
                    
                    # 保存HTML
                    html_path = os.path.join(project_folder, 'index.html')
                    if os.path.exists(os.path.dirname(html_path)):
                        with open(html_path, 'w', encoding='utf-8') as f:
                            f.write(html_content)

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
        gen = self.call_ai_model_streaming(prompt, images, cancellable_project_id=thread_project_id)

        try:
            for chunk_text, full_content, done in gen:

                accumulated_content = full_content

                # 推送数据块到 SSE 缓冲区
                if thread_project_id and thread_project_id in generating_tasks:
                    with tasks_lock:
                        task = generating_tasks[thread_project_id]
                        task['accumulated_content'] = accumulated_content

                        if chunk_text:
                            stream_lock = task.get('stream_lock')
                            if stream_lock:
                                with stream_lock:
                                    task['stream_chunks'].append(chunk_text)

                            # 通知 SSE 端点
                            stream_event = task.get('stream_event')
                            if stream_event:
                                stream_event.set()

                            # 启发式进度更新（20 ~ 80 区间）
                            estimated = min(80, 20 + len(accumulated_content) // 100)
                            task['progress'] = estimated

                if done:
                    break
        finally:
            try:
                gen.close()
            except RuntimeError:
                pass

        return self.extract_html(accumulated_content)

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
                'You are a professional UI/UX Developer specializing in high-fidelity HTML prototype generation. '
                'When reference images or HTML templates are provided, you must FIRST carefully analyze every visual detail '
                '(colors, typography, spacing, layout, components), then reproduce the design as accurately as possible '
                'using HTML + Tailwind CSS. When an existing system HTML template is provided, match its design language exactly. '
                'Always respond with complete HTML code, not explanations.')

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ]

            # 动态获取当前选中模型配置
            selected_model = get_selected_model()
            model_name = selected_model.get('model', API_CONFIG.get('model', 'gpt-4'))
            base_url = selected_model.get('base_url', API_CONFIG.get('base_url', ''))
            api_key = selected_model.get('api_key', API_CONFIG.get('api_key', ''))
            logger.info(f"[AI] 使用模型: {selected_model.get('name', model_name)} ({model_name})")
            
            # 准备请求数据（优先使用模型配置，fallback 到全局配置）
            max_tokens = selected_model.get('max_tokens') or AI_OPTIONS.get('max_tokens', 100000)
            temperature = selected_model.get('temperature') or AI_OPTIONS.get('temperature', 0.7)
            payload = {
                "model": model_name,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature
            }
            
            url = f"{base_url}/chat/completions"
            headers = {
                'Content-Type': 'application/json',
                'Authorization': f"Bearer {api_key}"
            }
            
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

    def call_ai_model_streaming(self, prompt_or_messages, images=None, cancellable_project_id=None):
        """流式调用 AI 大模型，逐步产出内容块。

        Args:
            prompt_or_messages: 字符串(prompt+images 构建消息) 或 预构建的 messages 列表
            images: base64 图片列表（仅 prompt_or_messages 为字符串时使用）
            cancellable_project_id: 用于支持外部中断的项目 ID

        Yields:
            (chunk_text, accumulated_content, done) 元组
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
            system_prompt = AI_OPTIONS.get('system_prompt',
                'You are a professional UI/UX Developer specializing in high-fidelity HTML prototype generation. '
                'When reference images or HTML templates are provided, you must FIRST carefully analyze every visual detail '
                '(colors, typography, spacing, layout, components), then reproduce the design as accurately as possible '
                'using HTML + Tailwind CSS. When an existing system HTML template is provided, match its design language exactly. '
                'Always respond with complete HTML code, not explanations.')
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ]

        # 获取模型配置
        selected_model = get_selected_model()
        model_name = selected_model.get('model', API_CONFIG.get('model', 'gpt-4'))
        base_url = selected_model.get('base_url', API_CONFIG.get('base_url', ''))
        api_key = selected_model.get('api_key', API_CONFIG.get('api_key', ''))
        max_tokens = selected_model.get('max_tokens') or AI_OPTIONS.get('max_tokens', 100000)
        temperature = selected_model.get('temperature') or AI_OPTIONS.get('temperature', 0.7)
        timeout = selected_model.get('timeout') or AI_OPTIONS.get('timeout', 300)

        payload = {
            "model": model_name,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True  # 关键：启用流式
        }

        url = f"{base_url}/chat/completions"
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f"Bearer {api_key}"
        }

        logger.info(f"[AI流式] 使用模型: {selected_model.get('name', model_name)} ({model_name})")

        # 尝试流式请求（3 次重试）
        last_error = None
        for attempt in range(3):
            try:
                if attempt > 0:
                    logger.info(f"[AI流式] 重试第 {attempt+1} 次...")

                session = requests.Session()
                session.trust_env = False
                session.headers.update({
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    'Connection': 'close'
                })

                if cancellable_project_id:
                    with tasks_lock:
                        if cancellable_project_id in generating_tasks:
                            generating_tasks[cancellable_project_id]['session'] = session

                response = session.post(
                    url, json=payload, headers=headers,
                    stream=True, timeout=timeout, verify=False
                )
                response.raise_for_status()
                response.encoding = 'utf-8'  # 强制 UTF-8，避免中文乱码

                accumulated = ""
                for line in response.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    if line.startswith('data: '):
                        data_str = line[6:]
                        if data_str.strip() == '[DONE]':
                            break
                        try:
                            chunk = json.loads(data_str)
                            delta = chunk.get('choices', [{}])[0].get('delta', {})
                            content = delta.get('content', '')
                            reasoning = delta.get('reasoning_content', '')
                            if content:
                                accumulated += content
                                yield content, accumulated, False
                            elif reasoning:
                                # 思考内容用特殊标记推送，不计入 accumulated
                                yield f'[think]{reasoning}', accumulated, False
                        except json.JSONDecodeError:
                            continue

                # 流式完成
                logger.info(f"[AI流式] 响应完成，总长度: {len(accumulated)} 字符")
                yield '', accumulated, True
                return

            except Exception as e:
                last_error = e
                logger.warning(f"[AI流式] 第 {attempt+1} 次尝试失败: {e}")
                if attempt < 2:
                    time.sleep(1)

        # 流式全部失败，降级为非流式调用
        logger.info("[AI流式] 全部失败，降级为非流式调用...")
        payload_no_stream = {k: v for k, v in payload.items() if k != 'stream'}
        result = self.call_ai_model_via_curl(url, headers, payload_no_stream, timeout)
        if not result:
            try:
                session = requests.Session()
                session.trust_env = False
                resp = session.post(url, json=payload_no_stream, headers=headers,
                                    timeout=timeout, verify=False)
                resp.raise_for_status()
                result = resp.json()
            except Exception as e2:
                logger.error(f"[AI流式降级] 非流式调用也失败: {e2}")
                raise last_error

        content = result.get('choices', [{}])[0].get('message', {}).get('content', '')
        yield content, content, True

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
        if html and '<html' in html.lower():
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

    def extract_html(self, content):
        """从AI响应中提取HTML代码"""
        # 尝试匹配 ```html 代码块
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
        
        # 返回原始内容作为预览
        return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>生成结果</title>
    <script src="/static/js/tailwindcss.js"></script>
</head>
<body class="bg-gray-100 p-8">
    <div class="bg-white rounded-lg shadow p-6 max-w-4xl mx-auto">
        <h1 class="text-xl font-bold text-red-600 mb-4">⚠️ HTML提取失败</h1>
        <p class="text-gray-600 mb-4">AI返回内容格式不符合预期：</p>
        <pre class="bg-gray-50 p-4 rounded text-sm overflow-auto">{content[:5000]}</pre>
    </div>
</body>
</html>'''

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

    def assemble_iframe_html(self, ai_html, frame_html, css_path='template/template.css'):
        """将 AI 生成的内容 HTML 与外框架 HTML 拼接成最终页面。

        策略：外框架 HTML 中包含 {{AI_GENERATED_CONTENT}} 占位符，
        将 AI 生成的 HTML 作为 iframe srcdoc 的内容嵌入。

        同时进行以下修正：
        1. 移除/修改 iframe 的 sandbox 属性，允许脚本执行和外部资源加载
        2. 将框架中的外部系统链接替换为 javascript:void(0)，防止跳转离开原型页面
        """
        if not frame_html or not ai_html:
            return ai_html

        # 检查 AI 是否已经生成了框架元素（侧边栏等）
        has_sidebar = bool(re.search(r'(sidebar|side-bar|侧边栏)', ai_html, re.IGNORECASE))
        has_navbar = bool(re.search(r'(navbar|nav-bar|top-bar|顶栏|头部导航)', ai_html, re.IGNORECASE))
        if has_sidebar and has_navbar:
            logger.info("[组装] AI 已生成包含框架的完整页面，跳过框架拼接")
            return ai_html

        # ===== 修正框架 HTML =====

        # 1. 移除 CSP (Content-Security-Policy) meta 标签
        # SingleFile 保存的页面可能有严格的 CSP 策略，阻止 srcdoc 内加载外部资源
        frame_html = re.sub(
            r'<meta[^>]*http-equiv=["\']?content-security-policy["\']?[^>]*>',
            '', frame_html, flags=re.IGNORECASE
        )

        # 2. 移除 iframe sandbox 属性
        # 原始系统的 sandbox 限制会阻止 srcdoc 内的脚本执行和资源加载
        # 导出后在 file:// 协议下 sandbox 也会导致跨域冲突，所以直接移除
        frame_html = re.sub(
            r'\s*\bsandbox=["\'][^"\']*["\']',
            '', frame_html, flags=re.IGNORECASE
        )

        # 3. 将框架中的外部系统链接（href）替换为 javascript:void(0)
        # 注意：SingleFile 输出的 HTML 可能省略引号，如 href=http://...
        # 所以需要同时匹配有引号和无引号两种格式
        frame_html = re.sub(
            r'href=(["\']?)https?://[^"\s>]+\1',
            'href="javascript:void(0)"',
            frame_html,
            flags=re.IGNORECASE
        )

        # 4. 移除 IE 版本检测跳转脚本
        frame_html = re.sub(
            r'<!--\[if lt IE [\d]+\]>.*?<!\[endif\]-->',
            '', frame_html, flags=re.DOTALL | re.IGNORECASE
        )
        frame_html = re.sub(
            r"window\.location\.href\s*=\s*['\"][^'\"]*['\"]",
            '', frame_html, flags=re.IGNORECASE
        )

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

        # 6. 解析 AI 输出中的 SIDEBAR_ADD 标记，注入新菜单项到框架侧边栏
        # AI 输出格式: <!-- SIDEBAR_ADD: 菜单名称 -->
        sidebar_additions = re.findall(r'<!--\s*SIDEBAR_ADD:\s*(.+?)\s*-->', ai_html)
        if sidebar_additions:
            logger.info(f"[组装] 检测到侧边栏添加请求: {sidebar_additions}")
            # 从现有菜单中获取一个模板（找一个 nest-menu 项）
            # 典型结构: <div class=nest-menu><a href=...><li ...><span>名称</span></li></a></div>
            menu_item_pattern = r'(<div\s+class=nest-menu><a\s+href=[^>]*><li\s[^>]*>)(.*?<span>)([^<]*)(</span>.*?</li></a></div>)'
            existing_items = list(re.finditer(menu_item_pattern, frame_html, re.DOTALL | re.IGNORECASE))

            if existing_items:
                # 用最后一个菜单项作为模板
                template_item = existing_items[-1]
                template_prefix = template_item.group(1)
                template_inner_before = template_item.group(2)
                template_inner_after = template_item.group(4)

                # 移除原模板中的 is-active 类
                template_prefix_clean = template_prefix.replace(' is-active', '').replace('router-link-exact-active ', '').replace('router-link-active ', '')

                for menu_name in sidebar_additions:
                    new_item = template_prefix_clean + template_inner_before + menu_name + template_inner_after
                    # 在最后一个菜单项后面插入
                    insert_pos = template_item.end()
                    frame_html = frame_html[:insert_pos] + new_item + frame_html[insert_pos:]

                # 取消当前激活项的 is-active，让最后一项（新添加的）成为激活项
                frame_html = frame_html.replace(' is-active', '', 1)  # 只替换第一个（原来的激活项）
                # 给新添加的最后一项加上 is-active
                last_nest_end = frame_html.rfind('</div>', frame_html.rfind('nest-menu'))
                if last_nest_end > 0:
                    # 找新添加项的 li 标签，加入 is-active
                    new_item_start = frame_html.rfind('<div class=nest-menu>', 0, last_nest_end)
                    if new_item_start >= 0:
                        li_pos = frame_html.find('class=el-menu-item', new_item_start)
                        if li_pos >= 0:
                            frame_html = frame_html[:li_pos + len('class=el-menu-item')] + ' is-active' + frame_html[li_pos + len('class=el-menu-item'):]

                logger.info(f"[组装] 已向侧边栏注入 {len(sidebar_additions)} 个菜单项")

            # 确保新增菜单项的 href 也被替换（因为注入在步骤 3 之后）
            frame_html = re.sub(
                r'href=(["\']?)https?://[^"\s>]+\1',
                'href="javascript:void(0)"',
                frame_html,
                flags=re.IGNORECASE
            )

            # 从 AI 输出中移除标记注释
            ai_html = re.sub(r'<!--\s*SIDEBAR_ADD:\s*.+?\s*-->', '', ai_html)

        # ===== 拼接 AI 内容 =====

        # srcdoc 内容需要 HTML 实体编码（& → &amp; 等）
        ai_escaped = ai_html.replace('&', '&amp;').replace('"', '&quot;')

        # 替换占位符
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

        logger.info(f"[组装] 框架+内容拼接完成: 框架 {len(frame_html)} 字符 + 内容 {len(ai_html)} 字符 → 总计 {len(assembled)} 字符")
        return assembled

    def inject_page_navigation_listener(self, html_content):
        """在 HTML 中注入页面切换消息监听器"""
        
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
        
        # 在 </body> 标签前注入
        if '</body>' in html_content:
            html_content = html_content.replace('</body>', listener_script + '\n</body>')
        elif '</html>' in html_content:
            html_content = html_content.replace('</html>', listener_script + '\n</html>')
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
    
    def handle_stop_generation(self):
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
                'You are a professional UI/UX Developer. Generate complete, standalone HTML prototypes with realistic data.')

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
                        for chunk_text, full_content, done in gen:
                            accumulated = full_content
                            with tasks_lock:
                                if project_id in generating_tasks:
                                    task = generating_tasks[project_id]
                                    task['accumulated_content'] = accumulated
                                    if chunk_text:
                                        with task.get('stream_lock', threading.Lock()):
                                            task['stream_chunks'].append(chunk_text)
                                        evt = task.get('stream_event')
                                        if evt:
                                            evt.set()
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

        # ===== 第一步：预处理 — 去掉对设计参考无用的内容 =====

        # 1. 去掉所有 <script> 标签（JS 对设计参考无用）
        cleaned = re.sub(r'<script[^>]*>.*?</script>', '', html_content, flags=re.DOTALL | re.IGNORECASE)

        # 2. 去掉所有 <svg> 标签（图标 SVG 体积大且对 AI 理解布局帮助有限）
        cleaned = re.sub(r'<svg[^>]*>.*?</svg>', '<!-- svg icon -->', cleaned, flags=re.DOTALL | re.IGNORECASE)

        # 3. 去掉 <noscript> 标签
        cleaned = re.sub(r'<noscript[^>]*>.*?</noscript>', '', cleaned, flags=re.DOTALL | re.IGNORECASE)

        # 4. 替换 img 标签中的 data URL 为占位符（保留 alt 和尺寸信息）
        def replace_data_img(m):
            attrs = m.group(1)
            alt_m = re.search(r'alt=["\']([^"\']*)["\']', attrs, re.IGNORECASE)
            width_m = re.search(r'width=["\']([^"\']*)["\']', attrs, re.IGNORECASE)
            height_m = re.search(r'height=["\']([^"\']*)["\']', attrs, re.IGNORECASE)
            alt = alt_m.group(1) if alt_m else 'image'
            w = width_m.group(1) if width_m else ''
            h = height_m.group(1) if height_m else ''
            size = f' {w}x{h}' if w and h else ''
            return f'<img alt="{alt}{size}" src="[image]">'
        cleaned = re.sub(r'<img([^>]*?)src=["\']data:image/[^"\']*["\']([^>]*?)>', replace_data_img, cleaned, flags=re.IGNORECASE)
        # 非 data URL 的 img 保留（可能是外部引用，体积小）
        # 但如果 src 很长也截断
        cleaned = re.sub(r'(<img[^>]*src=["\'][^"\']{200,}["\'][^>]*>)', '<!-- image -->', cleaned, flags=re.IGNORECASE)

        # ===== 第二步：提取 CSS 样式（最高优先级） =====

        # 提取所有 <style> 块
        style_blocks = re.findall(r'<style[^>]*>(.*?)</style>', cleaned, re.DOTALL | re.IGNORECASE)
        styles_text = '\n'.join(style_blocks)

        # CSS 中去掉 data URL（background-image 中的 base64 图片）
        styles_text = re.sub(r'url\(data:image/[^)]*\)', 'url([image])', styles_text, flags=re.IGNORECASE)
        # 去掉 @font-face（字体数据体积大，AI 不需要）
        styles_text = re.sub(r'@font-face\s*\{[^}]*\}', '', styles_text, flags=re.DOTALL | re.IGNORECASE)

        # ===== 第三步：提取 body 布局结构 =====

        body_match = re.search(r'<body[^>]*>(.*)</body>', cleaned, re.DOTALL | re.IGNORECASE)
        body_html = body_match.group(1) if body_match else ''

        # 截断重复列表项（保留前3个）
        body_html = re.sub(
            r'((<li[^>]*>.*?</li>\s*){3})',
            lambda m: m.group(1) + '<!-- ... more items -->',
            body_html,
            flags=re.DOTALL | re.IGNORECASE
        )
        # 截断重复表格行（保留前3个）
        body_html = re.sub(
            r'((<tr[^>]*>.*?</tr>\s*){3})',
            lambda m: m.group(1) + '<!-- ... more rows -->',
            body_html,
            flags=re.DOTALL | re.IGNORECASE
        )
        # 截断重复 div 卡片/列表项（常见于后台管理系统的列表）
        for tag in ['div', 'article', 'section']:
            pattern = rf'((<{tag}[^>]*class=["\'][^"\']*["\'][^>]*>.*?</{tag}>\s*){{3}})'
            body_html = re.sub(
                pattern,
                lambda m: m.group(1) + f'<!-- ... more {tag}s -->',
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
        logger.info(f'[模板] 开始解析 HTML: {total_len} 字符')

        # ===== 1. 快速检测 iframe srcdoc 并定位位置 =====
        # 使用字符串查找比正则快几个数量级
        frame_html = ''
        raw_frame_html = ''
        is_iframe_layout = False
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
                clean_frame = re.sub(r'<script[^>]*>.*?</script>', '', frame_part, flags=re.DOTALL | re.IGNORECASE)
                clean_frame = re.sub(r'<svg[^>]*>.*?</svg>', '', clean_frame, flags=re.DOTALL | re.IGNORECASE)
                clean_frame = re.sub(r'<noscript[^>]*>.*?</noscript>', '', clean_frame, flags=re.DOTALL | re.IGNORECASE)
                clean_frame = re.sub(r'<img[^>]*src=["\']data:image/[^"\']*["\'][^>]*>', '<!-- img -->', clean_frame, flags=re.IGNORECASE)
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
            logger.info('[模板] 未检测到 iframe 布局，使用普通模板模式')

        # ===== 2. 提取 CSS =====
        # 对于大型 HTML，只搜索外层 <head> 部分的 <style> 标签
        # 重要：如果 srcdoc 内容中有 </head>，find 会错误定位到那里
        # 所以限制搜索范围为 iframe 之前（如果存在 iframe）
        if is_iframe_layout:
            css_search_limit = iframe_start
        else:
            head_end = html_content.find('</head>')
            css_search_limit = head_end if head_end > 0 else min(500000, total_len)
        head_section = html_content[:css_search_limit]

        style_blocks = re.findall(r'<style[^>]*>(.*?)</style>', head_section, re.DOTALL | re.IGNORECASE)
        raw_css = '\n'.join(style_blocks)

        # 清洗 CSS：去掉 @font-face、data URL
        clean_css = re.sub(r'@font-face\s*\{[^}]*\}', '', raw_css, flags=re.DOTALL | re.IGNORECASE)
        clean_css = re.sub(r'url\(data:[^)]*\)', 'url()', clean_css, flags=re.IGNORECASE)

        # 去掉第三方库 CSS（通过类名前缀识别整条规则）
        third_party_prefixes = [
            r'\.ql-[\w-]+',           # Quill Editor
            r'\.monaco[\w-]*',        # Monaco Editor
            r'\.CodeMirror[\w-]*',    # CodeMirror
            r'\.cm-[\w-]+',           # CodeMirror
            r'\.katex[\w-]*',         # KaTeX
            r'\.hljs[\w-]*',          # highlight.js
            r'\.swiper[\w-]*',        # Swiper
            r'\.cropper[\w-]*',       # Cropper
            r'\.video-js[\w-]*',      # Video.js
        ]
        for prefix in third_party_prefixes:
            clean_css = re.sub(
                rf'[^{{}}]*{prefix}[^{{}}]*\{{[^{{}}]*\}}',
                '', clean_css, flags=re.IGNORECASE
            )

        # 去掉版权注释块
        clean_css = re.sub(r'/\*![\s\S]*?\*/', '', clean_css)

        # 如果 CSS 仍然超过 200KB，截断
        max_css_size = 200 * 1024
        if len(clean_css) > max_css_size:
            clean_css = clean_css[:max_css_size] + '\n/* ... CSS truncated */'

        # 去掉空行
        clean_css = re.sub(r'\n\s*\n', '\n', clean_css).strip()

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
                # 只取前 50000 字符的 body 内容进行处理
                body_chunk = html_content[body_inner_start:min(body_inner_start + 50000, body_end_tag)]

                # 清理
                body_chunk = re.sub(r'<script[^>]*>.*?</script>', '', body_chunk, flags=re.DOTALL | re.IGNORECASE)
                body_chunk = re.sub(r'<svg[^>]*>.*?</svg>', '', body_chunk, flags=re.DOTALL | re.IGNORECASE)
                body_chunk = re.sub(r'<noscript[^>]*>.*?</noscript>', '', body_chunk, flags=re.DOTALL | re.IGNORECASE)
                body_chunk = re.sub(r'<style[^>]*>.*?</style>', '', body_chunk, flags=re.DOTALL | re.IGNORECASE)
                body_chunk = re.sub(r'<img[^>]*src=["\']data:image/[^"\']*["\'][^>]*>', '<!-- img -->', body_chunk, flags=re.IGNORECASE)
                for tag in ['li', 'tr']:
                    body_chunk = re.sub(
                        rf'((<{tag}[^>]*>.*?</{tag}>\s*){{3}})',
                        lambda m, t=tag: m.group(1) + f'<!-- ... more {t}s -->',
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

        # ===== 4. 从 CSS 提取设计令牌 =====
        design_tokens = CustomHandler._extract_design_tokens(clean_css)

        logger.info(f'[模板] 解析完成: CSS {len(clean_css)} 字符, HTML结构 {len(html_structure)} 字符, 设计令牌 {len(design_tokens)} 字符')

        return {
            'css': clean_css,
            'html_structure': html_structure,
            'design_tokens': design_tokens,
            'frame_html': frame_html,
            'is_iframe_layout': is_iframe_layout,
            'raw_frame_html': raw_frame_html
        }

    @staticmethod
    def _extract_design_tokens(css_text):
        """从 CSS 文本中提取关键设计令牌摘要，用于 AI prompt 注入。
        提取有语义的视觉属性：页面背景色、文字色、主色调、字体等。
        输出精简的设计规范描述，通常在 500-1000 字符以内。"""
        tokens = []

        # ===== 1. 提取有语义的关键样式 =====

        # 页面背景色（body 或 .main-container 的 background-color）
        page_bg = None
        for selector in ['body', '.main-container', '.app-wrapper', '.app-main', '.main-content']:
            m = re.search(re.escape(selector) + r'\s*\{[^}]*background(?:-color)?\s*:\s*([^;}{]+)',
                          css_text, re.IGNORECASE)
            if m:
                val = m.group(1).strip()
                if 'url(' not in val and 'data:' not in val and val != 'transparent':
                    page_bg = val
                    break
        if page_bg:
            tokens.append(f"页面背景色: {page_bg}")

        # 内容区/卡片背景色
        content_bg = None
        for selector in ['.content-container', '.page-container', '.app-main', '.el-main',
                         '.main-content', '.card', '.el-card', '.panel']:
            m = re.search(re.escape(selector) + r'\s*\{[^}]*background(?:-color)?\s*:\s*([^;}{]+)',
                          css_text, re.IGNORECASE)
            if m:
                val = m.group(1).strip()
                if 'url(' not in val and 'data:' not in val and val != 'transparent':
                    content_bg = val
                    break
        if content_bg:
            tokens.append(f"内容区背景色: {content_bg}")

        # body 文字色
        text_color = None
        m = re.search(r'body\s*\{[^}]*color\s*:\s*(#[0-9a-fA-F]{3,8})', css_text, re.IGNORECASE)
        if m:
            text_color = m.group(1)
        else:
            # 从 .el-menu-item 或常规文字提取
            m = re.search(r'(?:\.el-menu-item|\.text-regular|p|span)\s*\{[^}]*color\s*:\s*(#[0-9a-fA-F]{3,8})',
                           css_text, re.IGNORECASE)
            if m:
                text_color = m.group(1)
        if text_color:
            tokens.append(f"正文文字色: {text_color}")

        # 主色调（最常出现的 #1890ff 类颜色）
        primary_color = None
        # 优先从 active/primary 选择器的 color 属性提取主色调
        for selector in ['.el-button--primary', '.el-menu-item.is-active', '.primary',
                         '.el-link--primary', '.active', '.el-pagination button:hover']:
            # 先找 color（文字色=主色调）
            m = re.search(re.escape(selector) + r'\s*\{[^}]*\bcolor\s*:\s*(#[0-9a-fA-F]{3,8})',
                          css_text, re.IGNORECASE)
            if m:
                val = m.group(1).lower()
                if val not in ('#fff', '#ffffff', '#333', '#000', '#303133'):
                    primary_color = m.group(1)
                    break
            # 再找 background-color
            m = re.search(re.escape(selector) + r'\s*\{[^}]*background(?:-color)?\s*:\s*(#[0-9a-fA-F]{3,8})',
                          css_text, re.IGNORECASE)
            if m:
                val = m.group(1).lower()
                if val not in ('#fff', '#ffffff', '#f5f5f5', '#ededed'):
                    primary_color = m.group(1)
                    break
        if not primary_color:
            # 回退：找常见的蓝色系
            m = re.search(r'(?:color|background)\s*:\s*(#1890ff|#409eff|#1677ff|#eb4b4b|#f56c6c)', css_text, re.IGNORECASE)
            if m:
                primary_color = m.group(1)
        if primary_color:
            tokens.append(f"主色调: {primary_color}")

        # 字体
        fonts = set()
        for m in re.finditer(r'body\s*\{[^}]*font-family\s*:\s*([^;}{]+)', css_text, re.IGNORECASE):
            font_val = m.group(1).strip().strip('"\'')
            if 'icon' not in font_val.lower():
                fonts.add(font_val)
        if fonts:
            tokens.append(f"字体: {', '.join(sorted(fonts))}")

        # 基础字号
        base_size = None
        m = re.search(r'body\s*\{[^}]*font-size\s*:\s*([^;}{]+)', css_text, re.IGNORECASE)
        if m:
            base_size = m.group(1).strip()
            tokens.append(f"基础字号: {base_size}")

        # ===== 2. 补充颜色参考 =====
        colors = set()
        for m in re.finditer(r'(?:color|background|border-color)\s*:[^;]*'
                             r'(#[0-9a-fA-F]{3,8})', css_text, re.IGNORECASE):
            colors.add(m.group(1).lower())
        if colors:
            sorted_colors = sorted(colors, key=lambda c: c)
            if len(sorted_colors) > 12:
                sorted_colors = sorted_colors[:12]
            tokens.append(f"其他颜色参考: {', '.join(sorted_colors)}")

        # 圆角
        radii = set()
        for m in re.finditer(r'border-radius\s*:\s*([^;}{]+)', css_text, re.IGNORECASE):
            val = m.group(1).strip()
            if val != '0' and val != '0px':
                radii.add(val)
        if radii:
            tokens.append(f"圆角: {', '.join(sorted(radii)[:5])}")

        # 阴影
        shadows = set()
        for m in re.finditer(r'box-shadow\s*:\s*([^;}{]+)', css_text, re.IGNORECASE):
            val = m.group(1).strip()
            if val != 'none' and len(val) < 80:
                shadows.add(val)
        if shadows:
            sorted_shadows = sorted(shadows)[:3]
            tokens.append(f"阴影: {', '.join(sorted_shadows)}")

        if not tokens:
            return '(未提取到设计令牌)'

        return '\n'.join(tokens)

    # ==================== Inspector 微调 API ====================

    def handle_inspector_apply(self):
        """处理微调模式的 AI 修改请求"""
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
                backup_file = os.path.join(PROJECTS_DIR, project_id, 'index.html.bak')
                with open(backup_file, 'w', encoding='utf-8') as f:
                    f.write(current_html)
                logger.info(f"[Inspector] 备份已创建: {backup_file}")
                
                # 保存修改后的 HTML
                with open(html_file, 'w', encoding='utf-8') as f:
                    f.write(modified_html)
                
                logger.info(f"[Inspector] HTML 已更新: {html_file}")
                self.send_json_response({
                    'success': True, 
                    'message': '修改成功',
                    'backupFile': 'index.html.bak'
                })
                
            except Exception as ai_error:
                logger.info(f"[Inspector] AI 调用失败: {ai_error}")
                import traceback
                traceback.print_exc()
                self.send_error_response(f"AI 调用失败: {str(ai_error)}")
            
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
        """获取项目的页面列表（解析 HTML）"""
        try:
            project_id = query.get('projectId', [''])[0]
            
            if not project_id:
                self.send_error_response("缺少 projectId")
                return
            
            html_file = os.path.join(PROJECTS_DIR, project_id, 'index.html')
            if not os.path.exists(html_file):
                self.send_error_response("项目不存在")
                return
            
            with open(html_file, 'r', encoding='utf-8') as f:
                html_content = f.read()
            
            pages = self.extract_pages_from_html(html_content)
            self.send_json_response({'pages': pages})
            
        except Exception as e:
            logger.error(f"[Pages错误] {e}")
            self.send_error_response(str(e))
    
    def handle_get_flowchart(self, query):
        """生成流程图（解析 HTML 中的页面跳转关系）"""
        try:
            project_id = query.get('projectId', [''])[0]
            
            if not project_id:
                self.send_error_response("缺少 projectId")
                return
            
            html_file = os.path.join(PROJECTS_DIR, project_id, 'index.html')
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
                        'error': task_info.get('error', '')
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
        self.end_headers()

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
                            self._send_sse_data(json.dumps({'content': chunk}, ensure_ascii=False))
                        last_chunk_index = len(task.get('stream_chunks', []))

                    # 发送最终状态事件
                    self._send_sse_event('status', json.dumps({
                        'status': current_status,
                        'error': task.get('error', ''),
                        'progress': task.get('progress', 0)
                    }, ensure_ascii=False))
                    break

                # 排空新数据块
                with task.get('stream_lock', threading.Lock()):
                    chunks = task.get('stream_chunks', [])
                    new_chunks = chunks[last_chunk_index:]
                    last_chunk_index = len(chunks)

                for chunk in new_chunks:
                    self._send_sse_data(json.dumps({'content': chunk}, ensure_ascii=False))

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

    def _send_sse_data(self, data):
        """发送 SSE data 行"""
        self.wfile.write(f'data: {data}\n\n'.encode('utf-8'))
        self.wfile.flush()

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
            for chunk_text, full_content, done in gen:
                content = full_content
                # 推送数据块到 import_tasks（如果有关联任务）
                with tasks_lock:
                    for tid, task in import_tasks.items():
                        if task.get('status') == STATUS_GENERATING and task.get('progress', 0) >= 30:
                            task['accumulated_content'] = full_content
                            if chunk_text:
                                stream_lock = task.get('stream_lock')
                                if stream_lock:
                                    with stream_lock:
                                        task['stream_chunks'].append(chunk_text)
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
