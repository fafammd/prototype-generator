# -*- coding: utf-8 -*-
"""
PRD 讨论管理器 — 需求讨论会话管理

处理：
- 讨论会话 CRUD（discussions.json 索引）
- 讨论状态持久化（session.json）
- 需求规格卡片管理（spec_card.json）
- 需求成熟度追踪（RA0-RA5）
- 两阶段 PRD 生成（预览版/交付版）
- 附件支持（图片/文档上传与 AI 分析）
"""

import json
import os
import io
import time
import uuid
import logging
import base64
import re

logger = logging.getLogger('prototype')


# ==================== 文档文本提取 ====================

def extract_text_from_base64(base64_data, filename):
    """从 base64 编码的文件中提取文本内容。

    支持 .txt, .md (直接读取), .docx (zipfile+XML解析), .pdf (PyPDF2 或提示)
    """
    try:
        # 解码 base64
        if ',' in base64_data:
            base64_data = base64_data.split(',', 1)[1]
        file_bytes = base64.b64decode(base64_data)
    except Exception as e:
        logger.warning(f'Base64 解码失败 {filename}: {e}')
        return ''

    ext = os.path.splitext(filename)[1].lower()

    if ext in ('.txt', '.md'):
        # 尝试多种编码
        for encoding in ('utf-8', 'gbk', 'gb2312', 'latin-1'):
            try:
                return file_bytes.decode(encoding)
            except (UnicodeDecodeError, ValueError):
                continue
        return file_bytes.decode('utf-8', errors='replace')

    elif ext == '.docx':
        return _extract_docx_text(file_bytes)

    elif ext == '.pdf':
        return _extract_pdf_text(file_bytes)

    else:
        logger.warning(f'不支持的文档格式: {ext}')
        return f'[不支持的文件格式: {ext}]'


def _extract_docx_text(file_bytes):
    """使用标准库从 DOCX 文件中提取文本（无需 python-docx）。"""
    try:
        import zipfile
        import xml.etree.ElementTree as ET

        with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
            # DOCX 主体内容在 word/document.xml
            if 'word/document.xml' not in zf.namelist():
                return '[DOCX 文件结构异常：缺少 word/document.xml]'

            with zf.open('word/document.xml') as doc_xml:
                tree = ET.parse(doc_xml)
                root = tree.getroot()

            # Word 命名空间
            ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
            paragraphs = root.findall('.//w:p', ns)

            texts = []
            for para in paragraphs:
                runs = para.findall('.//w:t', ns)
                para_text = ''.join(r.text for r in runs if r.text)
                if para_text.strip():
                    texts.append(para_text.strip())

            return '\n'.join(texts)
    except Exception as e:
        logger.warning(f'DOCX 文本提取失败: {e}')
        return f'[DOCX 文本提取失败: {e}]'


def _extract_pdf_text(file_bytes):
    """尝试使用 PyPDF2 提取 PDF 文本，不可用时提示用户。"""
    try:
        import io
        from PyPDF2 import PdfReader

        reader = PdfReader(io.BytesIO(file_bytes))
        texts = []
        for page in reader.pages:
            text = page.extract_text()
            if text and text.strip():
                texts.append(text.strip())

        return '\n'.join(texts) if texts else '[PDF 中未提取到文本内容，可能为扫描件]'
    except ImportError:
        return '[PDF 文本提取需要安装 PyPDF2：pip install PyPDF2。建议将 PDF 转为 TXT 后上传]'
    except Exception as e:
        logger.warning(f'PDF 文本提取失败: {e}')
        return f'[PDF 文本提取失败: {e}]'


DISCUSSIONS_DIR = 'prd_discussions'
DISCUSSIONS_INDEX = 'discussions.json'
GLOBAL_DISCUSSIONS_DIR = 'prd_discussions_global'  # 创建项目前的全局讨论目录


