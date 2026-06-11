"""
agent_integration.py — 现有代码集成垫片

为三个现有的 agentic 循环提供适配器，使现有代码可通过配置开关
切换到新的 AgentLoop 引擎，无需修改现有逻辑。

三个场景：
- Inspector 微调 (server.py:8145-9700) — InspectorAdapter
- 增量页面生成 (server_context_engineering.py:4801-5200) — IncrementalAdapter
- 代码审查 (server.py:6401-6900) — ReviewAdapter

用法（在 server.py 中）：
    USE_AGENT_LOOP = True  # 配置开关

    if USE_AGENT_LOOP:
        from agent_integration import InspectorAdapter
        adapter = InspectorAdapter(server, session, project_folder)
        result = adapter.run(system_prompt, user_message, sse_send_fn)
        # 处理 result ...
    else:
        # 现有内联循环代码 (保持不变)
"""

import logging
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from agent_loop import (
    AgentLoop,
    AgentLoopConfig,
    CompactionConfig,
    ContextManager,
    DiskOverflowManager,
    HookManager,
    HookResult,
    HookType,
    LoopResult,
    LoopState,
    ParallelToolExecutor,
    StreamEventType,
    TerminalReason,
)
from agent_tools import (
    PermissionManager,
    ToolContext,
    ToolRegistry,
    create_inspector_registry,
    create_incremental_registry,
    create_review_registry,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SSE 适配器
# ---------------------------------------------------------------------------


class SSEStreamingAdapter:
    """将 server.py 的 _send_sse_data / push_event 适配为 AgentLoop 的 streaming_callback

    server.py 的 SSE 推送签名：
        _send_sse_data(event_type, data, session)
        push_event(event_type, data)

    AgentLoop 的 streaming_callback 签名：
        callback(event_type: str, data: dict) -> None

    用法：
        adapter = SSEStreamingAdapter(server._send_sse_data, session)
        result = loop.run(..., streaming_callback=adapter.callback)
    """

    def __init__(
        self,
        send_sse_fn: Optional[Callable] = None,
        session: Any = None,
        push_event_fn: Optional[Callable] = None,
    ) -> None:
        self.send_sse_fn = send_sse_fn
        self.session = session
        self.push_event_fn = push_event_fn
        self.events: List[Tuple[str, Dict]] = []  # 记录所有事件 (调试用)

    def callback(self, event_type: str, data: Dict) -> None:
        """AgentLoop 的 streaming_callback 适配器"""
        self.events.append((event_type, data))

        # 映射 StreamEventType 到 server.py 的 SSE 事件类型
        sse_event = self._map_event_type(event_type)
        sse_data = self._map_data(event_type, data)

        if self.send_sse_fn and self.session:
            try:
                self.send_sse_fn(sse_event, sse_data, self.session)
            except Exception as e:
                logger.debug(f"[SSEAdapter] send_sse 异常 (忽略): {e}")

        if self.push_event_fn:
            try:
                self.push_event_fn(sse_event, sse_data)
            except Exception as e:
                logger.debug(f"[SSEAdapter] push_event 异常 (忽略): {e}")

    @staticmethod
    def _map_event_type(event_type: str) -> str:
        """将 AgentLoop 事件类型映射到 server.py 的 SSE 事件类型"""
        mapping = {
            'text_delta': 'ai_chunk',
            'reasoning_delta': 'ai_reasoning',
            'tool_call_started': 'tool_start',
            'tool_call_progress': 'tool_progress',
            'tool_call_completed': 'tool_result',
            'context_compacted': 'context_compact',
            'turn_start': 'turn_start',
            'turn_end': 'turn_end',
            'loop_end': 'loop_end',
        }
        return mapping.get(event_type, event_type)

    @staticmethod
    def _map_data(event_type: str, data: Dict) -> Dict:
        """转换事件数据格式"""
        if event_type == 'text_delta':
            return {'text': data.get('text', '')}
        elif event_type == 'tool_call_completed':
            return {
                'tool_name': data.get('tool_name', ''),
                'success': data.get('success', False),
                'content_length': data.get('content_length', 0),
            }
        return data


# ---------------------------------------------------------------------------
# AI 调用适配器
# ---------------------------------------------------------------------------


class AICallAdapter:
    """将 server.py 的 call_ai_model_streaming 适配为 AgentLoop 的 ai_call_fn

    server.py 的 AI 调用签名：
        call_ai_model_streaming(
            prompt_or_messages, images, tools=None,
            cancellable_project_id=None,
            ...
        ) -> Generator[tuple(text, full_content, done, tool_calls, reasoning)]

    AgentLoop 期望的 ai_call_fn 签名：
        fn(messages, images, tools=..., **kwargs) -> Generator
    """

    def __init__(self, server: Any, model_config: Optional[Dict] = None) -> None:
        self.server = server
        self.model_config = model_config or {}

    def __call__(
        self,
        messages: List[Dict],
        images: List,
        tools: Optional[List[Dict]] = None,
        **kwargs,
    ):
        """调用 AI 模型的流式接口"""
        if not self.server or not hasattr(self.server, 'call_ai_model_streaming'):
            # 无 server 实例 → 返回空生成器
            yield ('', '', True, [], '')
            return

        gen = self.server.call_ai_model_streaming(
            messages,
            images,
            tools=tools,
            **{**self.model_config, **kwargs},
        )
        yield from gen


# ---------------------------------------------------------------------------
# Inspector 微调适配器
# ---------------------------------------------------------------------------


class InspectorAdapter:
    """Inspector 微调场景适配器

    对应 server.py:8145-9700 的 agentic 微调循环。

    将 Inspector 场景的特定逻辑映射到 AgentLoop：
    - system_prompt → AgentLoopConfig.system_prompt
    - edit_tools → create_inspector_registry()
    - _send_sse_data → SSEStreamingAdapter
    - 防护逻辑 → Hook (max_consecutive_reads, verify_after_edit)
    - HTML 提取 → AgentLoopConfig.extract_html
    """

    def __init__(
        self,
        server: Any,
        session: Any = None,
        project_folder: str = '',
        project_id: str = '',
        enable_parallel: bool = False,
        enable_disk_overflow: bool = True,
    ) -> None:
        self.server = server
        self.session = session
        self.project_folder = project_folder
        self.project_id = project_id
        self.enable_parallel = enable_parallel
        self.enable_disk_overflow = enable_disk_overflow

    def run(
        self,
        system_prompt: str = '',
        user_message: str = '',
        send_sse_fn: Optional[Callable] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        initial_pages: Optional[Dict[str, str]] = None,
        page_order: Optional[List[str]] = None,
        initial_messages: Optional[List[Dict[str, Any]]] = None,
        streaming_callback: Optional[Callable] = None,
    ) -> LoopResult:
        """运行 Inspector 微调循环

        Args:
            system_prompt: 系统提示词 (Inspector 微调专用)
            user_message: 用户请求 (如 "将按钮改为红色")
            send_sse_fn: SSE 推送函数 (server._send_sse_data)
            cancel_check: 取消检查函数
            initial_pages: 初始页面 HTML
            page_order: 页面顺序
            initial_messages: 完整消息列表（提供时忽略 system_prompt 和 user_message）
            streaming_callback: 自定义流式回调（提供时忽略 send_sse_fn）

        Returns:
            LoopResult: 循环结果
        """
        # 构建 ToolContext
        extra = {}
        if initial_pages:
            extra['initial_pages'] = initial_pages
        if page_order:
            extra['page_order'] = page_order

        tool_ctx = ToolContext(
            project_id=self.project_id,
            project_folder=self.project_folder,
            server=self.server,
            session=self.session,
            extra=extra,
        )

        # 构建消息
        if initial_messages is not None:
            messages = list(initial_messages)
        else:
            messages = [
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_message},
            ]

        # 确定流式回调
        sse_cb = streaming_callback
        if sse_cb is None and send_sse_fn:
            sse_adapter = SSEStreamingAdapter(
                send_sse_fn=send_sse_fn,
                session=self.session,
            )
            sse_cb = sse_adapter.callback

        # 创建 Hook Manager
        hook_mgr = HookManager()
        # 注册内置 Hook
        from agent_tools import verify_after_edit_hook, anti_spin_post_hook
        hook_mgr.register(HookType.STOP, verify_after_edit_hook)
        hook_mgr.register(HookType.POST_TOOL_USE, anti_spin_post_hook)

        # 创建 Loop
        config = AgentLoopConfig(
            max_turns=50,
            max_consecutive_empty=2,
            max_consecutive_reads=3,
            max_explore_reads=6,
            max_verify_retries=3,
            tool_names=['read_page', 'edit_file', 'list_pages'],
            system_prompt=system_prompt,
            extract_html=True,
            parallel_execution=self.enable_parallel,
            disk_overflow=self.enable_disk_overflow,
            proactive_compaction=True,
        )

        loop = AgentLoop(
            config=config,
            tool_registry=create_inspector_registry(),
            hook_manager=hook_mgr,
        )

        if self.enable_disk_overflow:
            loop.disk_overflow = DiskOverflowManager()

        # 创建 AI 调用适配器
        ai_call = AICallAdapter(self.server)

        # 运行循环
        result = loop.run(
            initial_messages=messages,
            tool_context=tool_ctx,
            streaming_callback=sse_cb,
            cancel_check=cancel_check,
            ai_call_fn=ai_call,
        )

        # 清理磁盘溢出临时文件
        if loop.disk_overflow:
            loop.disk_overflow.cleanup_all()

        return result


