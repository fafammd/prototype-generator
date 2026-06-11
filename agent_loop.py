"""
agent_loop.py — CCB 风格的 Agent Loop 引擎

参照 claude-code-best/claude-code 的 queryLoop() 架构设计：

核心循环流程 (对应 CCB 的 queryLoop):
  while True:
    Phase A: _prepare_context(state)   # 上下文压缩检查
    Phase B: _stream_ai_call(state)    # 流式 AI 调用，累积 text + tool_calls
    Phase C: _handle_no_tools(state)   # 无工具 → 空响应处理/HTML 提取 → Terminal
    Phase D: _execute_tools(state)     # 遍历 tool_calls → ToolRegistry → ToolResult
    Phase E: 构建新 state → continue   # messages + assistant + tool_results

关键设计决策：
- 同步非异步：项目用 ThreadingTCPServer，无 asyncio
- 字符预算：沿用 MAX_CONTEXT_CHARS=240000（无 tiktoken 依赖）
- 复用 AI 调用：通过 ai_call_fn 调用现有 call_ai_model_streaming()
- 不可变状态：LoopState 通过 with_*() 方法创建新实例
- OpenAI 消息格式：内部统一用 OpenAI 格式
"""

import json
import logging
import os
import re
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Callable, Dict, Generator, List, Optional, Tuple

from agent_tools import (
    PermissionDecision,
    PermissionManager,
    ToolContext,
    ToolRegistry,
    ToolResult,
)


# ---------------------------------------------------------------------------
# Hook 系统
# ---------------------------------------------------------------------------


class HookType(Enum):
    """Hook 类型 (对应 CCB 的三类 Hook)"""
    PRE_TOOL_USE = "pre_tool_use"    # 工具执行前：可修改参数或阻止执行
    POST_TOOL_USE = "post_tool_use"  # 工具执行后：可检查/修改结果
    STOP = "stop"                    # 循环终止时：可阻止终止并注入消息


@dataclass
class HookResult:
    """Hook 执行结果 (不可变值对象)

    对应 CCB 的 HookReturn (src/services/hooks/types.ts)
    """
    proceed: bool = True                          # False = 阻止执行/阻止停止
    modified_args: Optional[Dict[str, Any]] = None   # Pre Hook: 修改后的工具参数
    modified_result: Optional[ToolResult] = None     # Post Hook: 修改后的结果
    inject_message: Optional[str] = None             # Stop Hook: 注入的消息


HookCallback = Callable[..., HookResult]


class HookManager:
    """Agent Loop Hook 管理器

    对应 CCB 的 Hook 调度器。

    用法：
        hm = HookManager()
        hm.register(HookType.PRE_TOOL_USE, my_pre_hook, priority=10)
        hm.register(HookType.STOP, my_stop_hook)
    """

    def __init__(self) -> None:
        self._hooks: Dict[HookType, List[Tuple[int, HookCallback]]] = {
            t: [] for t in HookType
        }

    def register(
        self, hook_type: HookType, callback: HookCallback, priority: int = 0
    ) -> None:
        """注册 Hook（priority 越小越先执行）"""
        self._hooks[hook_type].append((priority, callback))
        self._hooks[hook_type].sort(key=lambda x: x[0])

    def unregister(self, hook_type: HookType, callback: HookCallback) -> None:
        """取消注册"""
        self._hooks[hook_type] = [
            (p, cb) for p, cb in self._hooks[hook_type] if cb is not callback
        ]

    def run_pre_tool(
        self, tool_name: str, args: Dict[str, Any], context: ToolContext
    ) -> HookResult:
        """运行 PreToolUse Hook 链

        链式规则：
        - 任一 Hook 返回 proceed=False → 整体阻止
        - 后续 Hook 的 modified_args 会传递给下一个 Hook
        """
        current_args = args
        for priority, callback in self._hooks[HookType.PRE_TOOL_USE]:
            result = callback(tool_name, current_args, context)
            if not isinstance(result, HookResult):
                continue
            if result.modified_args is not None:
                current_args = result.modified_args
            if not result.proceed:
                return HookResult(proceed=False, modified_args=current_args)
        return HookResult(proceed=True, modified_args=current_args)

    def run_post_tool(
        self, tool_name: str, args: Dict[str, Any],
        result: ToolResult, context: ToolContext,
    ) -> HookResult:
        """运行 PostToolUse Hook 链"""
        current_result = result
        for priority, callback in self._hooks[HookType.POST_TOOL_USE]:
            hook_res = callback(tool_name, args, current_result, context)
            if not isinstance(hook_res, HookResult):
                continue
            if hook_res.modified_result is not None:
                current_result = hook_res.modified_result
            if not hook_res.proceed:
                return HookResult(proceed=False, modified_result=current_result)
        return HookResult(proceed=True, modified_result=current_result)

    def run_stop(
        self, reason: 'TerminalReason', state: 'LoopState'
    ) -> HookResult:
        """运行 Stop Hook 链

        返回 proceed=False 表示阻止终止并注入消息继续循环。
        """
        for priority, callback in self._hooks[HookType.STOP]:
            hook_res = callback(reason, state)
            if not isinstance(hook_res, HookResult):
                continue
            if not hook_res.proceed:
                return hook_res  # 第一个阻止终止的 Hook 获胜
        return HookResult(proceed=True)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 状态类型定义
# ---------------------------------------------------------------------------


class TerminalReason(Enum):
    """循环终止原因 (对应 CCB 的 Terminal 类型)"""
    COMPLETED = "completed"                 # AI 正常回复完毕，无需调用工具
    ABORTED_STREAMING = "aborted_streaming"  # 流式调用中断
    ABORTED_TOOLS = "aborted_tools"          # 工具执行中断
    PROMPT_TOO_LONG = "prompt_too_long"      # 即使压缩后上下文仍超限
    MAX_TURNS = "max_turns"                  # 达到最大轮次
    MODEL_ERROR = "model_error"              # API 调用失败
    CANCELLED = "cancelled"                  # 外部取消
    CONTEXT_OVERFLOW = "context_overflow"    # 上下文溢出


class ContinueReason(Enum):
    """循环继续原因 (对应 CCB 的 Continue 类型)"""
    NEXT_TURN = "next_turn"                  # 正常工具结果循环
    COMPACT_RETRY = "compact_retry"          # 压缩后重试
    EMPTY_RETRY = "empty_retry"              # 空响应重试
    INJECT_RETRY = "inject_retry"            # 注入消息后重试


class ContextStrategy(Enum):
    """上下文压缩策略"""
    CHAR_COUNT_TRUNCATE = "char_count_truncate"    # 字符数截断
    READ_TOOL_COMPACT = "read_tool_compact"         # 压缩旧 read 工具结果
    SKELETON_SUMMARY = "skeleton_summary"           # 行号骨架摘要


@dataclass
class LoopState:
    """循环中的可变状态 (不可变模式：通过 with_* 方法创建新实例)

    对应 CCB 的 State 类型 (src/query.ts)
    """
    messages: List[Dict[str, Any]] = field(default_factory=list)
    turn_count: int = 0
    accumulated_text: str = ''
    accumulated_reasoning: str = ''
    tool_calls_result: Optional[List[Dict[str, Any]]] = None
    consecutive_empty: int = 0
    consecutive_reads: int = 0
    has_ever_edited: bool = False
    has_update: bool = False
    edit_results: List[Dict[str, Any]] = field(default_factory=list)
    tool_calls_log: List[Dict[str, Any]] = field(default_factory=list)
    _cancel_detected: bool = False  # 内部标记：流式中检测到取消
    _output_tokens_recovery: int = 0  # max_output_tokens 截断恢复计数
    _api_error: bool = False  # 内部标记：AI 调用异常 (Stop Hook 死循环防护)
    _has_attempted_reactive_compact: bool = False  # 防止无限反应性压缩循环
    _fallback_attempted: bool = False  # 是否已尝试回退模型
    _last_text_length: int = 0  # 上一轮文本长度 (递减收益检测)
    _diminishing_returns_count: int = 0  # 连续小响应计数 (递减收益检测)

    def with_updates(self, **kwargs) -> 'LoopState':
        """创建更新部分字段的新状态实例 (不可变模式)"""
        return replace(self, **kwargs)


@dataclass
class LoopResult:
    """Agent Loop 的最终输出"""
    terminal_reason: TerminalReason
    state: LoopState
    final_text: str = ''
    final_html: str = ''
    summary: str = ''
    total_turns: int = 0
    total_tool_calls: int = 0


# ---------------------------------------------------------------------------
# 上下文压缩配置和管理器
# ---------------------------------------------------------------------------