def get_disc_folder_for_request(project_id):
    """根据请求参数确定讨论存储目录。
    有 project_id → 项目内讨论目录
    无 project_id → 全局讨论目录（创建项目前）
    Returns: (disc_folder, project_folder_or_None)
    """
    if project_id:
        project_folder = os.path.join('projects', project_id)
        return get_discussions_dir(project_folder), project_folder
    # 全局讨论目录
    d = os.path.join('projects', '..', GLOBAL_DISCUSSIONS_DIR)
    os.makedirs(d, exist_ok=True)
    return d, None

# 成熟度级别定义
MATURITY_LEVELS = {
    'RA0': '模糊想法',
    'RA1': '可讨论',
    'RA2': '可分析',
    'RA3': '可设计',
    'RA4': '可实现',
    'RA5': '可交付',
}

# 讨论层级定义
DISCUSSION_LAYERS = {
    'L0': '产品目标与范围',
    'L1': '功能细节',
    'L2': '技术约束与边界',
}


# ==================== 索引管理 ====================

def get_discussions_dir(project_folder=None):
    """返回讨论目录路径。
    project_folder 为 None 时返回 None，由调用者决定全局目录位置。
    """
    if project_folder:
        d = os.path.join(project_folder, DISCUSSIONS_DIR)
        os.makedirs(d, exist_ok=True)
        return d
    return None


def load_discussions_index(project_folder):
    """加载 discussions.json，不存在则返回空索引。
    Returns: (index_dict, index_file_path)
    """
    path = os.path.join(project_folder, DISCUSSIONS_INDEX)
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f), path
        except Exception as e:
            logger.warning(f"[PRD讨论] 索引读取失败: {e}")

    return {
        'active_discussion_id': None,
        'discussions': []
    }, path


def save_discussions_index(index, path):
    """保存讨论索引"""
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(index, f, ensure_ascii=False, indent=2)


def get_active_discussion_id(project_folder):
    """获取活跃讨论 ID"""
    index, _ = load_discussions_index(project_folder)
    return index.get('active_discussion_id')


def get_discussion_dir(project_folder, discussion_id):
    """获取讨论文件夹路径"""
    return os.path.join(
        get_discussions_dir(project_folder), discussion_id)


def get_session_path(project_folder, discussion_id):
    """获取指定讨论的 session.json 路径"""
    return os.path.join(
        get_discussion_dir(project_folder, discussion_id), 'session.json')


# ==================== 讨论 CRUD ====================

def create_discussion(project_folder, title=None, initial_idea=None):
    """创建新的 PRD 讨论会话。
    Returns: 讨论元数据 dict
    """
    disc_id = 'prd_' + uuid.uuid4().hex[:8]
    disc_dir = get_discussion_dir(project_folder, disc_id)
    os.makedirs(disc_dir, exist_ok=True)

    title = title or '需求讨论'
    now = time.time()

    session_data = {
        'discussion_id': disc_id,
        'project_id': os.path.basename(project_folder),
        'title': title,
        'messages': [],
        'maturity_level': 'RA0',
        'current_layer': 'L0',
        'confirmed': [],
        'assumptions': [],
        'open_questions': [],
        'created_at': now,
        'updated_at': now,
    }

    # 保存 session
    session_path = os.path.join(disc_dir, 'session.json')
    with open(session_path, 'w', encoding='utf-8') as f:
        json.dump(session_data, f, ensure_ascii=False, indent=2)

    # 初始化空规格卡片
    spec_card = {
        'product_goal': '',
        'target_users': '',
        'core_value': '',
        'pages': [],
        'global_design': {},
        'navigation': {},
        'confirmed': [],
        'assumptions': [],
        'open_questions': [],
    }
    spec_card_path = os.path.join(disc_dir, 'spec_card.json')
    with open(spec_card_path, 'w', encoding='utf-8') as f:
        json.dump(spec_card, f, ensure_ascii=False, indent=2)

    # 更新索引
    index, index_path = load_discussions_index(project_folder)
    disc_meta = {
        'id': disc_id,
        'title': title,
        'pinned': False,
        'created_at': now,
        'updated_at': now,
        'message_count': 0,
        'maturity_level': 'RA0',
    }
    index['discussions'].append(disc_meta)
    if not index['active_discussion_id']:
        index['active_discussion_id'] = disc_id
    save_discussions_index(index, index_path)

    logger.info(f"[PRD讨论] 创建讨论: {disc_id} ({title})")
    return disc_meta