# ---------------------------------------------------------------------------
# 增量页面生成适配器
# ---------------------------------------------------------------------------


class IncrementalAdapter:
    """增量页面生成场景适配器

    对应 server_context_engineering.py:4801-5200 的 _run_agentic_pages 循环。

    特点：
    - 多页面生成 (add_page)
    - 批量分块处理
    - 增量编辑 (edit_page)
    """

    def __init__(
        self,
        server: Any,
        session: Any = None,
        project_folder: str = '',
        project_id: str = '',
        enable_parallel: bool = False,
        enable_disk_overflow: bool = True,
    ) -> None:
        self.server = server
        self.session = session
        self.project_folder = project_folder
        self.project_id = project_id
        self.enable_parallel = enable_parallel
        self.enable_disk_overflow = enable_disk_overflow

    def run(
        self,
        system_prompt: str,
        user_prompt: str,
        send_sse_fn: Optional[Callable] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        existing_pages: Optional[Dict[str, str]] = None,
        page_order: Optional[List[str]] = None,
    ) -> LoopResult:
        """运行增量页面生成循环

        Args:
            system_prompt: 系统提示词 (含页面结构、设计要求)
            user_prompt: 用户需求描述
            send_sse_fn: SSE 推送函数
            cancel_check: 取消检查函数
            existing_pages: 已有页面 (用于增量编辑)
            page_order: 页面顺序

        Returns:
            LoopResult: 循环结果 (session.pages_html 中包含生成的页面)
        """
        tool_ctx = ToolContext(
            project_id=self.project_id,
            project_folder=self.project_folder,
            server=self.server,
            session=self.session,
        )

        messages = [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt},
        ]

        sse_adapter = SSEStreamingAdapter(
            send_sse_fn=send_sse_fn,
            session=self.session,
        )

        config = AgentLoopConfig(
            max_turns=30,
            max_consecutive_empty=2,
            tool_names=['read_page', 'add_page', 'edit_page'],
            system_prompt=system_prompt,
            parallel_execution=self.enable_parallel,
            disk_overflow=self.enable_disk_overflow,
            proactive_compaction=True,
        )

        hook_mgr = HookManager()
        from agent_tools import verify_after_edit_hook
        hook_mgr.register(HookType.STOP, verify_after_edit_hook)

        loop = AgentLoop(
            config=config,
            tool_registry=create_incremental_registry(),
            hook_manager=hook_mgr,
        )

        if self.enable_disk_overflow:
            loop.disk_overflow = DiskOverflowManager()

        ai_call = AICallAdapter(self.server)

        result = loop.run(
            initial_messages=messages,
            tool_context=tool_ctx,
            streaming_callback=sse_adapter.callback,
            cancel_check=cancel_check,
            ai_call_fn=ai_call,
        )

        if loop.disk_overflow:
            loop.disk_overflow.cleanup_all()

        return result