@dataclass
class CompactionConfig:
    """上下文压缩配置"""
    max_context_chars: int = 240000         # 上下文字符上限
    max_read_chars: int = 150000            # 单次 read 返回上限
    compact_threshold_chars: int = 200000   # 触发压缩的阈值
    compact_read_result_chars: int = 3000   # 压缩后 read 结果保留字符数
    protected_roles: List[str] = field(default_factory=lambda: ['system'])


class ContextManager:
    """上下文压缩管理器

    对应 CCB 的多策略压缩系统。从现有代码提取的三种策略：
    - CHAR_COUNT_TRUNCATE: 截断旧 read 工具结果 (server.py:8211-8335)
    - READ_TOOL_COMPACT: 智能摘要保留骨架 (server_context_engineering.py:4706-4728)
    - SKELETON_SUMMARY: 行号骨架提取
    """

    def __init__(self, config: Optional[CompactionConfig] = None) -> None:
        self.config = config or CompactionConfig()

    def estimate_size(self, messages: List[Dict[str, Any]]) -> int:
        """估算消息的总字符大小"""
        total = 0
        for m in messages:
            content = m.get('content', '')
            if isinstance(content, str):
                total += len(content)
            elif isinstance(content, list):
                # OpenAI 多模态消息格式
                for block in content:
                    if isinstance(block, dict):
                        total += len(block.get('text', ''))
                        total += len(str(block.get('image_url', '')))
            else:
                total += len(str(content))
        return total

    def should_compact(self, messages: List[Dict[str, Any]]) -> bool:
        """检查是否需要压缩"""
        return self.estimate_size(messages) > self.config.compact_threshold_chars

    def compact(
        self,
        messages: List[Dict[str, Any]],
        strategy: ContextStrategy = ContextStrategy.READ_TOOL_COMPACT,
    ) -> List[Dict[str, Any]]:
        """压缩消息列表，返回新列表 (不可变)"""
        if not self.should_compact(messages):
            return messages

        original_size = self.estimate_size(messages)

        if strategy == ContextStrategy.READ_TOOL_COMPACT:
            result = self._compact_read_tools(messages)
        elif strategy == ContextStrategy.CHAR_COUNT_TRUNCATE:
            result = self._compact_truncate(messages)
        elif strategy == ContextStrategy.SKELETON_SUMMARY:
            result = self._compact_skeleton(messages)
        else:
            result = messages

        new_size = self.estimate_size(result)
        if new_size < original_size:
            logger.info(
                f"[AgentLoop] 上下文压缩: {original_size:,} → {new_size:,} 字符 "
                f"(策略: {strategy.value})"
            )

        return result

    def _compact_read_tools(
        self, messages: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """压缩旧的 read 工具结果

        来自 server_context_engineering.py:4706-4728。
        保留前 N 个字符 + 截断提示。
        """
        max_retain = self.config.compact_read_result_chars
        compacted = []
        for m in messages:
            tool_name = m.get('name', '')
            role = m.get('role', '')
            content = m.get('content', '')

            # 只压缩 read 类工具的大结果
            if (role == 'tool' and tool_name in ('read_page', 'read_current_file', 'read_file')
                    and isinstance(content, str) and len(content) > max_retain):
                compacted.append({
                    **m,
                    'content': content[:max_retain] + '\n\n[已压缩 - 原始内容过长，请重新 read_page 获取]'
                })
            else:
                compacted.append(m)

        return compacted

    def _compact_truncate(
        self, messages: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """截断长助手/用户消息"""
        max_msg_chars = 50000
        compacted = []
        for m in messages:
            content = m.get('content', '')
            role = m.get('role', '')
            if role in self.config.protected_roles:
                compacted.append(m)
                continue
            if isinstance(content, str) and len(content) > max_msg_chars:
                compacted.append({
                    **m,
                    'content': content[:max_msg_chars] + '\n\n[已截断]'
                })
            else:
                compacted.append(m)
        return compacted

    def _compact_skeleton(
        self, messages: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """行号骨架提取 — 保留标签结构，移除文本内容"""
        compacted = []
        for m in messages:
            content = m.get('content', '')
            role = m.get('role', '')
            if role in self.config.protected_roles:
                compacted.append(m)
                continue

            tool_name = m.get('name', '')
            if (role == 'tool' and tool_name in ('read_page', 'read_current_file', 'read_file')
                    and isinstance(content, str)):
                # 提取 HTML 标签骨架
                lines = content.split('\n')
                skeleton_lines = []
                for line in lines:
                    stripped = line.strip()
                    if stripped.startswith('<') or stripped.startswith('```'):
                        skeleton_lines.append(line)
                    elif not stripped or stripped.startswith('//') or stripped.startswith('/*'):
                        skeleton_lines.append(line)
                skeleton = '\n'.join(skeleton_lines)
                if len(skeleton) < len(content):
                    compacted.append({**m, 'content': skeleton + '\n\n[骨架摘要 - 详细内容已移除]'})
                else:
                    compacted.append(m)
            else:
                compacted.append(m)

        return compacted

    def estimate_next_turn_size(self, messages: List[Dict[str, Any]]) -> int:
        """预测下一轮 AI 调用后的上下文大小

        对应 CCB 的预测性压缩：在发送 AI 请求前估算响应 + 工具结果的大小，
        如果预估会溢出则提前压缩，避免浪费 API 调用。

        估算公式：
            当前消息大小 + 预估 AI 响应大小 (~8000 字符) +
            预估工具结果大小 (按最近 read 结果的平均大小)

        Returns:
            预估的下一轮上下文字符数
        """
        current_size = self.estimate_size(messages)

        # 预估 AI 响应大小 (文本 + 可能的 tool_calls JSON)
        estimated_response = 8000

        # 估算可能的工具结果大小
        # 统计最近的 read 工具结果平均大小作为参考
        recent_read_sizes = []
        for m in reversed(messages[-10:]):
            if (m.get('role') == 'tool'
                    and m.get('name', '') in ('read_page', 'read_file', 'read_current_file')):
                content = m.get('content', '')
                if isinstance(content, str) and len(content) > 100:
                    recent_read_sizes.append(len(content))

        avg_read_size = 0
        if recent_read_sizes:
            avg_read_size = sum(recent_read_sizes) // len(recent_read_sizes)

        # 如果有历史 read 数据，预估可能产生 1-2 个工具结果
        estimated_tool_results = avg_read_size * min(2, max(1, len(recent_read_sizes)))

        return current_size + estimated_response + estimated_tool_results

    def should_proactive_compact(self, messages: List[Dict[str, Any]]) -> bool:
        """检查是否需要预测性压缩

        对应 CCB 的 proactive compaction：
        当前上下文 + 预估下一轮大小是否会超出上限。

        与 should_compact() 的区别：
        - should_compact() 是基于当前大小的被动检查
        - should_proactive_compact() 是基于预测大小的主动检查
        """
        predicted = self.estimate_next_turn_size(messages)
        return predicted > self.config.max_context_chars


# ---------------------------------------------------------------------------
# 流式事件
# ---------------------------------------------------------------------------


class StreamEventType(str, Enum):
    """结构化流式事件类型"""
    TEXT_DELTA = "text_delta"
    REASONING_DELTA = "reasoning_delta"
    TOOL_CALL_STARTED = "tool_call_started"
    TOOL_CALL_PROGRESS = "tool_call_progress"
    TOOL_CALL_COMPLETED = "tool_call_completed"
    CONTEXT_COMPACTED = "context_compacted"
    TURN_START = "turn_start"
    TURN_END = "turn_end"
    LOOP_END = "loop_end"


# ---------------------------------------------------------------------------
# Phase 3: 并行工具执行器
# ---------------------------------------------------------------------------


class ParallelToolExecutor:
    """并行工具执行器

    对应 CCB 的 parallel tool execution 能力。
    使用 ThreadPoolExecutor 并发执行标记为 concurrency_safe=True 的只读工具。
    写操作工具仍然顺序执行以保证安全。

    用法：
        executor = ParallelToolExecutor(registry, max_workers=3)
        results = executor.execute(tool_calls, tool_ctx)
    """

    def __init__(
        self,
        tool_registry: ToolRegistry,
        permission_manager: Optional[PermissionManager] = None,
        hook_manager: Optional[HookManager] = None,
        max_workers: int = 3,
    ) -> None:
        self.registry = tool_registry
        self.permissions = permission_manager or PermissionManager()
        self.hooks = hook_manager or HookManager()
        self.max_workers = max_workers

    def execute(
        self,
        tool_calls: List[Dict[str, Any]],
        tool_ctx: ToolContext,
        callback: Optional[Callable] = None,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
        """执行工具调用列表，并发执行只读工具

        Args:
            tool_calls: 工具调用列表 [{id, name, arguments}]
            tool_ctx: 工具上下文
            callback: 可选流式回调

        Returns:
            (tool_results, edit_results, tool_log) 元组
            - tool_results: tool result 消息列表
            - edit_results: 编辑结果记录
            - tool_log: 工具调用日志
        """
        if not tool_calls:
            return [], [], []

        # 分类：可并行的只读工具 vs 必须顺序的写操作工具
        parallel_group: List[Tuple[int, Dict[str, Any]]] = []  # (original_index, tc)
        sequential_group: List[Tuple[int, Dict[str, Any]]] = []

        for i, tc in enumerate(tool_calls):
            tool_name = tc.get('name', '')
            tool = self.registry.get(tool_name)
            if tool and tool.concurrency_safe:
                parallel_group.append((i, tc))
            else:
                sequential_group.append((i, tc))

        all_results: Dict[int, Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]] = {}
        # {original_index: (tool_msg, edit_result, log_entry)}

        # ---- 并行执行只读工具 ----
        if parallel_group:
            with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
                futures = {}
                for orig_idx, tc in parallel_group:
                    future = pool.submit(
                        self._execute_single_tool, tc, tool_ctx,
                    )
                    futures[future] = orig_idx

                for future in as_completed(futures):
                    orig_idx = futures[future]
                    try:
                        result = future.result()
                    except Exception as e:
                        tc = tool_calls[orig_idx]
                        result = ToolResult(
                            content=f"并行执行异常: {type(e).__name__}: {e}",
                            success=False,
                        )
                    tc = tool_calls[orig_idx]
                    tool_msg, edit_result, log_entry = self._process_result(
                        tc, result, tool_ctx, callback,
                    )
                    all_results[orig_idx] = (tool_msg, edit_result, log_entry)

        # ---- 顺序执行写操作工具 ----
        for orig_idx, tc in sequential_group:
            result = self._execute_single_tool(tc, tool_ctx)
            tool_msg, edit_result, log_entry = self._process_result(
                tc, result, tool_ctx, callback,
            )
            all_results[orig_idx] = (tool_msg, edit_result, log_entry)

        # 按原始顺序组装结果
        tool_results = []
        edit_results = []
        tool_log = []
        for i in range(len(tool_calls)):
            if i in all_results:
                msg, er, log = all_results[i]
                tool_results.append(msg)
                if er:
                    edit_results.append(er)
                tool_log.append(log)

        return tool_results, edit_results, tool_log

    def _execute_single_tool(
        self,
        tc: Dict[str, Any],
        tool_ctx: ToolContext,
    ) -> ToolResult:
        """执行单个工具调用（含权限检查和 Hook）"""
        tc_name = tc.get('name', '')
        tc_args = tc.get('arguments', {})

        # PreToolUse Hook
        pre_result = self.hooks.run_pre_tool(tc_name, tc_args, tool_ctx)
        if not pre_result.proceed:
            return ToolResult(
                content=f"Hook 阻止执行：工具 '{tc_name}' 被 PreToolUse Hook 拦截。",
                success=False,
            )

        effective_args = pre_result.modified_args or tc_args

        # 权限检查
        perm = self.permissions.check(tc_name, effective_args, tool_ctx)
        if perm == PermissionDecision.DENY:
            return ToolResult(
                content=f"权限拒绝：工具 '{tc_name}' 被禁止执行。",
                success=False,
            )

        # 执行工具
        return self.registry.execute_tool(tc_name, effective_args, tool_ctx)

    def _process_result(
        self,
        tc: Dict[str, Any],
        result: ToolResult,
        tool_ctx: ToolContext,
        callback: Optional[Callable] = None,
    ) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]], Dict[str, Any]]:
        """处理工具执行结果：运行 Post Hook、记录日志、构建消息"""
        tc_id = tc.get('id', '')
        tc_name = tc.get('name', '')
        tc_args = tc.get('arguments', {})

        # PostToolUse Hook
        post_result = self.hooks.run_post_tool(tc_name, tc_args, result, tool_ctx)
        if post_result.modified_result is not None:
            result = post_result.modified_result

        # 编辑结果记录
        edit_result = None
        if tc_name in ('edit_file', 'edit_page'):
            edit_result = {
                'tool': tc_name,
                'page': tc_args.get('page', tc_args.get('page_key', '')),
                'applied': result.success,
                'error': result.metadata.get('error') if not result.success else None,
            }

        # 日志（包含历史回放所需的丰富信息）
        log_entry = {
            'tool': tc_name,
            'args_keys': list(tc_args.keys()),
            'success': result.success,
            'content_length': len(result.content),
            'page': tc_args.get('page', tc_args.get('page_key', '')),
            'line_start': tc_args.get('start_line'),
            'line_end': tc_args.get('end_line'),
        }
        # 从工具返回的 metadata 补充信息
        if result.success and result.metadata:
            for key in ('page', 'line_start', 'line_end',
                        'total_lines', 'page_count'):
                if key in result.metadata and not log_entry.get(key):
                    log_entry[key] = result.metadata[key]
            if 'old_text' in result.metadata:
                log_entry['old_text'] = result.metadata['old_text']
            if 'new_text' in result.metadata:
                log_entry['new_text'] = result.metadata['new_text']

        # 流式事件（UI 层：携带 diff 数据供前端渲染）
        event_data = {
            'tool_call_id': tc_id,
            'tool_name': tc_name,
            'success': result.success,
            'content_length': len(result.content),
        }
        # 将 metadata 中的数据透传到 UI 层
        if result.success and result.metadata:
            # 通用字段：page、line_start、line_end、total_lines
            for key in ('page', 'line_start', 'line_end', 'total_lines'):
                if key in result.metadata:
                    event_data[key] = result.metadata[key]
            # diff 数据
            if 'old_text' in result.metadata or 'new_text' in result.metadata:
                event_data['old_text'] = result.metadata.get('old_text', '')
                event_data['new_text'] = result.metadata.get('new_text', '')
            if result.metadata.get('write_page'):
                event_data['write_page'] = True
        AgentLoop._emit(callback, StreamEventType.TOOL_CALL_COMPLETED, event_data)

        return result.to_message(tc_id, tc_name), edit_result, log_entry


