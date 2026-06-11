# -*- coding: utf-8 -*-
"""
对话管理器 — 多对话标签页支持

处理：
- conversations.json 索引读写
- 对话文件夹 CRUD
- 切换活跃对话（交换 index.html）
- 从旧版单会话自动迁移
- 撤回快照管理
"""

import json
import os
import shutil
import time
import uuid
import logging

logger = logging.getLogger('prototype')

CONVERSATIONS_DIR = 'conversations'
CONVERSATIONS_INDEX = 'conversations.json'


# ==================== 索引管理 ====================

def get_conversations_dir(project_folder):
    """返回 conversations/ 目录路径，不存在则创建"""
    d = os.path.join(project_folder, CONVERSATIONS_DIR)
    os.makedirs(d, exist_ok=True)
    return d


def load_conversations_index(project_folder):
    """加载 conversations.json，不存在则触发迁移或返回空索引。
    Returns: (index_dict, index_file_path)
    """
    path = os.path.join(project_folder, CONVERSATIONS_INDEX)
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f), path
        except Exception as e:
            logger.warning(f"[对话] 索引读取失败: {e}")

    # 检测旧版 session.json，自动迁移
    legacy_session = os.path.join(project_folder, 'session.json')
    if os.path.exists(legacy_session):
        migrate_legacy_session(project_folder)
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f), path
        except Exception:
            pass

    # 全新项目，返回空索引
    return {
        'active_conversation_id': None,
        'conversations': []
    }, path


def save_conversations_index(index, path):
    """保存对话索引"""
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(index, f, ensure_ascii=False, indent=2)


def get_active_conversation_id(project_folder):
    """获取活跃对话 ID"""
    index, _ = load_conversations_index(project_folder)
    return index.get('active_conversation_id')


def get_session_path(project_folder, conversation_id=None):
    """获取指定对话的 session.json 路径。
    conversation_id 为 None 时使用活跃对话。
    无对话系统时回退到项目根 session.json。
    """
    if not conversation_id:
        conversation_id = get_active_conversation_id(project_folder)
    if conversation_id:
        return os.path.join(
            get_conversations_dir(project_folder),
            conversation_id, 'session.json')
    return os.path.join(project_folder, 'session.json')


def get_conversation_dir(project_folder, conversation_id):
    """获取对话文件夹路径"""
    return os.path.join(
        get_conversations_dir(project_folder), conversation_id)


# ==================== 对话 CRUD ====================

def create_conversation(project_folder, title=None, clone_from_id=None):
    """创建新对话，可从已有对话克隆页面状态。
    Returns: 对话元数据 dict
    """
    conv_id = 'conv_' + uuid.uuid4().hex[:8]
    conv_dir = get_conversation_dir(project_folder, conv_id)
    os.makedirs(conv_dir, exist_ok=True)
    os.makedirs(os.path.join(conv_dir, 'snapshots'), exist_ok=True)

    title = title or '新对话'
    now = time.time()

    session_data = {
        'version': 2,
        'project_id': os.path.basename(project_folder),
        'conversation_id': conv_id,
        'title': title,
        'messages': [],
        'design_system': '',
        'page_names': [],
        'page_order': [],
        'global_config': {},
        'has_generated_html': False,
        'srcdoc_frame_html': '',
        '_chat_head_html': '',
        'created_at': now,
        'updated_at': now,
    }

    # 克隆页面状态（不克隆消息）
    if clone_from_id:
        src_dir = get_conversation_dir(project_folder, clone_from_id)
        src_session = os.path.join(src_dir, 'session.json')
        if os.path.exists(src_session):
            try:
                with open(src_session, 'r', encoding='utf-8') as f:
                    src_data = json.load(f)
                # 只克隆轻量元数据，HTML 通过文件复制（无论源是 v1 还是 v2）
                for key in ('page_order', 'global_config',
                            'design_system', '_chat_head_html'):
                    if key in src_data:
                        session_data[key] = src_data[key]
                # srcdoc_frame_html 保留（模板标记，非大体积内容）
                if 'srcdoc_frame_html' in src_data:
                    session_data['srcdoc_frame_html'] = src_data['srcdoc_frame_html']
                # 从源数据提取 page_names
                if 'page_names' in src_data:
                    session_data['page_names'] = src_data['page_names']
                elif src_data.get('pages_html'):
                    session_data['page_names'] = list(
                        src_data['pages_html'].keys())
                # 标记是否有 generated_html
                session_data['has_generated_html'] = bool(
                    src_data.get('generated_html')
                    or os.path.exists(os.path.join(src_dir, 'index.html')))
                # 不克隆 pages_html 和 generated_html 到 JSON
            except Exception as e:
                logger.warning(f"[对话] 克隆源读取失败: {e}")
        # 复制 index.html（HTML 内容的唯一来源）
        src_html = os.path.join(src_dir, 'index.html')
        if os.path.exists(src_html):
            shutil.copy2(src_html, os.path.join(conv_dir, 'index.html'))
        # 复制 pages/ 目录（多文件项目）
        src_pages = os.path.join(src_dir, 'pages')
        dst_pages = os.path.join(conv_dir, 'pages')
        if os.path.isdir(src_pages):
            shutil.copytree(src_pages, dst_pages, dirs_exist_ok=True)
        # 如果源对话没有 index.html 但有 pages_html（v1 格式），
        # 尝试从项目根目录复制
        if not os.path.exists(os.path.join(conv_dir, 'index.html')):
            root_html = os.path.join(project_folder, 'index.html')
            if os.path.exists(root_html):
                shutil.copy2(root_html, os.path.join(conv_dir, 'index.html'))

    # 保存 session
    session_path = os.path.join(conv_dir, 'session.json')
    with open(session_path, 'w', encoding='utf-8') as f:
        json.dump(session_data, f, ensure_ascii=False, indent=2)

    # 更新索引
    index, index_path = load_conversations_index(project_folder)
    conv_meta = {
        'id': conv_id,
        'title': title,
        'pinned': False,
        'created_at': now,
        'updated_at': now,
        'message_count': 0,
    }
    index['conversations'].append(conv_meta)
    if not index['active_conversation_id']:
        index['active_conversation_id'] = conv_id
        _activate_conversation_html(project_folder, conv_id)
    save_conversations_index(index, index_path)

    logger.info(f"[对话] 创建对话: {conv_id} ({title})")
    return conv_meta


