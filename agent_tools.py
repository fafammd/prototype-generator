"""
agent_tools.py — Agent Loop 工具系统

参照 CCB (claude-code-best/claude-code) 架构设计的工具基础设施：
- Tool 基类：所有 Agent 工具的抽象基类
- ToolRegistry：工具注册和查找
- ToolResult / ToolContext：类型定义
- PermissionManager：工具权限控制
- 内置工具：ReadPage / EditFile / ListPages / AddPage / EditPage / ReadFile

设计原则：
- 不可变模式：工具返回新的 ToolResult，不修改输入
- OpenAI 消息格式：统一用 OpenAI function calling 格式
- 零外部依赖：仅使用 Python 标准库
"""

import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# 类型定义
# ---------------------------------------------------------------------------


@dataclass
class ToolResult:
    """工具执行结果 (不可变值对象)"""
    content: str
    success: bool = True
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_message(self, tool_call_id: str, tool_name: str) -> Dict[str, Any]:
        """转换为 OpenAI tool 消息格式"""
        return {
            'role': 'tool',
            'tool_call_id': tool_call_id,
            'name': tool_name,
            'content': self.content,
        }


@dataclass
class ToolContext:
    """传递给工具的只读上下文"""
    project_id: str
    project_folder: str
    server: Any = None  # CustomHandler 实例 (用于 AI 调用、SSE 等)
    session: Any = None  # GenerationSession 实例
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def pages_html(self) -> Dict[str, str]:
        """获取页面 HTML 字典"""
        if self.session and hasattr(self.session, 'pages_html'):
            return self.session.pages_html or {}
        return {}

    @property
    def page_order(self) -> List[str]:
        """获取页面顺序"""
        if self.session and hasattr(self.session, 'page_order'):
            return self.session.page_order or []
        return []


# ---------------------------------------------------------------------------
# 权限系统
# ---------------------------------------------------------------------------


class PermissionDecision(Enum):
    """权限决策"""
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


@dataclass
class PermissionRule:
    """工具权限规则"""
    tool_name: str  # 工具名，'*' 匹配所有
    decision: PermissionDecision
    condition: Optional[Callable[[Dict, ToolContext], bool]] = None


class PermissionManager:
    """工具权限管理器

    用法：
        pm = PermissionManager()
        pm.add_rule(PermissionRule('read_page', PermissionDecision.ALLOW))
        pm.add_rule(PermissionRule('edit_file', PermissionDecision.ASK))
        pm.add_rule(PermissionRule('*', PermissionDecision.DENY))
    """

    def __init__(self) -> None:
        self._rules: List[PermissionRule] = []

    def add_rule(self, rule: PermissionRule) -> None:
        self._rules.append(rule)

    def check(self, tool_name: str, args: Dict[str, Any],
              context: ToolContext) -> PermissionDecision:
        """检查工具是否被允许执行

        优先级：特定规则 > 通配符规则（同优先级下后添加的优先）
        """
        # 第一轮：查找特定工具的规则
        for rule in reversed(self._rules):
            if rule.tool_name == tool_name:
                if rule.condition and not rule.condition(args, context):
                    continue
                return rule.decision
        # 第二轮：回退到通配符规则
        for rule in reversed(self._rules):
            if rule.tool_name == '*':
                if rule.condition and not rule.condition(args, context):
                    continue
                return rule.decision
        return PermissionDecision.ALLOW  # 默认允许

    def allow_all(self) -> 'PermissionManager':
        """快捷方法：允许所有工具"""
        self._rules.insert(0, PermissionRule('*', PermissionDecision.ALLOW))
        return self


# ---------------------------------------------------------------------------
# 工具基类
# ---------------------------------------------------------------------------