# ---------------------------------------------------------------------------
# Phase 3: 磁盘溢出管理器
# ---------------------------------------------------------------------------


@dataclass
class DiskOverflowConfig:
    """磁盘溢出配置"""
    max_in_memory_chars: int = 100000    # 超过此大小的工具结果存入磁盘
    summary_max_chars: int = 500         # 磁盘溢出时在消息中保留的摘要字符数
    temp_dir: Optional[str] = None       # 临时文件目录 (None=系统默认)
    cleanup_on_read: bool = True         # 读取后是否自动清理临时文件


class DiskOverflowManager:
    """大型工具结果磁盘持久化

    对应 CCB 的磁盘溢出能力：当工具结果过大时，将完整内容写入临时文件，
    在消息中只保留摘要引用，避免撑爆上下文窗口。

    用法：
        overflow = DiskOverflowManager()
        content_or_ref = overflow.maybe_spill(
            tool_name='read_page',
            content=large_content,
            tool_call_id='call-123',
        )
        # content_or_ref 可能是截断的摘要 + 文件路径引用
    """

    def __init__(self, config: Optional[DiskOverflowConfig] = None) -> None:
        self.config = config or DiskOverflowConfig()
        self._temp_files: Dict[str, str] = {}  # tool_call_id -> temp file path
        self._lock = threading.Lock()

    def maybe_spill(
        self,
        tool_name: str,
        content: str,
        tool_call_id: str,
    ) -> str:
        """检查工具结果是否需要溢出到磁盘

        Args:
            tool_name: 工具名称
            content: 工具结果的完整内容
            tool_call_id: 工具调用 ID

        Returns:
            处理后的内容 (可能是摘要 + 文件路径引用)
        """
        if len(content) <= self.config.max_in_memory_chars:
            return content

        # 写入临时文件
        temp_dir = self.config.temp_dir or tempfile.gettempdir()
        os.makedirs(temp_dir, exist_ok=True)

        filename = f"agent_overflow_{tool_call_id}_{tool_name}.txt"
        filepath = os.path.join(temp_dir, filename)

        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(content)
        except OSError as e:
            logger.warning(f"[DiskOverflow] 写入临时文件失败: {e}")
            # 降级：截断内容而非溢出
            return content[:self.config.max_in_memory_chars] + '\n\n[内容过大且磁盘溢出失败，已截断]'

        with self._lock:
            self._temp_files[tool_call_id] = filepath

        # 构建摘要 + 引用
        summary_chars = self.config.summary_max_chars
        summary = content[:summary_chars]

        result = (
            f"{summary}\n\n"
            f"---\n[内容过大 ({len(content):,} 字符)，完整内容已保存到: {filepath}]\n"
            f"[如需查看完整内容，请使用 read_file 读取该文件路径]\n"
            f"[溢出ID: {tool_call_id}]"
        )

        logger.info(
            f"[DiskOverflow] 工具 '{tool_name}' 结果溢出到磁盘: "
            f"{len(content):,} 字符 → {filepath}"
        )

        return result

    def retrieve(self, tool_call_id: str) -> Optional[str]:
        """从磁盘检索溢出的工具结果

        Args:
            tool_call_id: 工具调用 ID

        Returns:
            完整内容，如果未找到则返回 None
        """
        with self._lock:
            filepath = self._temp_files.get(tool_call_id)

        if not filepath:
            return None

        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
            if self.config.cleanup_on_read:
                self.cleanup(tool_call_id)
            return content
        except OSError as e:
            logger.warning(f"[DiskOverflow] 读取临时文件失败: {e}")
            return None

    def cleanup(self, tool_call_id: str) -> None:
        """清理指定工具调用的临时文件"""
        with self._lock:
            filepath = self._temp_files.pop(tool_call_id, None)

        if filepath and os.path.exists(filepath):
            try:
                os.remove(filepath)
                logger.debug(f"[DiskOverflow] 已清理临时文件: {filepath}")
            except OSError:
                pass

    def cleanup_all(self) -> None:
        """清理所有临时文件"""
        with self._lock:
            paths = list(self._temp_files.values())
            self._temp_files.clear()

        for filepath in paths:
            if os.path.exists(filepath):
                try:
                    os.remove(filepath)
                except OSError:
                    pass

    @property
    def overflow_count(self) -> int:
        """已溢出到磁盘的工具结果数量"""
        with self._lock:
            return len(self._temp_files)


