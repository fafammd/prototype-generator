# -*- coding: utf-8 -*-
"""
会话管理器 — 管理生成项目的对话历史和上下文

支持：
- 对话历史持久化（session.json）
- 上下文压缩（长对话自动摘要）
- 页面 HTML 管理（追踪每个页面的当前状态）
- 多轮对话的 AI 上下文构建
"""

import json
import os
import re
import time
import logging

logger = logging.getLogger('prototype')

# ==================== Token 估算 ====================

def _estimate_tokens(text):
    """简单的 token 估算"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if '\u4e00' <= ch <= '\u9fff')
    ascii_chars = len(text) - cjk
    return int(cjk * 2 + ascii_chars * 0.25)


# ==================== HTML 结构摘要 ====================

def html_to_context_summary(html, max_chars=4000):
    """将 HTML 压缩为 AI 可理解的结构摘要

    保留标签+class+关键属性，丢弃冗余内容。
    """
    if not html:
        return ''

    # 移除 script、style、svg
    html = re.sub(r'<script[^>]*>[\s\S]*?</script>', '', html, flags=re.IGNORECASE)
    html = re.sub(r'<style[^>]*>[\s\S]*?</style>', '', html, flags=re.IGNORECASE)
    html = re.sub(r'<svg[^>]*>[\s\S]*?</svg>', '', html, flags=re.IGNORECASE)

    # 提取 body 内容
    body_match = re.search(r'<body[^>]*>([\s\S]*?)</body>', html, re.IGNORECASE)
    if body_match:
        html = body_match.group(1)

    # 简化：保留标签名 + class + 关键属性，截断内容
    lines = []
    for line in html.split('\n'):
        stripped = line.strip()
        if not stripped:
            continue
        # 移除属性值过长的属性
        stripped = re.sub(r'(style|src|href)="[^"]{50,}"', '', stripped)
        # 截断过长行
        if len(stripped) > 200:
            stripped = stripped[:200] + '...'
        lines.append(stripped)

    result = '\n'.join(lines)
    if len(result) > max_chars:
        result = result[:max_chars] + '\n... (truncated)'
    return result


# ==================== GenerationSession ====================

class GenerationSession:
    """管理一次生成任务的完整生命周期，包括多轮对话"""

    SESSION_FILE = 'session.json'
    MAX_HISTORY_TURNS = 20
    COMPACT_THRESHOLD = 10
    MAX_CONTEXT_TOKENS = 40000

    def __init__(self, project_id, project_folder):
        self.project_id = project_id
        self.project_folder = project_folder
        self.conversation_id = ''      # 对话 ID（多对话支持）
        self.title = ''                # 对话标题
        self.messages = []             # [{role, content, html_changes?}]
        self.design_system = ''        # CSS 设计系统文本
        self.pages_html = {}           # {page_name: html_fragment}
        self.page_order = []           # [page_name, ...] 保持页面顺序
        self.global_config = {}        # 全局设计配置
        self.generated_html = ''       # 最终组装的完整 HTML
        self.srcdoc_frame_html = ''    # srcdoc 项目的外框架 HTML（含占位符）
        self._chat_head_html = ''     # 对话模式：侧边栏项目的 head 区域（CSS）
        self.created_at = time.time()
        self.updated_at = time.time()

    def add_message(self, role, content, html_changes=None):
        """添加一条对话消息"""
        # 部分 API（如讯飞）严格要求 assistant 消息必须有 content
        # 如果 content 为空，填充占位文本避免后续 API 调用被拒
        if role == 'assistant' and not content:
            content = '[无响应]'
        msg = {'role': role, 'content': content}
        if html_changes:
            msg['html_changes'] = html_changes
        self.messages.append(msg)
        self.updated_at = time.time()

    def get_ai_context(self, max_tokens=None):
        """构建发送给 AI 的对话上下文，控制在 token 预算内

        Returns:
            list[dict]: OpenAI 格式的 messages 列表
        """
        max_tokens = max_tokens or self.MAX_CONTEXT_TOKENS
        # 过滤掉空的 assistant 消息（部分 API 如讯飞严格要求
        # assistant 消息必须有 content 或 tool_calls）
        messages = []
        for m in self.messages:
            role = m.get('role', '')
            content = m.get('content', '')
            if role == 'assistant' and not content and not m.get('tool_calls'):
                continue  # 跳过空的 assistant 消息
            messages.append(m)

        total = sum(_estimate_tokens(m.get('content', '')) for m in messages)

        if total > max_tokens:
            self.compact_history()
            messages = list(self.messages)
            total = sum(_estimate_tokens(m.get('content', '')) for m in messages)

        # 如果还是超，截断页面 HTML 内容
        if total > max_tokens:
            messages = self._truncate_html_in_messages(messages, max_tokens)

        return messages

    def compact_history(self):
        """将早期对话压缩为摘要，保留最近 N 轮完整"""
        if len(self.messages) <= self.COMPACT_THRESHOLD:
            return

        # 保留最近 5 轮（10 条消息：用户+AI 各 5 条）
        recent = self.messages[-10:]
        early = self.messages[:-10]

        # 生成简单摘要
        summary_parts = []
        for msg in early:
            if msg.get('role') == 'user':
                summary_parts.append(f"用户: {msg.get('content', '')[:80]}")
            elif msg.get('role') == 'assistant':
                content = msg.get('content', '')[:80]
                summary_parts.append(f"助手: {content}")

        summary = '; '.join(summary_parts[-6:])  # 最近 6 条
        if len(summary) > 500:
            summary = summary[:500] + '...'

        self.messages = [
            {'role': 'system', 'content': f'之前的调整摘要: {summary}'},
            *recent
        ]
        logger.info(f"[会话] 对话历史压缩: {len(early) + len(recent)} → {len(self.messages)} 条")

    def _truncate_html_in_messages(self, messages, max_tokens):
        """截断消息中的 HTML 内容以控制在预算内

        处理三种大文本来源：
        1. 完整 HTML 页面（<!DOCTYPE> 或 <html> 开头）
        2. read_page 工具结果（"页面 [...] 完整内容" 格式）
        3. edit_file 工具结果（代码块中的 HTML）
        """
        truncated = []
        for msg in messages:
            content = msg.get('content', '')
            if not content or len(content) <= 4000:
                truncated.append(msg)
                continue

            # 情况 1：完整 HTML 页面
            if '<html' in content or '<!DOCTYPE' in content:
                summary = html_to_context_summary(content, max_chars=2000)
                new_msg = dict(msg)
                new_msg['content'] = f'(HTML 结构摘要)\n{summary}'
                truncated.append(new_msg)
                continue

            # 情况 2：read_page 工具结果
            if msg.get('role') == 'tool' and '完整内容' in content and '```' in content:
                # 提取页面名称并替换为摘要引用
                page_match = re.search(r'页面\s*\[([^\]]+)\]', content)
                page_name = page_match.group(1) if page_match else '未知'
                char_count = len(content)
                new_msg = dict(msg)
                new_msg['content'] = (
                    f'(页面 [{page_name}] 内容已在之前的对话轮次中读取，'
                    f'共 {char_count} 字符。如需再次查看请重新调用 read_page 工具。)'
                )
                truncated.append(new_msg)
                continue

            # 情况 3：超长普通消息（截断）
            if len(content) > 6000:
                new_msg = dict(msg)
                new_msg['content'] = content[:4000] + '\n... (内容已截断)'
                truncated.append(new_msg)
                continue

            truncated.append(msg)
        return truncated

    def update_page(self, page_name, html):
        """更新某个页面的 HTML"""
        self.pages_html[page_name] = html
        self.updated_at = time.time()

    def get_current_html(self):
        """获取当前完整的 HTML"""
        return self.generated_html

    def save(self, save_path=None):
        """持久化到 session.json。
        save_path: 可选，指定保存路径（多对话时用对话目录下的路径）。
        """
        state = {
            'project_id': self.project_id,
            'conversation_id': self.conversation_id,
            'title': self.title,
            'messages': self.messages,
            'design_system': self.design_system,
            'pages_html': self.pages_html,
            'page_order': self.page_order,
            'global_config': self.global_config,
            'generated_html': self.generated_html,
            'srcdoc_frame_html': self.srcdoc_frame_html,
            '_chat_head_html': self._chat_head_html,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
        }
        state_path = save_path or os.path.join(
            self.project_folder, self.SESSION_FILE)
        try:
            with open(state_path, 'w', encoding='utf-8') as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[会话] 保存失败: {e}")

    @staticmethod
    def load(project_id, project_folder, session_path=None):
        """从 session.json 恢复会话。
        session_path: 可选，指定加载路径（多对话时用对话目录下的路径）。
        """
        session = GenerationSession(project_id, project_folder)
        state_path = session_path or os.path.join(
            project_folder, GenerationSession.SESSION_FILE)

        if os.path.exists(state_path):
            try:
                with open(state_path, 'r', encoding='utf-8') as f:
                    state = json.load(f)
                session.messages = state.get('messages', [])
                # 清理空的 assistant 消息（避免部分 API 拒绝请求）
                session.messages = [
                    m for m in session.messages
                    if not (m.get('role') == 'assistant'
                            and not m.get('content')
                            and not m.get('tool_calls'))
                ]
                session.conversation_id = state.get(
                    'conversation_id', '')
                session.title = state.get('title', '')
                session.design_system = state.get('design_system', '')
                session.pages_html = state.get('pages_html', {})
                session.page_order = state.get('page_order', [])
                session.global_config = state.get('global_config', {})
                session.generated_html = state.get('generated_html', '')
                session.srcdoc_frame_html = state.get(
                    'srcdoc_frame_html', '')
                session._chat_head_html = state.get(
                    '_chat_head_html', '')
                session.created_at = state.get(
                    'created_at', time.time())
                session.updated_at = state.get(
                    'updated_at', time.time())
                logger.info(
                    f"[会话] 恢复会话: {len(session.messages)} "
                    f"条历史消息, "
                    f"{len(session.pages_html)} 个页面")
            except Exception as e:
                logger.warning(
                    f"[会话] 恢复失败，创建新会话: {e}")

        # 如果没有 generated_html，尝试从 index.html 加载
        if not session.generated_html:
            html_path = os.path.join(project_folder, 'index.html')
            if os.path.exists(html_path):
                with open(html_path, 'r', encoding='utf-8') as f:
                    session.generated_html = f.read()

        # 如果没有 design_system，尝试从 HTML 提取
        if not session.design_system and session.generated_html:
            root_match = re.search(r':root\s*\{([\s\S]*?)\}', session.generated_html)
            if root_match:
                session.design_system = ':root {' + root_match.group(1) + '}'

        return session

    def extract_pages_from_html(self, html):
        """从完整 HTML 中提取页面片段

        支持两种格式：
        1. v-if 分区（旧格式）
        2. srcdoc JS 字符串（assemble_multi_page_html 生成的格式）

        用于首次从生成结果初始化会话。
        """
        pages = {}

        # 方式 1：匹配 pageData 中的 JS 字符串条目
        # 格式: 'pageName': 'escaped_html_content'
        # 因为内容中有转义引号 \\' 和转义换行 \\n，不能用简单的 [^']+ 匹配
        # 使用逐字符解析来正确处理转义
        page_data_marker = 'pageData'
        pd_idx = html.find(page_data_marker)
        if pd_idx != -1:
            # 找到 pageData 对象的开始 {
            brace_start = html.find('{', pd_idx)
            if brace_start != -1:
                # 提取整个 pageData 对象内容
                brace_count = 1
                pos = brace_start + 1
                while pos < len(html) and brace_count > 0:
                    if html[pos] == '{':
                        brace_count += 1
                    elif html[pos] == '}':
                        brace_count -= 1
                    pos += 1
                page_data_str = html[brace_start:pos]

                # 解析每个 'key': 'value' 条目
                i = 0
                while i < len(page_data_str):
                    # 找 key
                    key_start = page_data_str.find("'", i)
                    if key_start == -1:
                        break
                    key_end = page_data_str.find("'", key_start + 1)
                    if key_end == -1:
                        break
                    page_name = page_data_str[key_start + 1:key_end]

                    # 找 : 后的 value（跳过转义引号）
                    colon_idx = page_data_str.find(':', key_end)
                    if colon_idx == -1:
                        break
                    val_start = page_data_str.find("'", colon_idx)
                    if val_start == -1:
                        break

                    # 逐字符读取 value，处理转义
                    val_chars = []
                    j = val_start + 1
                    while j < len(page_data_str):
                        ch = page_data_str[j]
                        if ch == '\\' and j + 1 < len(page_data_str):
                            next_ch = page_data_str[j + 1]
                            if next_ch == "'":
                                val_chars.append("'")
                                j += 2
                            elif next_ch == 'n':
                                val_chars.append('\n')
                                j += 2
                            elif next_ch == '\\':
                                val_chars.append('\\')
                                j += 2
                            elif next_ch == '/':
                                val_chars.append('/')
                                j += 2
                            else:
                                val_chars.append(ch)
                                j += 1
                        elif ch == "'":
                            # 未转义的引号 = 值结束
                            break
                        else:
                            val_chars.append(ch)
                            j += 1

                    content = ''.join(val_chars)
                    if content.strip():
                        pages[page_name] = content

                    i = j + 1

        # 方式 2：匹配 v-if="currentPage === 'xxx'" 模式（旧格式回退）
        if not pages:
            pattern = re.compile(
                r'<div\s+v-if="currentPage\s*===\s*\'([^\']+)\'"[^>]*>([\s\S]*?)</div>\s*(?=<div\s+v-if|$)',
                re.IGNORECASE
            )
            for match in pattern.finditer(html):
                page_name = match.group(1)
                content = match.group(2)
                pages[page_name] = content

        return pages