def switch_conversation(project_folder, conversation_id):
    """切换活跃对话。
    Returns: (success, conv_meta_or_error_msg)
    """
    index, index_path = load_conversations_index(project_folder)

    conv_entry = _find_conversation(index, conversation_id)
    if not conv_entry:
        return False, '对话不存在'

    # 保存当前活跃对话的 HTML 回其文件夹
    current_active = index.get('active_conversation_id')
    if current_active and current_active != conversation_id:
        _save_active_html_to_conv(project_folder, current_active)

    # 激活新对话
    index['active_conversation_id'] = conversation_id
    save_conversations_index(index, index_path)
    _activate_conversation_html(project_folder, conversation_id)

    logger.info(f"[对话] 切换到: {conversation_id}")
    return True, conv_entry


def delete_conversation(project_folder, conversation_id):
    """删除对话。不能删除最后一个。
    Returns: (success, error_msg_or_None)
    """
    index, index_path = load_conversations_index(project_folder)

    if len(index['conversations']) <= 1:
        return False, '无法删除最后一个对话'

    if not _find_conversation(index, conversation_id):
        return False, '对话不存在'

    # 删除文件夹
    conv_dir = get_conversation_dir(project_folder, conversation_id)
    if os.path.exists(conv_dir):
        shutil.rmtree(conv_dir)

    # 更新索引
    index['conversations'] = [
        c for c in index['conversations']
        if c['id'] != conversation_id
    ]

    # 如果删除的是活跃对话，切换到第一个
    if index['active_conversation_id'] == conversation_id:
        first = index['conversations'][0]
        index['active_conversation_id'] = first['id']
        _activate_conversation_html(project_folder, first['id'])

    save_conversations_index(index, index_path)
    logger.info(f"[对话] 删除对话: {conversation_id}")
    return True, None


def rename_conversation(project_folder, conversation_id, title):
    """重命名对话"""
    index, index_path = load_conversations_index(project_folder)
    for conv in index['conversations']:
        if conv['id'] == conversation_id:
            conv['title'] = title
            break
    save_conversations_index(index, index_path)
    # 同步更新 session.json 中的 title
    _update_session_field(project_folder, conversation_id, 'title', title)


def toggle_pin(project_folder, conversation_id, pinned):
    """切换置顶状态"""
    index, index_path = load_conversations_index(project_folder)
    for conv in index['conversations']:
        if conv['id'] == conversation_id:
            conv['pinned'] = pinned
            break
    save_conversations_index(index, index_path)