# ---------------------------------------------------------------------------
# Agent Loop 配置
# ---------------------------------------------------------------------------


@dataclass
class AgentLoopConfig:
    """Agent Loop 配置"""
    max_turns: int = 50                     # 最大循环轮次
    max_consecutive_empty: int = 2          # 连续空响应退出阈值
    max_consecutive_reads: int = 3          # 编辑后连续读取干预阈值
    max_explore_reads: int = 6              # 编辑前探索性读取上限
    max_verify_retries: int = 3             # 验证失败最大重试次数
    context_config: CompactionConfig = field(default_factory=CompactionConfig)
    tool_names: Optional[List[str]] = None  # 使用的工具子集 (None=全部)
    system_prompt: str = ''                 # 系统提示词 (仅在构建消息时需要)
    streaming: bool = True                  # 是否使用流式调用
    extract_html: bool = False              # 是否从文本中提取 HTML
    fallback_model: Optional[str] = None    # 主模型失败时的回退模型
    max_output_tokens_recovery: int = 3     # max_output_tokens 截断恢复最大重试次数
    max_output_tokens_escalate: int = 65536 # 截断恢复时升级到的 token 上限
    parallel_execution: bool = False         # Phase 3: 是否启用并行工具执行
    parallel_max_workers: int = 3            # Phase 3: 并行执行的最大线程数
    disk_overflow: bool = False              # Phase 3: 是否启用磁盘溢出
    proactive_compaction: bool = True        # Phase 3: 是否启用预测性压缩
    tool_result_max_chars: int = 100000      # 单条工具结果字符上限 (Tool Result Budget)


# ---------------------------------------------------------------------------
# 核心 AgentLoop 类
# ---------------------------------------------------------------------------