def delete_discussion(project_folder, discussion_id):
    """删除讨论。不能删除最后一个。
    Returns: (success, error_msg_or_None)
    """
    import shutil

    index, index_path = load_discussions_index(project_folder)

    if len(index['discussions']) <= 1:
        return False, '无法删除最后一个讨论'

    entry = _find_discussion(index, discussion_id)
    if not entry:
        return False, '讨论不存在'

    disc_dir = get_discussion_dir(project_folder, discussion_id)
    if os.path.exists(disc_dir):
        shutil.rmtree(disc_dir)

    index['discussions'] = [
        d for d in index['discussions']
        if d['id'] != discussion_id
    ]

    if index['active_discussion_id'] == discussion_id:
        first = index['discussions'][0]
        index['active_discussion_id'] = first['id']

    save_discussions_index(index, index_path)
    logger.info(f"[PRD讨论] 删除讨论: {discussion_id}")
    return True, None


def rename_discussion(project_folder, discussion_id, title):
    """重命名讨论"""
    index, index_path = load_discussions_index(project_folder)
    for disc in index['discussions']:
        if disc['id'] == discussion_id:
            disc['title'] = title
            break
    save_discussions_index(index, index_path)
    _update_session_field(project_folder, discussion_id, 'title', title)


def switch_discussion(project_folder, discussion_id):
    """切换活跃讨论。
    Returns: (success, disc_meta_or_error_msg)
    """
    index, index_path = load_discussions_index(project_folder)

    entry = _find_discussion(index, discussion_id)
    if not entry:
        return False, '讨论不存在'

    index['active_discussion_id'] = discussion_id
    save_discussions_index(index, index_path)

    logger.info(f"[PRD讨论] 切换到: {discussion_id}")
    return True, entry