# ---------------------------------------------------------------------------
# 代码审查适配器
# ---------------------------------------------------------------------------


class ReviewAdapter:
    """代码审查场景适配器

    对应 server.py:6401-6900 的 _run_code_review 循环。

    特点：
    - 只读为主 (read_file)
    - 少量编辑建议 (edit_file)
    - 轮次限制严格 (3 轮)
    - 结果为审查报告文本
    """

    def __init__(
        self,
        server: Any,
        project_folder: str = '',
        project_id: str = '',
        enable_disk_overflow: bool = True,
    ) -> None:
        self.server = server
        self.project_folder = project_folder
        self.project_id = project_id
        self.enable_disk_overflow = enable_disk_overflow

    def run(
        self,
        system_prompt: str,
        html_content: str,
        send_sse_fn: Optional[Callable] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        file_path: Optional[str] = None,
    ) -> LoopResult:
        """运行代码审查循环

        Args:
            system_prompt: 审查提示词 (含审查规则)
            html_content: 要审查的 HTML 内容
            send_sse_fn: SSE 推送函数
            cancel_check: 取消检查函数
            file_path: 文件路径 (用于 read_file 工具)

        Returns:
            LoopResult: 循环结果 (final_text 为审查报告)
        """
        # 构建 extra context
        extra = {}
        if html_content:
            extra['file_content'] = html_content
        if file_path:
            extra['file_path'] = file_path

        tool_ctx = ToolContext(
            project_id=self.project_id,
            project_folder=self.project_folder,
            server=self.server,
            extra=extra,
        )

        # 审查请求消息
        user_msg = f"请审查以下 HTML 代码，给出改进建议：\n\n{html_content[:5000]}"
        messages = [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_msg},
        ]

        sse_adapter = SSEStreamingAdapter(
            send_sse_fn=send_sse_fn,
        )

        config = AgentLoopConfig(
            max_turns=3,
            max_consecutive_empty=1,
            tool_names=['read_file', 'edit_file'],
            system_prompt=system_prompt,
            disk_overflow=self.enable_disk_overflow,
        )

        loop = AgentLoop(
            config=config,
            tool_registry=create_review_registry(),
        )

        if self.enable_disk_overflow:
            loop.disk_overflow = DiskOverflowManager()

        ai_call = AICallAdapter(self.server)

        result = loop.run(
            initial_messages=messages,
            tool_context=tool_ctx,
            streaming_callback=sse_adapter.callback,
            cancel_check=cancel_check,
            ai_call_fn=ai_call,
        )

        if loop.disk_overflow:
            loop.disk_overflow.cleanup_all()

        return result


# ---------------------------------------------------------------------------
# 便捷函数：从 server 配置创建适配器
# ---------------------------------------------------------------------------


def create_adapter_from_config(
    scenario: str,
    server: Any,
    session: Any = None,
    project_folder: str = '',
    project_id: str = '',
    **kwargs,
) -> Any:
    """根据场景名创建对应的适配器

    Args:
        scenario: 场景名 ('inspector', 'incremental', 'review')
        server: server 实例
        session: GenerationSession 实例
        project_folder: 项目文件夹路径
        project_id: 项目 ID

    Returns:
        对应的适配器实例
    """
    if scenario == 'inspector':
        return InspectorAdapter(
            server=server, session=session,
            project_folder=project_folder, project_id=project_id,
            **kwargs,
        )
    elif scenario == 'incremental':
        return IncrementalAdapter(
            server=server, session=session,
            project_folder=project_folder, project_id=project_id,
            **kwargs,
        )
    elif scenario == 'review':
        return ReviewAdapter(
            server=server,
            project_folder=project_folder, project_id=project_id,
            **kwargs,
        )
    else:
        raise ValueError(f"未知场景: {scenario}。支持: inspector, incremental, review")