class AgentLoop:
    """CCB 风格的 Agent Loop 引擎

    对应 CCB 的 queryLoop() (src/query.ts:392)

    用法：
        loop = AgentLoop(config, registry, context_manager)
        result = loop.run(
            initial_messages=[...],
            tool_context=tool_ctx,
            streaming_callback=my_sse_push,
            cancel_check=my_cancel_fn,
        )
    """

    def __init__(
        self,
        config: AgentLoopConfig,
        tool_registry: ToolRegistry,
        context_manager: Optional[ContextManager] = None,
        permission_manager: Optional[PermissionManager] = None,
        hook_manager: Optional[HookManager] = None,
        disk_overflow_manager: Optional[DiskOverflowManager] = None,
    ) -> None:
        self.config = config
        self.tools = tool_registry
        self.context = context_manager or ContextManager(config.context_config)
        self.permissions = permission_manager or PermissionManager()
        self.hooks = hook_manager or HookManager()
        self.disk_overflow = disk_overflow_manager
        self._parallel_executor: Optional[ParallelToolExecutor] = None

    def run(
        self,
        initial_messages: List[Dict[str, Any]],
        tool_context: ToolContext,
        streaming_callback: Optional[Callable[[str, Dict], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        ai_call_fn: Optional[Callable] = None,
    ) -> LoopResult:
        """主循环入口点 (同步阻塞)

        Args:
            initial_messages: 初始消息列表 (system + user)
            tool_context: 传递给工具的上下文
            streaming_callback: 可选的 SSE 推送函数 fn(event_type, data)
            cancel_check: 可选的取消检查函数 fn() -> bool
            ai_call_fn: 可选的 AI 调用覆盖
                        (默认 tool_context.server.call_ai_model_streaming)
                        签名: fn(messages, images, tools, ...) -> Generator

        Returns:
            LoopResult: 循环结果
        """
        state = LoopState(messages=list(initial_messages))
        start_time = time.time()
        total_tool_calls = 0

        self._emit(streaming_callback, StreamEventType.LOOP_END,
                    {'status': 'started', 'max_turns': self.config.max_turns})

        while True:
            # ---- 取消检查 ----
            if cancel_check and cancel_check():
                return self._build_result(
                    TerminalReason.CANCELLED, state, total_tool_calls, start_time
                )

            # ---- Phase A: 上下文准备 ----
            state = self._prepare_context(state, streaming_callback)

            # 上下文溢出检查
            if self.context.estimate_size(state.messages) > self.config.context_config.max_context_chars:
                # 尝试更激进的压缩
                state = state.with_updates(
                    messages=self.context.compact(
                        state.messages, ContextStrategy.SKELETON_SUMMARY
                    )
                )
                if self.context.estimate_size(state.messages) > self.config.context_config.max_context_chars:
                    return self._build_result(
                        TerminalReason.CONTEXT_OVERFLOW, state, total_tool_calls, start_time
                    )

            self._emit(streaming_callback, StreamEventType.TURN_START,
                        {'turn': state.turn_count})

            # ---- Phase B: 流式 AI 调用 ----
            state = self._stream_ai_call(
                state, tool_context, streaming_callback, ai_call_fn,
                cancel_check=cancel_check,
            )

            # 检查流式期间是否检测到取消
            if state._cancel_detected:
                return self._build_result(
                    TerminalReason.CANCELLED, state, total_tool_calls, start_time
                )

            # ---- Token Budget 递减收益检测 ----
            # 对应 CCB 的 continuationCount + deltaSinceLastCheck 检测
            # 连续 3 次产生 < 500 字符的文本响应（无工具调用）视为递减收益
            if state.accumulated_text and not state.tool_calls_result:
                text_len = len(state.accumulated_text.strip())
                if text_len < 500:
                    new_count = state._diminishing_returns_count + 1
                    state = state.with_updates(
                        _last_text_length=text_len,
                        _diminishing_returns_count=new_count,
                    )
                    if new_count >= 3:
                        logger.info(
                            f"[AgentLoop] 检测到递减收益 (连续 {new_count} 次小响应)，"
                            f"终止循环"
                        )
                        return self._build_result(
                            TerminalReason.COMPLETED, state,
                            total_tool_calls, start_time,
                        )
                else:
                    state = state.with_updates(
                        _last_text_length=text_len,
                        _diminishing_returns_count=0,
                    )
            else:
                # 有工具调用或空响应 → 重置计数
                if state._diminishing_returns_count > 0:
                    state = state.with_updates(_diminishing_returns_count=0)

            # ---- Phase C: 无工具 → 处理终止或继续 ----
            if not state.tool_calls_result:
                transition = self._handle_no_tools(state, streaming_callback)
                if isinstance(transition, TerminalReason):
                    # Stop Hook 死循环防护 (对应 CCB 的 isApiErrorMessage 检查)
                    # 当 AI 调用异常时跳过 Stop Hook，防止 error→hook→retry→error 死循环
                    if not state._api_error:
                        hook_res = self.hooks.run_stop(transition, state)
                        if not hook_res.proceed:
                            # Stop Hook 阻止终止 → 注入消息继续循环
                            inject = hook_res.inject_message or '请继续工作。'
                            state = state.with_updates(
                                messages=state.messages + [
                                    {'role': 'assistant', 'content': state.accumulated_text or '(无输出)'},
                                    {'role': 'user', 'content': inject},
                                ],
                                accumulated_text='',
                                accumulated_reasoning='',
                            )
                            continue
                    return self._build_result(
                        transition, state, total_tool_calls, start_time
                    )
                # ContinueReason → 注入消息继续
                state = transition
                continue

            # ---- Phase D: 执行工具 ----
            state = self._execute_tools(state, tool_context, streaming_callback)

            # 工具执行后再次检查取消 (对应 CCB 的 post-tools abort check)
            if cancel_check and cancel_check():
                return self._build_result(
                    TerminalReason.CANCELLED, state, total_tool_calls, start_time
                )

            total_tool_calls += len(state.tool_calls_result or [])

            # 检查是否有成功的编辑操作（基于实际执行结果）
            round_has_edit = any(
                er.get('applied', False)
                for er in state.edit_results
            )
            round_has_read = any(
                tc.get('name') in ('read_page', 'read_file', 'read_current_file')
                for tc in (state.tool_calls_result or [])
            )

            if round_has_edit:
                state = state.with_updates(
                    has_update=True,
                    has_ever_edited=True,
                    consecutive_reads=0,
                    consecutive_empty=0,
                )
            elif round_has_read:
                state = state.with_updates(
                    consecutive_reads=state.consecutive_reads + 1,
                )

            self._emit(streaming_callback, StreamEventType.TURN_END,
                        {'turn': state.turn_count, 'tool_calls': len(state.tool_calls_result or [])})

            # ---- Phase E: 构建下一轮状态 ----
            state = state.with_updates(
                turn_count=state.turn_count + 1,
                accumulated_text='',
                accumulated_reasoning='',
                tool_calls_result=None,
            )

            # 最大轮次检查
            if state.turn_count >= self.config.max_turns:
                return self._build_result(
                    TerminalReason.MAX_TURNS, state, total_tool_calls, start_time
                )

            # 连续读取干预
            if (state.consecutive_reads > self.config.max_consecutive_reads
                    and state.has_ever_edited):
                inject_msg = (
                    "你已连续读取多轮但未进行编辑。"
                    "请基于已读取的信息进行编辑操作，或告知完成。"
                )
                state = state.with_updates(
                    messages=state.messages + [{
                        'role': 'user',
                        'content': inject_msg,
                    }],
                    consecutive_reads=0,
                )

    # ------------------------------------------------------------------
    # Phase A: 上下文准备
    # ------------------------------------------------------------------

    def _prepare_context(
        self,
        state: LoopState,
        callback: Optional[Callable] = None,
    ) -> LoopState:
        """压缩上下文 (如果需要)

        Phase 3 增强：在被动压缩基础上增加预测性压缩。
        预测性压缩在 AI 调用前预判下一轮是否会溢出，提前压缩避免浪费。
        同时应用 Tool Result Budget 截断过大的工具结果。
        """
        # Tool Result Budget: 截断过大的工具结果 (对应 CCB 的 applyToolResultBudget)
        budget_messages = self._apply_tool_result_budget(state.messages)
        if budget_messages is not state.messages:
            state = state.with_updates(messages=budget_messages)

        # 被动压缩：当前大小超过阈值
        if self.context.should_compact(state.messages):
            new_messages = self.context.compact(state.messages)
            self._emit(callback, StreamEventType.CONTEXT_COMPACTED, {
                'strategy': 'read_tool_compact',
                'before': self.context.estimate_size(state.messages),
                'after': self.context.estimate_size(new_messages),
            })
            return state.with_updates(messages=new_messages)

        # Phase 3: 预测性压缩
        if self.config.proactive_compaction and self.context.should_proactive_compact(state.messages):
            new_messages = self.context.compact(state.messages)
            self._emit(callback, StreamEventType.CONTEXT_COMPACTED, {
                'strategy': 'proactive_read_tool_compact',
                'before': self.context.estimate_size(state.messages),
                'after': self.context.estimate_size(new_messages),
                'predicted_next': self.context.estimate_next_turn_size(state.messages),
            })
            return state.with_updates(messages=new_messages)

        return state

    def _apply_tool_result_budget(
        self, messages: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """对工具结果应用大小上限 (Tool Result Budget)

        对应 CCB 的 applyToolResultBudget()。
        截断过大的工具结果，防止单条结果消耗整个上下文窗口。
        """
        budget = self.config.tool_result_max_chars
        changed = False
        result = []
        for msg in messages:
            if msg.get('role') == 'tool' and isinstance(msg.get('content'), str):
                content = msg['content']
                if len(content) > budget:
                    truncated = content[:budget] + (
                        f"\n\n[... 工具结果已截断：原始 {len(content)} 字符，"
                        f"保留 {budget} 字符 ...]"
                    )
                    result.append({**msg, 'content': truncated})
                    changed = True
                    continue
            result.append(msg)
        return result if changed else messages

    # ------------------------------------------------------------------
    # Phase B: 流式 AI 调用
    # ------------------------------------------------------------------

    def _stream_ai_call(
        self,
        state: LoopState,
        tool_ctx: ToolContext,
        callback: Optional[Callable],
        ai_call_fn: Optional[Callable] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> LoopState:
        """调用 AI 模型 (流式)，累积文本和工具调用

        对应 CCB 的 Phase B (src/query.ts:877-1197)
        在流式循环中检查 cancel_check，实现中途取消。
        包含模型回退机制 (FallbackTriggeredError + Tombstone)。
        """
        messages = state.messages
        tools_spec = self.tools.get_tools_spec(self.config.tool_names)

        # 确定 AI 调用函数
        ai_fn = self._resolve_ai_fn(ai_call_fn, tool_ctx)
        if ai_fn is None:
            return state.with_updates(
                accumulated_text='',
                tool_calls_result=None,
                consecutive_empty=state.consecutive_empty + 1,
            )

        # 调用 AI 流式接口
        try:
            gen = ai_fn(
                messages,  # prompt_or_messages
                [],  # images
                tools=tools_spec if tools_spec else None,
                cancellable_project_id=tool_ctx.project_id,
            )
        except Exception as e:
            # 生成器创建失败
            logger.error(f"[AgentLoop] AI 生成器创建失败: {e}")
            return state.with_updates(
                accumulated_text='',
                tool_calls_result=None,
                consecutive_empty=state.consecutive_empty + 1,
                _api_error=True,
            )

        # 累积流式响应 (不抛异常，返回部分结果)
        accumulated_text, accumulated_reasoning, tool_calls_accum, cancelled, stream_error = (
            self._accumulate_stream(gen, callback, cancel_check)
        )

        # 取消处理
        if cancelled:
            logger.info("[AgentLoop] 流式期间检测到取消信号")
            synthetic_msgs = self._create_tombstone_messages(
                state.messages, accumulated_text, tool_calls_accum,
                RuntimeError("用户取消"),
            )
            return state.with_updates(
                accumulated_text='',
                messages=synthetic_msgs if tool_calls_accum else state.messages,
                _cancel_detected=True,
            )

        # 流式错误处理 (含模型回退)
        if stream_error is not None:
            e = stream_error
            logger.error(f"[AgentLoop] AI 调用异常: {e}")

            # 创建 Tombstone 消息 (对应 CCB 的 yieldMissingToolResultBlocks)
            synthetic_messages = self._create_tombstone_messages(
                state.messages, accumulated_text, tool_calls_accum, e,
            )

            # 模型回退机制 (对应 CCB 的 FallbackTriggeredError)
            if (self.config.fallback_model
                    and not state._fallback_attempted
                    and ai_call_fn):
                logger.info(
                    f"[AgentLoop] 尝试回退模型: {self.config.fallback_model}"
                )
                try:
                    fallback_messages = (
                        synthetic_messages if tool_calls_accum else state.messages
                    )
                    fb_gen = ai_call_fn(
                        fallback_messages,
                        [],
                        tools=tools_spec if tools_spec else None,
                        model=self.config.fallback_model,
                        cancellable_project_id=tool_ctx.project_id,
                    )
                    fb_text, fb_reasoning, fb_tc, fb_cancelled, fb_error = (
                        self._accumulate_stream(fb_gen, callback, cancel_check)
                    )
                    if fb_cancelled:
                        return state.with_updates(
                            accumulated_text='',
                            messages=fallback_messages,
                            _cancel_detected=True,
                            _fallback_attempted=True,
                        )
                    if fb_error is not None:
                        logger.error(f"[AgentLoop] 回退模型也失败: {fb_error}")
                    else:
                        # 回退成功 — 构建结果
                        final_tc = self._build_tool_calls(fb_tc)
                        if final_tc:
                            for tc in final_tc:
                                self._emit(callback, StreamEventType.TOOL_CALL_STARTED, {
                                    'tool_call_id': tc['id'],
                                    'tool_name': tc['name'],
                                })
                        return state.with_updates(
                            messages=fallback_messages,
                            accumulated_text=fb_text,
                            accumulated_reasoning=fb_reasoning,
                            tool_calls_result=final_tc if final_tc else None,
                            _fallback_attempted=True,
                        )
                except Exception as fallback_create_e:
                    logger.error(f"[AgentLoop] 回退模型创建失败: {fallback_create_e}")

            # 无回退或回退也失败
            return state.with_updates(
                accumulated_text='',
                accumulated_reasoning='',
                tool_calls_result=None,
                messages=synthetic_messages if tool_calls_accum else state.messages,
                consecutive_empty=state.consecutive_empty + 1,
                _api_error=True,
                _fallback_attempted=True,
            )

        # 正常完成 — 处理累积的工具调用
        final_tool_calls = self._build_tool_calls(tool_calls_accum)

        if final_tool_calls:
            for tc in final_tool_calls:
                self._emit(callback, StreamEventType.TOOL_CALL_STARTED, {
                    'tool_call_id': tc['id'],
                    'tool_name': tc['name'],
                })

        return state.with_updates(
            accumulated_text=accumulated_text,
            accumulated_reasoning=accumulated_reasoning,
            tool_calls_result=final_tool_calls if final_tool_calls else None,
        )

    def _resolve_ai_fn(
        self, ai_call_fn: Optional[Callable], tool_ctx: ToolContext,
    ) -> Optional[Callable]:
        """解析 AI 调用函数"""
        if ai_call_fn:
            return ai_call_fn
        if tool_ctx.server and hasattr(tool_ctx.server, 'call_ai_model_streaming'):
            return tool_ctx.server.call_ai_model_streaming
        return None

    def _accumulate_stream(
        self,
        gen: Generator,
        callback: Optional[Callable],
        cancel_check: Optional[Callable[[], bool]],
    ) -> Tuple[str, str, Dict[int, Dict], bool, Optional[Exception]]:
        """从 AI 流式生成器中累积文本、推理和工具调用

        内部捕获异常以保留已累积的部分结果。

        Returns:
            (accumulated_text, accumulated_reasoning, tool_calls_accum,
             cancel_detected, stream_error)
        """
        accumulated_text = ''
        accumulated_reasoning = ''
        tool_calls_accum = {}  # {index: {id, name, arguments_str}}

        try:
            for chunk in gen:
                # 兼容不同长度的返回元组
                if isinstance(chunk, tuple):
                    chunk_text = chunk[0] if len(chunk) > 0 else ''
                    done = chunk[2] if len(chunk) > 2 else False
                    tool_calls_raw = chunk[3] if len(chunk) > 3 else []
                    reasoning = chunk[4] if len(chunk) > 4 else ''
                else:
                    chunk_text = str(chunk)
                    done = False
                    tool_calls_raw = []
                    reasoning = ''

                if chunk_text:
                    accumulated_text += chunk_text
                    self._emit(callback, StreamEventType.TEXT_DELTA, {
                        'text': chunk_text,
                    })

                if reasoning:
                    accumulated_reasoning += reasoning
                    self._emit(callback, StreamEventType.REASONING_DELTA, {
                        'text': reasoning,
                    })

                if tool_calls_raw:
                    # 兼容多种格式：
                    # 1. 增量 dict: {str_key: {id, name, arguments(str)}} (call_ai_model_streaming 增量)
                    # 2. 最终 list: [{id, name, arguments(dict)}] (call_ai_model_streaming 完成)
                    # 3. OpenAI 标准增量 list: [{index, id, function:{name,arguments}}]
                    if isinstance(tool_calls_raw, dict):
                        for _key, tc_data in tool_calls_raw.items():
                            # 用已存在的 index 或分配新的
                            idx = len(tool_calls_accum)
                            # 检查是否已有同 id 的条目
                            tc_id = tc_data.get('id', '')
                            for existing_idx, existing in tool_calls_accum.items():
                                if existing.get('id') and existing['id'] == tc_id:
                                    idx = existing_idx
                                    break
                            if idx not in tool_calls_accum:
                                tool_calls_accum[idx] = {
                                    'id': tc_data.get('id', ''),
                                    'name': tc_data.get('name', ''),
                                    'arguments': '',
                                }
                            if tc_data.get('name'):
                                tool_calls_accum[idx]['name'] = tc_data['name']
                            raw_args = tc_data.get('arguments', '')
                            if raw_args:
                                if isinstance(raw_args, dict):
                                    raw_args = json.dumps(raw_args, ensure_ascii=False)
                                tool_calls_accum[idx]['arguments'] += raw_args
                            if tc_data.get('id'):
                                tool_calls_accum[idx]['id'] = tc_data['id']
                    elif isinstance(tool_calls_raw, list):
                        for tc in tool_calls_raw:
                            # 支持 OpenAI 标准格式 {index, id, function:{name,arguments}}
                            # 和扁平格式 {id, name, arguments}
                            if 'function' in tc and isinstance(tc['function'], dict):
                                fn = tc['function']
                                idx = tc.get('index', len(tool_calls_accum))
                                tc_name = fn.get('name', '')
                                tc_args = fn.get('arguments', '')
                                tc_id = tc.get('id', '')
                            else:
                                idx = tc.get('index', len(tool_calls_accum))
                                tc_name = tc.get('name', '')
                                tc_args = tc.get('arguments', '')
                                tc_id = tc.get('id', '')
                            if idx not in tool_calls_accum:
                                tool_calls_accum[idx] = {
                                    'id': tc_id,
                                    'name': tc_name,
                                    'arguments': '',
                                }
                            if tc_name:
                                tool_calls_accum[idx]['name'] = tc_name
                            if tc_args:
                                if isinstance(tc_args, dict):
                                    tc_args = json.dumps(tc_args, ensure_ascii=False)
                                tool_calls_accum[idx]['arguments'] += tc_args
                            if tc_id:
                                tool_calls_accum[idx]['id'] = tc_id

                # 流式期间检查取消信号 (对应 CCB 的 abortController.signal)
                if cancel_check and cancel_check():
                    return accumulated_text, accumulated_reasoning, tool_calls_accum, True, None

        except Exception as e:
            # 保留已累积的部分结果，返回错误供上层处理
            return accumulated_text, accumulated_reasoning, tool_calls_accum, False, e

        return accumulated_text, accumulated_reasoning, tool_calls_accum, False, None

    def _build_tool_calls(
        self, tool_calls_accum: Dict[int, Dict],
    ) -> List[Dict[str, Any]]:
        """从累积的工具调用构建最终工具调用列表"""
        final_tool_calls = []
        for idx in sorted(tool_calls_accum.keys()):
            tc = tool_calls_accum[idx]
            args = {}
            if tc['arguments']:
                try:
                    args = json.loads(tc['arguments'])
                except (json.JSONDecodeError, TypeError):
                    args = {'_raw_arguments': tc['arguments']}

            final_tool_calls.append({
                'id': tc['id'],
                'name': tc['name'],
                'arguments': args,
            })
        return final_tool_calls

    @staticmethod
    def _create_tombstone_messages(
        existing_messages: List[Dict[str, Any]],
        accumulated_text: str,
        tool_calls_accum: Dict[int, Dict],
        error: Exception,
    ) -> List[Dict[str, Any]]:
        """为孤立的 tool_use 块创建 Tombstone 消息

        对应 CCB 的 yieldMissingToolResultBlocks()。
        当 AI 调用异常时，为已累积的部分 tool_use 块生成合成的 tool_result，
        确保下轮 API 调用不会因 orphan tool_use 消息报错。
        """
        result = list(existing_messages)
        if not tool_calls_accum:
            return result

        # 构建包含已累积 tool_calls 的 assistant 消息
        partial_assistant = {
            'role': 'assistant',
            'content': accumulated_text or None,
            'tool_calls': [],
        }
        for idx in sorted(tool_calls_accum.keys()):
            tc = tool_calls_accum[idx]
            partial_assistant['tool_calls'].append({
                'id': tc.get('id', f'synthetic-{idx}'),
                'type': 'function',
                'function': {
                    'name': tc.get('name', 'unknown'),
                    'arguments': tc.get('arguments', '{}'),
                },
            })
        result.append(partial_assistant)

        # 为每个 tool_call 生成 Tombstone tool_result
        error_msg = (
            f"[Tombstone] AI 调用过程中发生异常 "
            f"({type(error).__name__}: {error})，"
            f"工具调用被中断。请重新发起请求。"
        )
        for idx in sorted(tool_calls_accum.keys()):
            tc = tool_calls_accum[idx]
            result.append({
                'role': 'tool',
                'tool_call_id': tc.get('id', f'synthetic-{idx}'),
                'name': tc.get('name', 'unknown'),
                'content': error_msg,
            })

        return result

    # ------------------------------------------------------------------
    # Phase C: 无工具 → 处理终止或继续
    # ------------------------------------------------------------------

    def _handle_no_tools(
        self,
        state: LoopState,
        callback: Optional[Callable] = None,
    ) -> object:
        """AI 未返回工具调用时的处理

        包含错误恢复机制 (对应 CCB 的错误恢复路径):
        - 反应性压缩: 上下文过大时压缩后重试
        - max_output_tokens 恢复: 响应被截断时升级 token 限制并重试

        Returns:
            TerminalReason — 循环终止
            LoopState — 新状态继续循环
        """
        text = state.accumulated_text.strip()
        reasoning = state.accumulated_reasoning.strip()

        # ---- 检查是否有文本截断信号 ----
        # AI 响应被截断的常见特征：文本突然结束，没有句号/闭合标签
        if text and self._is_truncated(text):
            recovery_count = getattr(state, '_output_tokens_recovery', 0)
            if recovery_count < self.config.max_output_tokens_recovery:
                logger.info(
                    f"[AgentLoop] 检测到截断响应，执行恢复 (第 {recovery_count + 1} 次)"
                )
                inject = (
                    "你的上一次响应似乎被截断了。请直接继续输出，不要重复已有内容。"
                )
                new_messages = state.messages + [
                    {'role': 'assistant', 'content': text},
                    {'role': 'user', 'content': inject},
                ]
                return state.with_updates(
                    messages=new_messages,
                    accumulated_text='',
                    accumulated_reasoning='',
                    _output_tokens_recovery=recovery_count + 1,
                )

        # 有文本内容 → 可能是完成
        if text:
            # 检查是否需要提取 HTML
            if self.config.extract_html:
                self._extract_html(text)

            return TerminalReason.COMPLETED

        # ---- 检查是否需要反应性压缩 ----
        # 对应 CCB 的 reactive_compact：当上下文过大导致 AI 无法正常响应时
        # hasAttemptedReactiveCompact 守卫：防止无限压缩循环
        current_size = self.context.estimate_size(state.messages)
        if (current_size > self.config.context_config.compact_threshold_chars
                and not state._has_attempted_reactive_compact):
            logger.info("[AgentLoop] 检测到上下文过大，执行反应性压缩后重试")
            compacted = self.context.compact(
                state.messages, ContextStrategy.SKELETON_SUMMARY
            )
            self._emit(callback, StreamEventType.CONTEXT_COMPACTED, {
                'strategy': 'reactive_skeleton',
                'before': current_size,
                'after': self.context.estimate_size(compacted),
            })
            inject = "上下文已压缩。请继续你的工作。"
            return state.with_updates(
                messages=compacted + [
                    {'role': 'assistant', 'content': '(无文本输出)'},
                    {'role': 'user', 'content': inject},
                ],
                accumulated_text='',
                accumulated_reasoning='',
                _has_attempted_reactive_compact=True,
            )

        # 纯 reasoning 无文本且无工具调用 → 空响应
        state = state.with_updates(
            consecutive_empty=state.consecutive_empty + 1,
        )

        if state.consecutive_empty >= self.config.max_consecutive_empty:
            logger.info(
                f"[AgentLoop] 连续空响应 {state.consecutive_empty} 次，退出循环"
            )
            return TerminalReason.COMPLETED

        # 注入提示消息继续
        inject = "请继续工作。如果已完成所有任务，请回复总结。"
        new_messages = state.messages + [
            {
                'role': 'assistant',
                'content': reasoning if reasoning else '(无文本输出)',
            },
            {
                'role': 'user',
                'content': inject,
            },
        ]

        return state.with_updates(messages=new_messages)

    @staticmethod
    def _is_truncated(text: str) -> bool:
        """检测 AI 响应是否被截断

        截断的典型特征：
        - 文本以未闭合的代码块结尾 (``` 或 ```)
        - 文本以未闭合的 HTML 标签结尾
        - 文本以不完整的句子结尾 (无句号/换行)
        """
        stripped = text.rstrip()
        if not stripped:
            return False

        # 以未闭合的代码块结尾
        code_block_count = stripped.count('```')
        if code_block_count % 2 != 0:
            return True

        # 以未闭合的大括号/括号结尾 (常见于代码截断)
        last_50 = stripped[-50:]
        open_braces = last_50.count('{') - last_50.count('}')
        open_brackets = last_50.count('[') - last_50.count(']')
        if open_braces > 2 or open_brackets > 2:
            return True

        # 以非终止字符结尾 (常见于中文/英文截断)
        last_char = stripped[-1]
        terminal_chars = set('.。!！?？`"\'）)】]}>\n')
        if last_char not in terminal_chars and len(stripped) > 200:
            # 只在较长文本中检查（短文本可能是正常的）
            # 检查最后20个字符是否看起来像一个完整句子
            last_line = stripped.split('\n')[-1]
            if len(last_line) > 80 and last_char not in {',', '，', ';', '；', ':'}:
                return True

        return False

    # ------------------------------------------------------------------
    # Phase D: 执行工具
    # ------------------------------------------------------------------

    def _execute_tools(
        self,
        state: LoopState,
        tool_ctx: ToolContext,
        callback: Optional[Callable] = None,
    ) -> LoopState:
        """执行所有工具调用

        Phase 1: 顺序执行
        Phase 3: 可配置并行执行 + 磁盘溢出
        """
        tool_calls = state.tool_calls_result or []
        if not tool_calls:
            return state

        # 构建 assistant 消息（带 tool_calls）
        assistant_msg = self._build_assistant_message(state)
        new_messages = state.messages + [assistant_msg]

        # Phase 3: 并行执行模式
        if self.config.parallel_execution and len(tool_calls) > 1:
            tool_results, edit_results, tool_log = self._execute_parallel(
                tool_calls, tool_ctx, callback,
            )
        else:
            tool_results, edit_results, tool_log = self._execute_sequential(
                tool_calls, tool_ctx, callback,
            )

        new_messages = new_messages + tool_results

        return state.with_updates(
            messages=new_messages,
            edit_results=list(state.edit_results) + edit_results,
            tool_calls_log=list(state.tool_calls_log) + tool_log,
        )

    def _execute_sequential(
        self,
        tool_calls: List[Dict[str, Any]],
        tool_ctx: ToolContext,
        callback: Optional[Callable] = None,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
        """顺序执行工具调用"""
        tool_results = []
        edit_results = []
        tool_log = []

        for tc in tool_calls:
            tc_id = tc.get('id', '')
            tc_name = tc.get('name', '')
            tc_args = tc.get('arguments', {})

            # ---- PreToolUse Hook ----
            pre_result = self.hooks.run_pre_tool(tc_name, tc_args, tool_ctx)
            if not pre_result.proceed:
                result = ToolResult(
                    content=f"Hook 阻止执行：工具 '{tc_name}' 被 PreToolUse Hook 拦截。",
                    success=False,
                )
            else:
                effective_args = pre_result.modified_args or tc_args

                # 权限检查
                perm = self.permissions.check(tc_name, effective_args, tool_ctx)
                if perm == PermissionDecision.DENY:
                    result = ToolResult(
                        content=f"权限拒绝：工具 '{tc_name}' 被禁止执行。",
                        success=False,
                    )
                else:
                    result = self.tools.execute_tool(tc_name, effective_args, tool_ctx)

            # ---- PostToolUse Hook ----
            post_result = self.hooks.run_post_tool(tc_name, tc_args, result, tool_ctx)
            if post_result.modified_result is not None:
                result = post_result.modified_result

            # Phase 3: 磁盘溢出
            if self.config.disk_overflow and self.disk_overflow:
                result = self._maybe_spill_result(result, tc_id, tc_name)

            # 记录编辑结果
            if tc_name in ('edit_file', 'edit_page'):
                edit_results.append({
                    'tool': tc_name,
                    'page': tc_args.get('page', tc_args.get('page_key', '')),
                    'applied': result.success,
                    'error': result.metadata.get('error') if not result.success else None,
                })

            tool_log.append({
                'tool': tc_name,
                'args_keys': list(tc_args.keys()),
                'success': result.success,
                'content_length': len(result.content),
                # 用于历史回放的关键字段
                'page': tc_args.get('page', tc_args.get('page_key', '')),
                'line_start': tc_args.get('start_line'),
                'line_end': tc_args.get('end_line'),
            })
            # 从工具返回的 metadata 补充信息
            if result.success and result.metadata:
                for key in ('page', 'line_start', 'line_end',
                            'total_lines', 'page_count'):
                    if key in result.metadata and not tool_log[-1].get(key):
                        tool_log[-1][key] = result.metadata[key]
                if 'old_text' in result.metadata:
                    tool_log[-1]['old_text'] = result.metadata['old_text']
                if 'new_text' in result.metadata:
                    tool_log[-1]['new_text'] = result.metadata['new_text']

            tool_msg = result.to_message(tc_id, tc_name)
            tool_results.append(tool_msg)

            completed_event = {
                'tool_call_id': tc_id,
                'tool_name': tc_name,
                'success': result.success,
                'content_length': len(result.content),
            }
            if result.success and result.metadata:
                for key in ('page', 'line_start', 'line_end',
                            'total_lines'):
                    if key in result.metadata:
                        completed_event[key] = result.metadata[key]
                if ('old_text' in result.metadata
                        or 'new_text' in result.metadata):
                    completed_event['old_text'] = (
                        result.metadata.get('old_text', ''))
                    completed_event['new_text'] = (
                        result.metadata.get('new_text', ''))
                if result.metadata.get('write_page'):
                    completed_event['write_page'] = True
            self._emit(callback, StreamEventType.TOOL_CALL_COMPLETED,
                       completed_event)

        return tool_results, edit_results, tool_log

    def _execute_parallel(
        self,
        tool_calls: List[Dict[str, Any]],
        tool_ctx: ToolContext,
        callback: Optional[Callable] = None,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
        """并行执行工具调用 (Phase 3)

        委托给 ParallelToolExecutor，然后对结果应用磁盘溢出。
        """
        # 懒初始化并行执行器
        if self._parallel_executor is None:
            self._parallel_executor = ParallelToolExecutor(
                tool_registry=self.tools,
                permission_manager=self.permissions,
                hook_manager=self.hooks,
                max_workers=self.config.parallel_max_workers,
            )

        tool_results, edit_results, tool_log = self._parallel_executor.execute(
            tool_calls, tool_ctx, callback,
        )

        # Phase 3: 对并行结果应用磁盘溢出
        if self.config.disk_overflow and self.disk_overflow:
            for i, tc in enumerate(tool_calls):
                tc_id = tc.get('id', '')
                tc_name = tc.get('name', '')
                msg = tool_results[i]
                content = msg.get('content', '')
                if isinstance(content, str) and len(content) > self.disk_overflow.config.max_in_memory_chars:
                    spilled_content = self.disk_overflow.maybe_spill(tc_name, content, tc_id)
                    tool_results[i] = {**msg, 'content': spilled_content}

        return tool_results, edit_results, tool_log

    def _maybe_spill_result(
        self, result: ToolResult, tc_id: str, tc_name: str,
    ) -> ToolResult:
        """对工具结果应用磁盘溢出"""
        if not self.disk_overflow:
            return result
        spilled_content = self.disk_overflow.maybe_spill(tc_name, result.content, tc_id)
        if spilled_content != result.content:
            return ToolResult(
                content=spilled_content,
                success=result.success,
                metadata={**result.metadata, 'disk_overflow': True},
            )
        return result

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _build_assistant_message(self, state: LoopState) -> Dict[str, Any]:
        """从当前状态构建 assistant 消息 (带 tool_calls)"""
        tool_calls = state.tool_calls_result or []
        content = state.accumulated_text.strip()

        if not tool_calls:
            return {'role': 'assistant', 'content': content or ''}

        # OpenAI 格式：content + tool_calls
        msg = {
            'role': 'assistant',
            'content': content or None,
            'tool_calls': [
                {
                    'id': tc.get('id', ''),
                    'type': 'function',
                    'function': {
                        'name': tc.get('name', ''),
                        'arguments': json.dumps(
                            tc.get('arguments', {}), ensure_ascii=False
                        ),
                    },
                }
                for tc in tool_calls
            ],
        }
        return msg

    def _build_result(
        self,
        reason: TerminalReason,
        state: LoopState,
        total_tool_calls: int,
        start_time: float,
    ) -> LoopResult:
        """构建最终结果"""
        elapsed = time.time() - start_time
        logger.info(
            f"[AgentLoop] 循环结束: {reason.value}, "
            f"轮次 {state.turn_count}, "
            f"工具调用 {total_tool_calls}, "
            f"耗时 {elapsed:.1f}s"
        )
        return LoopResult(
            terminal_reason=reason,
            state=state,
            final_text=state.accumulated_text,
            total_turns=state.turn_count,
            total_tool_calls=total_tool_calls,
            summary=f"{reason.value}, {state.turn_count} turns, {total_tool_calls} tool calls",
        )

    @staticmethod
    def _extract_html(text: str) -> str:
        """从 AI 文本中提取 HTML 内容"""
        # 尝试从 markdown 代码块提取
        html_match = re.search(
            r'```(?:html)?\s*\n(.*?)```', text, re.DOTALL
        )
        if html_match:
            return html_match.group(1).strip()

        # 尝试匹配完整的 HTML
        if '<!DOCTYPE' in text or '<html' in text:
            start = text.find('<!DOCTYPE') if '<!DOCTYPE' in text else text.find('<html')
            end = text.rfind('</html>')
            if end > start:
                return text[start:end + len('</html>')].strip()

        return ''

    @staticmethod
    def _emit(
        callback: Optional[Callable[[str, Dict], None]],
        event_type: StreamEventType,
        data: Dict[str, Any],
    ) -> None:
        """发送流式事件"""
        if callback:
            try:
                callback(event_type.value, data)
            except Exception as e:
                logger.debug(f"[AgentLoop] SSE 回调异常 (忽略): {e}")


# ---------------------------------------------------------------------------
# 便捷工厂函数
# ---------------------------------------------------------------------------


def create_inspector_loop(
    server: Any = None,
    session: Any = None,
    project_folder: str = '',
    project_id: str = '',
    system_prompt: str = '',
    enable_parallel: bool = False,
    enable_disk_overflow: bool = True,
) -> AgentLoop:
    """创建检查器微调场景的 Agent Loop

    对应 server.py:8145-9700 的 Agentic Loop
    """
    from agent_tools import create_inspector_registry

    config = AgentLoopConfig(
        max_turns=50,
        max_consecutive_empty=2,
        max_consecutive_reads=3,
        max_explore_reads=6,
        max_verify_retries=3,
        tool_names=['read_page', 'edit_file', 'list_pages'],
        system_prompt=system_prompt,
        parallel_execution=enable_parallel,
        disk_overflow=enable_disk_overflow,
        proactive_compaction=True,
    )

    loop = AgentLoop(
        config=config,
        tool_registry=create_inspector_registry(),
    )

    if enable_disk_overflow:
        loop.disk_overflow = DiskOverflowManager()

    return loop


def create_incremental_loop(
    server: Any = None,
    session: Any = None,
    project_folder: str = '',
    project_id: str = '',
    system_prompt: str = '',
    enable_parallel: bool = False,
    enable_disk_overflow: bool = True,
) -> AgentLoop:
    """创建增量页面生成场景的 Agent Loop

    对应 server_context_engineering.py:4801-5200 的 _run_agentic_pages
    """
    from agent_tools import create_incremental_registry

    config = AgentLoopConfig(
        max_turns=30,
        max_consecutive_empty=2,
        tool_names=['read_page', 'add_page', 'edit_page'],
        system_prompt=system_prompt,
        parallel_execution=enable_parallel,
        disk_overflow=enable_disk_overflow,
        proactive_compaction=True,
    )

    loop = AgentLoop(
        config=config,
        tool_registry=create_incremental_registry(),
    )

    if enable_disk_overflow:
        loop.disk_overflow = DiskOverflowManager()

    return loop


def create_review_loop(
    server: Any = None,
    session: Any = None,
    project_folder: str = '',
    project_id: str = '',
    system_prompt: str = '',
    enable_parallel: bool = False,
    enable_disk_overflow: bool = True,
) -> AgentLoop:
    """创建代码审查场景的 Agent Loop

    对应 server.py:6401-6900 的 _run_code_review
    """
    from agent_tools import create_review_registry

    config = AgentLoopConfig(
        max_turns=3,
        max_consecutive_empty=1,
        tool_names=['read_file', 'edit_file'],
        system_prompt=system_prompt,
        parallel_execution=enable_parallel,
        disk_overflow=enable_disk_overflow,
        proactive_compaction=True,
    )

    loop = AgentLoop(
        config=config,
        tool_registry=create_review_registry(),
    )

    if enable_disk_overflow:
        loop.disk_overflow = DiskOverflowManager()

    return loop