def search_conversations(project_folder, query):
    """搜索对话内容，返回匹配的对话列表"""
    index, _ = load_conversations_index(project_folder)
    query_lower = query.lower()
    results = []

    for conv in index['conversations']:
        matched = False
        # 搜索标题
        if query_lower in conv.get('title', '').lower():
            matched = True
        # 搜索消息内容
        if not matched:
            session_path = get_session_path(
                project_folder, conv['id'])
            if os.path.exists(session_path):
                try:
                    with open(session_path, 'r',
                              encoding='utf-8') as f:
                        data = json.load(f)
                    for msg in data.get('messages', []):
                        if query_lower in msg.get(
                                'content', '').lower():
                            matched = True
                            break
                except Exception:
                    pass
        if matched:
            results.append(conv)

    return results


def update_conversation_meta(project_folder, conversation_id):
    """更新索引中对话的 message_count 和 updated_at"""
    index, index_path = load_conversations_index(project_folder)
    session_path = get_session_path(
        project_folder, conversation_id)
    msg_count = 0
    updated = time.time()
    if os.path.exists(session_path):
        try:
            with open(session_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            msg_count = len(data.get('messages', []))
            updated = data.get('updated_at', updated)
        except Exception:
            pass
    for conv in index['conversations']:
        if conv['id'] == conversation_id:
            conv['message_count'] = msg_count
            conv['updated_at'] = updated
            break
    save_conversations_index(index, index_path)


# ==================== 撤回快照 ====================

def save_snapshot(project_folder, conversation_id, message_index,
                  pages_html, page_order, generated_html,
                  srcdoc_frame_html='', chat_head_html=''):
    """保存编辑前的页面状态快照。

    v2 格式：HTML 内容存入文件，JSON 只存元数据引用。
    这样大幅减少快照体积（原来每个快照 200KB+，现在 <1KB）。
    """
    conv_dir = get_conversation_dir(project_folder, conversation_id)
    snap_dir = os.path.join(conv_dir, 'snapshots')
    os.makedirs(snap_dir, exist_ok=True)

    # 将 HTML 内容写入快照专属文件
    snap_prefix = f'msg_{message_index}'
    if generated_html:
        snap_index = os.path.join(snap_dir, f'{snap_prefix}_index.html')
        with open(snap_index, 'w', encoding='utf-8') as f:
            f.write(generated_html)

    if pages_html:
        snap_pages_dir = os.path.join(snap_dir, f'{snap_prefix}_pages')
        os.makedirs(snap_pages_dir, exist_ok=True)
        for page_name, page_html in pages_html.items():
            import re
            safe_name = re.sub(r'[^\w\u4e00-\u9fff-]', '_', page_name)
            page_file = os.path.join(snap_pages_dir, f'{safe_name}.html')
            with open(page_file, 'w', encoding='utf-8') as f:
                f.write(page_html)

    # JSON 只存轻量元数据
    snapshot = {
        'version': 2,
        'page_names': list(pages_html.keys()) if pages_html else [],
        'page_order': page_order,
        'has_generated_html': bool(generated_html),
        'srcdoc_frame_html': srcdoc_frame_html,
        '_chat_head_html': chat_head_html,
    }
    snap_path = os.path.join(snap_dir, f'msg_{message_index}.json')
    with open(snap_path, 'w', encoding='utf-8') as f:
        json.dump(snapshot, f, ensure_ascii=False)
    logger.info(
        f"[对话] 保存快照(v2): {conversation_id}/msg_{message_index}")


def load_snapshot(project_folder, conversation_id, message_index):
    """加载指定消息的快照。
    Returns: snapshot dict 或 None

    v2 格式从文件重建 HTML，v1 格式从 JSON 直接读取。
    """
    conv_dir = get_conversation_dir(project_folder, conversation_id)
    snap_dir = os.path.join(conv_dir, 'snapshots')
    snap_path = os.path.join(snap_dir, f'msg_{message_index}.json')
    if os.path.exists(snap_path):
        try:
            with open(snap_path, 'r', encoding='utf-8') as f:
                snapshot = json.load(f)

            # v2 格式：从文件重建 HTML
            if snapshot.get('version') == 2:
                snap_prefix = f'msg_{message_index}'
                # 重建 generated_html
                snap_index = os.path.join(snap_dir, f'{snap_prefix}_index.html')
                if os.path.exists(snap_index):
                    with open(snap_index, 'r', encoding='utf-8') as f:
                        snapshot['generated_html'] = f.read()
                else:
                    snapshot['generated_html'] = ''

                # 重建 pages_html
                snap_pages_dir = os.path.join(snap_dir, f'{snap_prefix}_pages')
                pages_html = {}
                page_names = snapshot.get('page_names', [])
                if os.path.isdir(snap_pages_dir) and page_names:
                    import re
                    for page_name in page_names:
                        safe_name = re.sub(r'[^\w\u4e00-\u9fff-]', '_', page_name)
                        page_file = os.path.join(snap_pages_dir, f'{safe_name}.html')
                        if os.path.exists(page_file):
                            with open(page_file, 'r', encoding='utf-8') as f:
                                pages_html[page_name] = f.read()
                snapshot['pages_html'] = pages_html

            return snapshot
        except Exception as e:
            logger.warning(f"[对话] 快照读取失败: {e}")
    return None


# ==================== 迁移 ====================

def migrate_legacy_session(project_folder):
    """从旧版单会话迁移到多对话结构。
    幂等操作：重复调用不会出错。
    """
    conversations_dir = get_conversations_dir(project_folder)
    index_path = os.path.join(project_folder, CONVERSATIONS_INDEX)

    # 已经迁移过
    if os.path.exists(index_path):
        return

    conv_id = 'conv_default'
    conv_dir = os.path.join(conversations_dir, conv_id)
    os.makedirs(conv_dir, exist_ok=True)
    os.makedirs(os.path.join(conv_dir, 'snapshots'), exist_ok=True)

    # 复制 session.json
    src_session = os.path.join(project_folder, 'session.json')
    session_data = {}
    if os.path.exists(src_session):
        dst_session = os.path.join(conv_dir, 'session.json')
        shutil.copy2(src_session, dst_session)
        try:
            with open(dst_session, 'r', encoding='utf-8') as f:
                session_data = json.load(f)
            session_data['conversation_id'] = conv_id
            session_data.setdefault('title', '默认对话')
            with open(dst_session, 'w', encoding='utf-8') as f:
                json.dump(session_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[迁移] session.json 处理失败: {e}")

    # 复制 index.html
    src_html = os.path.join(project_folder, 'index.html')
    if os.path.exists(src_html):
        shutil.copy2(src_html, os.path.join(conv_dir, 'index.html'))

    # 复制备份
    src_bak = os.path.join(project_folder, 'index.html.bak')
    if os.path.exists(src_bak):
        shutil.copy2(src_bak, os.path.join(conv_dir, 'index.html.bak'))

    # 写索引
    now = time.time()
    index = {
        'active_conversation_id': conv_id,
        'conversations': [{
            'id': conv_id,
            'title': session_data.get('title', '默认对话'),
            'pinned': False,
            'created_at': session_data.get('created_at', now),
            'updated_at': session_data.get('updated_at', now),
            'message_count': len(session_data.get('messages', [])),
        }]
    }
    with open(index_path, 'w', encoding='utf-8') as f:
        json.dump(index, f, ensure_ascii=False, indent=2)

    logger.info(f"[迁移] {os.path.basename(project_folder)} "
                f"已迁移到多对话结构")


# ==================== 内部辅助 ====================

def _find_conversation(index, conversation_id):
    """在索引中查找对话条目"""
    for conv in index.get('conversations', []):
        if conv['id'] == conversation_id:
            return conv
    return None


def _activate_conversation_html(project_folder, conversation_id):
    """将对话的 index.html 复制到项目根"""
    if not conversation_id:
        return
    src = os.path.join(
        get_conversation_dir(project_folder, conversation_id),
        'index.html')
    dst = os.path.join(project_folder, 'index.html')
    if os.path.exists(src):
        shutil.copy2(src, dst)


def _save_active_html_to_conv(project_folder, conversation_id):
    """将项目根的 index.html 保存回对话文件夹"""
    src = os.path.join(project_folder, 'index.html')
    dst = os.path.join(
        get_conversation_dir(project_folder, conversation_id),
        'index.html')
    if os.path.exists(src):
        shutil.copy2(src, dst)


def _update_session_field(project_folder, conversation_id,
                          field, value):
    """更新对话 session.json 中的单个字段"""
    session_path = get_session_path(project_folder, conversation_id)
    if os.path.exists(session_path):
        try:
            with open(session_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            data[field] = value
            with open(session_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[对话] 更新字段失败: {e}")