class Tool(ABC):
    """所有 Agent 工具的抽象基类"""

    @property
    @abstractmethod
    def name(self) -> str:
        """工具名称"""

    @property
    @abstractmethod
    def description(self) -> str:
        """工具描述（给 AI 使用）"""

    @property
    def parameters_schema(self) -> Dict[str, Any]:
        """OpenAI function calling 参数 JSON Schema"""
        return {"type": "object", "properties": {}, "required": []}

    @property
    def concurrency_safe(self) -> bool:
        """是否可并发执行（只读工具返回 True）"""
        return False

    def validate_input(self, args: Dict[str, Any]) -> Optional[str]:
        """校验工具输入参数

        对应 CCB 的 Zod inputSchema.safeParse() 校验。
        子类可覆盖此方法提供更详细的校验。

        Returns:
            None — 校验通过
            str — 错误消息
        """
        schema = self.parameters_schema
        required = schema.get('required', [])
        properties = schema.get('properties', {})
        errors = []

        # 检查必填参数
        for param_name in required:
            if param_name not in args or args[param_name] is None:
                errors.append(f"缺少必填参数 '{param_name}'")

        # 检查参数类型
        for param_name, value in args.items():
            if param_name not in properties:
                continue
            expected_type = properties[param_name].get('type')
            if expected_type and value is not None:
                type_ok = True
                if expected_type == 'string' and not isinstance(value, str):
                    type_ok = False
                elif expected_type == 'integer' and not isinstance(value, int):
                    type_ok = False
                elif expected_type == 'number' and not isinstance(value, (int, float)):
                    type_ok = False
                elif expected_type == 'boolean' and not isinstance(value, bool):
                    type_ok = False
                if not type_ok:
                    errors.append(
                        f"参数 '{param_name}' 类型错误：期望 {expected_type}，"
                        f"实际 {type(value).__name__}"
                    )

        if errors:
            return '；'.join(errors)
        return None

    @abstractmethod
    def execute(self, args: Dict[str, Any], context: ToolContext) -> ToolResult:
        """执行工具"""

    def to_openai_spec(self) -> Dict[str, Any]:
        """转换为 OpenAI function calling 格式"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters_schema,
            }
        }


# ---------------------------------------------------------------------------
# 工具注册表
# ---------------------------------------------------------------------------


class ToolRegistry:
    """工具注册表

    用法：
        registry = ToolRegistry()
        registry.register(ReadPageTool())
        registry.register(EditFileTool())

        # 获取 OpenAI 格式工具定义
        specs = registry.get_tools_spec(['read_page', 'edit_file'])

        # 执行工具
        result = registry.execute_tool('read_page', {'page': '首页'}, ctx)
    """

    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def get_all_names(self) -> List[str]:
        return list(self._tools.keys())

    def get_tools_spec(self, tool_names: Optional[List[str]] = None) -> List[Dict]:
        """获取 OpenAI 格式的工具定义列表"""
        if tool_names is None:
            return [t.to_openai_spec() for t in self._tools.values()]
        return [
            self._tools[n].to_openai_spec()
            for n in tool_names
            if n in self._tools
        ]

    def execute_tool(self, name: str, args: Dict[str, Any],
                     context: ToolContext) -> ToolResult:
        """执行工具调用"""
        tool = self._tools.get(name)
        if not tool:
            return ToolResult(
                content=f"错误：未知工具 '{name}'。可用工具: {list(self._tools.keys())}",
                success=False,
            )
        # 输入校验 (对应 CCB 的 Zod safeParse)
        validation_error = tool.validate_input(args)
        if validation_error:
            return ToolResult(
                content=f"工具 '{name}' 参数校验失败：{validation_error}",
                success=False,
            )
        try:
            return tool.execute(args, context)
        except Exception as e:
            return ToolResult(
                content=f"工具 '{name}' 执行异常: {type(e).__name__}: {e}",
                success=False,
            )


# ---------------------------------------------------------------------------
# 内置工具实现
# ---------------------------------------------------------------------------


class ReadPageTool(Tool):
    """读取页面 HTML 内容

    支持按行号范围读取、大页面自动截断、子页面引导。
    来自 server.py:8860-9013 的 read_page 工具逻辑。
    """

    MAX_READ_CHARS = 150000

    @property
    def name(self) -> str:
        return "read_page"

    @property
    def description(self) -> str:
        return (
            "Read the HTML source of a page. "
            "You MUST call this before using edit_file or write_page.\n"
            "The output uses line numbers (e.g. '  123→<div>'). "
            "When copying content for edit_file's old_string, "
            "copy ONLY the content after the arrow, NOT the line number prefix.\n"
            "For large pages, use start_line and end_line to read "
            "only the section you need to modify (typically 20-50 lines). "
            "For small pages (<1000 lines), you can read the entire page."
        )

    @property
    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "page": {
                    "type": "string",
                    "description": "Page name (must match exactly a name from list_pages)",
                },
                "start_line": {
                    "type": "integer",
                    "description": "Line number to start reading from (1-indexed). Only provide if the page is too large to read at once",
                },
                "end_line": {
                    "type": "integer",
                    "description": "Line number to end reading at (inclusive). Only provide if the page is too large to read at once.",
                },
            },
            "required": ["page"],
        }

    @property
    def concurrency_safe(self) -> bool:
        return True

    def execute(self, args: Dict[str, Any], context: ToolContext) -> ToolResult:
        req_page = args.get('page', '')
        start_line = args.get('start_line')
        end_line = args.get('end_line')
        pages = context.pages_html

        if not pages and context.session and hasattr(context.session, 'generated_html'):
            pages = {'主页面': context.session.generated_html}

        if not pages:
            return ToolResult(
                content="错误：项目中没有可用页面。",
                success=False,
            )

        # 特殊处理：index.html 是组装页面
        if req_page.lower().endswith('index.html') and len(context.page_order) > 1:
            guide = (
                f"'index.html' is an assembled file "
                f"(reading it directly would exceed the context limit).\n\n"
                f"Use one of these page names instead:\n"
            )
            for pn in context.page_order:
                if pn in pages:
                    p_len = len(pages[pn])
                    guide += f'- read_page(page="{pn}") ({p_len:,} 字符)\n'
            return ToolResult(content=guide, success=True,
                              metadata={'redirected': True})

        # 精确匹配页面名
        target_html = None
        target_name = req_page
        if req_page in pages:
            target_html = pages[req_page]
            target_name = req_page

        if not target_html:
            return ToolResult(
                content=f"Error: page '{req_page}' not found. Available pages: {list(pages.keys())}. Use the exact page name.",
                success=False,
            )

        # 按行号范围读取
        if start_line and end_line:
            lines = target_html.split('\n')
            s = max(0, start_line - 1)
            e = min(len(lines), end_line)
            selected = lines[s:e]
            numbered = '\n'.join(
                f'{start_line + i:>6}→{line}'
                for i, line in enumerate(selected)
            )
            content = (
                f"[{target_name}] lines {start_line}-{end_line} "
                f"({e - s + 1} lines):\n"
                f"{numbered}"
            )
        else:
            all_lines = target_html.split('\n')
            total_lines = len(all_lines)

            if len(target_html) <= self.MAX_READ_CHARS:
                numbered_all = '\n'.join(
                    f'{i + 1:>6}→{line}'
                    for i, line in enumerate(all_lines)
                )
                content = (
                    f"[{target_name}] full content "
                    f"({total_lines} lines, {len(target_html)} chars):\n"
                    f"{numbered_all}"
                )
            else:
                result_lines = []
                used = 0
                for i, line_item in enumerate(all_lines):
                    if used + len(line_item) + 1 > self.MAX_READ_CHARS:
                        result_lines.append(
                            f"\n[truncated] page has {total_lines} lines, "
                            f"returned first {i}. Use start_line={i + 1} to continue."
                        )
                        break
                    result_lines.append(f'{i + 1:>6}→{line_item}')
                    used += len(line_item) + 1

                content = (
                    f"[{target_name}] full content "
                    f"({total_lines} lines, {len(target_html)} chars):\n"
                    + '\n'.join(result_lines)
                )

        total_lines = len(target_html.split('\n'))
        return ToolResult(
            content=content,
            success=True,
            metadata={
                'page': target_name,
                'content_length': len(content),
                'total_lines': total_lines,
                'line_start': start_line,
                'line_end': end_line,
            },
        )


def _norm_to_orig(original: str, norm_idx: int) -> Optional[int]:
    """将空白归一化字符串的索引映射回原始字符串索引

    来自 server.py:6294-6312 的 _norm_to_orig 方法。
    """
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


def _apply_edit_three_layer(
    current_html: str, old_string: str, new_string: str,
    replace_all: bool = False,
) -> Tuple[Optional[str], Optional[str], bool]:
    """三层编辑匹配：精确 → 归一化 → 逐行剥离

    来自 server.py:9138-9402 的编辑匹配逻辑。

    Returns:
        (new_html, matched_old_text, applied)
        - new_html: 编辑后的 HTML (如果匹配成功)
        - matched_old_text: 实际匹配到的原始文本
        - applied: 是否成功应用
    """
    applied = False
    matched_old = None
    new_html = None

    # 第一层：精确匹配
    idx = current_html.find(old_string)
    if idx != -1:
        second_idx = current_html.find(old_string, idx + 1)
        if second_idx != -1 and not replace_all:
            # 多处匹配且未指定 replace_all
            return None, None, False
        if replace_all:
            new_html = current_html.replace(old_string, new_string)
        else:
            new_html = (
                current_html[:idx] + new_string
                + current_html[idx + len(old_string):]
            )
        return new_html, old_string, True

    # 第二层：空白归一化匹配
    norm_search = re.sub(r'\s+', ' ', old_string)
    norm_html = re.sub(r'\s+', ' ', current_html)
    norm_idx = norm_html.find(norm_search)
    if norm_idx != -1:
        abs_start = _norm_to_orig(current_html, norm_idx)
        abs_end = _norm_to_orig(current_html, norm_idx + len(norm_search))
        if abs_start is not None and abs_end is not None:
            new_html = (
                current_html[:abs_start] + new_string
                + current_html[abs_end:]
            )
            return new_html, old_string, True

    # 第三层：逐行 strip 后匹配
    if not old_string.strip():
        return None, None, False

    old_lines = old_string.split('\n')
    html_lines = current_html.split('\n')
    stripped_old = [line.strip() for line in old_lines]
    stripped_html = [line.strip() for line in html_lines]

    match_start = -1
    for i in range(len(stripped_html) - len(stripped_old) + 1):
        if stripped_html[i:i + len(stripped_old)] == stripped_old:
            # 检查唯一匹配
            next_match = -1
            for j in range(i + 1, len(stripped_html) - len(stripped_old) + 1):
                if stripped_html[j:j + len(stripped_old)] == stripped_old:
                    next_match = j
                    break
            if next_match == -1:
                match_start = i
            break

    if match_start != -1:
        match_end = match_start + len(old_lines)
        actual_old = '\n'.join(html_lines[match_start:match_end])
        new_html_result = (
            '\n'.join(html_lines[:match_start])
            + '\n' + new_string
            + '\n' + '\n'.join(html_lines[match_end:])
        )
        return new_html_result, actual_old, True

    return None, None, False


class EditFileTool(Tool):
    """搜索替换编辑工具

    三层匹配策略：精确匹配 → 空白归一化 → 逐行剥离
    来自 server.py:9014-9416 的 edit_file 工具逻辑。
    """

    @property
    def name(self) -> str:
        return "edit_file"

    @property
    def description(self) -> str:
        return (
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
        )

    @property
    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "page": {
                    "type": "string",
                    "description": "Page name (must match exactly a name from list_pages)",
                },
                "old_string": {
                    "type": "string",
                    "description": "The text to replace (2-5 lines, must be unique in the page)",
                },
                "new_string": {
                    "type": "string",
                    "description": "The text to replace it with (must be different from old_string)",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace all occurrences of old_string (default false)",
                    "default": False,
                },
            },
            "required": ["page", "old_string", "new_string"],
        }

    def execute(self, args: Dict[str, Any], context: ToolContext) -> ToolResult:
        page_name = args.get('page', '')
        old_string = args.get('old_string', '')
        new_string = args.get('new_string', '')
        replace_all = args.get('replace_all', False)
        pages = context.pages_html

        if not pages and context.session and hasattr(context.session, 'generated_html'):
            pages = {'主页面': context.session.generated_html}

        # index.html 特殊处理
        if page_name.lower().endswith('index.html') and len(context.page_order) > 1:
            guide = (
                "'index.html' is an assembled file. "
                "Edit the specific sub-pages instead:\n"
            )
            for pn in context.page_order:
                if pn in pages:
                    guide += f'- edit_file(page="{pn}", ...)\n'
            return ToolResult(
                content=guide,
                success=False,
                metadata={'error': 'index.html is assembled, edit sub-pages'},
            )

        # 精确匹配页面名
        current_html = pages.get(page_name)
        if not current_html:
            available = list(pages.keys())
            return ToolResult(
                content=f"Error: page '{page_name}' not found. Available pages: {available}. Use the exact page name.",
                success=False,
            )

        # 搜索替换
        if not old_string:
            return ToolResult(
                content="Error: old_string is required.",
                success=False,
            )

        new_html, matched_old, applied = _apply_edit_three_layer(
            current_html, old_string, new_string, replace_all
        )

        if not applied:
            # 匹配失败诊断
            diag = self._build_match_failure_diagnosis(
                old_string, current_html, page_name
            )
            return ToolResult(
                content=diag,
                success=False,
                metadata={'page': page_name, 'error': 'not_found'},
            )

        # 应用成功
        if context.session and hasattr(context.session, 'pages_html'):
            context.session.pages_html[page_name] = new_html

        return ToolResult(
            content=f"The page [{page_name}] has been updated.",
            success=True,
            metadata={'page': page_name, 'old_text': matched_old, 'new_text': new_string},
        )

    def _build_match_failure_diagnosis(
        self, old_string: str, current_html: str, page_name: str
    ) -> str:
        """构建匹配失败的诊断消息"""
        parts = [f"Error: old_string not found in page [{page_name}]. "]
        old_lines = old_string.strip().split('\n')
        if old_lines:
            first_line = old_lines[0].strip()
            last_line = old_lines[-1].strip()
            first_found = first_line in current_html
            last_found = last_line in current_html
            if first_found and last_found:
                parts.append("First and last lines exist but middle content does not match (whitespace/indent difference likely).")
            elif first_found:
                parts.append(f"First line '{first_line[:60]}' found but overall match failed.")
            elif last_found:
                parts.append(f"Last line '{last_line[:60]}' found but overall match failed.")
            else:
                parts.append(
                    f"First line '{first_line[:60]}' not found. "
                    f"Content may have been modified. Re-read with read_page."
                )
        parts.append(
            " Re-read the page with read_page to get current content, then retry."
        )
        return ''.join(parts)


class ReadFrameworkTool(Tool):
    """读取项目框架代码（index.html 的侧边栏、导航、Vue 挂载逻辑）

    多页项目的 index.html 由框架（sidebar + Vue app + 子页面组装）构成。
    当问题涉及页面切换、导航、全局交互、Vue 生命周期等跨页问题时，
    需要查看框架代码才能定位根因。
    """

    @property
    def name(self) -> str:
        return "read_framework"

    @property
    def description(self) -> str:
        return (
            "读取项目的框架代码（index.html 中的侧边栏、Vue app 挂载逻辑、"
            "页面切换机制、全局样式等）。"
            "当遇到跨页面交互问题（如页面切换失败、全局事件不响应、"
            "Vue 钩子未挂载、导航异常）时，需要查看框架代码定位根因。"
            "返回框架部分的关键代码段，不含子页面内容。"
        )

    @property
    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "section": {
                    "type": "string",
                    "description": (
                        "要读取的框架部分："
                        "'sidebar'(侧边栏+导航), "
                        "'vue_app'(Vue createApp + setup), "
                        "'styles'(全局样式), "
                        "'all'(全部框架代码)"
                    ),
                },
            },
            "required": [],
        }

    @property
    def concurrency_safe(self) -> bool:
        return True

    def execute(self, args: Dict[str, Any], context: ToolContext) -> ToolResult:
        import re
        section = args.get('section', 'all')
        project_folder = context.project_folder

        index_path = os.path.join(project_folder, 'index.html')
        if not os.path.exists(index_path):
            return ToolResult(
                content="项目没有 index.html 框架文件。",
                success=False,
            )

        with open(index_path, 'r', encoding='utf-8') as f:
            content = f.read()

        lines = content.split('\n')

        # 定位关键结构边界
        body_start = None
        body_end = None
        style_end = None
        script_start = None
        script_end = None
        sidebar_start = None
        sidebar_end = None

        for i, line in enumerate(lines):
            stripped = line.strip().lower()
            if '<body' in stripped and body_start is None:
                body_start = i
            if '<style' in stripped:
                pass  # track last style end
            if '</style>' in stripped and '<style' not in stripped:
                style_end = i
            if '<div class="sidebar' in line.lower() and sidebar_start is None:
                sidebar_start = i
            if sidebar_start is not None and sidebar_end is None:
                if '</div>' in stripped and i > sidebar_start + 5:
                    # 侧边栏通常在第一个深层 </div> 处结束
                    depth = 0
                    for j in range(sidebar_start, i + 1):
                        depth += lines[j].lower().count('<div') - lines[j].lower().count('</div')
                    if depth <= 0:
                        sidebar_end = i
            if '<script>' in stripped and script_start is None and i > (style_end or 0):
                script_start = i
            if '</script>' in stripped and script_start is not None and script_end is None:
                script_end = i
            if '</body>' in stripped:
                body_end = i

        # 提取各部分
        results = []

        if section in ('all', 'styles') and style_end is not None:
            # 只取框架样式（第一个 <style> 块）
            style_block_start = None
            for i, line in enumerate(lines):
                if '<style' in line.lower() and 'class=' not in line:
                    style_block_start = i
                    break
            if style_block_start is not None:
                end = style_end + 1
                results.append(
                    "=== 全局样式 (L{}-L{}) ===\n{}".format(
                        style_block_start + 1, end,
                        '\n'.join(lines[style_block_start:end])
                    )
                )

        if section in ('all', 'sidebar'):
            # 侧边栏 + main-content 框架结构
            if body_start is not None:
                # 从 body 开始到第一个 v-show div 之前
                framework_end = body_start + 1
                for i in range(body_start + 1, len(lines)):
                    if 'v-show' in lines[i] or 'page-content-panel' in lines[i]:
                        framework_end = i
                        break
                if framework_end > body_start + 1:
                    results.append(
                        "=== 框架 HTML 结构 (L{}-L{}) ===\n{}".format(
                            body_start + 1, framework_end,
                            '\n'.join(lines[body_start:framework_end])
                        )
                    )

        if section in ('all', 'vue_app') and script_start is not None:
            script_end_pos = script_end + 1 if script_end else len(lines)
            results.append(
                "=== Vue App 脚本 (L{}-L{}) ===\n{}".format(
                    script_start + 1, script_end_pos,
                    '\n'.join(lines[script_start:script_end_pos])
                )
            )

        # 如果是 all 模式，额外给出项目结构概览
        if section == 'all':
            pages_dir = os.path.join(project_folder, 'pages')
            if os.path.isdir(pages_dir):
                page_files = sorted([
                    f for f in os.listdir(pages_dir) if f.endswith('.html')])
                results.insert(0, (
                    "=== 项目结构 ===\n"
                    f"index.html: {len(lines)} 行, {len(content):,} 字符\n"
                    f"pages/ 目录: {len(page_files)} 个子页面文件\n"
                    + '\n'.join(f'  - {pf}' for pf in page_files)
                ))

        if not results:
            return ToolResult(
                content="未找到框架代码。可能是单页项目或文件格式异常。",
                success=False,
            )

        return ToolResult(content='\n\n'.join(results), success=True)


class ListPagesTool(Tool):
    """列出项目中所有页面及其概要信息"""

    @property
    def name(self) -> str:
        return "list_pages"

    @property
    def description(self) -> str:
        return (
            "列出项目中的所有页面及其概要信息。"
            "返回每个页面的名称、行数、字符数。"
            "在逐页 read_page 之前先调用此工具快速了解项目全貌。"
        )

    @property
    def parameters_schema(self) -> Dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    @property
    def concurrency_safe(self) -> bool:
        return True

    def execute(self, args: Dict[str, Any], context: ToolContext) -> ToolResult:
        pages = context.pages_html
        if not pages:
            return ToolResult(content="项目中没有可用页面。", success=True)

        lines = ["项目页面列表：\n"]
        for i, (name, html) in enumerate(pages.items()):
            line_count = html.count('\n') + 1
            char_count = len(html)
            lines.append(f"{i + 1}. [{name}] — {line_count} 行, {char_count:,} 字符")
        lines.append(f"\n共 {len(pages)} 个页面")

        return ToolResult(content='\n'.join(lines), success=True)


class AddPageTool(Tool):
    """添加新页面到项目

    来自 server_context_engineering.py 的 add_page 工具。
    """

    @property
    def name(self) -> str:
        return "add_page"

    @property
    def description(self) -> str:
        return "向项目中添加一个新页面。提供页面名称和 HTML 内容。"

    @property
    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "page_key": {
                    "type": "string",
                    "description": "页面标识名（如 'dashboard', 'user-list'）",
                },
                "html_content": {
                    "type": "string",
                    "description": "页面的完整 HTML 内容",
                },
            },
            "required": ["page_key", "html_content"],
        }

    def execute(self, args: Dict[str, Any], context: ToolContext) -> ToolResult:
        page_key = args.get('page_key', '')
        html_content = args.get('html_content', '')

        if not page_key:
            return ToolResult(content="错误：必须提供 page_key 参数。", success=False)
        if not html_content:
            return ToolResult(content="错误：必须提供 html_content 参数。", success=False)

        if context.session and hasattr(context.session, 'pages_html'):
            context.session.pages_html[page_key] = html_content
            if hasattr(context.session, 'page_order') and page_key not in context.session.page_order:
                context.session.page_order.append(page_key)

        return ToolResult(
            content=f"页面 [{page_key}] 已添加 ({len(html_content)} 字符)。",
            success=True,
            metadata={'page_key': page_key, 'size': len(html_content)},
        )


class EditPageTool(Tool):
    """编辑指定页面的工具（增量页面生成场景）

    与 EditFileTool 类似，但面向增量生成上下文。
    """

    @property
    def name(self) -> str:
        return "edit_page"

    @property
    def description(self) -> str:
        return (
            "Performs exact string replacements in a generated page.\n"
            "Usage:\n"
            "1. You MUST call read_page first to see the exact content."
            " old_string must be copied verbatim from read_page output.\n"
            "2. old_string should be 2-5 lines to ensure unique match."
            " The edit will fail if old_string is not unique.\n"
            "3. Use replace_all=true to replace all occurrences of old_string.\n"
            "4. If the edit fails, re-read the page and retry."
        )

    @property
    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "page_key": {
                    "type": "string",
                    "description": "要编辑的页面标识名",
                },
                "old_string": {
                    "type": "string",
                    "description": "The text to replace (2-5 lines, must be unique in the page)",
                },
                "new_string": {
                    "type": "string",
                    "description": "The text to replace it with (must be different from old_string)",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace all occurrences of old_string (default false)",
                    "default": False,
                },
            },
            "required": ["page_key", "old_string", "new_string"],
        }

    def execute(self, args: Dict[str, Any], context: ToolContext) -> ToolResult:
        page_key = args.get('page_key', '')
        old_string = args.get('old_string', '')
        new_string = args.get('new_string', '')
        replace_all = args.get('replace_all', False)
        pages = context.pages_html

        current_html = pages.get(page_key)
        if not current_html:
            return ToolResult(
                content=f"Error: page '{page_key}' not found. Available pages: {list(pages.keys())}",
                success=False,
            )

        new_html, matched_old, applied = _apply_edit_three_layer(
            current_html, old_string, new_string, replace_all
        )

        if not applied:
            return ToolResult(
                content=f"Error: old_string not found in page [{page_key}]. Re-read with read_page and retry.",
                success=False,
            )

        if context.session and hasattr(context.session, 'pages_html'):
            context.session.pages_html[page_key] = new_html

        return ToolResult(
            content=f"The page [{page_key}] has been updated.",
            success=True,
            metadata={'page_key': page_key},
        )


class ReadFileTool(Tool):
    """通用文件读取工具（代码审查场景）"""

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return (
            "读取文件源代码。可以指定 start_line 和 end_line 分段读取大文件。"
            "在用 edit_file 之前先 read_file 查看精确代码。"
        )

    @property
    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "start_line": {
                    "type": "integer",
                    "description": "起始行号（从1开始），不指定则从第1行开始",
                },
                "end_line": {
                    "type": "integer",
                    "description": "结束行号（包含），不指定则读取到末尾",
                },
            },
            "required": [],
        }

    @property
    def concurrency_safe(self) -> bool:
        return True

    def execute(self, args: Dict[str, Any], context: ToolContext) -> ToolResult:
        start_line = args.get('start_line')
        end_line = args.get('end_line')

        # 代码审查场景：内容通过 context.extra 传入
        content = context.extra.get('file_content', '')
        if not content:
            # 尝试从文件系统读取
            file_path = context.extra.get('file_path', '')
            if file_path and os.path.exists(file_path):
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read()
            else:
                return ToolResult(content="错误：未提供文件内容。", success=False)

        lines = content.split('\n')
        total_lines = len(lines)

        if start_line and end_line:
            s = max(0, start_line - 1)
            e = min(total_lines, end_line)
            result = '\n'.join(lines[s:e])
            return ToolResult(
                content=f"L{start_line}-{end_line} ({len(result)} 字符):\n```\n{result}\n```",
                success=True,
            )
        else:
            return ToolResult(
                content=f"完整内容 ({total_lines} 行, {len(content)} 字符):\n```\n{content}\n```",
                success=True,
            )


# ---------------------------------------------------------------------------
# 内置 Hook 实现
# ---------------------------------------------------------------------------


import logging as _logging

_hook_logger = _logging.getLogger(__name__)


def logging_hook(tool_name, args, context_or_result, *args2):
    """LoggingHook — 记录所有工具调用到日志

    用法：
        hm.register(HookType.PRE_TOOL_USE, logging_hook)
        hm.register(HookType.POST_TOOL_USE, logging_hook)
    """
    from agent_loop import HookResult

    # PreToolUse: (tool_name, args, context)
    if len(args2) == 0:
        _hook_logger.info(f"[Hook:Logging] PreToolUse: {tool_name}({list(args.keys())})")
    # PostToolUse: (tool_name, args, result, context)
    elif len(args2) >= 1:
        result = context_or_result
        status = 'OK' if result.success else 'FAIL'
        _hook_logger.info(
            f"[Hook:Logging] PostToolUse: {tool_name} → {status} ({len(result.content)} chars)"
        )
    return HookResult()


class MetricsCollector:
    """工具调用指标收集器

    用法：
        metrics = MetricsCollector()
        hm.register(HookType.POST_TOOL_USE, metrics.post_tool_hook)
        # ... run loop ...
        print(metrics.summary())
    """

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def post_tool_hook(self, tool_name, args, result, context):
        """PostToolUse Hook — 记录每次工具调用的指标"""
        from agent_loop import HookResult
        self.calls.append({
            'tool': tool_name,
            'success': result.success,
            'content_length': len(result.content),
        })
        return HookResult()

    @property
    def total_calls(self) -> int:
        return len(self.calls)

    @property
    def success_count(self) -> int:
        return sum(1 for c in self.calls if c['success'])

    @property
    def failure_count(self) -> int:
        return self.total_calls - self.success_count

    @property
    def by_tool(self) -> Dict[str, Dict[str, int]]:
        """按工具名统计"""
        stats: Dict[str, Dict[str, int]] = {}
        for c in self.calls:
            name = c['tool']
            if name not in stats:
                stats[name] = {'total': 0, 'success': 0, 'failure': 0}
            stats[name]['total'] += 1
            if c['success']:
                stats[name]['success'] += 1
            else:
                stats[name]['failure'] += 1
        return stats

    def summary(self) -> str:
        """返回指标摘要"""
        lines = [
            f"工具调用指标: {self.total_calls} 次 "
            f"(成功 {self.success_count}, 失败 {self.failure_count})"
        ]
        for name, s in self.by_tool.items():
            lines.append(f"  {name}: {s['total']} 次 (成功 {s['success']}, 失败 {s['failure']})")
        return '\n'.join(lines)


def verify_after_edit_hook(reason, state):
    """VerifyAfterEditHook (Stop Hook) — 编辑后未验证则阻止终止

    对应 server.py 中的 pending_verify_fix 逻辑：
    编辑完成后 AI 必须通过 read_page 验证结果，
    否则阻止终止并注入验证提示。

    用法：
        hm.register(HookType.STOP, verify_after_edit_hook)
    """
    from agent_loop import HookResult, TerminalReason

    # 只在正常完成时检查
    if reason != TerminalReason.COMPLETED:
        return HookResult(proceed=True)

    # 检查是否刚进行过编辑但未验证
    if not state.has_ever_edited:
        return HookResult(proceed=True)

    # 检查最后几条工具调用：编辑后是否有 read
    recent_logs = state.tool_calls_log[-3:] if state.tool_calls_log else []
    if not recent_logs:
        return HookResult(proceed=True)

    last_tool = recent_logs[-1].get('tool', '')
    if last_tool in ('edit_file', 'edit_page', 'add_page'):
        # 最后一个操作是编辑，没有验证读取
        return HookResult(
            proceed=False,
            inject_message=(
                "你刚完成了编辑操作，但尚未验证结果。"
                "请使用 read_page 读取修改后的页面内容，确认编辑正确后再结束。"
            ),
        )

    return HookResult(proceed=True)


def anti_spin_hook(tool_name, args, context):
    """AntiSpinHook (PreToolUse Hook) — 连续过多读取则注入编辑提示

    对应 server.py 中的 MAX_CONSECUTIVE_READS 逻辑。
    当连续读取次数超过阈值时，在工具结果中附加提示信息。

    用法：
        hm.register(HookType.PRE_TOOL_USE, anti_spin_hook)
    """
    from agent_loop import HookResult

    if tool_name not in ('read_page', 'read_file', 'read_current_file'):
        return HookResult(proceed=True)

    # 通过 state 获取连续读取计数
    state = getattr(context, '_loop_state', None)
    if state and hasattr(state, 'consecutive_reads'):
        if state.consecutive_reads >= 5:
            # 注入提示但不阻止读取
            pass  # 提示通过 post hook 添加

    return HookResult(proceed=True)


def anti_spin_post_hook(tool_name, args, result, context):
    """AntiSpinHook 的 Post 部分 — 在 read 结果后追加提示"""
    from agent_loop import HookResult

    if tool_name not in ('read_page', 'read_file'):
        return HookResult(proceed=True)

    state = getattr(context, '_loop_state', None)
    if not state or not hasattr(state, 'consecutive_reads'):
        return HookResult(proceed=True)

    if state.consecutive_reads >= 4 and state.has_ever_edited:
        hint = (
            "\n\n[提示] 你已连续读取多轮但未进行编辑。"
            "请基于已有信息进行编辑操作。"
        )
        return HookResult(
            proceed=True,
            modified_result=ToolResult(
                content=result.content + hint,
                success=result.success,
                metadata=result.metadata,
            ),
        )

    return HookResult(proceed=True)


class WritePageTool(Tool):
    """整页写入工具（大范围重构场景）

    与 EditFileTool 互补：edit_file 用于精确搜索替换，
    write_page 用于需要大量修改时的整页覆写。
    """

    @property
    def name(self) -> str:
        return "write_page"

    @property
    def description(self) -> str:
        return (
            "Write the complete HTML content for a page,"
            " overwriting the existing content."
            " Use this for large-scale refactoring that would"
            " require many individual edits."
            " You MUST call read_page first to see the current content"
            " before overwriting."
        )

    @property
    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "page": {
                    "type": "string",
                    "description": "Page name (must match exactly a name from list_pages)",
                },
                "html": {
                    "type": "string",
                    "description": "The complete HTML content to write for the page",
                },
            },
            "required": ["page", "html"],
        }

    def execute(self, args: Dict[str, Any], context: ToolContext) -> ToolResult:
        page_name = args.get('page', '')
        new_html = args.get('html', '')
        pages = context.pages_html

        if not pages and context.session and hasattr(context.session, 'generated_html'):
            pages = {'主页面': context.session.generated_html}

        current_html = pages.get(page_name)
        if not current_html:
            available = list(pages.keys())
            return ToolResult(
                content=f"Error: page '{page_name}' not found. Available pages: {available}. Use the exact page name.",
                success=False,
            )

        if context.session and hasattr(context.session, 'pages_html'):
            context.session.pages_html[page_name] = new_html

        return ToolResult(
            content=f"The page [{page_name}] has been written successfully.",
            success=True,
            metadata={'page': page_name, 'write_page': True, 'old_text': current_html, 'new_text': new_html},
        )


# ---------------------------------------------------------------------------
# 工厂函数
# ---------------------------------------------------------------------------


def create_default_registry() -> ToolRegistry:
    """创建包含所有内置工具的默认注册表"""
    registry = ToolRegistry()
    registry.register(ReadPageTool())
    registry.register(EditFileTool())
    registry.register(ListPagesTool())
    registry.register(AddPageTool())
    registry.register(EditPageTool())
    registry.register(ReadFileTool())
    return registry


def create_inspector_registry() -> ToolRegistry:
    """创建检查器微调场景的工具注册表 (read_page + edit_file + write_page + list_pages + read_framework)"""
    registry = ToolRegistry()
    registry.register(ReadPageTool())
    registry.register(EditFileTool())
    registry.register(WritePageTool())
    registry.register(ListPagesTool())
    registry.register(ReadFrameworkTool())
    return registry


def create_incremental_registry() -> ToolRegistry:
    """创建增量页面生成场景的工具注册表 (add_page + read_page + edit_page)"""
    registry = ToolRegistry()
    registry.register(ReadPageTool())
    registry.register(AddPageTool())
    registry.register(EditPageTool())
    return registry


def create_review_registry() -> ToolRegistry:
    """创建代码审查场景的工具注册表 (read_file + edit_file)"""
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    registry.register(EditFileTool())
    return registry