def update_discussion_meta(project_folder, discussion_id):
    """更新索引中讨论的 message_count 和 updated_at"""
    index, index_path = load_discussions_index(project_folder)
    session_path = get_session_path(project_folder, discussion_id)
    msg_count = 0
    updated = time.time()
    maturity = 'RA0'
    if os.path.exists(session_path):
        try:
            with open(session_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            msg_count = len(data.get('messages', []))
            updated = data.get('updated_at', updated)
            maturity = data.get('maturity_level', 'RA0')
        except Exception:
            pass
    for disc in index['discussions']:
        if disc['id'] == discussion_id:
            disc['message_count'] = msg_count
            disc['updated_at'] = updated
            disc['maturity_level'] = maturity
            break
    save_discussions_index(index, index_path)


def list_discussions(project_folder):
    """列出所有讨论会话"""
    index, _ = load_discussions_index(project_folder)
    return index


# ==================== 会话管理 ====================

class PRDDiscussionSession:
    """管理一次 PRD 讨论的完整生命周期"""

    MAX_HISTORY_TURNS = 30
    COMPACT_THRESHOLD = 15

    def __init__(self):
        self.discussion_id = ''
        self.project_id = ''
        self.title = ''
        self.messages = []
        self.maturity_level = 'RA0'
        self.current_layer = 'L0'
        self.confirmed = []
        self.assumptions = []
        self.open_questions = []
        self.created_at = time.time()
        self.updated_at = time.time()

    def add_message(self, role, content, attachments=None):
        """添加一条讨论消息

        Args:
            role: 'user', 'assistant', 'system'
            content: 消息文本内容
            attachments: 可选，附件列表 [{type, name, base64?, ext?, url?, extracted_text?}]
                - 图片附件: type='image', base64 包含图片数据
                - 文件附件: type='file', extracted_text 包含提取的文档文本
        """
        msg = {'role': role, 'content': content}
        if attachments:
            # 保存附件元数据（不含 base64 大数据，节省存储）
            msg['attachments'] = []
            for att in attachments:
                att_meta = {
                    'type': att.get('type', ''),
                    'name': att.get('name', ''),
                    'ext': att.get('ext', ''),
                }
                if att.get('url'):
                    att_meta['url'] = att['url']
                if att.get('extracted_text'):
                    att_meta['extracted_text'] = att['extracted_text']
                # 图片：保存 base64 用于 AI vision 调用（后续 get_ai_context 会用到）
                if att.get('type') == 'image' and att.get('base64'):
                    att_meta['base64'] = att['base64']
                msg['attachments'].append(att_meta)
        self.messages.append(msg)
        self.updated_at = time.time()

    def get_ai_context(self, max_tokens=30000):
        """构建发送给 AI 的对话上下文。

        对于包含图片附件的用户消息，构建 vision API 格式（text + image_url）。
        对于包含文档附件的用户消息，将提取的文本内联到消息内容中。
        返回 OpenAI messages 格式列表。不修改 self.messages。
        """
        messages = list(self.messages)
        total = sum(len(m.get('content', '')) for m in messages)

        if total > max_tokens * 2:
            messages = self._create_compacted_view(messages)

        # 转换为 OpenAI API 格式
        api_messages = []
        for msg in messages:
            role = msg.get('role', 'user')
            content = msg.get('content', '')
            attachments = msg.get('attachments', [])

            if role == 'system':
                api_messages.append({'role': 'system', 'content': content})
                continue

            if not attachments:
                api_messages.append({'role': role, 'content': content})
                continue

            # 有附件的用户消息 → 构建多模态 content
            user_content_parts = []

            # 先添加文本
            if content:
                user_content_parts.append({"type": "text", "text": content})

            # 处理附件
            for att in attachments:
                if att.get('type') == 'image' and att.get('base64'):
                    # 图片 → vision API image_url 格式
                    b64 = att['base64']
                    if not b64.startswith('data:image'):
                        # 补全 data URI 前缀
                        ext = att.get('ext', '.png')
                        mime_map = {
                            '.png': 'image/png', '.jpg': 'image/jpeg',
                            '.jpeg': 'image/jpeg', '.gif': 'image/gif',
                            '.webp': 'image/webp', '.svg': 'image/svg+xml'
                        }
                        mime = mime_map.get(ext, 'image/png')
                        b64 = f'data:{mime};base64,{b64}'
                    user_content_parts.append({
                        "type": "image_url",
                        "image_url": {"url": b64}
                    })
                elif att.get('type') == 'file' and att.get('extracted_text'):
                    # 文档 → 内联文本
                    doc_text = att['extracted_text']
                    # 截断过长文档
                    if len(doc_text) > 5000:
                        doc_text = doc_text[:5000] + '\n...(文档内容过长，已截断)'
                    user_content_parts.append({
                        "type": "text",
                        "text": f"\n[文档: {att.get('name', '未知文件')}]\n---\n{doc_text}\n---"
                    })

            # 如果只有文本部分（文档附件被内联），降级为纯文本消息
            has_image = any(p.get('type') == 'image_url' for p in user_content_parts)
            if has_image:
                api_messages.append({'role': role, 'content': user_content_parts})
            else:
                # 合并所有文本部分
                combined_text = ' '.join(
                    p.get('text', '') for p in user_content_parts if p.get('type') == 'text'
                )
                api_messages.append({'role': role, 'content': combined_text})

        return api_messages

    def compact_history(self):
        """历史压缩（已改为非破坏性，保留兼容接口）"""
        pass

    def _create_compacted_view(self, messages):
        """创建压缩的对话视图，不修改 self.messages"""
        if len(messages) <= self.COMPACT_THRESHOLD:
            return messages

        recent = messages[-10:]
        early = messages[:-10]

        summary_parts = []
        for msg in early:
            role = msg.get('role', '')
            content = msg.get('content', '')[:120]
            if role == 'user':
                summary_parts.append(f"用户: {content}")
            elif role == 'assistant':
                summary_parts.append(f"助手: {content}")

        summary = '; '.join(summary_parts[-8:])
        if len(summary) > 800:
            summary = summary[:800] + '...'

        logger.info(f"[PRD讨论] 对话历史压缩视图: {len(early) + len(recent)} → {11} 条")
        return [
            {'role': 'system', 'content': f'之前的讨论摘要: {summary}'},
            *recent
        ]

    def update_spec_from_ai_response(self, ai_response):
        """从 AI 响应中提取并更新规格卡片

        AI 响应可能包含 JSON 块，格式如：
        ```json
        {"maturity": "RA2", "confirmed": [...], "assumptions": [...], "open_questions": [...]}
        ```
        """
        import re
        json_blocks = re.findall(
            r'```json\s*([\s\S]*?)\s*```', ai_response)
        for block in json_blocks:
            try:
                data = json.loads(block)
                if 'maturity' in data:
                    self.maturity_level = data['maturity']
                if 'confirmed' in data:
                    self.confirmed = data['confirmed']
                if 'assumptions' in data:
                    self.assumptions = data['assumptions']
                if 'open_questions' in data:
                    self.open_questions = data['open_questions']
                if 'layer' in data:
                    self.current_layer = data['layer']
                logger.info(
                    f"[PRD讨论] 规格更新: 成熟度={self.maturity_level}, "
                    f"已确认={len(self.confirmed)}, "
                    f"假设={len(self.assumptions)}, "
                    f"待讨论={len(self.open_questions)}")
            except json.JSONDecodeError:
                continue

    def get_spec_card(self):
        """获取当前规格卡片"""
        return {
            'maturity_level': self.maturity_level,
            'current_layer': self.current_layer,
            'confirmed': self.confirmed,
            'assumptions': self.assumptions,
            'open_questions': self.open_questions,
        }

    def save(self, project_folder):
        """持久化到 session.json"""
        disc_dir = get_discussion_dir(project_folder, self.discussion_id)
        os.makedirs(disc_dir, exist_ok=True)

        state = {
            'discussion_id': self.discussion_id,
            'project_id': self.project_id,
            'title': self.title,
            'messages': self.messages,
            'maturity_level': self.maturity_level,
            'current_layer': self.current_layer,
            'confirmed': self.confirmed,
            'assumptions': self.assumptions,
            'open_questions': self.open_questions,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
        }
        session_path = os.path.join(disc_dir, 'session.json')
        try:
            with open(session_path, 'w', encoding='utf-8') as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[PRD讨论] 保存失败: {e}")

        # 同时保存规格卡片
        spec_card = self.get_spec_card()
        spec_card_path = os.path.join(disc_dir, 'spec_card.json')
        try:
            with open(spec_card_path, 'w', encoding='utf-8') as f:
                json.dump(spec_card, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[PRD讨论] 规格卡片保存失败: {e}")

    @staticmethod
    def load(project_folder, discussion_id):
        """从 session.json 恢复讨论会话"""
        session = PRDDiscussionSession()
        session_path = get_session_path(project_folder, discussion_id)

        if os.path.exists(session_path):
            try:
                with open(session_path, 'r', encoding='utf-8') as f:
                    state = json.load(f)
                session.discussion_id = state.get('discussion_id', discussion_id)
                session.project_id = state.get('project_id', '')
                session.title = state.get('title', '')
                session.messages = state.get('messages', [])
                session.maturity_level = state.get('maturity_level', 'RA0')
                session.current_layer = state.get('current_layer', 'L0')
                session.confirmed = state.get('confirmed', [])
                session.assumptions = state.get('assumptions', [])
                session.open_questions = state.get('open_questions', [])
                session.created_at = state.get('created_at', time.time())
                session.updated_at = state.get('updated_at', time.time())
                logger.info(
                    f"[PRD讨论] 恢复会话: {discussion_id}, "
                    f"{len(session.messages)} 条消息, "
                    f"成熟度={session.maturity_level}")
            except Exception as e:
                logger.warning(f"[PRD讨论] 恢复失败，创建新会话: {e}")
        else:
            session.discussion_id = discussion_id

        return session


# ==================== AI Prompt 构建 ====================

def build_discussion_system_prompt(session, project_context=None):
    """构建 PRD 讨论的系统提示"""
    maturity = session.maturity_level
    layer = session.current_layer
    layer_desc = DISCUSSION_LAYERS.get(layer, '产品目标与范围')

    prompt = f"""你是产品需求专家，擅长通过对话帮助用户逐步明确和细化产品需求。

## 当前状态
- 需求成熟度: {maturity} ({MATURITY_LEVELS.get(maturity, '未知')})
- 当前讨论层级: {layer} ({layer_desc})
- 已确认需求: {len(session.confirmed)} 条
- 假设: {len(session.assumptions)} 条
- 待讨论问题: {len(session.open_questions)} 条

## 讨论原则
1. **共创式提问** - 基于用户输入提出建设性建议，而非单纯审问
2. **一次聚焦一个问题** - 避免一次性提出多个问题
3. **渐进深入** - 从宏观到微观，逐步细化
4. **具体化** - 引导用户给出具体、可量化的需求描述
5. **识别假设** - 主动指出用户描述中隐含的假设

## 层级讨论指南
- L0 (产品目标与范围): 关注"我们要构建什么？为谁构建？解决什么问题？"
- L1 (功能细节): 关注"每个功能的具体需求、数据结构、用户流程"
- L2 (技术约束与边界): 关注"技术栈、性能、安全、兼容性等约束"

## 回答格式
每次回答时，先给出你的分析和建议（自然语言），然后在最后用 JSON 块更新规格状态：

```json
{{
  "maturity": "{maturity}",
  "layer": "{layer}",
  "confirmed": ["已确认的需求条目..."],
  "assumptions": ["待验证的假设..."],
  "open_questions": ["需要进一步讨论的问题..."]
}}
```

**成熟度评估标准**:
- RA0: 只有模糊想法
- RA1: 有基本目标和用户画像
- RA2: 核心功能明确，有初步功能列表
- RA3: 功能规格清晰，可开始 UI 设计
- RA4: 技术方案明确，交互细节确定
- RA5: 完整规格，可直接交付开发

当用户回答足够充分时，主动提升成熟度等级。"""

    if project_context:
        prompt += f"\n\n## 项目背景\n{project_context}"

    if session.confirmed:
        confirmed_text = '\n'.join(f'- {item}' for item in session.confirmed)
        prompt += f"\n\n## 已确认的需求\n{confirmed_text}"

    if session.assumptions:
        assumptions_text = '\n'.join(f'- {item}' for item in session.assumptions)
        prompt += f"\n\n## 当前假设\n{assumptions_text}"

    if session.open_questions:
        questions_text = '\n'.join(f'- {item}' for item in session.open_questions)
        prompt += f"\n\n## 待讨论的问题\n{questions_text}"

    return prompt


def build_prd_generation_prompt(session, mode='preview'):
    """构建 PRD 文档生成提示

    Args:
        session: PRDDiscussionSession 实例
        mode: 'preview' (允许 TBD) 或 'delivery' (禁止 TBD)
    """
    is_delivery = mode == 'delivery'

    mode_desc = "交付级" if is_delivery else "预览版"
    tbd_rule = (
        "## 禁止项\n- 禁止使用 TBD、待讨论、待确认等占位符\n"
        "- 所有内容必须明确、具体、可执行\n"
        "- 如果信息不完整，请基于行业标准提供合理的推荐方案\n"
        "- 在不确定的内容后用注释标注 `<!-- 假设: ... -->`"
    ) if is_delivery else (
        "## 允许的占位符\n"
        "- TBD: 待确定的功能或细节\n"
        "- 待讨论: 需要进一步讨论的内容\n"
        "- 待确认: 需要利益相关者确认的内容"
    )

    prompt = f"""你是资深产品经理，请基于以下需求讨论结果生成一份结构化的{mode_desc} PRD 文档。

{tbd_rule}

## 需求规格
- 成熟度: {session.maturity_level}
- 已确认需求: {len(session.confirmed)} 条
- 假设: {len(session.assumptions)} 条
- 待讨论: {len(session.open_questions)} 条

### 已确认的需求
{chr(10).join(f'{i+1}. {item}' for i, item in enumerate(session.confirmed)) if session.confirmed else '暂无'}

### 假设
{chr(10).join(f'{i+1}. {item}' for i, item in enumerate(session.assumptions)) if session.assumptions else '暂无'}

### 待讨论的问题
{chr(10).join(f'{i+1}. {item}' for i, item in enumerate(session.open_questions)) if session.open_questions else '暂无'}

## 讨论历史摘要
{chr(10).join(f'{"用户" if m["role"] == "user" else "助手"}: {m["content"][:150]}' for m in session.messages[-20:])}

## PRD 文档结构要求
请按以下结构生成 Markdown 格式的 PRD：

# 产品需求文档 (PRD)

## 1. 产品概述
### 1.1 产品背景
### 1.2 产品目标
### 1.3 目标用户
### 1.4 核心价值

## 2. 功能需求
### 2.1 功能总览
### 2.2 功能详情（每个功能包含）
- 功能描述
- 用户流程
- 数据结构
- 交互说明
- 验收标准

## 3. 非功能需求
### 3.1 性能要求
### 3.2 安全要求
### 3.3 兼容性要求

## 4. 页面清单
（列出所有页面及其核心功能）

## 5. 数据字典
（关键数据实体和字段定义）

## 6. 术语表

请生成完整的 PRD 文档。"""

    return prompt


def build_requirements_extraction_prompt(prd_markdown):
    """从 PRD 文档中提取结构化需求数据（用于填充生成表单）

    Returns: 与现有 call_ai_for_requirements() 相同的输出格式
    """
    prompt = f"""你是资深产品需求分析师和 UI/UX 设计师。请从以下 PRD 文档中提取结构化的原型设计规格。

## PRD 文档
{prd_markdown}

## 提取要求

请提取以下信息并以 JSON 格式返回：

### 1. global - 全局设计规格
- primaryColor: 主色调（十六进制）
- secondaryColor: 辅助色（十六进制）
- backgroundMode: "light" 或 "dark"
- componentStyle: 组件风格，如 "Ant Design", "Material Design", "Tailwind UI"
- fontFamily: 字体偏好
- designStyle: 整体设计风格描述
- enums: 全局枚举值（跨页面共享的状态、类型等）

### 2. navigation - 导航结构
- type: "sidebar" | "topbar" | "hybrid"
- items: 导航菜单项数组，每项包含 name 和 icon（FontAwesome 图标名）

### 3. pages - 页面详情数组
每个页面包含：
- name: 页面名称
- description: 一句话描述
- layout: 详细布局描述（结构、比例、位置、响应式行为）
- components: UI 组件列表（头部、筛选、数据展示、表单、弹窗、图表、分页等）
- dataStructure: 数据字段描述
- interactions: 用户操作流程和页面响应
- userFlow: 典型用户操作步骤
- enums: 页面特定枚举值

### 枚举提取规则
从表格、状态流转、类型定义、角色列表中提取结构化值：
- 格式: {{"enumName": ["value1", "value2"]}}
- 区分全局枚举（多页面共享）和页面枚举（单页面使用）

请返回纯 JSON，不要包含 markdown 代码块标记：
{{"global": {{...}}, "navigation": {{...}}, "pages": [...]}}

"""
    return prompt


# ==================== 内部辅助 ====================

def _find_discussion(index, discussion_id):
    """在索引中查找讨论条目"""
    for disc in index.get('discussions', []):
        if disc['id'] == discussion_id:
            return disc
    return None


def _update_session_field(project_folder, discussion_id, field, value):
    """更新讨论 session.json 中的单个字段"""
    session_path = get_session_path(project_folder, discussion_id)
    if os.path.exists(session_path):
        try:
            with open(session_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            data[field] = value
            with open(session_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[PRD讨论] 更新字段失败: {e}")
