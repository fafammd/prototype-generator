# -*- coding: utf-8 -*-
"""
上下文工程模块 — 多轮生成架构

将单次 AI 调用生成所有页面，改为三轮生成：
  Round 1: 生成 CSS 设计系统
  Round 2: 逐页独立生成（每页拥有完整 context window）
  Round 3: 组装为带 Vue 导航的完整 HTML

单页/简单项目自动走原有单次调用流程（零回归）。
"""

import json
import os
import re
import sys
import time
import threading
import logging
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger('prototype')

# ==================== UI 视觉质量增强指令 ====================
# 提炼自现代前端设计最佳实践，注入到各轮提示词中提升生成质量

UI_QUALITY_INSTRUCTIONS = """
## UI 视觉质量标准（必须达到）

### 1. 色彩运用层次
- 主色只用于关键操作按钮、激活态、重要标记，不要大面积使用
- 卡片、面板使用白色/近白背景 + 精致边框或柔和阴影，不要用纯色块堆砌
- 文字层次分明：标题 #1f1f1f、正文 #333、辅助说明 #666、禁用/提示 #999
- 状态色彩明确：成功 #52c41a、警告 #faad14、错误 #ff4d4f、信息 #1890ff
- 使用 rgba 透明度变体处理背景色（如主色 8% 透明度做背景、15% 做悬停）

### 2. 阴影与深度系统
- 卡片使用多层阴影：box-shadow: 0 1px 2px rgba(0,0,0,0.04), 0 4px 12px rgba(0,0,0,0.08)
- 悬停时阴影加深过渡：transition: box-shadow 0.3s ease
- 弹窗/模态框使用更强的阴影：0 8px 30px rgba(0,0,0,0.15)
- 避免扁平无阴影的设计，也避免过于浓重的阴影

### 3. 圆角与边框
- 卡片/面板：border-radius: 12px（大容器）或 8px（小组件）
- 按钮：border-radius: 6px
- 输入框：border-radius: 6px
- 标签/徽章：border-radius: 4px 或 999px（胶囊型）
- 边框使用浅色：border: 1px solid rgba(0,0,0,0.06) 或 #e8e8e8

### 4. 微交互与过渡动画
- 所有可点击元素必须有 hover 过渡（transition: all 0.2s ease）
- 按钮 hover：背景色加深/变浅 + 轻微上移（transform: translateY(-1px)）
- 按钮 active：轻微下压（transform: translateY(0) 或 scale(0.98)）
- 卡片 hover：阴影加深 + 轻微上浮（transform: translateY(-2px)）
- 输入框 focus：边框变主色 + 外发光（box-shadow: 0 0 0 3px rgba(主色, 0.15)）
- 模态框打开：fade + scale 动画（opacity 0→1, scale 0.95→1）
- 页面切换/内容加载：使用 fade-in 过渡，不要突然出现
- 列表项 hover：背景色微变（rgba(0,0,0,0.02)~rgba(0,0,0,0.04)）

### 5. 间距韵律
- 页面内边距：24px~32px
- 卡片内边距：20px~24px
- 卡片间距：16px~24px
- 表单项间距：16px~20px
- 按钮与相邻元素间距：8px~12px
- 标签文字与输入框间距：4px~8px
- 使用 4px 为基准的间距系统（4、8、12、16、20、24、32）

### 6. 排版质量
- 标题字重 600（semibold），正文字重 400（regular）
- 标题字号梯度：页面标题 20-24px、区块标题 16-18px、卡片标题 14-16px
- 正文 14px、辅助文字 12px
- 行高 1.5~1.7，段间距 8px~12px
- 表格表头字重 500、文字略小（13px）、颜色略浅

### 7. 组件精修标准
- **按钮**：内边距 8px 16px（中号）、圆角 6px、hover 变色、active 缩放、禁用半透明+不可点击
- **输入框**：高度 32-36px、边框 #d9d9d9、聚焦变主色+发光、placeholder 用 #bfbfbf
- **表格**：表头背景 #fafafa、行高 54px、斑马纹 (#fafafa)、hover 行高亮、底部边框
- **卡片**：白色背景、圆角 8-12px、柔和阴影、内边距 20px
- **标签页**：底部边框指示器（2-3px 主色）、未选中 #666、选中 #333 加粗
- **标签/徽章**：小圆角、小字号（12px）、彩色背景+白色文字 或 浅色背景+深色文字
- **下拉菜单**：白色背景、阴影、hover 高亮、分割线
- **空状态**：居中插图（用 FontAwesome 大图标代替）、灰色说明文字、操作按钮

### 8. 页面整体质感
- 页面背景不要纯白，使用 #f5f7fa 或 #f0f2f5 浅灰
- 卡片与背景形成对比（白卡片在灰底上）
- 顶部区域可以有微妙的渐变或主题色装饰条
- 数据密集页面注意留白，不要塞满
- 使用 FontAwesome 图标增强信息表达，不要纯文字堆砌
"""

UI_QUALITY_FOR_DESIGN_SYSTEM = """
## 设计系统生成质量要求

生成 CSS 变量时，请确保：

### 色彩系统完整度
- 除基础色外，必须包含每种语义色的浅色变体（用于背景、hover 态）：
  - --color-primary-light: 主色的 20%~30% 亮度变体
  - --color-primary-bg: 主色的 6%~10% 透明度背景色
  - --color-success-bg, --color-warning-bg, --color-danger-bg: 语义色背景
- 文字色至少 4 级层次：primary(#1f1f1f) → secondary(#595959) → muted(#8c8c8c) → disabled(#bfbfbf)

### 阴影系统层次
- shadow-sm: 用于小按钮、输入框（1px 偏移，低透明度）
- shadow-md: 用于卡片、面板（多层阴影，柔和过渡）
- shadow-lg: 用于弹窗、下拉菜单（大偏移，中透明度）
- 每级阴影都应是复合阴影（2~3 层叠加），不要单层阴影

### 圆角系统
- radius-sm: 4px（标签、徽章）
- radius-md: 8px（按钮、输入框）
- radius-lg: 12px（卡片、面板、模态框）
- radius-xl: 16px（大型容器）

### 间距系统
- 基于 4px 网格：xs=4, sm=8, md=12, lg=16, xl=24, 2xl=32

### 组件规格要求
描述组件时请包含：
- 尺寸（高度、内边距）
- 颜色（背景、文字、边框）
- 状态变化（hover、active、focus、disabled、selected）
- 过渡动画（transition 属性值）
- 与其他组件的间距规范
"""


def _get_server_module():
    """获取真正运行中的 server 模块（处理 __main__ 启动场景）

    当通过 `python server.py` 启动时，主模块名是 __main__ 而非 server。
    直接 `import server` 会创建新的模块实例，导致 generating_tasks 不一致。
    """
    # 优先从 sys.modules 获取已加载的 server 模块
    if 'server' in sys.modules:
        srv = sys.modules['server']
        # 检查是否包含必要属性（确认不是空壳模块）
        if hasattr(srv, 'generating_tasks') and hasattr(srv, 'tasks_lock'):
            return srv
    # 回退到 __main__（直接运行 server.py 的场景）
    if '__main__' in sys.modules:
        main_mod = sys.modules['__main__']
        if hasattr(main_mod, 'generating_tasks') and hasattr(main_mod, 'tasks_lock'):
            return main_mod
    # 最终回退：直接 import（可能拿到不同实例，但至少不会崩溃）
    import server as srv
    return srv

# ==================== Phase 0: 跨页规格生成 (Round 0) ====================

def should_skip_spec_round(pages_data, config=None):
    """判断是否跳过 Round 0（跨页规格生成）

    跳过条件（任一满足即跳过）：
    - 只有 1 页
    - 2 页且描述合计 < 500 字符
    - 配置中指定跳过
    """
    config = config or {}
    if config.get('skip_spec_round', False):
        return True
    if len(pages_data) <= 1:
        return True
    if len(pages_data) == 2:
        total_desc = sum(len(p.get('description', '')) for p in pages_data)
        if total_desc < 500:
            return True
    return False


def build_spec_prompt(pages_data, global_config, layout_type='plain',
                      template_design_tokens='', template_html_summary='',
                      template_frame_html='', template_sidebar_meta=None,
                      adjustment_note='', previous_spec=None):
    """构建 Round 0 跨页规格 prompt

    产出结构化 JSON spec，约束后续各页生成的一致性。
    """
    primary = global_config.get('primaryColor', '#004fff')
    secondary = global_config.get('secondaryColor', '#10b981')

    # 格式化页面列表
    pages_desc = []
    for i, p in enumerate(pages_data):
        desc = f"  {i+1}. **{p.get('name', f'页面{i+1}')}**"
        if p.get('description'):
            desc += f": {p['description']}"
        if p.get('features'):
            desc += f"\n     组件: {p['features']}"
        if p.get('dataStructure'):
            desc += f"\n     数据: {p['dataStructure']}"
        if p.get('interaction'):
            desc += f"\n     交互: {p['interaction']}"
        pages_desc.append(desc)
    pages_str = '\n'.join(pages_desc)

    # 布局类型说明
    layout_desc = {
        'sidebar': '侧边栏布局（sidebar + 顶栏 + 内容区），各页共享侧边栏',
        'iframe': 'iframe 嵌套布局，各页在 iframe 内独立渲染',
        'plain': '普通布局，各页用 Vue 标签页切换'
    }.get(layout_type, '普通布局')

    prompt = f"""你是 UI/UX 架构师。分析以下多页面原型需求，输出结构化规格。

## 全局设计规范
- 主色: {primary}
- 强调色: {secondary}
- 背景: {'浅色' if global_config.get('backgroundMode', 'light') == 'light' else '深色'}
- 组件风格: {global_config.get('componentStyle', 'Ant Design')}

## 页面列表（{len(pages_data)} 页）
{pages_str}

## 布局类型
{layout_desc}
"""

    # 模板上下文
    if template_design_tokens:
        prompt += f"\n## 模板设计令牌\n{template_design_tokens}\n"
    if template_html_summary:
        import re as _re
        _cleaned = _re.sub(
            r'<img([^>]*?)\s+src\s*=\s*["\']data:image/[^"\']+["\']',
            r'<img\1 src="<!-- base64 -->"',
            template_html_summary, flags=_re.IGNORECASE
        )
        prompt += f"\n## 模板 HTML 结构摘要\n```html\n{_cleaned[:4000]}\n```\n"
    if template_sidebar_meta:
        prompt += f"\n## 侧边栏元数据\n{json.dumps(template_sidebar_meta, ensure_ascii=False, indent=2)}\n"

    # 上一次生成的 spec（用于重新分析时提供上下文）
    if previous_spec:
        prompt += f"\n## 上一次生成的规格（请基于用户反馈修正此规格）\n```json\n{json.dumps(previous_spec, ensure_ascii=False, indent=2)}\n```\n"

    # 用户反馈调整要求
    if adjustment_note:
        prompt += f"\n## 用户反馈调整要求（请基于此反馈修正上面的规格）\n{adjustment_note}\n"

    prompt += """
## 输出要求

输出严格的 JSON（不要输出其他内容），格式如下：

```json
{
  "shared_data_models": [
    {"name": "ModelName", "fields": [
      {"name": "fieldName", "type": "string|number|enum", "sample": "示例值", "values": ["opt1","opt2"]}
    ]}
  ],
  "navigation": {
    "pages": [
      {"id": "page_id", "name": "页面名称", "is_entry": true, "show_in_nav": true}
    ],
    "default_page": "page_id",
    "links": [
      {"from": "page_id_1", "to": "page_id_2", "trigger": "触发动作描述"}
    ]
  },
  "shared_components": [
    {"name": "ComponentName", "variants": ["variant1"], "usage": "使用场景"}
  ],
  "pages": [
    {
      "id": "page_id",
      "name": "页面名称",
      "show_in_nav": true,
      "data_sources": ["ModelName"],
      "components_needed": ["ComponentName"],
      "cross_references": {
        "navigates_to": ["other_page_id"],
        "must_match_style_of": ["other_page_id"]
      }
    }
  ]
}
```

关键约束：
1. 所有页面使用相同的数据模型（字段名、枚举值必须一致）
2. 导航规则必须完整（从哪页到哪页、触发方式）
3. 共享组件确保视觉一致性
4. page_id 使用英文下划线格式
5. shared_components 中必须包含 "breadcrumb"（面包屑导航），所有使用面包屑的页面必须引用此共享组件，确保样式统一
6. **页面导航可见性（show_in_nav）**：
   - `show_in_nav: true` — 该页面显示在侧边栏/主导航菜单中，作为一级入口
   - `show_in_nav: false` — 该页面是子页面/详情页/弹窗页，**不显示在侧边栏菜单中**，只能从其他页面通过按钮/链接跳转到达
   - 判断标准：如果用户描述中明确说"从 XX 页面点击跳转"、"不需要单独菜单"、"作为详情页/子页面"等，则 `show_in_nav: false`
   - 如果用户没有明确说明，默认 `show_in_nav: true`
   - navigation.pages 和 pages 中都要包含 show_in_nav 字段，且必须一致
"""
    return prompt


def extract_spec_from_response(ai_response):
    """从 Round 0 AI 响应中提取结构化 spec JSON"""
    # 尝试 ```json 代码块
    json_match = re.search(r'```(?:json)?\s*\n([\s\S]*?)```', ai_response)
    if json_match:
        candidate = json_match.group(1).strip()
        try:
            parsed = json.loads(candidate)
            if 'pages' in parsed or 'navigation' in parsed:
                return parsed
        except json.JSONDecodeError:
            pass

    # 回退：找最外层 { }
    brace_start = ai_response.find('{')
    brace_end = ai_response.rfind('}')
    if brace_start != -1 and brace_end > brace_start:
        candidate = ai_response[brace_start:brace_end + 1]
        try:
            parsed = json.loads(candidate)
            if 'pages' in parsed or 'navigation' in parsed:
                return parsed
        except json.JSONDecodeError:
            pass

    logger.warning("[Round 0] 无法从 AI 响应中提取 spec JSON")
    return {}


def build_spec_summary_for_page(spec, page_index):
    """为某页生成精简 spec 摘要（~500 字符）

    GA L1 Insight Index 思路：只注入当前页必须知道的约束，
    其他信息让 AI 从对话历史中自行获取。
    """
    if not spec:
        return ''

    pages = spec.get('pages', [])
    current = pages[page_index] if page_index < len(pages) else None
    if not current:
        return ''

    lines = ["## 跨页约束"]
    page_name = current.get('name', f'页面{page_index+1}')
    page_id = current.get('id', '')

    # 1. 当前页必须实现的导航（唯一不可省略的行动项）
    nav = spec.get('navigation', {})
    links = nav.get('links', [])
    outbound = [l for l in links if l.get('from') == page_id]
    if outbound:
        lines.append(f"### 必须实现的跳转")
        for link in outbound:
            target_id = link.get('to', '?')
            target_idx = next((i for i, p in enumerate(pages) if p.get('id') == target_id), -1)
            target_name = next((p.get('name', target_id) for p in pages if p.get('id') == target_id), target_id)
            trigger = link.get('trigger', '跳转')
            nav_method = f"navigateTo('page_{target_idx}')" if target_idx >= 0 else f"跳转到「{target_name}」"
            lines.append(f"- {trigger} → {nav_method}")

    # 2. 当前页的数据模型（精简：只列字段名和类型）
    data_sources = current.get('data_sources', [])
    models = spec.get('shared_data_models', [])
    relevant = [m for m in models if m.get('name') in data_sources]
    if relevant:
        for m in relevant:
            fields = ', '.join(f"{f['name']}({f.get('type','string')})" for f in m.get('fields', []))
            lines.append(f"- 数据: {m['name']} [{fields}]")

    # 3. 共享组件（一行）
    components = spec.get('shared_components', [])
    if components:
        comp_names = ', '.join(c['name'] for c in components)
        lines.append(f"- 共享组件: {comp_names}")

    # 4. 当前页的导航可见性
    show_in_nav = current.get('show_in_nav', True)
    if not show_in_nav:
        lines.append(f"- **此页面不显示在侧边栏导航中**，只能从其他页面跳转到达")

    # 5. 列出不在导航中但存在的其他页面（子页面/详情页）
    non_nav_pages = [p for p in pages if not p.get('show_in_nav', True)]
    if non_nav_pages and show_in_nav:
        non_nav_names = ', '.join(p.get('name', p.get('id', '?')) for p in non_nav_pages)
        lines.append(f"- 子页面（不在侧边栏中）: {non_nav_names}")

    result = '\n'.join(lines)
    if len(result) > 800:
        result = result[:800] + "\n..."
    return result


def get_nav_visible_indices(cross_page_spec, total_pages):
    """从跨页规格中提取应显示在侧边栏导航中的页面索引列表

    Args:
        cross_page_spec: Round 0 生成的跨页规格 dict
        total_pages: 总页面数

    Returns:
        list[int]: 应出现在侧边栏菜单中的页面索引（0-based）
    """
    if not cross_page_spec:
        return list(range(total_pages))

    spec_pages = cross_page_spec.get('pages', [])
    indices = []
    for i in range(min(total_pages, len(spec_pages))):
        if spec_pages[i].get('show_in_nav', True):
            indices.append(i)

    # 如果过滤后没有任何页面，回退到全部显示
    if not indices:
        return list(range(total_pages))

    # 安全网：如果 AI 错误地将大部分页面标记为不可见（只剩1个可见但实际有多页），
    # 回退到全部显示，避免侧边栏只有一个菜单项
    if total_pages > 1 and len(indices) == 1:
        logger.warning(
            f"[导航] cross_page_spec 只标记了 1/{total_pages} 个页面为 show_in_nav=True，"
            f"可能是 AI 规格错误，回退为全部显示"
        )
        return list(range(total_pages))

    return indices
    """估算 messages 数组的总 token 数"""
    total = 0
    for msg in messages:
        content = msg.get('content', '')
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    if block.get('type') == 'text':
                        total += estimate_tokens(block.get('text', ''))
                    elif block.get('type') == 'image_url':
                        total += 1000
    return total


# ==================== Phase 1: Token 估算与策略选择 ====================

def estimate_tokens(text):
    """基于 CJK/ASCII 字符数的 token 估算（保守偏高）

    经验公式：
    - CJK 字符 ≈ 2 tokens/字
    - ASCII 字符 ≈ 0.25 tokens/字（约 4 字符/token）
    - 图片 ≈ 1000 tokens/张（保守估算）
    """
    if not text:
        return 0
    cjk_count = 0
    ascii_count = 0
    for ch in text:
        if '\u4e00' <= ch <= '\u9fff' or '\u3000' <= ch <= '\u303f':
            cjk_count += 1
        else:
            ascii_count += 1
    return int(cjk_count * 2 + ascii_count * 0.25)


def estimate_messages_tokens(messages):
    """估算消息列表的总 token 数"""
    if not messages:
        return 0
    total = 0
    for msg in messages:
        if isinstance(msg, dict):
            role = msg.get('role', '')
            content = msg.get('content', '')
            if isinstance(content, list):
                # 多模态消息（含图片）
                for part in content:
                    if isinstance(part, dict):
                        if part.get('type') == 'text':
                            total += estimate_tokens(part.get('text', ''))
                        elif part.get('type') == 'image_url':
                            total += 1000  # 图片估算
            elif isinstance(content, str):
                total += estimate_tokens(content)
            # role 本身也有少量 token
            total += estimate_tokens(role)
    return total


def estimate_prompt_tokens(prompt, image_count=0):
    """估算完整 prompt 的 token 数（文本 + 图片）"""
    text_tokens = estimate_tokens(prompt)
    image_tokens = image_count * 1000
    return text_tokens + image_tokens


def is_framework_page(page_spec):
    """判断是否为框架页面（应内嵌到 index.html，而非放入 pages/）

    框架页面特征：登录、注册、404 等非业务功能页面。
    这些页面直接嵌入 index.html，通过 Vue v-show 切换，
    不在 iframe 中加载。

    只匹配页面名称（name），不匹配描述（description），
    因为描述中容易误匹配（如"数据源注册"包含"注册"）。

    Args:
        page_spec: 页面规格 dict {name, description, ...}

    Returns:
        bool: True 表示框架页面，False 表示业务页面
    """
    if not page_spec:
        return False

    name = page_spec.get('name', '')
    name_lower = name.lower()

    framework_keywords = [
        '登录', 'login', 'signin', 'sign in',
        '注册', 'register', 'signup', 'sign up',
        '404', 'not found', '错误页',
        '认证', 'auth', 'authentication',
        '忘记密码', 'forgot password', 'reset password',
    ]

    for keyword in framework_keywords:
        if keyword in name_lower:
            return True

    return False


def determine_strategy(prompt, page_count, image_count, config=None):
    """根据 token 估算和页面数决定生成策略

    Args:
        prompt: 完整的 prompt 文本
        page_count: 页面数量
        image_count: 参考图片数量
        config: context_engineering 配置节

    Returns:
        dict: {strategy: 'single'|'multi_round', reason, estimated_tokens}
    """
    config = config or {}
    force_strategy = config.get('force_strategy', 'auto')

    estimated = estimate_prompt_tokens(prompt, image_count)

    # 用户强制指定
    if force_strategy == 'single':
        return {'strategy': 'single', 'reason': '用户指定单次调用', 'estimated_tokens': estimated}
    if force_strategy == 'multi_round':
        return {'strategy': 'multi_round', 'reason': '用户指定多轮生成', 'estimated_tokens': estimated}

    # 自动策略
    single_threshold = config.get('single_page_token_threshold', 40000)
    multi_threshold = config.get('multi_page_token_threshold', 80000)

    if page_count <= 1:
        if estimated >= single_threshold:
            return {'strategy': 'multi_round', 'reason': f'单页但需求复杂 ({estimated} tokens)', 'estimated_tokens': estimated}
        return {'strategy': 'single', 'reason': f'单页简单需求 ({estimated} tokens)', 'estimated_tokens': estimated}

    if page_count == 2:
        if estimated >= multi_threshold:
            return {'strategy': 'multi_round', 'reason': f'2 页但需求复杂 ({estimated} tokens)', 'estimated_tokens': estimated}
        return {'strategy': 'single', 'reason': f'2 页简单需求 ({estimated} tokens)', 'estimated_tokens': estimated}

    # 3 页及以上，一律多轮
    return {'strategy': 'multi_round', 'reason': f'{page_count} 页 ({estimated} tokens)', 'estimated_tokens': estimated}


# ==================== Phase 2: 设计系统生成 (Round 1) ====================

def should_skip_design_system_round(template_tokens, global_config):
    """判断是否可以跳过 Round 1（模板令牌充足时）

    如果模板已提取出 3+ 个关键属性，直接将令牌转为 CSS 变量，省掉一次 AI 调用。
    """
    if not template_tokens:
        return False
    key_count = 0
    for keyword in ['主色调', '背景色', '文字色', '字体', '字号', '页面背景色', '正文文字色']:
        if keyword in template_tokens:
            key_count += 1
    return key_count >= 3


def build_design_system_prompt(global_config, template_tokens=None, template_html_summary=None):
    """构建 Round 1 设计系统 prompt"""
    primary = global_config.get('primaryColor', '#004fff')
    secondary = global_config.get('secondaryColor', '#10b981')
    bg_mode = global_config.get('backgroundMode', 'light')
    component_style = global_config.get('componentStyle', 'Ant Design')

    prompt = f"""你是一个资深的 UI 设计系统专家，精通 Ant Design / Element Plus 等成熟设计系统的视觉规范。
请根据以下需求，生成一套精致、完整的 CSS 设计系统。设计系统必须达到专业 UI 框架的视觉水准。

## 全局设计规范
- 主色: {primary}
- 强调色: {secondary}
- 背景: {'浅色' if bg_mode == 'light' else '深色'}
- 组件风格: {component_style}
"""
    if template_tokens:
        prompt += f"""
## 模板设计令牌（必须严格遵循）
{template_tokens}
"""

    prompt += """
""" + UI_QUALITY_FOR_DESIGN_SYSTEM + """
## 输出要求

生成以下两部分：

### 1. CSS 变量（放在 ```css 代码块中）
```css
:root {
  /* 颜色系统 — 必须包含浅色变体和背景色变体 */
  --color-primary: ...;
  --color-primary-light: ...;       /* 主色 20%~30% 亮度变体 */
  --color-primary-dark: ...;
  --color-primary-bg: ...;          /* 主色 6%~10% 透明度背景 */
  --color-secondary: ...;
  --color-bg-page: ...;             /* 页面底色，不要纯白 */
  --color-bg-card: ...;
  --color-bg-sidebar: ...;
  --color-text-primary: ...;
  --color-text-secondary: ...;
  --color-text-muted: ...;
  --color-text-disabled: ...;
  --color-border: ...;
  --color-border-light: ...;        /* 更浅的边框色，用于分割线 */
  --color-success: ...;
  --color-success-bg: ...;          /* 成功色背景 */
  --color-warning: ...;
  --color-warning-bg: ...;
  --color-danger: ...;
  --color-danger-bg: ...;

  /* 字体系统 */
  --font-family: ...;
  --font-size-xs: 12px;
  --font-size-sm: 14px;
  --font-size-base: 14px 或 16px;
  --font-size-lg: 16px 或 18px;
  --font-size-xl: 20px 或 24px;
  --font-size-2xl: 24px 或 30px;

  /* 间距系统 — 基于 4px 网格 */
  --spacing-xs: 4px;
  --spacing-sm: 8px;
  --spacing-md: 12px;
  --spacing-lg: 16px;
  --spacing-xl: 24px;
  --spacing-2xl: 32px;

  /* 圆角系统 */
  --radius-sm: 4px;
  --radius-md: 8px;
  --radius-lg: 12px;
  --radius-xl: 16px;

  /* 阴影系统 — 必须使用多层复合阴影 */
  --shadow-sm: ...;                 /* 小偏移，低透明度，2 层 */
  --shadow-md: ...;                 /* 中偏移，中透明度，2-3 层 */
  --shadow-lg: ...;                 /* 大偏移，高透明度，2-3 层 */

  /* 过渡动画 */
  --transition-fast: 0.15s ease;
  --transition-normal: 0.25s ease;
  --transition-slow: 0.35s ease;
}
```

### 2. 组件规格描述
详细描述以下组件的视觉规格（包括尺寸、颜色、状态变化、过渡动画）：
- **按钮**：主按钮、次按钮、文字按钮 — 各状态的背景色、圆角、内边距、hover/active/disabled 效果
- **输入框**：边框色、高度、聚焦效果（边框+发光）、placeholder 颜色
- **表格**：表头样式、行高、斑马纹色、hover 行高亮色、底部边框
- **卡片**：背景色、圆角、阴影、内边距、hover 效果
- **导航**：高度、激活项样式（背景/文字/左边框）、hover 过渡
- **标签页**：底部指示器、选中/未选中样式
- **标签/徽章**：圆角、字号、彩色背景 vs 浅色背景方案
- **模态框**：遮罩透明度、动画、圆角、阴影
"""
    return prompt


def tokens_to_css_variables(template_tokens, global_config):
    """将模板设计令牌直接转为 CSS 变量（跳过 AI 调用时使用）"""
    tokens = {}
    for line in template_tokens.split('\n'):
        if ':' in line:
            key, _, val = line.partition(':')
            tokens[key.strip()] = val.strip().rstrip(',')

    primary = global_config.get('primaryColor', '#004fff')
    secondary = global_config.get('secondaryColor', '#10b981')

    # 从令牌中提取颜色值
    def find_token(*keys):
        for k in keys:
            if k in tokens:
                return tokens[k]
        return ''

    return f""":root {{
  --color-primary: {find_token('主色调') or primary};
  --color-primary-light: {find_token('主色调') or primary}20;
  --color-primary-dark: {find_token('主色调') or primary};
  --color-secondary: {secondary};
  --color-bg-page: {find_token('页面背景色', '背景色') or '#f5f7fa'};
  --color-bg-card: {find_token('内容区背景色') or '#ffffff'};
  --color-bg-sidebar: {find_token('侧边栏背景色') or '#304156'};
  --color-text-primary: {find_token('正文文字色', '文字色') or '#303133'};
  --color-text-secondary: #606266;
  --color-text-muted: #909399;
  --color-border: #dcdfe6;
  --color-success: #67c23a;
  --color-warning: #e6a23c;
  --color-danger: #f56c6c;
  --font-family: {find_token('字体') or 'system-ui, -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif'};
  --font-size-xs: 12px;
  --font-size-sm: 13px;
  --font-size-base: {find_token('基础字号') or '14px'};
  --font-size-lg: 16px;
  --font-size-xl: 18px;
  --font-size-2xl: 20px;
  --spacing-xs: 4px;
  --spacing-sm: 8px;
  --spacing-md: 12px;
  --spacing-lg: 16px;
  --spacing-xl: 24px;
  --radius-sm: 4px;
  --radius-md: {find_token('圆角') or '8px'};
  --radius-lg: 12px;
  --shadow-sm: 0 1px 3px rgba(0,0,0,0.08);
  --shadow-md: {find_token('阴影') or '0 2px 12px rgba(0,0,0,0.1)'};
  --shadow-lg: 0 4px 20px rgba(0,0,0,0.12);
}}"""


def extract_design_system_from_response(ai_response):
    """从 Round 1 AI 响应中提取设计系统

    Returns:
        dict: {css_variables: str, component_specs: str}
    """
    css_variables = ''
    component_specs = ''

    # 提取 CSS 代码块
    css_match = re.search(r'```(?:css|CSS)?\s*\n([\s\S]*?)```', ai_response)
    if css_match:
        candidate = css_match.group(1).strip()
        if ':root' in candidate or '--' in candidate:
            css_variables = candidate

    if not css_variables:
        # 尝试找 :root 块
        root_match = re.search(r':root\s*\{([\s\S]*?)\}', ai_response)
        if root_match:
            css_variables = ':root {\n' + root_match.group(1).strip() + '\n}'

    # 提取组件规格（CSS 块之后的内容）
    if css_match:
        after_css = ai_response[css_match.end():]
    else:
        after_css = ai_response

    specs_lines = []
    for line in after_css.split('\n'):
        stripped = line.strip()
        if stripped and not stripped.startswith('```') and not stripped.startswith('#'):
            specs_lines.append(stripped)
    component_specs = '\n'.join(specs_lines[:2000])  # 限制长度

    return {
        'css_variables': css_variables,
        'component_specs': component_specs
    }


# ==================== Phase 3: 逐页生成 (Round 2) ====================

def build_single_page_prompt(page_spec, design_system, page_index, total_pages,
                             global_config=None, template_css_path=None,
                             is_iframe_layout=False,
                             template_design_tokens='', template_html_summary='',
                             template_frame_html='', template_layout_type='plain',
                             cross_page_spec_summary='',
                             template_style_card='',
                             include_full_template=True,
                             standalone=False,
                             iframe_context=False):
    """构建 Round 2 单页生成 prompt

    Args:
        ...（同原参数）
        standalone: True 时生成完整独立 HTML 页面（Route B 多文件架构），
                   不需要系统组装，页面可直接在浏览器中打开
        iframe_context: True 时表示该页面将在 iframe 中加载，父页面已有侧边栏/导航，
                       不应再生成侧边栏或导航栏
    """
    primary = global_config.get('primaryColor', '#004fff') if global_config else '#004fff'
    secondary = global_config.get('secondaryColor', '#10b981') if global_config else '#10b981'
    bg_mode = 'light' if (global_config or {}).get('backgroundMode', 'light') == 'light' else 'dark'
    component_style = global_config.get('componentStyle', 'Ant Design') if global_config else 'Ant Design'

    prompt = f"""严格按照以下格式输出，不要在标记之外输出任何内容：
<artifact type="html" title="{page_spec.get('name', f'页面{page_index + 1}')}">
（完整 HTML 代码）
</artifact>

关键要求：
1. 必须用 <artifact> 标签包裹 HTML 输出
2. 标签内直接写 HTML 代码，不要用 ```html``` 包裹
3. 不要在 <artifact> 标签之外输出任何文字（不要解释、不要总结、不要问候）

## 设计系统 CSS 变量（必须使用这些变量保持风格一致）
```css
{design_system.get('css_variables', '')}
```

## 组件规格参考
{design_system.get('component_specs', '')}

## 全局设计规范
- 主色: {primary}
- 强调色: {secondary}
- 背景: {'浅色' if bg_mode == 'light' else '深色'}
- 组件风格: {component_style}

""" + UI_QUALITY_INSTRUCTIONS + f"""

## 页面需求：{page_spec.get('name', f'页面{page_index + 1}')}
"""
    if page_spec.get('description'):
        prompt += f"**用途**: {page_spec['description']}\n\n"
    if page_spec.get('layout'):
        prompt += f"**布局结构**:\n{page_spec['layout']}\n\n"
    if page_spec.get('features'):
        prompt += f"**UI 组件**:\n{page_spec['features']}\n\n"
    if page_spec.get('dataStructure'):
        prompt += f"**数据字段**（请生成真实示例数据）:\n{page_spec['dataStructure']}\n\n"
    if page_spec.get('interaction'):
        prompt += f"**交互行为**:\n{page_spec['interaction']}\n\n"
    if page_spec.get('userFlow'):
        prompt += f"**用户操作流程**:\n{page_spec['userFlow']}\n\n"

    # 跨页规格上下文
    if cross_page_spec_summary:
        prompt += f"{cross_page_spec_summary}\n"

    if template_css_path:
        prompt += f"\n## 模板 CSS 引用\n在 HTML <head> 中添加: <link rel=\"stylesheet\" href=\"{template_css_path}\">\n"

    # ===== 注入模板视觉规范 =====
    import re as _re
    has_template = template_design_tokens or template_html_summary or template_frame_html
    if has_template:
        if include_full_template:
            # ===== 首页：完整模板注入 =====
            prompt += "\n## 现有系统模板（必须严格遵循此模板的视觉风格！）\n"
            prompt += "**绝对不能偏离模板的配色、字体、组件风格！**\n\n"

            if template_design_tokens:
                prompt += f"### 模板设计规范\n{template_design_tokens}\n\n"

            if template_html_summary:
                # 清理 base64 图片
                _cleaned_summary = _re.sub(
                    r'<img([^>]*?)\s+src\s*=\s*["\']data:image/[^"\']+["\']',
                    r'<img\1 src="<!-- base64_image -->"',
                    template_html_summary,
                    flags=_re.IGNORECASE
                )
                prompt += f"### 模板页面结构（使用相同的布局模式和 CSS class）\n```html\n{_cleaned_summary[:12000]}\n```\n\n"
        else:
            # ===== 后续页面：精简样式参考卡 =====
            if template_style_card:
                prompt += f"\n## 模板样式参考（与第一页使用的模板一致）\n"
                prompt += f"{template_style_card}\n\n"
                prompt += "**严格遵循上述配色、组件风格和 CSS class 命名。**\n"
            elif template_design_tokens:
                # 兜底：没有样式卡时，只注入 design_tokens
                prompt += f"\n## 模板设计规范\n{template_design_tokens}\n\n"
                prompt += "**严格遵循上述配色和组件风格。**\n"

        if include_full_template and is_iframe_layout and template_frame_html:
            is_sidebar_mode = (template_layout_type == 'sidebar')
            if is_sidebar_mode:
                prompt += "### 模板注入说明\n"
                prompt += "此模板采用「侧边栏 + 顶栏 + 内容区」布局。系统已保留完整的模板 HTML（含侧边栏、顶栏、CSS），你**只需生成主内容区域的 HTML 片段**。\n"
                prompt += "**关键要求**：\n"
                prompt += "- **不要**生成完整的 HTML 页面（不要 <!DOCTYPE html>、<html>、<head>、<body>）\n"
                prompt += "- **不要**生成侧边栏、顶栏、导航栏（模板已有，系统会自动处理）\n"
                prompt += "- 只生成主内容区域的 HTML 代码片段（即 `<div class=pageContent>` 内部的内容）\n"
                prompt += "- 使用模板已有的 CSS class，模板的 CSS 已全部内联（参考模板 HTML 中的 class 命名）\n"
                prompt += "- 如需额外样式，用 `<style>` 标签包裹\n"
                prompt += "- 可以使用 Vue 3 (CDN) 实现交互（搜索、过滤、弹窗等），用 `<script>` 标签包裹\n"
                prompt += "- **不要**复制模板原始页面的特有数据字段（如「数据周期」「指标波动」等），按用户需求生成全新的内容\n\n"
                prompt += "**修改侧边栏**（可选）：\n"
                prompt += "- 如果需要在侧边栏添加新菜单项，在输出末尾加 `<!-- SIDEBAR_ADD: 菜单名称 -->`\n"
                prompt += "- 如果需要设置某个菜单项为激活状态，在输出末尾加 `<!-- SIDEBAR_ACTIVE: 菜单名称 -->`\n\n"
            else:
                prompt += "### 框架说明\n此模板采用「侧边栏+顶栏+iframe 内容区」布局。不要生成侧边栏/顶栏，只生成 iframe 内容。\n\n"

            # 清理 base64 图片以节省 prompt token
            _cleaned_frame = _re.sub(
                r'<img([^>]*?)\s+src\s*=\s*["\']data:image/[^"\']+["\']',
                r'<img\1 src="<!-- base64_image -->"',
                template_frame_html,
                flags=_re.IGNORECASE
            )
            _cleaned_frame = _re.sub(
                r'url\(data:image/[^)]+\)',
                'url(<!-- base64_image -->)',
                _cleaned_frame,
                flags=_re.IGNORECASE
            )
            if is_sidebar_mode:
                prompt += f"### 模板内容区现有结构（参考其组件样式和 class 命名）\n```html\n{_cleaned_frame[:15000]}\n```\n\n"
            else:
                prompt += f"### 模板 HTML 结构（参考其组件样式和 class 命名，用于指导你使用 Ant Design 组件）\n```html\n{_cleaned_frame[:15000]}\n```\n\n"

        # sidebar 模式输出要求（无论是否首页都需要）
        is_sidebar_mode = (template_layout_type == 'sidebar')
        if is_sidebar_mode and not standalone:
            # ===== sidebar + 单文件模式：只生成内容片段 =====
            # ===== sidebar 模式输出要求：只生成内容片段 =====
            prompt += f"""
### 模板还原要求（严格遵守）
1. 配色方案**必须**与模板设计规范一致，不要自创配色
2. 组件样式（按钮、表格、表单、卡片、下拉框、输入框）必须与模板一致
3. 如果模板使用了 Ant Design，你也必须使用 Ant Design 风格
4. 视觉风格绝对不能偏离

## 输出要求
生成**主内容区域的 HTML 片段**（不是完整页面），包含：
1. 页面内容的 HTML（使用模板的 Ant Design CSS class）
2. `<style>` 标签中仅写页面特有的自定义样式
3. `<script>` 标签中使用 Vue 3 (CDN) 实现页面内交互（搜索、过滤、弹窗等）
4. 真实中文数据，不要用 Lorem ipsum
5. 这是第 {page_index + 1}/{total_pages} 页，只需生成这一个页面

**关键样式要求**：
- **必须**使用模板的配色、字体、组件风格
- 按钮用 `ant-btn` 系列 class，表格用 `ant-table` 系列，表单用 `ant-form` 系列

输出格式：
<artifact type="html" title="{page_spec.get('name', f'页面{page_index + 1}')}">
<style>/* 页面特有样式 */</style>
<div>
    <!-- 页面内容 -->
</div>
<script src="https://unpkg.com/vue@3/dist/vue.global.prod.js"></script>
<script>
// Vue 3 应用
</script>
</artifact>
"""
        else:
            prompt += """### 模板还原要求（严格遵守）
1. 配色方案**必须**与模板设计规范一致，不要自创配色
2. 组件样式（按钮、表格、表单、卡片、下拉框、输入框）必须与模板一致
3. 如果模板使用了 Ant Design，你也必须使用 Ant Design 风格
4. 不要引入与模板不协调的 CDN 库（模板用 Ant Design 就不要用 Tailwind 做布局）
5. 视觉风格绝对不能偏离
"""

            # ===== 非 sidebar 模式输出要求：生成完整独立页面 =====
            prompt += f"""
## 输出要求
生成一个**完整独立**的 HTML 页面，包含：
1. <!DOCTYPE html>、<head>（用 `<link rel="stylesheet" href="{template_css_path or 'template/template.css'}">` 引用模板 CSS）
2. <style> 中仅写页面特有的自定义样式（复用模板的 Ant Design 组件样式）
3. <body> 中是页面内容，使用 Ant Design 的 CSS class（ant-table、ant-btn、ant-form 等）
4. 使用 Vue 3 (CDN) 实现页面内交互（搜索、过滤、弹窗等）
5. 真实中文数据，不要用 Lorem ipsum
6. 这是第 {page_index + 1}/{total_pages} 页，只需生成这一个页面

**关键样式要求**：
- **必须**使用模板的配色、字体、组件风格（已通过模板 CSS 提供）
- 按钮用 `ant-btn` 系列 class，表格用 `ant-table` 系列，表单用 `ant-form` 系列
- 页面布局根据用户需求自行设计，不受模板框架限制

**布局与自适应要求**：
- 根容器使用 min-height: 100vh 填满视窗
- 不要在根容器上使用 max-width 或 container 限制宽度
- 表格用 `overflow-x: auto` 容器包裹
- 图表容器使用 width: 100%
- 使用 flexbox 布局让内容区自适应占满空间

输出格式：
<artifact type="html" title="{page_spec.get('name', f'页面{page_index + 1}')}">
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <link rel="stylesheet" href="{template_css_path or 'template/template.css'}">
    <script src="https://unpkg.com/vue@3/dist/vue.global.prod.js"></script>
    <style>/* 页面特有样式 */</style>
</head>
<body>
    <!-- 页面内容 -->
</body>
</html>
</artifact>
"""
    # ===== 非模板模式：多页面布局约束 =====
    if not has_template and total_pages > 1:
        if standalone:
            # ===== Route B 多文件架构：生成完整独立页面 =====
            prompt += f"""
## 多文件架构说明
本项目包含 {total_pages} 个页面，每个页面是**独立的完整 HTML 文件**，由一个导航框架统一管理。
你生成的页面将被保存为独立文件，在浏览器中直接打开即可查看。

**禁止输出以下元素**（导航框架已自动处理）：
- 侧边栏（sidebar / aside / .sidebar / nav-sidebar 等）
- 顶部导航栏（header / navbar / topbar / .header 等）
- 页面切换相关逻辑（tab、router、menu 切换等）

**你只需要输出**：一个**完整独立的 HTML 页面**，包含 <!DOCTYPE html>、<head>、<body>。

## 输出要求
生成一个**完整独立**的 HTML 页面，包含：
1. <!DOCTYPE html>、<html lang="zh-CN">、<head>、<body>
2. <head> 中引入 Tailwind CSS CDN：<script src="https://cdn.tailwindcss.com"></script>
3. <head> 中引入 FontAwesome CDN：<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">
4. <style> 中写入设计系统 CSS 变量和页面特有样式
5. <body> 中是页面核心内容
6. 使用 Vue 3 (CDN) 或原生 JS 实现页面内交互
7. 真实中文数据，不要用 Lorem ipsum
8. 这是第 {page_index + 1}/{total_pages} 页

**布局要求**（非常重要）：
- 根容器使用 min-height: 100vh 填满视窗
- **禁止**设置 max-width、container 等 class 限制宽度
- 内容宽度 100%，让块级元素自然占满
- **表格**必须包裹在 overflow-x: auto 容器中
- 图表/可视化容器使用 width: 100%
- 使用 flexbox 或 CSS Grid 进行布局

输出格式：
<artifact type="html" title="{page_spec.get('name', f'页面{page_index + 1}')}">
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">
    <style>
    :root {{ /* 设计系统 CSS 变量 */ }}
    /* 页面特有样式 */
    </style>
</head>
<body>
    <!-- 页面核心内容 -->
    <script src="https://unpkg.com/vue@3/dist/vue.global.prod.js"></script>
    <script>
    // 页面交互逻辑
    </script>
</body>
</html>
</artifact>
"""
        else:
            # ===== 原有模式：生成内容片段，由系统组装 =====
            prompt += f"""
## 多页面布局说明
本项目包含 {total_pages} 个页面，系统会自动生成侧边栏导航菜单来实现页面切换。
因此你**必须只生成页面的主内容区域**。

**禁止输出以下元素**（系统已自动处理）：
- 侧边栏（sidebar / aside / .sidebar / nav-sidebar 等）
- 顶部导航栏（header / navbar / topbar / .header 等）
- 全局布局容器（app-wrapper / app-layout / .app 等）
- 页面切换相关逻辑（tab、router、menu 切换等）

**你只需要输出**：页面的**核心内容区域**——表格、卡片、表单、图表、统计面板等。

**宽度要求**（非常重要，违反会导致页面布局错乱）：
- 内容区域会自动占满右侧空间，**不要设置 max-width、maxW 或固定宽度**
- 使用 width: 100% 或让块级元素自然占满宽度
- 如果使用 Tailwind，**禁止**加 max-w-*、w-[固定值]、container 等 class
- 根容器**必须** width: 100%，不要用 mx-auto 居中

**高度与滚动要求**（非常重要）：
- 页面内容应使用 `min-height: 100%` 或 `min-height: calc(100vh - XXpx)` 填满可视区域
- **表格**和**网格**等宽内容必须包裹在 `overflow-x: auto` 的容器中，确保超宽时出现水平滚动条
- 不要使用固定像素高度的容器来包裹动态内容
- 如果页面内容较长，确保整体可垂直滚动

**自适应布局规范**：
- 使用 flexbox 或 CSS Grid 进行布局，让内容自适应可用空间
- 卡片、统计面板等组件应使用 `flex: 1` 或 `grid` 自动分配宽度
- 图表/可视化容器使用 `width: 100%` 并设置合理的 `aspect-ratio` 或高度
- 这是第 {page_index + 1}/{total_pages} 页

**面包屑导航一致性要求**：
- 如果页面需要面包屑，使用统一结构: `<nav class="breadcrumb"><a href="#">父级</a><span class="sep">/</span><span class="current">当前页</span></nav>`
- 面包屑样式必须在设计系统 CSS 变量中统一定义（参考 shared_components），不要每个页面各自定义
- 链接颜色 #666，当前页 #333 加粗，分隔符 "/"，字号 14px

输出格式：
<artifact type="html" title="{page_spec.get('name', f'页面{page_index + 1}')}">
<style>/* 页面特有样式 */</style>
<div>
    <!-- 页面核心内容：表格、卡片、表单、图表等 -->
</div>
<script>
// Vue 3 应用逻辑（如需要）
</script>
</artifact>
"""
    elif not has_template:
        # 单页非模板模式：生成完整页面
        prompt += f"""
## 输出要求
生成一个**完整独立**的 HTML 页面，包含：
1. <!DOCTYPE html>、<head>、<style>、<body>
2. 使用 Vue 3 (CDN) 实现页面内交互（搜索、过滤、弹窗等）
3. 真实中文数据，不要用 Lorem ipsum

## 布局与自适应要求（非常重要）
- 页面根容器使用 `height: 100vh` 或 `min-height: 100vh` 填满视窗
- 使用 flexbox（display: flex）构建整体布局，让内容区自适应占满剩余空间
- **禁止**在根容器上使用 max-width、container 等 Tailwind class 限制宽度
- 表格和网格等宽内容必须用 `overflow-x: auto` 容器包裹
- 卡片、面板等组件使用 flex: 1 或 CSS Grid 自动分配宽度
- 图表/可视化容器使用 `width: 100%` 并设置合理高度

输出格式：
<artifact type="html" title="{page_spec.get('name', f'页面{page_index + 1}')}">
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <script src="https://unpkg.com/vue@3/dist/vue.global.prod.js"></script>
    <style>/* 样式 */</style>
</head>
<body>
    <!-- 页面内容 -->
</body>
</html>
</artifact>
"""
    # ===== iframe 上下文注入（Route B：页面在 iframe 中加载，父页面有侧边栏） =====
    if iframe_context:
        prompt += """
## iframe 加载上下文（非常重要）
此页面将在 iframe 中加载，**父页面已有侧边栏和顶部导航栏**。

**严格禁止输出以下元素**（父页面已提供）：
- 侧边栏（sidebar / aside / .sidebar / nav-sidebar / .nav 等）
- 顶部导航栏（header / navbar / topbar / .header 等）
- 全局布局容器（app-wrapper / app-layout / .app 等）
- 页面切换相关逻辑（tab、router、menu 切换等）
- 登录/注册表单

**你只需要生成**：页面的**核心内容区域**——表格、卡片、表单、图表、统计面板等。
"""
    return prompt


def build_framework_page_prompt(page_spec, design_system, global_config):def build_framework_page_prompt(page_spec, design_system, global_config):
    """构建框架页面（登录页等）的生成 prompt

    框架页面会被内嵌到 index.html 中，因此只需生成内容片段（非完整 HTML）。

    Args:
        page_spec: 页面规格 {name, description, features, ...}
        design_system: 设计系统 dict
        global_config: 全局设计配置

    Returns:
        str: prompt 文本
    """
    page_name = page_spec.get('name', '页面')
    description = page_spec.get('description', '')
    features = page_spec.get('features', '')
    interaction = page_spec.get('interaction', '')

    primary = (global_config or {}).get('primaryColor', '#004fff')
    secondary = (global_config or {}).get('secondaryColor', '#10b981')
    css_vars = design_system.get('css_variables', '') if design_system else ''

    prompt = f"""## 生成框架页面：{page_name}

{description}
"""

    if features:
        prompt += f"\n**UI 组件**：\n{features}\n"
    if interaction:
        prompt += f"\n**交互行为**：\n{interaction}\n"

    if css_vars:
        prompt += f"\n## 设计系统 CSS 变量（必须遵循）\n```css\n{css_vars[:3000]}\n```\n\n"

    prompt += f"""
## 输出要求
生成一个**内容片段**（不是完整 HTML），包含：
1. `<style>` 标签中的页面特有样式
2. 页面的核心内容（登录表单、注册表单等）
3. Vue 3 交互逻辑（表单验证、提交处理等）
4. 真实中文数据，不要用 Lorem ipsum

**配色要求**：主色 {primary}，辅助色 {secondary}

**重要约束**：
- **不要**输出 `<!DOCTYPE html>`, `<html>`, `<head>`, `<body>` 等标签
- **不要**包含侧边栏、导航栏
- **不要**引入 Tailwind CDN、FontAwesome CDN（已在父页面中引入）
- **不要**引入 Vue CDN（父页面已引入 Vue 3）
- **必须**在登录/注册成功后调用 `window.loginSuccess()` 函数切换到主应用
- **登录验证**：这是原型演示，**不要**做真实的用户名密码校验。表单提交后直接（或短暂 loading 动画后）调用 `window.loginSuccess()`，任何输入都应该能登录成功。不要设置固定的测试账号。

**页面布局**：
- 页面居中显示（使用 flexbox 居中）
- 宽度适中（max-width: 400px 左右）
- 全屏背景，使用设计系统的配色

输出格式：
```html
<style>
/* 页面特有样式 */
</style>
<div class="framework-page-container">
  <!-- 页面内容 -->
</div>
<script>
const {{ createApp, ref }} = Vue;
createApp({{
  setup() {{
    const loading = ref(false);
    async function handleSubmit() {{
      loading.value = true;
      // 原型演示：短暂 loading 后直接进入，不做密码校验
      await new Promise(r => setTimeout(r, 500));
      loading.value = false;
      if (typeof window.loginSuccess === 'function') window.loginSuccess();
    }}
    return {{ loading, handleSubmit }};
  }}
}}).mount('.framework-page-container');
</script>
```
"""
    return prompt


def extract_page_fragment(ai_response, page_name=''):
    """从 Round 2 AI 响应中提取页面 HTML

    处理 AI 可能输出完整 HTML 文档或带 markdown 包裹的情况。
    """
    # 安全网：先清除 AI 思考标记（防止 [think] 泄漏到 HTML）
    cleaned = re.sub(r'\[think\]', '', ai_response)

    # 策略 0：<artifact> 标签提取（优先级最高）
    artifact_match = re.search(r'<artifact[^>]*>([\s\S]*?)</artifact>', cleaned, re.IGNORECASE)
    if artifact_match:
        html = artifact_match.group(1).strip()
        if html:
            return html

    # 尝试提取 ```html 代码块（含闭合围栏）
    html_match = re.search(r'```(?:html|HTML)?\s*\n([\s\S]*?)```', cleaned)
    if html_match:
        fragment = html_match.group(1).strip()
        return fragment

    # 尝试提取 ```html 开头但无闭合围栏的情况（AI 有时会忘记闭合）
    # 只匹配以 ```html 开头的行，取其后的所有内容
    unclosed_match = re.match(r'^```(?:html|HTML)?\s*\n([\s\S]*)', cleaned.lstrip())
    if unclosed_match:
        fragment = unclosed_match.group(1).strip()
        if '<!DOCTYPE' in fragment or '<html' in fragment:
            return fragment

    # 尝试直接找完整 HTML
    doctype_match = re.search(r'(<!DOCTYPE[\s\S]*</html>)', cleaned, re.IGNORECASE)
    if doctype_match:
        return doctype_match.group(1).strip()

    # 尝试找 <html> 到 </html>
    html_tag_match = re.search(r'(<html[\s\S]*?</html>)', cleaned, re.IGNORECASE)
    if html_tag_match:
        return html_tag_match.group(1).strip()

    # 降级：返回全部内容，但去除可能残留的代码围栏
    result = cleaned.strip()
    if result.startswith('```'):
        # 去除开头的代码围栏行
        lines = result.split('\n')
        if lines[0].startswith('```'):
            lines = lines[1:]
        if lines and lines[-1].strip() == '```':
            lines = lines[:-1]
        result = '\n'.join(lines).strip()
    return result


def _extract_partial_html_from_args(args_str):
    """从 tool call 增量参数中提取部分 HTML 内容

    args_str 是流式累积的 JSON 字符串（可能不完整），
    格式如: {"page_key":"xxx","html_content":"<!DOCTYPE html>..."}
    提取 html_content 的部分值并反转义。
    """
    if not args_str or len(args_str) < 50:
        return ''

    # 找 html_content 字段起始
    marker = '"html_content"'
    idx = args_str.find(marker)
    if idx == -1:
        # 尝试其他可能的字段名
        for alt in ('"content"', '"page_html"', '"html"'):
            idx = args_str.find(alt)
            if idx != -1:
                marker = alt
                break
    if idx == -1:
        return ''

    # 从标记后找 ": " 或 ":"
    after_marker = args_str[idx + len(marker):].lstrip()
    if not after_marker.startswith(':'):
        return ''
    after_colon = after_marker[1:].lstrip()
    if not after_colon.startswith('"'):
        return ''

    # 提取引号内的内容（可能未闭合）
    content_start = args_str.index('"', idx + len(marker)) + 1
    raw = args_str[content_start:]

    # 如果有闭合引号，取完整内容
    end_quote = -1
    i = 0
    while i < len(raw):
        if raw[i] == '\\' and i + 1 < len(raw):
            i += 2  # 跳过转义序列
        elif raw[i] == '"':
            end_quote = i
            break
        else:
            i += 1

    if end_quote >= 0:
        raw = raw[:end_quote]

    # 反转义 JSON 字符串
    result = raw.replace('\\"', '"').replace('\\\\', '\\')
    result = result.replace('\\n', '\n').replace('\\t', '\t').replace('\\r', '\r')
    result = result.replace('\\/', '/')

    return result


def extract_complete_page_html(ai_response):
    """从 AI 响应中提取完整独立 HTML 页面（Route B 多文件架构用）

    与 extract_page_fragment 不同，这个函数要求返回的是完整的 HTML 文档
    （含 <!DOCTYPE html>），如果不是完整文档，则包装为完整文档。
    """
    # 安全网：清除 AI 思考标记
    cleaned = re.sub(r'\[think\]', '', ai_response)

    # 尝试提取 ```html 代码块
    html_match = re.search(r'```(?:html|HTML)?\s*\n([\s\S]*?)```', cleaned)
    if html_match:
        content = html_match.group(1).strip()
        if '<!DOCTYPE' in content or '<html' in content:
            return content
        # 代码块里是片段，包装为完整页面
        return _wrap_as_standalone(content)

    # 尝试直接找完整 HTML
    doctype_match = re.search(r'(<!DOCTYPE[\s\S]*</html>)', cleaned, re.IGNORECASE)
    if doctype_match:
        return doctype_match.group(1).strip()

    html_tag_match = re.search(r'(<html[\s\S]*?</html>)', cleaned, re.IGNORECASE)
    if html_tag_match:
        return html_tag_match.group(1).strip()

    # 降级：提取片段并包装
    fragment = extract_page_fragment(cleaned)
    if fragment and len(fragment) > 50:
        return _wrap_as_standalone(fragment)

    return ''


def _wrap_as_standalone(fragment):
    """将内容片段包装为完整独立 HTML 页面"""
    if '<!DOCTYPE' in fragment or '<html' in fragment:
        return fragment

    parts = [
        '<!DOCTYPE html>',
        '<html lang="zh-CN">',
        '<head>',
        '    <meta charset="UTF-8">',
        '    <meta name="viewport" content="width=device-width, initial-scale=1.0">',
        '    <script src="https://cdn.tailwindcss.com"></script>',
        '    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">',
        '</head>',
        '<body>',
        fragment,
        '</body>',
        '</html>'
    ]
    return '\n'.join(parts)


# ==================== Phase 3.5: 多文件架构工具函数 ====================

def ensure_project_directory_structure(project_path):
    """确保多文件项目目录结构存在 (pages/, assets/)"""
    pages_dir = os.path.join(project_path, 'pages')
    assets_dir = os.path.join(project_path, 'assets')
    os.makedirs(pages_dir, exist_ok=True)
    os.makedirs(assets_dir, exist_ok=True)
    return pages_dir, assets_dir


def build_standalone_page_html(page_fragment, design_system_css='',
                                template_css_path=None, layout_type='plain'):
    """将页面片段包装为独立的完整 HTML 文件

    Args:
        page_fragment: AI 生成的 HTML 片段（可能是完整 HTML 或内容片段）
        design_system_css: 设计系统 CSS 变量
        template_css_path: 模板 CSS 路径
        layout_type: 布局类型 ('sidebar'|'iframe'|'plain')

    Returns:
        str: 完整的独立 HTML 文件内容
    """
    # 如果已经是完整 HTML 文档，直接返回（加 shared.css 引用）
    if '<!DOCTYPE' in page_fragment or '<html' in page_fragment:
        # 注入 shared.css 引用
        if '</head>' in page_fragment:
            shared_link = '<link rel="stylesheet" href="assets/shared.css">\n'
            page_fragment = page_fragment.replace('</head>', shared_link + '</head>', 1)
        return page_fragment

    # 否则包装为完整 HTML
    template_link = ''
    if template_css_path:
        template_link = f'    <link rel="stylesheet" href="{template_css_path}">\n'

    bg_mode = 'light'
    parts = [
        '<!DOCTYPE html>',
        '<html lang="zh-CN">',
        '<head>',
        '    <meta charset="UTF-8">',
        '    <meta name="viewport" content="width=device-width, initial-scale=1.0">',
        '    <script src="https://cdn.tailwindcss.com"></script>',
        '    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">',
        template_link,
        '    <link rel="stylesheet" href="assets/shared.css">',
        '    <style>',
        design_system_css,
        '    /* 页面基础样式 */',
        '    * { margin: 0; padding: 0; box-sizing: border-box; }',
        '    html, body { height: 100%; font-family: system-ui, -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; }',
        '    </style>',
        '</head>',
        '<body>',
        page_fragment,
        '</body>',
        '</html>'
    ]
    return '\n'.join(parts)


def build_lightweight_index_frame(project_id, page_names, page_specs,
                                   design_system_css='', layout_type='plain',
                                   global_config=None, cross_page_spec=None):
    """生成轻量级 index.html 导航框架

    替代原有的大型单文件组装。生成 ~5KB 的导航框架，
    通过 iframe src 切换加载各独立页面。

    Args:
        project_id: 项目 ID
        page_names: 页面名称列表
        page_specs: 页面规格列表（含 name 字段）
        design_system_css: 设计系统 CSS
        layout_type: 布局类型
        global_config: 全局配置

    Returns:
        str: 轻量级 index.html 内容
    """
    primary = (global_config or {}).get('primaryColor', '#004fff')

    # 图标映射 — 使用更直观的图标
    icon_map = {
        0: 'fa-th-large', 1: 'fa-project-diagram', 2: 'fa-file-alt',
        3: 'fa-table', 4: 'fa-edit', 5: 'fa-chart-bar',
        6: 'fa-server', 7: 'fa-clock', 8: 'fa-users', 9: 'fa-cloud-upload-alt'
    }

    # 构建侧边栏菜单 —— 过滤掉 show_in_nav=false 的子页面/详情页
    nav_visible_indices = get_nav_visible_indices(cross_page_spec, len(page_names))
    sidebar_items = []
    page_entries = []
    for i, name in enumerate(page_names):
        safe_name = _make_safe_page_id(name)
        page_filename = f"pages/page_{i}_{name}.html"
        page_entries.append({
            'index': i,
            'name': name,
            'safe_name': safe_name,
            'filename': page_filename,
            'show_in_nav': i in nav_visible_indices,
        })

    for entry in page_entries:
        if not entry.get('show_in_nav', True):
            continue
        icon = icon_map.get(entry["index"], 'fa-file')
        sidebar_items.append(
            f'<li onclick="navigateTo(\'{entry["filename"]}\')"\n'
            f'    id="nav-{entry["safe_name"]}"\n'
            f'    class="sidebar-menu-item">\n'
            f'  <span class="sidebar-icon-wrap"><i class="fas {icon}"></i></span>\n'
            f'  <span class="sidebar-label">{entry["name"]}</span>\n'
            f'</li>'
        )

    sidebar_str = '\n'.join(sidebar_items)
    title_str = page_names[0] if page_names else '原型'
    first_page_file = page_entries[0]['filename'] if page_entries else ''

    parts = [
        '<!DOCTYPE html>',
        '<html lang="zh-CN">',
        '<head>',
        '    <meta charset="UTF-8">',
        '    <meta name="viewport" content="width=device-width, initial-scale=1.0">',
        '    <title>' + title_str + '</title>',
        '    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">',
        '    <style>',
        design_system_css,
        '',
        '/* 布局 */',
        '* { margin: 0; padding: 0; box-sizing: border-box; }',
        'html, body { height: 100%; font-family: system-ui, -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; }',
        '.app-layout { display: flex; height: 100vh; }',
        '.sidebar {',
        '    width: 240px; min-width: 240px;',
        f'    background: linear-gradient(180deg, #1e293b 0%, #0f172a 100%);',
        '    color: #e2e8f0; display: flex; flex-direction: column;',
        '    overflow-y: auto; border-right: 1px solid rgba(255,255,255,0.06);',
        '}',
        '.sidebar-header {',
        f'    padding: 24px 20px 20px; font-size: 17px; font-weight: 700;',
        '    color: #f8fafc; letter-spacing: 0.5px;',
        f'    border-bottom: 1px solid rgba(255,255,255,0.08);',
        '    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;',
        '}',
        '.sidebar-menu { list-style: none; padding: 12px 8px; flex: 1; }',
        '.sidebar-menu-item {',
        '    padding: 11px 14px; margin: 2px 0; cursor: pointer;',
        '    display: flex; align-items: center; gap: 12px;',
        '    font-size: 14px; color: #94a3b8; border-radius: 8px;',
        '    transition: all 0.2s ease; white-space: nowrap;',
        '}',
        '.sidebar-menu-item:hover {',
        '    background: rgba(255,255,255,0.06); color: #e2e8f0;',
        '}',
        '.sidebar-menu-item.active {',
        f'    background: {primary}; color: #ffffff;',
        '    box-shadow: 0 2px 8px rgba(0,0,0,0.15);',
        '}',
        '.sidebar-icon-wrap {',
        '    width: 28px; height: 28px; display: flex; align-items: center;',
        '    justify-content: center; border-radius: 6px; flex-shrink: 0;',
        '    font-size: 13px;',
        '}',
        '.sidebar-menu-item:hover .sidebar-icon-wrap { background: rgba(255,255,255,0.08); }',
        '.sidebar-menu-item.active .sidebar-icon-wrap { background: rgba(255,255,255,0.15); }',
        '.sidebar-label { font-weight: 500; }',
        '.main-content {',
        '    flex: 1; overflow: hidden; background: #f0f2f5;',
        '}',
        '.main-content iframe {',
        '    width: 100%; height: 100%; border: none;',
        '}',
        '@keyframes fadeIn {',
        '    from { opacity: 0; }',
        '    to { opacity: 1; }',
        '}',
        '    </style>',
        '</head>',
        '<body>',
        '<div class="app-layout">',
        '    <!-- 侧边栏 -->',
        '    <div class="sidebar">',
        '        <div class="sidebar-header">' + title_str + '</div>',
        '        <ul class="sidebar-menu">',
        sidebar_str,
        '        </ul>',
        '    </div>',
        '    <!-- 内容区 -->',
        '    <div class="main-content">',
        f'        <iframe id="content-frame" src="{first_page_file}"></iframe>',
        '    </div>',
        '</div>',
        '',
        '<script>',
        '    // 当前页面状态',
        f"    let currentSrc = '{first_page_file}';",
        '',
        '    // iframe src 切换导航',
        "    function navigateTo(pageFile) {",
        '        const frame = document.getElementById("content-frame");',
        '        if (frame.src.endsWith(pageFile)) return;',
        '        frame.src = pageFile;',
        '        currentSrc = pageFile;',
        '',
        '        // 更新侧边栏高亮',
        '        document.querySelectorAll(".sidebar-menu-item").forEach(item => item.classList.remove("active"));',
        '        event.currentTarget.classList.add("active");',
        '',
        '        // 通知父级 viewer（如果在 iframe 中）',
        '        if (window.parent !== window) {',
        '            // 从文件名提取页面名: pages/page_0_数据汇聚.html -> 数据汇聚',
        '            const match = pageFile.match(/page_\\d+_(.+)\\.html/);',
        '            const pageName = match ? match[1] : pageFile;',
        '            window.parent.postMessage({ type: "pageChange", page: pageName }, "*");',
        '        }',
        '    }',
        '',
        '    // 监听父级导航指令（viewer -> index.html）',
        '    window.addEventListener("message", function(e) {',
        '        if (e.data && e.data.type === "navigateTo") {',
        '            // 查找对应页面文件',
        '            const pageName = e.data.page;',
        '            const navItems = document.querySelectorAll(".sidebar-menu-item");',
        '            for (const item of navItems) {',
        '                const onclickStr = item.getAttribute("onclick") || "";',
        '                if (onclickStr.includes(pageName)) {',
        '                    item.click();',
        '                    break;',
        '                }',
        '            }',
        '        }',
        '    });',
        '',
        '    // 初始高亮',
        '    document.addEventListener("DOMContentLoaded", function() {',
        '        const firstItem = document.querySelector(".sidebar-menu-item");',
        '        if (firstItem) firstItem.classList.add("active");',
        '    });',
        '',
        '    // 通知父级 viewer 当前页面',
        '    if (window.parent !== window) {',
        f"        const match = currentSrc.match(/page_\\d+_(.+)\\.html/);",
        '        const pageName = match ? match[1] : currentSrc;',
        '        window.parent.postMessage({ type: "pageChange", page: pageName }, "*");',
        '    }',
        '</script>',
        '</body>',
        '</html>'
    ]
    return '\n'.join(parts)


def build_index_with_framework_pages(framework_pages_html, design_system_css='',
                                     business_page_names=None,
                                     global_config=None, cross_page_spec=None):
    """生成包含框架页面（登录页等）的 index.html

    框架页面直接内嵌到 index.html 中（Vue v-show 切换），
    业务页面通过 iframe src 加载。

    Args:
        framework_pages_html: {page_name: html_fragment} 框架页面内容
        design_system_css: 设计系统 CSS 变量
        business_page_names: 业务页面名称列表
        global_config: 全局配置
        cross_page_spec: 跨页规格（用于 show_in_nav 判断）

    Returns:
        str: 完整的 index.html 内容
    """
    business_page_names = business_page_names or []
    sidebar_bg = '#304156'
    sidebar_active_bg = '#1890ff'
    title = list(framework_pages_html.keys())[0] if framework_pages_html else (
        business_page_names[0] if business_page_names else '原型')

    # 图标映射
    icon_map = {
        0: 'fa-tachometer-alt', 1: 'fa-database', 2: 'fa-folder-open',
        3: 'fa-table', 4: 'fa-edit', 5: 'fa-chart-line',
        6: 'fa-hdd', 7: 'fa-clock', 8: 'fa-users', 9: 'fa-upload'
    }

    # 构建框架页面视图
    # 关键：框架页与主应用使用完全独立的 DOM 树和 Vue 实例
    # #framework-wrapper 包含框架页（登录/注册），有自己的 Vue app
    # #app 包含主应用（侧边栏+iframe），有自己的 Vue app
    # loginSuccess() 通过原生 JS 切换两个容器的 display 来切换视图
    framework_views = []
    first_framework_id = ''
    for page_name, page_html in framework_pages_html.items():
        safe_id = _make_safe_page_id(page_name)
        if not first_framework_id:
            first_framework_id = safe_id

        # 移除框架页中可能引入的 Vue CDN（父页面已引入）
        page_html = _strip_vue_cdn_from_html(page_html)

        framework_views.append(
            f'    <!-- {page_name} -->\n'
            f'    <div id="fw-{safe_id}" class="framework-view">\n'
            f'      {page_html}\n'
            f'    </div>\n'
        )

    framework_views_str = '\n'.join(framework_views)

    # 构建侧边栏（仅业务页面）
    nav_visible_indices = get_nav_visible_indices(cross_page_spec, len(business_page_names))
    sidebar_items = []
    for i, name in enumerate(business_page_names):
        if i not in nav_visible_indices:
            continue
        safe_name = _make_safe_page_id(name)
        page_filename = f"pages/page_{i}_{name}.html"
        icon = icon_map.get(i, 'fa-file')
        sidebar_items.append(
            f'        <li @click="navigateTo(\'{page_filename}\', $event)"\n'
            f'            id="nav-{safe_name}"\n'
            f'            class="sidebar-menu-item"\n'
            f'            :class="{{active: currentPageFile === \'{page_filename}\'}}">\n'
            f'          <i class="fas {icon}"></i>\n'
            f'          <span>{name}</span>\n'
            f'        </li>\n'
        )

    sidebar_str = '\n'.join(sidebar_items)
    first_business_page = f"pages/page_0_{business_page_names[0]}.html" if business_page_names else ''

    # 当有框架页面时，框架页可见，主应用隐藏（通过 CSS display:none 控制）
    # 没有框架页面时，主应用直接显示

    parts = [
        '<!DOCTYPE html>',
        '<html lang="zh-CN">',
        '<head>',
        '    <meta charset="UTF-8">',
        '    <meta name="viewport" content="width=device-width, initial-scale=1.0">',
        f'    <title>{title}</title>',
        '    <script src="https://unpkg.com/vue@3/dist/vue.global.prod.js"></script>',
        '    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">',
        '    <style>',
        design_system_css,
        '',
        '/* 布局 */',
        '* { margin: 0; padding: 0; box-sizing: border-box; }',
        'html, body { height: 100%; font-family: system-ui, -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; }',
        '',
        '/* 框架页面容器（独立于 #app，避免 Vue 实例冲突） */',
        '#framework-wrapper {',
        '    position: fixed; top: 0; left: 0; width: 100%; height: 100vh;',
        '    z-index: 1000;',
        '}',
        '.framework-view {',
        '    width: 100%; height: 100vh;',
        '    display: flex; align-items: center; justify-content: center;',
        '    background: #f0f2f5;',
        '}',
        '',
        '#app { height: 100%; }',
        '',
        '/* 主应用布局 */',
        '.app-layout { display: flex; height: 100vh; }',
        '.sidebar {',
        f'    width: 220px; min-width: 220px; background: {sidebar_bg}; color: #fff;',
        '    display: flex; flex-direction: column; overflow-y: auto;',
        '    box-shadow: 2px 0 6px rgba(0,0,0,0.1);',
        '}',
        '.sidebar-header {',
        '    padding: 20px 16px; font-size: 16px; font-weight: 600;',
        '    border-bottom: 1px solid rgba(255,255,255,0.1);',
        '    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;',
        '}',
        '.sidebar-menu { list-style: none; padding: 8px 0; flex: 1; }',
        '.sidebar-menu-item {',
        '    padding: 12px 20px; cursor: pointer; display: flex; align-items: center; gap: 10px;',
        '    font-size: 14px; color: #bfcbd9; transition: all 0.2s; white-space: nowrap;',
        '}',
        '.sidebar-menu-item:hover { background: rgba(255,255,255,0.05); color: #fff; }',
        f'.sidebar-menu-item.active {{ color: #fff; background: {sidebar_active_bg}; }}',
        '.sidebar-menu-item i { width: 18px; text-align: center; font-size: 14px; }',
        '.main-content {',
        '    flex: 1; overflow: hidden; background: #f0f2f5;',
        '}',
        '.main-content iframe {',
        '    width: 100%; height: 100%; border: none;',
        '}',
        '    </style>',
        '</head>',
        '<body>',
        # 框架页面容器（独立 DOM 树，与 #app 完全分离）
        '<div id="framework-wrapper">',
        framework_views_str,
        '</div>',
        # 主应用容器（独立 Vue 实例）
        '<div id="app" style="display: none;">',
        '    <div class="app-layout">',
        '        <div class="sidebar">',
        f'            <div class="sidebar-header">{title}</div>',
        '            <ul class="sidebar-menu">',
        sidebar_str,
        '            </ul>',
        '        </div>',
        '        <div class="main-content">',
        '            <iframe :src="currentPageFile"></iframe>',
        '        </div>',
        '    </div>',
        '</div>',
        '',
        '<script>',
        '    // === 框架页切换逻辑（原生 JS） ===',
        '    function loginSuccess() {',
        '        var fw = document.getElementById("framework-wrapper");',
        '        var app = document.getElementById("app");',
        '        if (fw) fw.style.display = "none";',
        '        if (app) app.style.display = "";',
        '        // 通知父级 viewer',
        '        if (window.parent !== window) {',
        '            window.parent.postMessage({ type: "pageChange", page: "app" }, "*");',
        '        }',
        '    }',
        '    window.loginSuccess = loginSuccess;',
        '',
        '    // === 主应用 Vue 实例（避免与框架页的 const createApp 冲突，使用 Vue.xxx） ===',
        '    Vue.createApp({',
        '        setup() {',
        f"            const currentPageFile = Vue.ref('{first_business_page}');",
        '',
        '            function navigateTo(pageFile, event) {',
        '                if (currentPageFile.value === pageFile) return;',
        '                currentPageFile.value = pageFile;',
        '',
        '                // 通知父级 viewer',
        '                if (window.parent !== window) {',
        '                    const match = pageFile.match(/page_\\d+_(.+)\\.html/);',
        '                    const pageName = match ? match[1] : pageFile;',
        '                    window.parent.postMessage({ type: "pageChange", page: pageName }, "*");',
        '                }',
        '            }',
        '',
        '            // 监听父级 viewer 导航指令',
        '            window.addEventListener("message", function(e) {',
        '                if (e.data && e.data.type === "navigateTo") {',
        '                    const pageName = e.data.page;',
        '                    const navItems = document.querySelectorAll(".sidebar-menu-item");',
        '                    for (const item of navItems) {',
        '                        if (item.getAttribute("id") && item.getAttribute("id").includes(pageName)) {',
        '                            item.click();',
        '                            break;',
        '                        }',
        '                    }',
        '                }',
        '            });',
        '',
        '            // 通知父级 viewer 当前页面',
        '            if (window.parent !== window && currentPageFile.value) {',
        '                const match = currentPageFile.value.match(/page_\\d+_(.+)\\.html/);',
        '                const pageName = match ? match[1] : currentPageFile.value;',
        '                window.parent.postMessage({ type: "pageChange", page: pageName }, "*");',
        '            }',
        '',
        '            return {',
        '                currentPageFile,',
        '                navigateTo',
        '            };',
        '        }',
        '    }).mount("#app");',
        '</script>',
        '</body>',
        '</html>'
    ]
    return '\n'.join(parts)


def extract_shared_styles(page_fragments):
    """从所有页面片段中提取共享样式到 shared.css

    分析各页面的 <style> 内容，提取公共部分。

    Returns:
        str: 共享 CSS 内容
    """
    if not page_fragments:
        return ''

    all_styles = []
    for fragment in page_fragments:
        if not fragment:
            continue
        # 提取 <style> 标签内容
        style_matches = re.findall(r'<style[^>]*>([\s\S]*?)</style>', fragment, re.IGNORECASE)
        for style_content in style_matches:
            if style_content.strip():
                all_styles.append(style_content.strip())

    if len(all_styles) <= 1:
        # 单页或无样式，直接返回全部
        return '\n'.join(all_styles) if all_styles else ''

    # 简单策略：提取出现在 2+ 页面中的公共 CSS 规则
    from collections import Counter

    # 按规则拆分（简单的按 } 分割）
    rule_counter = Counter()
    rule_fragments = []
    for style_content in all_styles:
        rules = [r.strip() for r in style_content.split('}') if r.strip()]
        for rule in rules:
            normalized = re.sub(r'\s+', ' ', rule).strip()
            if normalized:
                rule_counter[normalized] += 1

    # 提取出现 >= 2 次的规则作为共享样式
    shared_rules = []
    for rule, count in rule_counter.items():
        if count >= 2:
            shared_rules.append(rule)

    if shared_rules:
        return '\n'.join(f'{rule} }}' for rule in shared_rules)
    return ''


def save_multi_file_output(project_dir, page_fragments, page_names,
                           design_system_css='', template_css_path=None,
                           layout_type='plain', global_config=None,
                           cross_page_spec=None):
    """多文件架构输出：将页面保存为独立文件 + 轻量级导航框架

    Args:
        project_dir: 项目目录路径
        page_fragments: 页面 HTML 片段列表
        page_names: 页面名称列表
        design_system_css: 设计系统 CSS
        template_css_path: 模板 CSS 路径
        layout_type: 布局类型
        global_config: 全局配置

    Returns:
        str: 轻量级 index.html 的内容
    """
    pages_dir, assets_dir = ensure_project_directory_structure(project_dir)

    # 1. 保存各页面为独立文件
    for idx, (fragment, name) in enumerate(zip(page_fragments, page_names)):
        filename = f"page_{idx}_{name}.html"
        page_path = os.path.join(pages_dir, filename)

        standalone = build_standalone_page_html(
            fragment, design_system_css, template_css_path, layout_type
        )
        # 修正相对路径：pages/ 子目录中的页面需要 ../ 前缀
        standalone = standalone.replace('href="template/template.css"', 'href="../template/template.css"')
        standalone = standalone.replace("href='template/template.css'", "href='../template/template.css'")
        standalone = standalone.replace('href="assets/shared.css"', 'href="../assets/shared.css"')
        standalone = standalone.replace("href='assets/shared.css'", "href='../assets/shared.css'")
        with open(page_path, 'w', encoding='utf-8') as f:
            f.write(standalone)
        logger.info(f"[多文件] 已保存页面: {filename} ({len(standalone)} 字符)")

    # 2. 提取并保存 shared.css
    shared_css = extract_shared_styles(page_fragments)
    shared_css_path = os.path.join(assets_dir, 'shared.css')
    with open(shared_css_path, 'w', encoding='utf-8') as f:
        f.write(shared_css)
    logger.info(f"[多文件] 已保存 shared.css ({len(shared_css)} 字符)")

    # 3. 生成轻量级 index.html
    # page_specs 需要从 page_names 构建
    page_specs = [{'name': name} for name in page_names]
    index_html = build_lightweight_index_frame(
        project_dir, page_names, page_specs,
        design_system_css, layout_type, global_config,
        cross_page_spec=cross_page_spec
    )
    index_path = os.path.join(project_dir, 'index.html')
    with open(index_path, 'w', encoding='utf-8') as f:
        f.write(index_html)
    logger.info(f"[多文件] 已保存 index.html ({len(index_html)} 字符)")

    return index_html


def migrate_single_to_multifile(project_dir):
    """将现有单文件项目迁移为多文件架构

    读取 index.html，提取各页面片段，保存为独立文件。
    原始 index.html 备份为 index.html.single-file-backup。

    Args:
        project_dir: 项目目录路径

    Returns:
        bool: 是否成功迁移
    """
    index_path = os.path.join(project_dir, 'index.html')
    if not os.path.exists(index_path):
        logger.error(f"[迁移] index.html 不存在: {project_dir}")
        return False

    # 检查是否已经是多文件架构
    pages_dir = os.path.join(project_dir, 'pages')
    if os.path.isdir(pages_dir):
        page_files = [f for f in os.listdir(pages_dir) if f.endswith('.html')]
        if page_files:
            logger.info(f"[迁移] 已经是多文件架构: {len(page_files)} 个页面文件")
            return True

    with open(index_path, 'r', encoding='utf-8') as f:
        html_content = f.read()

    # 检测页面定义模式
    page_names = []

    # 模式1: v-show="currentPage === 'xxx'"
    vshow_pattern = r'v-show=["\']currentPage\s*===?\s*["\']([^"\']+)["\']'
    vshow_matches = re.findall(vshow_pattern, html_content)
    if vshow_matches:
        page_names = list(dict.fromkeys(vshow_matches))  # 去重保序

    # 模式2: v-if="currentPage === 'xxx'"
    if not page_names:
        vif_pattern = r'v-if=["\']currentPage\s*===?\s*["\']([^"\']+)["\']'
        vif_matches = re.findall(vif_pattern, html_content)
        page_names = list(dict.fromkeys(vif_matches))

    if not page_names:
        logger.error("[迁移] 未检测到多页面结构，无法迁移")
        return False

    logger.info(f"[迁移] 检测到 {len(page_names)} 个页面: {page_names}")

    # 创建目录结构
    pages_dir, assets_dir = ensure_project_directory_structure(project_dir)

    # 从 HTML 中提取各页面的内容
    page_fragments = []
    for i, page_name in enumerate(page_names):
        safe_name = _make_safe_page_id(page_name)

        # 尝试提取 v-show 包裹的内容
        pattern = rf'v-show=["\']currentPage\s*===?\s*["\']{re.escape(safe_name)}["\'][^>]*>([\s\S]*?)(?=<div\s+v-show=["\']currentPage|$)'
        match = re.search(pattern, html_content)

        if not match:
            # 尝试 v-if 模式
            pattern = rf'v-if=["\']currentPage\s*===?\s*["\']{re.escape(safe_name)}["\'][^>]*>([\s\S]*?)(?=<div\s+(?:v-if|v-show)=["\']currentPage|$)'
            match = re.search(pattern, html_content)

        if match:
            fragment = match.group(1).strip()
        else:
            fragment = f'<div class="p-8 text-center text-gray-400">页面「{page_name}」内容提取失败</div>'

        # 包装为完整 HTML
        styles, scripts, body = _extract_parts_from_fragment(
            '<!DOCTYPE html><html><head></head><body>' + fragment + '</body></html>'
        )
        page_fragments.append(fragment)

    # 提取设计系统 CSS
    design_css = ''
    css_match = re.search(r'<style[^>]*>([\s\S]*?)</style>', html_content, re.IGNORECASE)
    if css_match:
        design_css = css_match.group(1)

    # 保存多文件输出
    save_multi_file_output(
        project_dir=project_dir,
        page_fragments=page_fragments,
        page_names=page_names,
        design_system_css=design_css,
        layout_type='plain'
    )

    # 备份原始单文件
    backup_path = os.path.join(project_dir, 'index.html.single-file-backup')
    if not os.path.exists(backup_path):
        shutil.copy2(index_path, backup_path)
        logger.info(f"[迁移] 已备份原始文件: index.html.single-file-backup")

    logger.info(f"[迁移] 迁移完成: {len(page_names)} 个页面已保存到 pages/ 目录")
    return True


# ==================== Phase 4: 组装 (Round 3) ====================

def assemble_multi_page_html(page_fragments, design_system_css, page_names,
                              global_config=None, template_css_path=None,
                              layout_type='plain',
                              project_dir=None, output_format='dual',
                              cross_page_spec=None):
    """将多个页面 HTML 片段组装为带 Vue 导航的完整 HTML

    关键：使用与 inject_page_navigation_listener() 兼容的 currentPage 变量。

    Args:
        layout_type: 'iframe'|'sidebar'|'plain'
            - 'sidebar': 返回内容块（无外层 HTML 包装），用于注入框架
            - 'iframe'/'plain': 返回完整独立 HTML 页面
        project_dir: 项目目录路径（多文件输出需要）
        output_format: 'dual'(默认) | 'multi-file' | 'single-file'
            - 'dual': 同时生成多文件和单文件
            - 'multi-file': 仅生成多文件架构
            - 'single-file': 仅生成传统单文件
    """
    bg_mode = 'light' if (global_config or {}).get('backgroundMode', 'light') == 'light' else 'dark'
    bg_class = 'bg-gray-50' if bg_mode == 'light' else 'bg-gray-900'

    # 从每个片段中提取 body 内容（去除外层结构）
    page_sections = []
    for i, (fragment, name) in enumerate(zip(page_fragments, page_names)):
        safe_name = _make_safe_page_id(name)
        # sidebar 模式：AI 输出的是内容片段，不需要提取或包装
        if layout_type == 'sidebar':
            body_content = fragment
        else:
            body_content = _extract_body_from_fragment(fragment)
        page_sections.append((safe_name, name, body_content))

    # ===== sidebar 模式：AI 生成内容片段，后续由 assemble_iframe_html 注入模板框架 =====
    if layout_type == 'sidebar':
        logger.info(f"[组装] sidebar 模式：返回内容片段，由框架注入（{len(page_sections)} 页）")
        # 单页：直接返回内容片段
        if len(page_sections) == 1:
            return page_sections[0][2]  # body_content

        # 多页：用 v-if 切换所有内容区 + Vue 3 页面切换脚本
        sections = []
        for safe_name, display_name, body_content in page_sections:
            sections.append(
                f'<div v-if="currentPage === \'{safe_name}\'" '
                f'data-page-id="{safe_name}">\n'
                f'{body_content}\n</div>'
            )

        first_page = page_sections[0][0]
        vue_script = (
            '<script src="https://unpkg.com/vue@3/dist/vue.global.prod.js"></script>\n'
            '<script>\n'
            '(function(){\n'
            f"  const {{ createApp, ref, watch, onMounted }} = Vue;\n"
            f"  const container = document.querySelector('[data-page-id]')?.parentElement;\n"
            '  if(!container) return;\n'
            f"  createApp({{setup(){{\n"
            f"    const currentPage = ref('{first_page}');\n"
            '    window.currentPage = currentPage;\n'
            '    watch(currentPage, val => {\n'
            "      if(window.parent !== window) window.parent.postMessage({type:'pageChange',page:val},'*');\n"
            '    });\n'
            "    window.addEventListener('message', e => {\n"
            "      if(e.data?.type === 'navigateTo') currentPage.value = e.data.page;\n"
            '    });\n'
            "    onMounted(() => {\n"
            "      if(window.parent !== window) window.parent.postMessage({type:'pageChange',page:currentPage.value},'*');\n"
            '    });\n'
            '    return { currentPage };\n'
            '  }}).mount(container);\n'
            '})();\n'
            '</script>'
        )
        return '\n\n'.join(sections) + '\n' + vue_script

    # ===== 非 sidebar 模式 =====
    primary = (global_config or {}).get('primaryColor', '#004fff')

    # 提取各页面的 styles、scripts、body
    collected_styles = []
    collected_scripts = []
    page_bodies = []
    for safe_name, display_name, content in page_sections:
        styles, scripts, body = _extract_parts_from_fragment(content)
        collected_styles.append(styles)
        collected_scripts.append(scripts)
        page_bodies.append(body)

    # 合并去重 styles
    all_styles = '\n'.join(s for s in collected_styles if s)

    title_str = page_sections[0][1] if page_sections else '原型'
    first_page_id = page_sections[0][0] if page_sections else 'page'

    template_link = ''
    if template_css_path:
        template_link = f'    <link rel="stylesheet" href="{template_css_path}">\n'

    # ===== 单页项目：无侧边栏，全宽展示内容 =====
    if len(page_sections) == 1:
        logger.info("[组装] 单页项目，跳过侧边栏，生成全宽页面")
        single_body = page_bodies[0] or '<div class="p-8 text-center text-gray-400">页面内容为空</div>'
        single_scripts = collected_scripts[0] or ''

        # 处理 AI 生成的 Vue app 脚本（单页场景）
        ai_setup_extracts = []
        ai_single_component_defs = []
        ai_single_components_reg = None
        if single_scripts and 'createApp' in single_scripts and '.mount(' in single_scripts:
            logger.info("[组装] 单页项目包含 AI Vue app 脚本，提取业务逻辑")

            # 提取 createApp(...) 之前的组件定义
            createapp_pos = single_scripts.find('createApp(')
            if createapp_pos > 0:
                before_createapp = single_scripts[:createapp_pos]
                comp_def_matches = re.findall(
                    r'(const\s+\w+\s*=\s*\{[\s\S]*?\};)',
                    before_createapp
                )
                if comp_def_matches:
                    ai_single_component_defs = comp_def_matches
                    logger.info(f"[组装] 单页提取到 {len(comp_def_matches)} 个外部组件定义")

            # 提取 components 注册
            components_match = re.search(
                r'createApp\s*\(\s*\{[\s\S]*?components\s*:\s*\{([\s\S]*?)\}',
                single_scripts
            )
            if components_match:
                comp_reg_str = components_match.group(1).strip()
                if comp_reg_str:
                    ai_single_components_reg = comp_reg_str

            setup_match = re.search(
                r'setup\s*\(\s*\)\s*\{([\s\S]*?)\n\s*return\s*\{([\s\S]*?)\};?\s*\n\s*\}',
                single_scripts
            )
            if setup_match:
                setup_body = setup_match.group(1).strip()
                return_body = setup_match.group(2).strip()
                ai_setup_extracts.append((0, setup_body, return_body))
                single_scripts = ''  # 不再追加原始脚本

        parts = [
            '<!DOCTYPE html>',
            '<html lang="zh-CN">',
            '<head>',
            '    <meta charset="UTF-8">',
            '    <meta name="viewport" content="width=device-width, initial-scale=1.0">',
            '    <title>' + title_str + '</title>',
            '    <script src="https://unpkg.com/vue@3/dist/vue.global.prod.js"></script>',
            '    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">',
            template_link,
            '    <style>',
            design_system_css,
            '',
            '/* 布局 */',
            '* { margin: 0; padding: 0; box-sizing: border-box; }',
            'html, body { height: 100%; font-family: system-ui, -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; }',
            '.single-page-wrapper { min-height: 100vh; width: 100%; }',
            all_styles,
            '    </style>',
            '</head>',
            '<body>',
            '<div id="app">',
            '<div class="single-page-wrapper">',
            single_body,
            '</div>',
            '</div>',
            '',
            '<script>',
            'const { createApp, ref, watch, onMounted } = Vue;',
            '',
        ]

        # 合并 AI 的 setup 逻辑
        if ai_setup_extracts:
            # 先注入外部组件定义（如 TreeNode），放在 createApp 之前
            if ai_single_component_defs:
                for comp_def in ai_single_component_defs:
                    parts.append(comp_def)
                logger.info(f"[组装] 单页注入 {len(ai_single_component_defs)} 个外部组件定义")

            merged_setup_body = '\n'.join(ex[1] for ex in ai_setup_extracts)
            merged_return = ', '.join(ex[2] for ex in ai_setup_extracts)
            parts += [
                'createApp({',
            ]
            # 注入 components 注册
            if ai_single_components_reg:
                parts.append(f'    components: {{ {ai_single_components_reg} }},')
            parts += [
                '    setup() {',
                f"        const currentPage = ref('{first_page_id}');",
                '        window.currentPage = currentPage;',
                '',
                '        // 页面切换 → 通知父级 viewer',
                '        watch(currentPage, val => {',
                '            if (window.parent !== window) {',
                '                window.parent.postMessage({',
                "                    type: 'pageChange',",
                '                    page: val',
                "                }, '*');",
                '            }',
                '        });',
                '',
                '        // 监听父级导航指令',
                "        window.addEventListener('message', e => {",
                "            if (e.data && e.data.type === 'navigateTo') {",
                '                currentPage.value = e.data.page;',
                '            }',
                '        });',
                '',
                '        onMounted(() => {',
                '            if (window.parent !== window) {',
                '                window.parent.postMessage({',
                "                    type: 'pageChange',",
                '                    page: currentPage.value',
                "                }, '*');",
                '            }',
                '        });',
                '',
                '        // === AI 页面业务逻辑 ===',
                merged_setup_body,
                '',
                '        return { currentPage, ' + merged_return + ' };',
                '    }',
                "}).mount('#app');",
            ]
        else:
            parts += [
                'createApp({',
                '    setup() {',
                f"        const currentPage = ref('{first_page_id}');",
                '        window.currentPage = currentPage;',
                '',
                '        // 页面切换 → 通知父级 viewer',
                '        watch(currentPage, val => {',
                '            if (window.parent !== window) {',
                '                window.parent.postMessage({',
                "                    type: 'pageChange',",
                '                    page: val',
                "                }, '*');",
                '            }',
                '        });',
                '',
                '        // 监听父级导航指令',
                "        window.addEventListener('message', e => {",
                "            if (e.data && e.data.type === 'navigateTo') {",
                '                currentPage.value = e.data.page;',
                '            }',
                '        });',
                '',
                '        onMounted(() => {',
                '            if (window.parent !== window) {',
                '                window.parent.postMessage({',
                "                    type: 'pageChange',",
                '                    page: currentPage.value',
                "                }, '*');",
                '            }',
                '        });',
                '',
                '        return { currentPage };',
                '    }',
                "}).mount('#app');",
            ]

        parts += [
            '</script>',
            '</body>',
            '</html>'
        ]

        # 追加非 Vue app 的独立脚本
        if single_scripts:
            # 在 </body> 之前插入脚本
            body_idx = None
            for idx, p in enumerate(parts):
                if p == '</body>':
                    body_idx = idx
                    break
            if body_idx is not None:
                parts.insert(body_idx, single_scripts)
            else:
                parts.append(single_scripts)

        logger.info("[组装] 单页全宽 HTML 组装完成")
        return '\n'.join(p for p in parts if p is not None)

    # ===== 多页项目：生成侧边栏 + 内容区的单页应用 =====
    # 构建侧边栏菜单 —— 过滤掉 show_in_nav=false 的子页面/详情页
    nav_visible_indices = get_nav_visible_indices(cross_page_spec, len(page_sections))
    sidebar_items = []
    icon_map = {
        0: 'fa-th-large', 1: 'fa-project-diagram', 2: 'fa-file-alt',
        3: 'fa-table', 4: 'fa-edit', 5: 'fa-chart-bar',
        6: 'fa-server', 7: 'fa-clock', 8: 'fa-users', 9: 'fa-cloud-upload-alt'
    }
    for i, (safe_name, display_name, _) in enumerate(page_sections):
        if i not in nav_visible_indices:
            continue  # 子页面/详情页不在侧边栏显示
        icon = icon_map.get(i, 'fa-file')
        sidebar_items.append(
            f'<li @click="currentPage = \'{safe_name}\'"\n'
            f'    :class="[\'sidebar-menu-item\', currentPage === \'{safe_name}\' ? \'active\' : \'\']">\n'
            f'  <span class="sidebar-icon-wrap"><i class="fas {icon}"></i></span>\n'
            f'  <span class="sidebar-label">{display_name}</span>\n'
            f'</li>'
        )

    # 构建内容区
    content_divs = []
    for i, (safe_name, display_name, _) in enumerate(page_sections):
        body = page_bodies[i] or '<div class="p-8 text-center text-gray-400">页面内容为空</div>'
        content_divs.append(
            f'<div v-show="currentPage === \'{safe_name}\'" class="page-content-panel">\n'
            f'{body}\n'
            f'</div>'
        )

    sidebar_str = '\n'.join(sidebar_items)
    content_str = '\n'.join(content_divs)

    parts = [
        '<!DOCTYPE html>',
        '<html lang="zh-CN">',
        '<head>',
        '    <meta charset="UTF-8">',
        '    <meta name="viewport" content="width=device-width, initial-scale=1.0">',
        '    <title>' + title_str + '</title>',
        '    <script src="https://unpkg.com/vue@3/dist/vue.global.prod.js"></script>',
        '    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">',
        template_link,
        '    <style>',
        design_system_css,
        '',
        '/* 布局 */',
        '* { margin: 0; padding: 0; box-sizing: border-box; }',
        'html, body { height: 100%; font-family: system-ui, -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; }',
        '.app-layout { display: flex; height: 100vh; }',
        '.sidebar {',
        '    width: 240px; min-width: 240px;',
        f'    background: linear-gradient(180deg, #1e293b 0%, #0f172a 100%);',
        '    color: #e2e8f0; display: flex; flex-direction: column;',
        '    overflow-y: auto; border-right: 1px solid rgba(255,255,255,0.06);',
        '}',
        '.sidebar-header {',
        f'    padding: 24px 20px 20px; font-size: 17px; font-weight: 700;',
        '    color: #f8fafc; letter-spacing: 0.5px;',
        f'    border-bottom: 1px solid rgba(255,255,255,0.08);',
        '    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;',
        '}',
        '.sidebar-menu { list-style: none; padding: 12px 8px; flex: 1; }',
        '.sidebar-menu-item {',
        '    padding: 11px 14px; margin: 2px 0; cursor: pointer;',
        '    display: flex; align-items: center; gap: 12px;',
        '    font-size: 14px; color: #94a3b8; border-radius: 8px;',
        '    transition: all 0.2s ease; white-space: nowrap;',
        '}',
        '.sidebar-menu-item:hover {',
        '    background: rgba(255,255,255,0.06); color: #e2e8f0;',
        '}',
        '.sidebar-menu-item.active {',
        f'    background: {primary}; color: #ffffff;',
        '    box-shadow: 0 2px 8px rgba(0,0,0,0.15);',
        '}',
        '.sidebar-icon-wrap {',
        '    width: 28px; height: 28px; display: flex; align-items: center;',
        '    justify-content: center; border-radius: 6px; flex-shrink: 0;',
        '    font-size: 13px;',
        '}',
        '.sidebar-menu-item:hover .sidebar-icon-wrap { background: rgba(255,255,255,0.08); }',
        '.sidebar-menu-item.active .sidebar-icon-wrap { background: rgba(255,255,255,0.15); }',
        '.sidebar-label { font-weight: 500; }',
        '.main-content {',
        '    flex: 1; overflow: auto; background: #f0f2f5; width: 0; /* flex:1 + width:0 强制占满剩余空间 */',
        '}',
        '.page-content-panel {',
        '    min-height: 100%; height: 100%; padding: 20px; width: 100%; box-sizing: border-box;',
        '    animation: fadeIn 0.15s ease;',
        '}',
        '/* 强制 AI 生成内容自适应宽度 */',
        '.page-content-panel > * { max-width: 100% !important; }',
        '.page-content-panel .container, .page-content-panel .wrapper { width: 100% !important; max-width: 100% !important; }',
        '/* 表格溢出处理 */',
        '.page-content-panel table { display: block; max-width: 100%; overflow-x: auto; }',
        '.page-content-panel .ant-table-wrapper, .page-content-panel .el-table, .page-content-panel [class*="table-wrapper"], .page-content-panel [class*="table-container"] { max-width: 100%; overflow-x: auto; }',
        '/* 阻止 AI 使用固定宽度或 max-width 限制根容器 */',
        '.page-content-panel > div:first-child { width: 100% !important; }',
        '/* 网格/图表容器自适应 */',
        '.page-content-panel canvas, .page-content-panel svg { max-width: 100%; height: auto; }',
        '@keyframes fadeIn {',
        '    from { opacity: 0; }',
        '    to { opacity: 1; }',
        '}',
        all_styles,
        '    </style>',
        '</head>',
        '<body>',
        '<div id="app">',
        '<div class="app-layout">',
        '    <!-- 侧边栏 -->',
        '    <div class="sidebar">',
        '        <div class="sidebar-header">' + title_str + '</div>',
        '        <ul class="sidebar-menu">',
        sidebar_str,
        '        </ul>',
        '    </div>',
        '    <!-- 内容区 -->',
        '    <div class="main-content">',
        content_str,
        '    </div>',
        '</div>',
        '</div>',
        '',
        '<script>',
        'const { createApp, ref, watch, onMounted } = Vue;',
        '',
        'createApp({',
        '    setup() {',
        "        const currentPage = ref('" + first_page_id + "');",
        '        window.currentPage = currentPage;',
        '',
        '        // 页面切换 → 通知父级 viewer',
        '        watch(currentPage, val => {',
        '            if (window.parent !== window) {',
        '                window.parent.postMessage({',
        "                    type: 'pageChange',",
        '                    page: val',
        "                }, '*');",
        '            }',
        '        });',
        '',
        '        // 监听父级导航指令',
        "        window.addEventListener('message', e => {",
        "            if (e.data && e.data.type === 'navigateTo') {",
        '                currentPage.value = e.data.page;',
        '            }',
        '        });',
        '',
        '        onMounted(() => {',
        '            if (window.parent !== window) {',
        '                window.parent.postMessage({',
        "                    type: 'pageChange',",
        '                    page: currentPage.value',
        "                }, '*');",
        '            }',
        '        });',
        '',
        '        return { currentPage };',
        '    }',
        "}).mount('#app');",
        '</script>',
    ]

    # 追加各页面的独立 scripts
    # 处理 AI 生成的 Vue app 脚本：提取 setup() 内的业务逻辑，合并到框架 Vue app
    ai_setup_extracts = []  # 收集各页面 AI 的 setup 代码片段
    ai_component_defs = []  # 收集各页面 createApp 外部的组件定义（如 TreeNode）
    ai_components_regs = []  # 收集各页面 createApp 内的 components 注册
    for i, scripts in enumerate(collected_scripts):
        if not scripts:
            continue
        # 检测是否包含 Vue createApp（AI 生成的独立 Vue app）
        if 'createApp' in scripts and '.mount(' in scripts:
            logger.info(f"[组装] 页面 {page_sections[i][1] if i < len(page_sections) else i} 包含 AI Vue app 脚本，提取业务逻辑")

            # 提取 createApp(...) 调用之前的组件定义（如 const TreeNode = {...}）
            # 这些是定义在 createApp 外部、被 components 注册的 Vue 子组件
            createapp_pos = scripts.find('createApp(')
            if createapp_pos > 0:
                before_createapp = scripts[:createapp_pos]
                # 匹配 const XxxComponent = { ... }; 模式的组件定义
                comp_def_matches = re.findall(
                    r'(const\s+\w+\s*=\s*\{[\s\S]*?\};)',
                    before_createapp
                )
                if comp_def_matches:
                    ai_component_defs.extend(comp_def_matches)
                    logger.info(f"[组装] 提取到 {len(comp_def_matches)} 个外部组件定义")

            # 提取 createApp 内的 components 注册（如 components: { TreeNode }）
            components_match = re.search(
                r'createApp\s*\(\s*\{[\s\S]*?components\s*:\s*\{([\s\S]*?)\}',
                scripts
            )
            if components_match:
                comp_reg_str = components_match.group(1).strip()
                if comp_reg_str:
                    ai_components_regs.append(comp_reg_str)
                    logger.info(f"[组装] 提取到 components 注册: {comp_reg_str}")

            # 提取 setup() 函数体内容
            setup_match = re.search(
                r'setup\s*\(\s*\)\s*\{([\s\S]*?)\n\s*return\s*\{([\s\S]*?)\};?\s*\n\s*\}',
                scripts
            )
            if setup_match:
                setup_body = setup_match.group(1).strip()
                return_body = setup_match.group(2).strip()
                ai_setup_extracts.append((i, setup_body, return_body))
            # 不追加原始脚本（已提取到框架 app 中）
            continue
        parts.append(scripts)

    # 如果有 AI setup 提取内容，合并到框架 Vue app 的 setup() 中
    if ai_setup_extracts:
        # 找到 'return { currentPage };' 在 parts 中的位置
        return_idx = None
        for idx, p in enumerate(parts):
            if 'return { currentPage };' in p:
                return_idx = idx
                break

        if return_idx is not None:
            # 在 return 行之前插入 AI 的 setup 代码
            ai_setup_parts = []
            all_return_extras = []
            for page_idx, setup_body, return_body in ai_setup_extracts:
                page_label = page_sections[page_idx][1] if page_idx < len(page_sections) else str(page_idx)
                ai_setup_parts.append(f'        // === 页面 {page_label} 业务逻辑 ===')
                # 移除 onMounted（框架已有自己的 onMounted）
                cleaned_setup = re.sub(
                    r'onMounted\s*\(\s*\(\)\s*=>\s*\{[\s\S]*?\}\s*\)\s*;?',
                    '',
                    setup_body
                ).strip()
                if cleaned_setup:
                    ai_setup_parts.append(cleaned_setup)
                all_return_extras.append(return_body)

            # 替换 return 行：合并 currentPage + AI return 值
            ai_return_str = ',\n            '.join(all_return_extras)
            parts[return_idx] = parts[return_idx].replace(
                'return { currentPage };',
                f'return {{ currentPage,\n            {ai_return_str}\n        }};'
            )

            # 在 return 行之前插入 AI setup 代码
            for j, setup_line in enumerate(ai_setup_parts):
                parts.insert(return_idx + j, setup_line)

            logger.info(f"[组装] 已将 {len(ai_setup_extracts)} 个页面的 AI 业务逻辑合并到框架 Vue app")

    # 注入 AI 外部组件定义（如 TreeNode）到框架 Vue app
    if ai_component_defs:
        # 找到 'createApp({' 在 parts 中的位置
        createapp_idx = None
        for idx, p in enumerate(parts):
            if 'createApp({' in p:
                createapp_idx = idx
                break
        if createapp_idx is not None:
            # 在 createApp 之前插入组件定义
            for j, comp_def in enumerate(ai_component_defs):
                parts.insert(createapp_idx + j, comp_def)
            logger.info(f"[组装] 已注入 {len(ai_component_defs)} 个外部组件定义")

    # 注入 components 注册到 createApp 调用中
    if ai_components_regs:
        # 找到 'createApp({' 行，在其后插入 components 注册
        for idx, p in enumerate(parts):
            if 'createApp({' in p:
                merged_comps = ', '.join(ai_components_regs)
                parts.insert(idx + 1, f'    components: {{ {merged_comps} }},')
                logger.info(f"[组装] 已注入 components 注册: {merged_comps}")
                break

    parts.extend([
        '</body>',
        '</html>'
    ])
    single_file_html = '\n'.join(parts)

    # ===== 多文件架构输出 =====
    if output_format in ('dual', 'multi-file') and project_dir:
        try:
            save_multi_file_output(
                project_dir=project_dir,
                page_fragments=page_fragments,
                page_names=page_names,
                design_system_css=design_system_css,
                template_css_path=template_css_path,
                layout_type=layout_type,
                global_config=global_config,
                cross_page_spec=cross_page_spec,
            )
        except Exception as e:
            logger.warning(f"[组装] 多文件输出失败（不影响单文件）: {e}")

    if output_format == 'multi-file' and project_dir:
        # 仅多文件模式，不需要返回单文件 HTML
        index_path = os.path.join(project_dir, 'index.html')
        if os.path.exists(index_path):
            with open(index_path, 'r', encoding='utf-8') as f:
                return f.read()
        return single_file_html

    return single_file_html


def _make_safe_page_id(name):
    """将页面名转为安全的 Vue 变量名"""
    # 保留中文、字母、数字、下划线
    safe = re.sub(r'[^a-zA-Z0-9\u4e00-\u9fff_-]', '_', name)
    if not safe:
        safe = 'page'
    return safe


def _insert_before(html, marker, insertion):
    """在 HTML 中的 marker 位置前插入内容。"""
    if marker not in html:
        logger.warning(f"[注入] 未找到标记: {marker[:50]}")
        return html
    return html.replace(marker, insertion + marker, 1)


def _extract_body_from_fragment(html):
    """从完整 HTML 片段中提取 <body> 内容或整个内容"""
    if not html:
        return ''

    # 如果是完整 HTML 文档，提取全部内容
    if '<!DOCTYPE' in html or '<html' in html:
        return html  # 完整文档直接用于 srcdoc

    # 否则包装为完整文档
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">
</head>
<body>
{html}
</body>
</html>"""


def _extract_parts_from_fragment(html):
    """从完整 HTML 中分离 styles、scripts、body 内容

    用于侧边栏布局组装：把每个页面内的 <style>、<script>、<body> 内容
    分别提取出来，以便在组装时统一管理。
    """
    if not html:
        return ('', '', '')

    styles = ''
    scripts = ''
    body_content = html

    # 如果不是完整 HTML，直接作为 body
    if '<!DOCTYPE' not in html and '<html' not in html:
        return ('', '', html)

    # 提取 <style> 标签
    style_matches = re.findall(r'(<style[^>]*>[\s\S]*?</style>)', html, re.IGNORECASE)
    if style_matches:
        styles = '\n'.join(style_matches)

    # 提取 <body> 内容
    body_match = re.search(r'<body[^>]*>([\s\S]*?)</body>', html, re.IGNORECASE)
    if body_match:
        body_content = body_match.group(1).strip()
        # 从 body 中移除 <style> 标签（已收集到 styles 中）
        body_content = re.sub(r'<style[^>]*>[\s\S]*?</style>', '', body_content, flags=re.IGNORECASE).strip()

    # 提取 <script> 标签（排除 CDN 引用，只保留内联脚本）
    # 注意：只从完整 HTML 中提取，body 中的脚本会被自动包含
    script_matches = re.findall(r'(<script(?![^>]*src=)[^>]*>[\s\S]*?</script>)', html, re.IGNORECASE)
    if script_matches:
        scripts = '\n'.join(script_matches)

    # 从 body 中移除 script 标签（已收集到 scripts 中）
    body_content = re.sub(r'<script(?![^>]*src=)[^>]*>[\s\S]*?</script>', '', body_content, flags=re.IGNORECASE).strip()

    # 剥离侧边栏/导航元素（防止组装时出现重复侧边栏）
    # 匹配常见 sidebar/nav 结构：<aside>, <nav>, 以及 class 含 sidebar/nav 的 div
    # 对于 div 使用匹配嵌套的平衡标签方法
    def _strip_tag_with_content(text, tag_name, class_pattern=None):
        """剥离指定标签及其全部内容（处理嵌套）"""
        if class_pattern:
            open_pat = re.compile(
                r'<div[^>]*\bclass="[^"]*\b(?:' + '|'.join(class_pattern) + r')\b[^"]*"[^>]*>',
                re.IGNORECASE
            )
            close_tag = '</div>'
        else:
            open_pat = re.compile(r'<' + tag_name + r'[^>]*>', re.IGNORECASE)
            close_tag = '</' + tag_name + '>'

        result = text
        while True:
            m = open_pat.search(result)
            if not m:
                break
            start = m.start()
            # 平衡匹配：从 opening tag 开始计数嵌套层级
            depth = 1
            pos = m.end()
            open_re = re.compile(r'<' + (tag_name if not class_pattern else 'div') + r'(?:\s[^>]*)?\s*>', re.IGNORECASE)
            close_re = re.compile(r'</' + (tag_name if not class_pattern else 'div') + r'\s*>', re.IGNORECASE)
            while depth > 0 and pos < len(result):
                next_open = open_re.search(result, pos)
                next_close = close_re.search(result, pos)
                if not next_close:
                    break
                if next_open and next_open.start() < next_close.start():
                    depth += 1
                    pos = next_open.end()
                else:
                    depth -= 1
                    pos = next_close.end()
                    if depth == 0:
                        result = result[:start] + result[pos:]
                        break
            else:
                # 未找到平衡的闭合标签，放弃剥离此标签
                break
        return result

    # 剥离 <aside> 和 <nav>
    new_content = _strip_tag_with_content(body_content, 'aside')
    new_content = _strip_tag_with_content(new_content, 'nav')
    # 剥离含 sidebar/nav class 的 <div>
    new_content = _strip_tag_with_content(
        new_content, 'div',
        class_pattern=['sidebar', 'side-bar', 'side_menu', 'side-menu', 'sider', 'menu-container']
    )
    if new_content != body_content:
        logger.info(f"[提取] 从页面 body 中剥离了侧边栏/导航元素")
        body_content = new_content.strip()

    # 剥离 AI 生成的 <div id="app"> 包装（外层框架已提供 #app，内部重复会导致 Vue 挂载冲突）
    # 匹配 body 顶层的 <div id="app"> ... </div>，将其内容展开
    app_wrapper_match = re.match(
        r'\s*<div\s+id=["\']app["\'][^>]*>([\s\S]*)</div>\s*$',
        body_content,
        re.IGNORECASE
    )
    if app_wrapper_match:
        inner = app_wrapper_match.group(1).strip()
        # 确保剥离的是最外层包装（内部内容不应为空）
        if inner:
            logger.info("[提取] 从页面 body 中剥离了 <div id=\"app\"> 包装")
            body_content = inner

    # 清理 body 中残留的 CDN <script src="..."> 标签（Vue、Tailwind 等）
    # 这些由框架统一引入，子页面中不应重复加载
    body_content = re.sub(
        r'<script[^>]*src=["\'][^"\']*(?:vue|tailwind|fontawesome|font-awesome)[^"\']*["\'][^>]*>\s*</script>',
        '', body_content, flags=re.IGNORECASE
    ).strip()

    # 清理 body 中所有剩余的 <script src="..."> 标签（框架统一管理脚本加载）
    body_content = re.sub(
        r'<script[^>]*src=["\'][^"\']*["\'][^>]*>\s*</script>',
        '', body_content, flags=re.IGNORECASE
    ).strip()

    # 清理 body 中非顶层但仍有 id="app" 的 div（改为 class="app-inner" 避免冲突）
    # 不再用 re.match 而是全局替换，避免嵌套 id="app" 导致 Vue 挂载混乱
    body_content = re.sub(
        r'<div\s+id=["\']app["\']',
        '<div class="app-inner"',
        body_content, flags=re.IGNORECASE
    )

    # 剥离 max-width 约束（防止内容宽度不自适应）
    # 处理内联 style 中的 max-width
    body_content = re.sub(
        r'max-width\s*:\s*\d+px',
        'max-width:100%',
        body_content,
        flags=re.IGNORECASE
    )
    body_content = re.sub(
        r'max-width\s*:\s*[^;]+;?',
        lambda m: '' if '100%' not in m.group(0) else m.group(0),
        body_content,
        flags=re.IGNORECASE
    )

    return (styles, scripts, body_content)


def _extract_body_content_only(html):
    """从完整 HTML 文档中提取纯 body 内容（去除 html/head/body 包装）
    用于侧边栏模式，将 AI 生成的完整页面转为内容片段注入框架。
    """
    if not html:
        return ''

    # 分离 <head> 和 <body> 部分
    head_content = ''
    body_content = html  # 默认返回全部

    head_match = re.search(r'<head[^>]*>([\s\S]*?)</head>', html, re.IGNORECASE)
    body_match = re.search(r'<body[^>]*>([\s\S]*?)</body>', html, re.IGNORECASE)

    if head_match:
        head_content = head_match.group(1)
    if body_match:
        body_content = body_match.group(1).strip()

    # 从 <head> 中提取 <style> 标签（仅 head 部分，避免与 body 中的重复）
    styles = re.findall(r'(<style[^>]*>[\s\S]*?</style>)', head_content, re.IGNORECASE)

    # 从 <head> 中提取内联 <script>（不含 src 属性）
    inline_scripts = re.findall(r'(<script(?![^>]*src=)[^>]*>[\s\S]*?</script>)', head_content, re.IGNORECASE)

    # 组装：head 中的 styles + body 内容 + head 中的 inline scripts
    parts = []
    for s in styles:
        parts.append(s)
    parts.append(body_content)
    for s in inline_scripts:
        parts.append(s)

    return '\n\n'.join(parts)


# ==================== Phase 4.5: 模板样式参考卡提取 ====================


def build_template_style_card(template_frame_html='', template_design_tokens='',
                              template_layout_type='plain'):
    """从模板中提取精简样式参考卡（~1500 字符）

    替代原始 template_frame_html (~15000) + template_html_summary (~12000)。
    只保留 AI 生成内容时真正需要参考的信息：
    - 配色与设计变量（从 design_tokens 来）
    - 内容区自定义 CSS class（从 frame_html 正则提取）
    - 内容区组件写法参考片段
    - 布局模式提示
    """
    if not template_frame_html and not template_design_tokens:
        return ''

    parts = []

    # 1. 设计令牌（直接复用）
    if template_design_tokens:
        parts.append(f"### 配色与设计变量\n{template_design_tokens[:800]}")

    # 2. 从模板内容区提取关键 CSS class
    if template_frame_html:
        all_classes = set(re.findall(r'class="([^"]+)"', template_frame_html))
        # 过滤框架 class（sidebar/header/nav/logo 相关）
        framework_keywords = {'sidebar', 'header', 'nav', 'menu', 'logo', 'collapse',
                              'footer', 'wrapper', 'container', 'layout'}
        content_classes = sorted([
            c for c in all_classes
            if not any(kw in c.lower().split() for kw in framework_keywords)
            and not c.startswith('ant-')  # ant-design 通用 class 不需要列出
        ])[:25]
        if content_classes:
            parts.append(f"### 内容区自定义 CSS class\n" + "、".join(content_classes))

        # 3. 提取内容区的一个代表性组件片段（作为 HTML 写法参考）
        content_match = re.search(
            r'<div[^>]*class=["\'][^"\']*pageContent[^"\']*["\'][^>]*>(.*?)</div>',
            template_frame_html, re.DOTALL
        )
        if not content_match:
            content_match = re.search(
                r'<div[^>]*class=["\'][^"\']*content-area[^"\']*["\'][^>]*>(.*?)</div>',
                template_frame_html, re.DOTALL
            )
        if content_match:
            snippet = content_match.group(1).strip()
            if len(snippet) > 500:
                snippet = snippet[:500] + '\n...'
            parts.append(f"### 内容区组件写法参考\n```html\n{snippet}\n```")

        # 4. 布局模式提示
        is_sidebar = template_layout_type == 'sidebar'
        if is_sidebar:
            parts.append("### 布局模式\n侧边栏+顶栏+内容区。你只需生成内容区 HTML 片段，不要生成完整页面。")
        else:
            parts.append("### 布局模式\niframe 嵌套。生成完整独立 HTML 页面。")

    return '\n\n'.join(parts)


def build_generated_style_card(existing_html, max_chars=1500):
    """从已生成的 HTML 中提取样式参考卡（供后续批次保持风格一致）

    与 build_template_style_card（从模板提取）不同，这个函数从
    AI 实际生成的页面中提取样式特征，确保后续批次复制已确立的视觉风格。

    提取内容：
    - 内联 <style> 中的关键 CSS 规则（class 名、颜色值、布局）
    - 典型组件的 HTML 写法片段（卡片、表格、按钮等）
    - Tailwind class 使用模式
    """
    if not existing_html or len(existing_html) < 200:
        return ''

    parts = []

    # 1. 提取 <style> 中的 CSS 规则（取最有价值的部分）
    style_matches = re.findall(r'<style[^>]*>([\s\S]*?)</style>', existing_html, re.IGNORECASE)
    if style_matches:
        all_css = '\n'.join(style_matches)
        custom_lines = []
        for line in all_css.split('\n'):
            stripped = line.strip()
            if not stripped or stripped.startswith('/*') or stripped == '*/':
                continue
            if any(kw in stripped for kw in ['--color', '--bg', '--border', '--font',
                                               '--primary', '--secondary', '--radius',
                                               '.card', '.btn', '.table', '.form',
                                               '.page_', '.search', '.filter',
                                               'background:', 'color:', 'border:',
                                               'box-shadow', 'border-radius', 'padding:']):
                custom_lines.append(stripped)
        if custom_lines:
            css_sample = '\n'.join(custom_lines[:30])
            if len(css_sample) > 600:
                css_sample = css_sample[:600] + '\n...'
            parts.append(f"### 已生成页面的关键 CSS 样式\n```css\n{css_sample}\n```")

    # 2. 提取高频 Tailwind class 使用模式
    all_classes = re.findall(r'class="([^"]+)"', existing_html)
    skip_prefixes = ('sidebar', 'nav', 'menu', 'logo', 'header', 'footer',
                     'collapse', 'layout', 'wrapper')
    content_classes = set()
    for cls_str in all_classes:
        for cls in cls_str.split():
            if not any(cls.startswith(p) for p in skip_prefixes):
                content_classes.add(cls)
    from collections import Counter
    class_counter = Counter(content_classes)
    common_patterns = [cls for cls, _ in class_counter.most_common(20)]
    if common_patterns:
        parts.append(f"### 高频 Tailwind class（按使用频率）\n" + '、'.join(common_patterns))

    # 3. 提取一个典型组件片段作为写法参考
    card_patterns = [
        r'<div[^>]*class="[^"]*bg-white[^"]*"[^>]*>(\s*<div[^>]*class="[^"]*p-[^"]*"[^>]*>.*?</div>\s*){2,}</div>',
        r'<div[^>]*class="[^"]*(?:card|rounded|shadow)[^"]*"[^>]*>.*?</div>',
    ]
    for pattern in card_patterns:
        match = re.search(pattern, existing_html, re.DOTALL)
        if match:
            snippet = match.group(0).strip()
            if 200 < len(snippet) < 800:
                parts.append(f"### 典型组件写法参考\n```html\n{snippet}\n```")
                break

    result = '\n\n'.join(parts)
    if len(result) > max_chars:
        result = result[:max_chars] + '\n...'
    return result


# ==================== 增量生成工具定义（Function Calling） ====================

incremental_tools = [
    {
        "type": "function",
        "function": {
            "name": "add_page",
            "description": (
                "Generate a complete standalone HTML page and save it as a separate file. "
                "You must provide a **complete HTML document** in html_content "
                "(including <!DOCTYPE html>, <head>, <body>). "
                "Only generate the main content area — "
                "the navigation sidebar is handled automatically by the framework. "
                "Call this once for each page to be generated."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "page_key": {
                        "type": "string",
                        "description": "页面的唯一标识符，必须与提示中提供的 page_key 完全一致，如 'page_1'、'page_2'"
                    },
                    "page_name": {
                        "type": "string",
                        "description": "页面中文显示名，如 '用户管理'、'订单列表'"
                    },
                    "html_content": {
                        "type": "string",
                        "description": "完整的独立 HTML 页面，包含 <!DOCTYPE html>、<html>、<head>（含 Tailwind CSS CDN、FontAwesome CDN、设计系统 CSS）、<body>。仅包含页面主体内容区域。"
                    }
                },
                "required": ["page_key", "page_name", "html_content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_page",
            "description": (
                "Read the content of a generated page file. "
                "You MUST call this before using edit_page.\n"
                "The output uses line numbers (e.g. '  123→content'). "
                "When copying content for edit_page's old_string, "
                "copy ONLY the content after the arrow, NOT the line number prefix.\n"
                "If page_key is not specified, returns a list of page files."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "page_key": {
                        "type": "string",
                        "description": "要读取的页面标识符，如 'page_1'。不指定则返回页面列表。"
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit_page",
            "description": (
                "Performs exact string replacements in a generated page.\n"
                "Usage:\n"
                "1. You MUST call read_page first to see the exact content."
                " old_string must be copied verbatim from read_page output.\n"
                "2. old_string should be 2-5 lines to ensure unique match."
                " The edit will fail if old_string is not unique.\n"
                "3. Use replace_all=true to replace all occurrences of old_string.\n"
                "4. If the edit fails, re-read the page and retry."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "page_key": {
                        "type": "string",
                        "description": "要编辑的页面标识符，如 'page_1'"
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
                "required": ["page_key", "old_string", "new_string"]
            }
        }
    }
]


def build_incremental_system_prompt(design_system, page_list, first_page_name,
                                     existing_structure_summary, global_config=None):
    """构建增量生成 agentic loop 的系统提示。"""
    page_list_str = '\n'.join(
        f'- page_key: "{p["key"]}", page_name: "{p["name"]}"'
        for p in page_list
    )

    # 构建主色强调
    color_constraints = ''
    if global_config:
        primary = global_config.get('primaryColor', '')
        secondary = global_config.get('secondaryColor', '')
        bg_mode = '浅色' if global_config.get('backgroundMode', 'light') == 'light' else '深色'
        component_style = global_config.get('componentStyle', '')
        if primary:
            color_constraints += f"\n- **主色（--color-primary）必须使用 {primary}**，所有主要按钮、链接、高亮、选中状态都使用此色"
        if secondary:
            color_constraints += f"\n- **强调色（--color-secondary）必须使用 {secondary}**，辅助操作、成功状态等使用此色"
        if bg_mode:
            color_constraints += f"\n- **背景模式**: {bg_mode}"
        if component_style:
            color_constraints += f"\n- **组件风格**: {component_style}"

    return f"""你是一个专业的前端开发者，正在向一个已有的多页 HTML 原型中逐页添加内容。

## 设计系统（必须使用这些 CSS 变量保持视觉一致）
```css
{design_system.get('css_variables', '')}
```

## 配色约束（严格遵守）
{color_constraints if color_constraints else "使用设计系统中的 CSS 变量，保持与第 1 页风格一致。"}

## 需要生成的页面（共 {len(page_list)} 个）
{page_list_str}

## 当前状态
- 第 1 页（"{first_page_name}"）已生成并存在于文件中，包含完整的侧边栏导航和 Vue 3 路由
- 你需要按顺序为上述每个页面调用 `add_page` 工具
- 所有页面共享同一个 currentPage 状态，通过 v-show 切换显示
- 页面内可通过全局函数 `navigateTo('page_N')` 跳转到其他页面（如按钮点击后跳转）

## 现有文件结构摘要
{existing_structure_summary}

## 工作方式
1. 对每个页面调用一次 `add_page`，传入该页面的内容
2. `html_content` 只包含页面主体内容区域——不要包含 <html>、<head>、<body>、侧边栏、导航
3. 使用设计系统 CSS 变量（如 --color-primary）保持风格一致
4. 所有元素 ID 必须以 page_key 为前缀避免冲突，如 id="page_1_search_input"
5. `script_content` 使用原生 JavaScript（不要创建 Vue 实例，父应用已存在）
6. 生成真实的中文示例数据，不要用 Lorem ipsum
7. 内容宽度自适应，不设 max-width 限制
8. 完成一个页面后继续下一个，不要等待确认
9. 如果不确定现有文件结构，先调用 `read_current_file` 查看
10. 如果需要在页面内跳转到其他页面，使用 `navigateTo('page_N')` 全局函数

## 输出质量要求
- 每个页面必须是完整的、可直接渲染的 HTML 片段
- 表格必须有完整的数据行（至少 8-10 行）
- 表单必须有完整的字段、验证逻辑和提交处理
- 交互元素（按钮、下拉框、标签页）必须有对应的 JS 事件处理
"""


# ==================== Phase 5: 多轮编排器 ====================

class MultiRoundGenerator:
    """多轮生成编排器 — 协调 Round 1/2/3 的执行"""

    def __init__(self, server_instance, project_id, project_folder):
        self.server = server_instance
        self.project_id = project_id
        self.project_folder = project_folder

        self.design_system = {'css_variables': '', 'component_specs': ''}
        self.page_fragments = []
        self.page_names = []
        self.cross_page_spec = {}
        self.conversation_messages = []   # 多轮对话历史
        self.generation_config = {}       # 前端传来的高级配置
        self.base_html = ''               # 第一页生成的 HTML（用于样式参考）
        self._template_raw_frame_html = ''  # 模板外框架 HTML（iframe/sidebar 布局时使用）
        self._template_sidebar_meta = None  # 模板侧边栏元数据

    def run(self, prompt, pages_data, images, global_config,
            template_tokens='', template_html_summary='',
            template_css_path='', template_is_iframe=False,
            template_frame_html='', template_raw_frame_html='',
            template_layout_type='plain', template_sidebar_meta=None,
            mode='full', confirmed_spec=None):
        """执行多轮生成，返回最终 HTML

        Args:
            prompt: 原始完整 prompt（用于参考）
            pages_data: 页面规格列表
            images: 参考图片 base64 列表
            global_config: 全局设计配置
            template_*: 模板相关参数
            mode: 'spec_only' | 'incremental' | 'full'
            confirmed_spec: 用户确认的规格（incremental 模式必需）

        Returns:
            str | dict: 最终 HTML 或 spec dict
        """
        self.page_names = [p.get('name', f'页面{i+1}') for i, p in enumerate(pages_data)]
        self._layout_type = template_layout_type
        self._template_raw_frame_html = template_raw_frame_html or ''
        self._template_sidebar_meta = template_sidebar_meta
        self.conversation_messages = []

        # === 模式分发 ===
        if mode == 'spec_only':
            return self._generate_spec_only(
                pages_data, global_config, template_tokens,
                template_html_summary, template_frame_html,
                template_layout_type, template_sidebar_meta
            )

        if mode == 'incremental':
            return self._run_incremental(
                prompt, pages_data, images, global_config,
                template_tokens, template_html_summary,
                template_css_path, template_is_iframe,
                template_frame_html, template_raw_frame_html,
                template_layout_type, template_sidebar_meta,
                confirmed_spec
            )

        # === 原有 full 模式（向后兼容） ===
        self.page_names = [p.get('name', f'页面{i+1}') for i, p in enumerate(pages_data)]
        self._layout_type = template_layout_type
        self._template_raw_frame_html = template_raw_frame_html or ''
        self._template_sidebar_meta = template_sidebar_meta
        self.conversation_messages = []

        # ---- Round 0: 跨页规格 ----
        self.cross_page_spec = {}

        if not should_skip_spec_round(pages_data, self.generation_config):
            self._update_phase(0, 'spec_generation', 'running')
            logger.info("[多轮] Round 0: 生成跨页规格...")

            spec_prompt = build_spec_prompt(
                pages_data=pages_data,
                global_config=global_config,
                layout_type=template_layout_type,
                template_design_tokens=template_tokens,
                template_html_summary=template_html_summary,
                template_frame_html=template_frame_html,
                template_sidebar_meta=template_sidebar_meta
            )
            spec_response = self._call_ai_streaming(spec_prompt, [])
            self.cross_page_spec = extract_spec_from_response(spec_response)

            if self.cross_page_spec:
                self._send_event('artifact', {
                    'name': 'cross-page-spec.json',
                    'content': json.dumps(self.cross_page_spec, ensure_ascii=False, indent=2),
                    'language': 'json',
                    'source': 'ai_generated'
                })

            self._update_phase(0, 'spec_generation', 'done')

            if self._is_cancelled():
                return None

        # ---- Round 1: 设计系统 ----
        self._update_phase(1, 'design_system', 'running')

        skip_round1 = should_skip_design_system_round(template_tokens, global_config)
        if skip_round1:
            logger.info("[多轮] Round 1 跳过：模板令牌充足，直接转为 CSS 变量")
            css_vars = tokens_to_css_variables(template_tokens, global_config)
            self.design_system = {'css_variables': css_vars, 'component_specs': ''}
            self._send_event('artifact', {
                'name': 'design-system.css',
                'content': css_vars,
                'language': 'css',
                'source': 'template_tokens'
            })
        else:
            logger.info("[多轮] Round 1: 生成设计系统...")
            ds_prompt = build_design_system_prompt(global_config, template_tokens, template_html_summary)
            ds_response = self._call_ai_streaming(ds_prompt, [])
            self.design_system = extract_design_system_from_response(ds_response)
            self._send_event('artifact', {
                'name': 'design-system.css',
                'content': self.design_system['css_variables'],
                'language': 'css',
                'source': 'ai_generated'
            })

        self._update_phase(1, 'design_system', 'done')

        # ---- Round 1.5: 生成模板样式参考卡（用于后续页面精简注入） ----
        self.template_style_card = build_template_style_card(
            template_frame_html=template_frame_html,
            template_design_tokens=template_tokens,
            template_layout_type=template_layout_type
        )

        # 取消检查
        if self._is_cancelled():
            return None

        # ---- Round 2: 逐页生成（多轮对话） ----
        total = len(pages_data)

        # Route B 多文件架构：所有项目统一使用独立文件
        is_route_b = True

        # Route B: 创建 pages/ 目录
        if is_route_b:
            ensure_project_directory_structure(self.project_folder)
            logger.info(f"[多轮] Route B 多文件架构：pages 目录已创建")

        for i, page in enumerate(pages_data):
            page_name = page.get('name', f'页面{i+1}')
            logger.info(f"[多轮] Round 2: 页面 {i+1}/{total} — {page_name}")
            self._update_phase(2, f'page_{i}', 'running', label=page_name,
                               progress={'current': i+1, 'total': total})

            if self._is_cancelled():
                logger.info(f"[多轮] 取消于页面 {i+1}/{total}")
                return None

            try:
                # 构建 spec 摘要
                spec_summary = ''
                if self.cross_page_spec:
                    spec_summary = build_spec_summary_for_page(self.cross_page_spec, i)

                # 获取该页面的参考图片
                page_images = self._get_page_images(page, images)

                # Route B: standalone=True 让 AI 生成完整独立页面
                page_prompt = build_single_page_prompt(
                    page_spec=page,
                    design_system=self.design_system,
                    page_index=i,
                    total_pages=total,
                    global_config=global_config,
                    template_css_path=template_css_path,
                    is_iframe_layout=template_is_iframe,
                    template_design_tokens=template_tokens,
                    template_html_summary=template_html_summary,
                    template_frame_html=template_frame_html,
                    template_layout_type=getattr(self, '_layout_type', 'plain'),
                    cross_page_spec_summary=spec_summary,
                    template_style_card=self.template_style_card,
                    include_full_template=(i == 0),
                    standalone=is_route_b,
                )

                # 多轮对话模式
                self.conversation_messages.append({"role": "user", "content": page_prompt})

                # 压缩检查
                self._maybe_compact()

                page_response = self._call_ai_streaming_with_history(
                    self.conversation_messages, page_images
                )
                self.conversation_messages.append({"role": "assistant", "content": page_response})

                # 提取页面 HTML
                if is_route_b:
                    page_html = extract_complete_page_html(page_response)
                    if not page_html or len(page_html) < 100:
                        raise Exception(f"页面「{page_name}」生成失败：AI 未返回有效内容")

                    # 直接保存到 pages/ 目录
                    page_filename = f"page_{i}_{page_name}.html"
                    page_path = os.path.join(self.project_folder, 'pages', page_filename)
                    # 修正相对路径：pages/ 子目录中的页面需要 ../template/ 而非 template/
                    page_html = page_html.replace('href="template/template.css"', 'href="../template/template.css"')
                    page_html = page_html.replace("href='template/template.css'", "href='../template/template.css'")
                    with open(page_path, 'w', encoding='utf-8') as f:
                        f.write(page_html)
                    logger.info(f"[多轮] Route B: 已保存 {page_filename} ({len(page_html)} 字符)")

                    self.page_fragments.append(page_html)
                else:
                    # 原有模式：提取片段
                    fragment = extract_page_fragment(page_response, page_name)
                    if not fragment or len(fragment) < 50:
                        raise Exception(f"页面「{page_name}」生成失败：AI 未返回有效内容")
                    self.page_fragments.append(fragment)

                self._send_event('preview', {'page': page_name, 'html_fragment': self.page_fragments[-1]})

                # 页面审查 + 自动修复（确保页面能正常打开、无报错）
                try:
                    page_spec = self._build_page_spec(page_name)
                    fixed_html, review_edits, review_summary = self._review_and_fix_page(
                        self.page_fragments[-1], page_name, page_spec=page_spec)
                    if review_edits > 0:
                        self.page_fragments[-1] = fixed_html
                        # 同步更新已保存的文件
                        if is_route_b and 'page_filename' in dir():
                            review_page_path = os.path.join(self.project_folder, 'pages', page_filename)
                            with open(review_page_path, 'w', encoding='utf-8') as rf:
                                rf.write(fixed_html)
                        logger.info(f"[多轮] 页面 {page_name} 审查修复 {review_edits} 处")
                except Exception as review_ex:
                    logger.warning(f"[多轮] 页面 {page_name} 审查异常（不影响结果）: {review_ex}")

            except Exception as e:
                logger.error(f"[多轮] 页面 {i+1} 生成失败: {e}")
                error_html = (
                    f'<div class="p-8 text-center text-gray-500">'
                    f'  <i class="fas fa-exclamation-triangle text-3xl text-orange-400 mb-4" style="display:block"></i>'
                    f'  <h3 class="text-lg font-medium">页面「{page_name}」生成失败</h3>'
                    f'  <p class="text-sm mt-2">{str(e)[:200]}</p>'
                    f'</div>'
                )
                self.page_fragments.append(error_html)
                self._send_event('error', {'message': str(e), 'page': page_name})

            self._update_phase(2, f'page_{i}', 'done', label=page_name,
                               progress={'current': i+1, 'total': total})

            # 保存中间状态
            self._save_intermediate()

        # 取消检查
        if self._is_cancelled():
            return None

        # ---- Round 3: 导航框架（Route B）或 组装（原有模式） ----
        self._update_phase(3, 'assembly', 'running')

        if is_route_b:
            # Route B: 生成 index.html 导航框架
            raw_frame_html = getattr(self, '_template_raw_frame_html', '')
            layout_type_attr = getattr(self, '_layout_type', 'plain')

            if layout_type_attr in ('iframe', 'sidebar') and raw_frame_html:
                # iframe/sidebar 模板：使用真实模板框架，src 替换为 pages/ 文件引用
                logger.info(f"[多轮] Route B: 使用模板框架，iframe src 指向 pages/ 文件...")
                first_page_file = f"pages/page_0_{self.page_names[0]}.html"
                final_html = raw_frame_html
                # 将 srcdoc="..." 替换为 src="pages/page_0_xxx.html"
                srcdoc_pattern = re.compile(r'(srcdoc\s*=\s*")([^"]*)"', re.DOTALL)
                if srcdoc_pattern.search(final_html):
                    final_html = srcdoc_pattern.sub(f'src="{first_page_file}"', final_html, count=1)
                    logger.info(f"[多轮] Route B: iframe srcdoc 已替换为 src={first_page_file}")
                else:
                    # 没有 srcdoc，尝试找 <iframe 并设置 src
                    iframe_pattern = re.compile(r'(<iframe[^>]*?)(></iframe>|/>)', re.DOTALL)
                    m = iframe_pattern.search(final_html)
                    if m:
                        tag = m.group(1)
                        if 'src=' not in tag:
                            final_html = final_html[:m.start()] + tag + f' src="{first_page_file}"' + final_html[m.end(1):]
                            logger.info(f"[多轮] Route B: iframe 已添加 src={first_page_file}")
            else:
                # 无模板或 plain 模式：生成通用导航框架
                logger.info(f"[多轮] Route B: 生成通用导航框架...")
                page_specs = [{'name': n} for n in self.page_names]
                final_html = build_lightweight_index_frame(
                    self.project_folder, self.page_names, page_specs,
                    self.design_system.get('css_variables', ''),
                    layout_type=layout_type_attr,
                    global_config=global_config,
                    cross_page_spec=self.cross_page_spec,
                )
            index_path = os.path.join(self.project_folder, 'index.html')
            with open(index_path, 'w', encoding='utf-8') as f:
                f.write(final_html)
            logger.info(f"[多轮] Route B: index.html 已生成 ({len(final_html)} 字符)")

            # 审查 index.html 导航框架
            try:
                fixed_final, final_edits, _ = self._review_and_fix_page(
                    final_html, '导航框架')
                if final_edits > 0:
                    with open(index_path, 'w', encoding='utf-8') as rf:
                        rf.write(fixed_final)
                    logger.info(f"[多轮] index.html 审查修复 {final_edits} 处")
            except Exception as final_review_ex:
                logger.warning(f"[多轮] index.html 审查异常: {final_review_ex}")
        else:
            # 原有模式：组装为单文件
            layout_type = getattr(self, '_layout_type', 'plain')
            raw_frame_html = getattr(self, '_template_raw_frame_html', '')
            sidebar_meta = getattr(self, '_template_sidebar_meta', None)

            # iframe/sidebar 模式且有模板框架：AI 内容注入模板框架（使用模板的真实侧边栏）
            if layout_type in ('iframe', 'sidebar') and raw_frame_html:
                logger.info(f"[多轮] {layout_type} 模式：将 AI 内容注入模板框架...")
                # 单页：直接取片段；多页：用 assemble_multi_page_html 生成 v-show 切换的内容
                if len(self.page_fragments) == 1:
                    ai_content = self.page_fragments[0]
                else:
                    # 多页：生成带 v-show 切换的纯内容（不含侧边栏）
                    ai_content = assemble_multi_page_html(
                        page_fragments=self.page_fragments,
                        design_system_css=self.design_system['css_variables'],
                        page_names=self.page_names,
                        global_config=global_config,
                        template_css_path=template_css_path,
                        layout_type='plain',  # 强制 plain，避免再生成侧边栏
                        project_dir=self.project_folder,
                        output_format='single-file'
                    )
                page_name = self.page_names[0] if self.page_names else ''
                final_html = self.server.assemble_iframe_html(
                    ai_content, raw_frame_html, template_css_path,
                    sidebar_meta, page_name
                )
                logger.info(f"[多轮] 模板框架注入完成: {len(final_html)} 字符")
            else:
                # plain 模式或无框架：使用 assemble_multi_page_html 生成完整页面
                logger.info(f"[多轮] 组装 {len(self.page_fragments)} 个页面...")
                final_html = assemble_multi_page_html(
                    page_fragments=self.page_fragments,
                    design_system_css=self.design_system['css_variables'],
                    page_names=self.page_names,
                    global_config=global_config,
                    template_css_path=template_css_path,
                    layout_type=layout_type,
                    project_dir=self.project_folder,
                    output_format='single-file'
                )

        self._send_event('complete', {'totalPages': len(self.page_fragments)})
        self._update_phase(3, 'assembly', 'done')

        logger.info(f"[多轮] 生成完成: {len(self.page_fragments)} 页, {len(final_html)} 字符")
        return final_html

    # ---- 增量生成模式 ----

    def _generate_spec_only(self, pages_data, global_config, template_tokens,
                            template_html_summary, template_frame_html,
                            template_layout_type, template_sidebar_meta,
                            adjustment_note='', previous_spec=None):
        """只生成跨页规格，供用户确认。

        Returns:
            dict: {'spec': dict, 'canSkip': bool, 'rawResponse': str}
        """
        if should_skip_spec_round(pages_data, self.generation_config):
            logger.info("[Spec] 跳过规格生成（单页或简单项目）")
            return {'spec': {}, 'canSkip': True, 'rawResponse': ''}

        self._update_phase(0, 'spec_generation', 'running')
        logger.info("[Spec] 生成跨页规格...")

        spec_prompt = build_spec_prompt(
            pages_data=pages_data,
            global_config=global_config,
            layout_type=template_layout_type,
            template_design_tokens=template_tokens,
            template_html_summary=template_html_summary,
            template_frame_html=template_frame_html,
            template_sidebar_meta=template_sidebar_meta,
            adjustment_note=adjustment_note,
            previous_spec=previous_spec
        )
        spec_response = self._call_ai_streaming(spec_prompt, [])
        spec = extract_spec_from_response(spec_response)

        self._update_phase(0, 'spec_generation', 'done')

        return {'spec': spec, 'canSkip': False, 'rawResponse': spec_response}

    # _extract_fragments_from_single_html 已移除
    # Route B: 页面直接生成为独立文件，不再需要从单文件中提取片段

    def _run_incremental(self, prompt, pages_data, images, global_config,
                         template_tokens, template_html_summary,
                         template_css_path, template_is_iframe,
                         template_frame_html, template_raw_frame_html,
                         template_layout_type, template_sidebar_meta,
                         confirmed_spec):
        """增量生成模式：Route B 多文件架构。

        流程：
        1. Round 1: 生成设计系统
        2. 第 1 页：生成完整独立 HTML → 写入 pages/page_0_xxx.html
        3. 第 2-N 页：agentic loop → 写入 pages/page_N_xxx.html
        4. 生成 index.html 导航框架（sidebar + iframe src 切换）
        """
        html_path = os.path.join(self.project_folder, 'index.html')
        total = len(pages_data)
        is_route_b = True

        # Route B: 创建 pages/ 目录
        if is_route_b:
            ensure_project_directory_structure(self.project_folder)
            logger.info(f"[增量] Route B 多文件架构：pages 目录已创建")

        # ---- Round 1: 设计系统 ----
        self._update_phase(1, 'design_system', 'running')

        skip_round1 = should_skip_design_system_round(template_tokens, global_config)
        if skip_round1:
            logger.info("[增量] Round 1 跳过：模板令牌充足")
            css_vars = tokens_to_css_variables(template_tokens, global_config)
            self.design_system = {'css_variables': css_vars, 'component_specs': ''}
            self._send_event('artifact', {
                'name': 'design-system.css',
                'content': css_vars,
                'language': 'css',
                'source': 'template_tokens'
            })
        else:
            logger.info("[增量] Round 1: 生成设计系统...")
            ds_prompt = build_design_system_prompt(global_config, template_tokens, template_html_summary)
            ds_response = self._call_ai_streaming(ds_prompt, [])
            self.design_system = extract_design_system_from_response(ds_response)
            self._send_event('artifact', {
                'name': 'design-system.css',
                'content': self.design_system['css_variables'],
                'language': 'css',
                'source': 'ai_generated'
            })

        self._update_phase(1, 'design_system', 'done')

        if self._is_cancelled():
            return None

        # ---- 识别框架页面和业务页面 ----
        framework_pages = []  # [(original_index, page_spec)]
        business_pages = []   # [(original_index, page_spec)]

        for i, page in enumerate(pages_data):
            if is_framework_page(page):
                framework_pages.append((i, page))
            else:
                business_pages.append((i, page))

        logger.info(f"[框架页面] 检测到 {len(framework_pages)} 个框架页面, "
                    f"{len(business_pages)} 个业务页面")

        # ---- Round 1.5: 生成框架页面并构建 index.html ----
        framework_pages_html = {}

        if framework_pages:
            self._update_phase(1, 'framework_pages', 'running',
                               label=f'生成 {len(framework_pages)} 个框架页面')

            def _gen_framework_page(orig_idx, page_spec):
                """单个框架页面生成（供并行调用）"""
                pn = page_spec.get('name', f'页面{orig_idx}')
                logger.info(f"[框架页面] 生成: {pn}")
                try:
                    pp = build_framework_page_prompt(
                        page_spec=page_spec,
                        design_system=self.design_system,
                        global_config=global_config,
                    )
                    pi = self._get_page_images(page_spec, images)
                    pr = self._call_ai_streaming(pp, pi)
                    ph = extract_page_fragment(pr, pn)

                    if ph and len(ph) > 30:
                        try:
                            fixed_fw, fw_edits, _ = self._review_and_fix_page(ph, pn, page_spec=self._build_page_spec(pn))
                            if fw_edits > 0:
                                ph = fixed_fw
                                logger.info(f"[框架页面] {pn} 审查修复 {fw_edits} 处")
                        except Exception as fw_review_ex:
                            logger.warning(f"[框架页面] {pn} 审查异常: {fw_review_ex}")
                        logger.info(f"[框架页面] {pn} 生成成功 ({len(ph)} 字符)")
                        return (pn, ph)
                    else:
                        logger.warning(f"[框架页面] {pn} 内容过短，跳过")
                        return (pn, None)
                except Exception as e:
                    logger.error(f"[框架页面] {pn} 生成失败: {e}")
                    self._send_event('error', {'message': str(e), 'page': pn})
                    return (pn, None)

            if len(framework_pages) <= 1:
                # 单个框架页：直接生成
                for orig_idx, page_spec in framework_pages:
                    pn, ph = _gen_framework_page(orig_idx, page_spec)
                    if ph:
                        framework_pages_html[pn] = ph
            else:
                # 多个框架页：并行生成
                logger.info(f"[框架页面] 并行生成 {len(framework_pages)} 个框架页")
                with ThreadPoolExecutor(max_workers=min(len(framework_pages), 2)) as fw_executor:
                    fw_futures = {
                        fw_executor.submit(_gen_framework_page, oi, ps): ps.get('name', '')
                        for oi, ps in framework_pages
                    }
                    for fw_future in as_completed(fw_futures):
                        pn, ph = fw_future.result()
                        if ph:
                            framework_pages_html[pn] = ph

            self._update_phase(1, 'framework_pages', 'done')

        # ---- 框架页面完成后立即生成 index.html（让用户可以预览） ----
        if framework_pages_html and is_route_b:
            try:
                fw_business_names = [p.get('name') for _, p in business_pages]
                fw_index_html = build_index_with_framework_pages(
                    framework_pages_html=framework_pages_html,
                    design_system_css=self.design_system.get('css_variables', ''),
                    business_page_names=fw_business_names,
                    global_config=global_config,
                    cross_page_spec=self.cross_page_spec,
                )
                fw_index_path = os.path.join(self.project_folder, 'index.html')
                with open(fw_index_path, 'w', encoding='utf-8') as fw_f:
                    fw_f.write(fw_index_html)
                logger.info(f"[框架页面] index.html 已提前生成 ({len(fw_index_html)} 字符)，用户可预览")
            except Exception as fw_idx_err:
                logger.warning(f"[框架页面] 提前生成 index.html 失败（不影响后续）: {fw_idx_err}")

        # 保存中间状态
        self.page_names = [p.get('name', f'页面{i+1}') for i, p in enumerate(pages_data)]
        self._save_incremental_state()

        # ---- Round 2: 逐页生成业务页面 ----
        business_total = len(business_pages)

        for bi, (orig_idx, page) in enumerate(business_pages):
            page_name = page.get('name', f'页面{orig_idx}')
            logger.info(f"[增量] 业务页面 {bi+1}/{business_total} — {page_name}")
            self._update_phase(2, f'page_{bi}', 'running', label=page_name,
                               progress={'current': bi+1, 'total': business_total})

            if self._is_cancelled():
                logger.info(f"[增量] 取消于业务页面 {bi+1}/{business_total}")
                return None

            try:
                # 构建当前页面的 spec 摘要
                spec_summary = ''
                if confirmed_spec:
                    spec_summary = build_spec_summary_for_page(confirmed_spec, orig_idx)

                page_images = self._get_page_images(page, images)

                if bi == 0:
                    # === 第 1 个业务页面 ===
                    if is_route_b:
                        # Route B: 生成完整独立页面
                        page_prompt = build_single_page_prompt(
                            page_spec=page,
                            design_system=self.design_system,
                            page_index=bi,
                            total_pages=business_total,
                            global_config=global_config,
                            template_css_path=template_css_path,
                            is_iframe_layout=template_is_iframe,
                            template_design_tokens=template_tokens,
                            template_html_summary=template_html_summary,
                            template_frame_html=template_frame_html,
                            template_layout_type=getattr(self, '_layout_type', template_layout_type),
                            cross_page_spec_summary=spec_summary,
                            template_style_card=getattr(self, 'template_style_card', ''),
                            include_full_template=True,
                            standalone=True,
                        )
                        page_response = self._call_ai_streaming(page_prompt, page_images)
                        first_page_html = extract_complete_page_html(page_response)
                        if not first_page_html or len(first_page_html) < 100:
                            raise Exception(f"页面「{page_name}」生成失败：AI 未返回有效内容")

                        # 保存到 pages/ 目录
                        page_filename = f"page_{bi}_{page_name}.html"
                        page_path = os.path.join(self.project_folder, 'pages', page_filename)
                        with open(page_path, 'w', encoding='utf-8') as f:
                            f.write(first_page_html)
                        logger.info(f"[增量] Route B: 已保存 {page_filename} ({len(first_page_html)} 字符)")
                        self.base_html = first_page_html
                    else:
                        # 单页面：使用原有逻辑
                        first_page_html = self._generate_first_page_complete(
                            page_spec=page,
                            design_system=self.design_system,
                            page_index=0,
                            total_pages=business_total,
                            global_config=global_config,
                            template_css_path=template_css_path,
                            template_is_iframe=template_is_iframe,
                            template_tokens=template_tokens,
                            template_html_summary=template_html_summary,
                            template_frame_html=template_frame_html,
                            template_layout_type=template_layout_type,
                            page_images=page_images,
                            spec_summary=spec_summary
                        )
                        with open(html_path, 'w', encoding='utf-8') as f:
                            f.write(first_page_html)
                        self.base_html = first_page_html

                    self._send_event('page_written', {
                        'page': page_name, 'index': bi,
                        'path': page_path if is_route_b else html_path,
                        'size': len(first_page_html)
                    })

                    # 发送 preview 事件让前端立即渲染页面到画布
                    self._send_event('preview', {
                        'page': page_name,
                        'html_fragment': first_page_html
                    })

                    # 页面审查 + 自动修复
                    try:
                        fixed_first, review_edits, _ = self._review_and_fix_page(
                            first_page_html, page_name, page_spec=self._build_page_spec(page_name))
                        if review_edits > 0:
                            first_page_html = fixed_first
                            self.base_html = fixed_first
                            # 更新已保存的文件
                            save_target = page_path if is_route_b else html_path
                            with open(save_target, 'w', encoding='utf-8') as rf:
                                rf.write(fixed_first)
                            logger.info(f"[增量] 页面 {page_name} 审查修复 {review_edits} 处")
                    except Exception as review_ex:
                        logger.warning(f"[增量] 页面 {page_name} 审查异常: {review_ex}")

                    # 保存中间状态
                    self.page_fragments = [first_page_html]
                    self._save_incremental_state()

                else:
                    # === 第 2-N 个业务页面：由 agentic loop 统一处理 ===
                    pass

                self._update_phase(2, f'page_{bi}', 'done', label=page_name,
                                   progress={'current': bi+1, 'total': business_total})

            except Exception as e:
                logger.error(f"[增量] 业务页面 {bi+1} 生成失败: {e}")
                self._send_event('error', {'message': str(e), 'page': page_name})

        # ---- Round 3: 生成后续业务页面 ----
        if business_total > 1:
            self._update_phase(3, 'agentic_pages', 'running',
                               label='生成后续业务页面')

            if is_route_b:
                # ===== Route B: 并行生成（每个页面独立文件，无依赖） =====
                first_biz_name = business_pages[0][1].get('name', '页面1')
                first_page_path = os.path.join(
                    self.project_folder, 'pages',
                    f"page_0_{first_biz_name}.html"
                )
                if os.path.exists(first_page_path):
                    with open(first_page_path, 'r', encoding='utf-8') as f:
                        existing_html = f.read()
                else:
                    existing_html = self.base_html or ''

                style_card = build_generated_style_card(existing_html)
                remaining_pages = business_pages[1:]
                max_workers = min(len(remaining_pages), 3)

                logger.info(f"[并行] Route B 并行生成 {len(remaining_pages)} 个页面，{max_workers} 并发")

                try:
                    with ThreadPoolExecutor(max_workers=max_workers) as executor:
                        futures = {}
                        for bi, (orig_idx, page) in enumerate(remaining_pages, start=1):
                            pname = page.get('name', f'页面{orig_idx}')
                            spec_sum = ''
                            if confirmed_spec:
                                spec_sum = build_spec_summary_for_page(confirmed_spec, orig_idx)
                            pimgs = self._get_page_images(page, images)

                            future = executor.submit(
                                self._generate_business_page_standalone,
                                page_spec=page,
                                page_idx=bi,
                                page_name=pname,
                                design_system=self.design_system,
                                global_config=global_config,
                                spec_summary=spec_sum,
                                page_images=pimgs,
                                style_card=style_card,
                            )
                            futures[future] = (bi, pname)

                        success_count = 0
                        fail_count = 0
                        for future in as_completed(futures):
                            bi, pname = futures[future]
                            try:
                                success, _, _, fpath = future.result()
                                if success:
                                    success_count += 1
                                    self._send_event('page_written', {
                                        'page': pname, 'index': bi,
                                        'path': fpath,
                                        'size': os.path.getsize(fpath) if fpath and os.path.exists(fpath) else 0
                                    })
                                else:
                                    fail_count += 1
                                    self._send_event('error', {
                                        'message': f'页面「{pname}」生成失败',
                                        'page': pname
                                    })
                            except Exception as e:
                                fail_count += 1
                                logger.error(f"[并行] 页面 {pname} 异常: {e}")
                                self._send_event('error', {
                                    'message': str(e), 'page': pname
                                })

                    logger.info(f"[并行] 完成: {success_count} 成功, {fail_count} 失败")
                    self._save_incremental_state()

                except Exception as e:
                    logger.error(f"[并行] 并行生成异常: {e}")
                    self._send_event('error', {'message': str(e)})
                    self._save_incremental_state()

            else:
                # ===== 单文件模式: 保持原有串行 agentic loop =====
                if not os.path.exists(html_path):
                    logger.error(f"[增量] 首页文件不存在: {html_path}")
                    self._send_event('error', {'message': '首页生成失败'})
                    existing_html = ''
                else:
                    with open(html_path, 'r', encoding='utf-8') as f:
                        existing_html = f.read()

                business_pages_data = [spec for _, spec in business_pages]
                page_images_map = {}
                spec_summaries_map = {}
                for bi in range(1, business_total):
                    orig_idx = business_pages[bi][0]
                    page_images_map[bi] = self._get_page_images(business_pages[bi][1], images)
                    if confirmed_spec:
                        spec_summaries_map[bi] = build_spec_summary_for_page(confirmed_spec, orig_idx)

                try:
                    final_html = self._run_agentic_pages(
                        pages_to_generate=business_pages_data[1:],
                        design_system=self.design_system,
                        global_config=global_config,
                        existing_html=existing_html,
                        html_path=html_path,
                        page_images_map=page_images_map,
                        spec_summaries=spec_summaries_map,
                        total_pages=business_total,
                        pages_data=business_pages_data
                    )
                    if final_html:
                        with open(html_path, 'w', encoding='utf-8') as f:
                            f.write(final_html)
                    self._save_incremental_state()
                except Exception as e:
                    logger.error(f"[增量] Agentic 生成失败: {e}")
                    self._send_event('error', {'message': str(e)})
                    self._save_incremental_state()

            self._update_phase(3, 'agentic_pages', 'done')

        # ---- 完成：生成 index.html 导航框架 ----
        logger.info(f"[增量] 完成，共 {total} 页")

        business_page_names = [p.get('name') for _, p in business_pages]

        if is_route_b:
            # Route B: 生成 index.html
            try:
                if framework_pages_html:
                    # 有框架页面：使用 build_index_with_framework_pages
                    index_html = build_index_with_framework_pages(
                        framework_pages_html=framework_pages_html,
                        design_system_css=self.design_system.get('css_variables', ''),
                        business_page_names=business_page_names,
                        global_config=global_config,
                        cross_page_spec=self.cross_page_spec,
                    )
                else:
                    # 无框架页面：使用 build_lightweight_index_frame
                    page_specs = [{'name': n} for n in business_page_names]
                    index_html = build_lightweight_index_frame(
                        self.project_folder, business_page_names, page_specs,
                        self.design_system.get('css_variables', ''),
                        layout_type=getattr(self, '_layout_type', template_layout_type),
                        global_config=global_config,
                        cross_page_spec=self.cross_page_spec,
                    )

                with open(html_path, 'w', encoding='utf-8') as f:
                    f.write(index_html)
                logger.info(f"[增量] Route B: index.html 已生成 ({len(index_html)} 字符)")

                # 审查 index.html 导航框架
                try:
                    fixed_index, idx_edits, _ = self._review_and_fix_page(
                        index_html, '导航框架')
                    if idx_edits > 0:
                        with open(html_path, 'w', encoding='utf-8') as rf:
                            rf.write(fixed_index)
                        logger.info(f"[增量] index.html 审查修复 {idx_edits} 处")
                except Exception as idx_review_ex:
                    logger.warning(f"[增量] index.html 审查异常: {idx_review_ex}")
            except Exception as e:
                logger.warning(f"[增量] index.html 生成失败: {e}")
                # 降级
                first_biz = os.path.join(self.project_folder, 'pages',
                                          f"page_0_{business_page_names[0]}.html") if business_page_names else None
                if first_biz and os.path.exists(first_biz):
                    with open(first_biz, 'r', encoding='utf-8') as f:
                        index_html = f.read()
                    with open(html_path, 'w', encoding='utf-8') as f:
                        f.write(index_html)
                else:
                    index_html = '<html><body><p>生成失败</p></body></html>'
                    with open(html_path, 'w', encoding='utf-8') as f:
                        f.write(index_html)
        else:
            # 单页面模式：index.html 已在 Round 2 写入
            pass

        # 通知完成（在 index.html 写入之后）
        self._send_event('complete', {'totalPages': total, 'path': html_path})

        with open(html_path, 'r', encoding='utf-8') as f:
            return f.read()

    def _generate_first_page_complete(self, page_spec, design_system, page_index,
                                     total_pages, global_config, template_css_path,
                                     template_is_iframe, template_tokens,
                                     template_html_summary, template_frame_html,
                                     template_layout_type, page_images, spec_summary):
        """生成第 1 页的完整 HTML（含侧边栏导航 + Vue 路由 + 第 1 页内容）。

        这是增量写入的起点。后续页面通过 agentic loop 的 add_page 工具添加，
        不再需要 PAGE_SLOT 占位或 INJECT_* 标记。
        """
        page_name = page_spec.get('name', '页面1')
        logger.info(f"[增量] 生成首页完整 HTML: {page_name}")

        # 构建页面列表（用于侧边栏菜单）—— 过滤掉 show_in_nav=false 的子页面
        nav_visible_indices = get_nav_visible_indices(self.cross_page_spec, len(self.page_names))
        page_list_defs = []
        for pi, pn in enumerate(self.page_names):
            nav_marker = '' if pi in nav_visible_indices else '（不在侧边栏中，通过其他页面跳转）'
            page_list_defs.append(f'   - page_key: "page_{pi}", 页面名: "{pn}"{nav_marker}')
        page_list_str = '\n'.join(page_list_defs)
        nav_visible_count = len(nav_visible_indices)

        # 构建 prompt
        if template_layout_type in ('sidebar', 'iframe') and (template_frame_html or template_tokens):
            page_prompt = build_single_page_prompt(
                page_spec=page_spec,
                design_system=design_system,
                page_index=0,
                total_pages=total_pages,
                global_config=global_config,
                template_css_path=template_css_path,
                is_iframe_layout=template_is_iframe,
                template_design_tokens=template_tokens,
                template_html_summary=template_html_summary,
                template_frame_html=template_frame_html,
                template_layout_type=template_layout_type,
                cross_page_spec_summary=spec_summary
            )
        else:
            page_prompt = build_single_page_prompt(
                page_spec=page_spec,
                design_system=design_system,
                page_index=0,
                total_pages=total_pages,
                global_config=global_config,
                template_css_path=template_css_path,
                is_iframe_layout=False,
                template_design_tokens='',
                template_html_summary='',
                template_frame_html='',
                template_layout_type='plain',
                cross_page_spec_summary=spec_summary
            )

        # 追加首页框架要求（所有模式共用）
        page_prompt += f"""

## 首页框架要求（必须包含）
这是多页应用的第一页。生成完整的单页应用 HTML，包含：

1. **侧边栏导航**：只列出 {nav_visible_count} 个主导航页面（标有"不在侧边栏中"的页面是子页面/详情页，不要在侧边栏菜单中显示它们）
   - 页面列表：
{page_list_str}
   - 当前页「{page_name}」高亮显示
   - 使用 Vue 3 的 `ref` 管理 currentPage 状态，初始值为 `"page_0"`
   - 点击菜单项设置 `currentPage = "page_N"`（N 为该页面的序号，从 0 开始）

2. **第 1 页内容**：在 `v-show="currentPage === 'page_0'"` 的 div 中显示完整内容
   - **必须完整实现第 1 页「{page_name}」的所有功能和视觉效果**，包括所有图表、表格、卡片、数据等
   - **严禁使用占位文本**（如"已有内容"、"待实现"等），必须输出完整的、可交互的页面
   - 只生成第 1 页的内容，不要为其他页面生成占位符
   - 后续页面会由系统自动注入

3. **postMessage 通信**：
   - 页面切换时通知父窗口：`window.parent.postMessage({{type:'pageChange', page: currentPage.value}}, '*')`
   - 监听父窗口导航指令：`window.addEventListener('message', e => {{ if(e.data?.type === 'navigateTo') currentPage.value = e.data.page; }})`
   - onMounted 时发送初始页面

4. **全局页面跳转函数（必须定义）**：
   在 `<script>` 中（Vue app 创建之前）定义全局函数：
   ```javascript
   // 暴露 currentPage ref 到 window，并定义全局 navigateTo
   // （后续页面中会使用 navigateTo('page_N') 进行跳转）
   window.navigateTo = function(page) {{
     if (window._vueCurrentPage) window._vueCurrentPage.value = page;
   }};
   ```
   在 Vue setup() 中将 currentPage ref 挂载到 `window._vueCurrentPage = currentPage;`
   同时在 `switchPage` 函数体中也要 `window._vueCurrentPage = currentPage;`（确保引用一致）

5. **宽度要求**：内容区域自适应占满右侧空间，不设 max-width 限制

输出完整的 HTML 文件。
"""

        # 调用 AI 生成
        page_response = self._call_ai_streaming(page_prompt, page_images)
        fragment = extract_page_fragment(page_response, page_name)

        if not fragment or len(fragment) < 50:
            raise Exception(f"首页「{page_name}」生成失败：AI 未返回有效内容")

        # 安全网：确保 fragment 以 <!DOCTYPE 或 <html 开头，而非代码围栏
        if fragment.startswith('```'):
            fragment = re.sub(r'^```(?:html|HTML)?\s*\n?', '', fragment)
            fragment = fragment.rstrip().rstrip('`').rstrip()
            logger.info(f"[增量] 已去除首页 fragment 中的代码围栏")

        return fragment

    # ---- 增量生成工具执行方法 ----

    def _execute_add_page(self, current_html, args, page_index, total_pages):
        """执行 add_page 工具调用。

        将新页面内容插入到 HTML 的正确位置。
        Returns: (updated_html, success, message)
        """
        page_key = args.get('page_key', '')
        page_name = args.get('page_name', '')
        html_content = args.get('html_content', '')
        css_content = args.get('css_content', '')
        script_content = args.get('script_content', '')

        if not page_key or not html_content:
            return current_html, False, "page_key 和 html_content 不能为空"

        # 0. 移除已有的同名 v-show 占位 div（AI 首页生成时常为后续页面创建占位符）
        cleaned = self._remove_existing_page_div(current_html, page_key)

        # 1. 创建页面区域
        page_section = (
            f'\n<!-- PAGE_{page_key} -->\n'
            f'<div v-show="currentPage === \'{page_key}\'" class="page-content-panel" '
            f'data-page-id="{page_key}">\n'
            f'{html_content}\n'
            f'</div>\n'
            f'<!-- END_PAGE_{page_key} -->\n'
        )

        # 2. 找到插入点（在已清理的 HTML 上操作）
        idx = self._find_page_insertion_point(cleaned)
        if idx is None:
            return cleaned, False, (
                "无法确定插入位置。请调用 read_current_file 查看文件结构后重试。"
            )

        updated = cleaned[:idx] + page_section + cleaned[idx:]

        # 3. 注入 CSS
        if css_content:
            style_block = f'\n/* === {page_name} 样式 === */\n{css_content}\n'
            style_close = updated.rfind('</style>')
            if style_close != -1:
                updated = updated[:style_close] + style_block + updated[style_close:]

        # 4. 注入 JS
        if script_content:
            script_block = (
                f'\n<!-- {page_name} 脚本 -->\n'
                f'<script>\n{script_content}\n</script>\n'
            )
            body_close = updated.rfind('</body>')
            if body_close != -1:
                updated = updated[:body_close] + script_block + updated[body_close:]

        # 5. 更新侧边栏（如需要）
        updated = self._ensure_sidebar_item(updated, page_key, page_name, page_index)

        msg = (f"页面 '{page_name}' ({page_key}) 添加成功。"
               f"{len(html_content)} 字符 HTML"
               + (f", {len(css_content)} 字符 CSS" if css_content else "")
               + (f", {len(script_content)} 字符 JS" if script_content else ""))

        return updated, True, msg

    def _find_page_insertion_point(self, html):
        """找到新页面 div 的插入位置。

        策略（优先级）：
        1. 最后一个 <!-- END_PAGE_N --> 标记之后
        2. <main class="main-content"> 对应的 </main> 之前（优先匹配 main 标签）
        3. <div class="main-content"> 对应的闭合 </div> 之前
        4. </main> 之前
        """
        # 策略 1：最后一个 END_PAGE 标记
        end_page_matches = list(re.finditer(r'<!-- END_PAGE_(\w+) -->', html))
        if end_page_matches:
            return end_page_matches[-1].end()

        mc_pos = html.find('class="main-content"')
        if mc_pos == -1:
            mc_pos = html.find("class='main-content'")

        if mc_pos != -1:
            # 检测容器标签类型：向前回溯找到开始标签名
            tag_name = 'div'  # 默认
            search_back = html[max(0, mc_pos - 50):mc_pos]
            main_match = re.search(r'<(main)\s', search_back)
            if main_match:
                tag_name = 'main'

            open_tag = '<' + tag_name
            close_tag = '</' + tag_name + '>'

            tag_close = html.find('>', mc_pos)
            if tag_close != -1:
                depth = 1
                pos = tag_close + 1
                while depth > 0 and pos < len(html):
                    next_open = html.find(open_tag, pos)
                    next_close = html.find(close_tag, pos)
                    if next_close == -1:
                        break
                    if next_open != -1 and next_open < next_close:
                        depth += 1
                        pos = next_open + len(open_tag)
                    else:
                        depth -= 1
                        if depth == 0:
                            return next_close
                        pos = next_close + len(close_tag)

        # 策略 4：</main> 兜底
        main_close = html.rfind('</main>')
        if main_close != -1:
            return main_close

        return None

    def _remove_existing_page_div(self, html, page_key):
        """移除已有的同名 v-show 页面 div（含嵌套子 div），防止 AI 首页占位符导致重复。

        AI 生成首页时经常忽略"不要为其他页面生成占位符"的指令，
        为所有页面创建 v-show div。后续 agentic loop 的 add_page
        会再创建同名 div，导致两个 div 同时可见、布局变形。

        Returns:
            str: 清理后的 HTML
        """
        # 查找所有 v-show 匹配此 page_key 的 <div> 开始位置
        pattern = re.compile(
            r'<div\s+v-show="currentPage\s*===\s*\'' + re.escape(page_key) + r'\'"[^>]*>'
        )
        match = pattern.search(html)
        if not match:
            return html

        # 用括号平衡找到对应的闭合 </div>
        div_start = match.start()
        tag_end = match.end()
        depth = 1
        pos = tag_end
        while depth > 0 and pos < len(html):
            next_open = html.find('<div', pos)
            next_close = html.find('</div>', pos)
            if next_close == -1:
                break
            if next_open != -1 and next_open < next_close:
                # 确认是真正的 <div 标签而非 <divxxx 或 </divxxx>
                after_open = html[next_open + 4:next_open + 5] if next_open + 4 < len(html) else ''
                if after_open in (' ', '>', '\n', '\t', '/'):
                    depth += 1
                pos = next_open + 4
            else:
                depth -= 1
                if depth == 0:
                    div_end = next_close + 6  # len('</div>')
                    logger.info(f"[Agent] 已移除 '{page_key}' 占位 div "
                                f"(pos {div_start}-{div_end}, {div_end - div_start} 字符)")
                    return html[:div_start] + html[div_end:]
                pos = next_close + 6

        # 未找到匹配的闭合标签，返回原 HTML
        logger.warning(f"[Agent] 找到 '{page_key}' 占位 div 开标签但未找到闭合标签，跳过清理")
        return html

    def _ensure_sidebar_item(self, html, page_key, page_name, page_index):
        """确保侧边栏包含此页面的菜单项。

        如果侧边栏已通过 v-for 包含所有页面（数据驱动），则不添加。
        如果页面标记为 show_in_nav=false，则不添加到侧边栏。
        如果是静态菜单且缺少此项，则追加。
        """
        # 如果 currentPage 比较已存在于某处，说明已在菜单或页面区域
        if f"currentPage === '{page_key}'" in html:
            return html

        # 检查此页面是否应显示在侧边栏导航中
        spec_pages = self.cross_page_spec.get('pages', []) if self.cross_page_spec else []
        if page_index < len(spec_pages):
            if not spec_pages[page_index].get('show_in_nav', True):
                logger.info(f"[Agent] 页面 '{page_name}' 标记为不在导航中，跳过侧边栏注入")
                return html

        # 查找侧边栏区域中的最后一个 </li>
        # 匹配 sidebar 到 </ul> 的区域
        sidebar_pattern = re.compile(
            r'<div[^>]*class="[^"]*sidebar[^"]*"[^>]*>.*?</ul>',
            re.DOTALL
        )
        sidebar_match = sidebar_pattern.search(html)
        if not sidebar_match:
            return html

        sidebar_section = sidebar_match.group(0)
        last_li_close = sidebar_section.rfind('</li>')
        if last_li_close == -1:
            return html

        icon_map = {
            0: 'fa-tachometer-alt', 1: 'fa-database', 2: 'fa-folder-open',
            3: 'fa-table', 4: 'fa-edit', 5: 'fa-chart-line',
            6: 'fa-hdd', 7: 'fa-clock', 8: 'fa-users', 9: 'fa-upload'
        }
        icon = icon_map.get(page_index, 'fa-file')

        new_item = (
            f'\n<li @click="currentPage = \'{page_key}\'"\n'
            f'    :class="[\'sidebar-menu-item\', currentPage === \'{page_key}\' ? \'active\' : \'\']">\n'
            f'  <i class="fas {icon}"></i>\n'
            f'  <span>{page_name}</span>\n'
            f'</li>'
        )

        abs_pos = sidebar_match.start() + last_li_close + len('</li>')
        return html[:abs_pos] + new_item + html[abs_pos:]

    def _execute_read_page(self, args):
        """执行 read_page 工具调用（Route B：读取 pages/ 目录下的页面文件）。"""
        pages_dir = os.path.join(self.project_folder, 'pages')
        page_key = args.get('page_key', '')

        if not page_key:
            # 返回页面列表摘要
            if not os.path.isdir(pages_dir):
                return "pages/ 目录不存在。"
            page_files = sorted([f for f in os.listdir(pages_dir) if f.endswith('.html')])
            if not page_files:
                return "pages/ 目录中没有页面文件。"
            summary_parts = [f"共 {len(page_files)} 个页面文件："]
            for pf in page_files:
                pf_path = os.path.join(pages_dir, pf)
                size = os.path.getsize(pf_path)
                summary_parts.append(f"  - {pf} ({size} 字符)")
            return '\n'.join(summary_parts)

        # 读取指定页面
        if not os.path.isdir(pages_dir):
            return "pages/ 目录不存在，当前为单文件项目，请使用 read_current_file。"

        page_file = None
        for pf in os.listdir(pages_dir):
            if pf.startswith(f'page_') and pf.endswith('.html'):
                match = re.match(r'page_(\d+)_', pf)
                if match and f'page_{page_key}_' in pf or match.group(1) == page_key:
                    page_file = pf
                    break

        if not page_file:
            # 模糊匹配
            for pf in os.listdir(pages_dir):
                if pf.endswith('.html') and page_key in pf:
                    page_file = pf
                    break

        if not page_file:
            available = [f for f in os.listdir(pages_dir) if f.endswith('.html')]
            return f"未找到 page_key '{page_key}' 对应的文件。可用页面：{available}"

        page_path = os.path.join(pages_dir, page_file)
        try:
            with open(page_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception as e:
            return f"读取文件出错: {e}"

        MAX_READ_CHARS = 80000
        if len(content) <= MAX_READ_CHARS:
            lines = content.split('\n')
            numbered = '\n'.join(f"L{i+1}: {line}" for i, line in enumerate(lines))
            return (
                f"文件 {page_file} 完整内容 "
                f"({len(content)} chars, {len(lines)} lines):\n"
                f"{numbered}"
            )
        else:
            return (
                f"文件 {page_file} 较大 ({len(content)} 字符)，"
                f"已返回前 {MAX_READ_CHARS} 字符。"
                f"如需查看完整内容，请缩小范围。"
            )

    def _execute_edit_page_file(self, args):
        """执行 edit_page 工具调用（Route B：编辑 pages/ 目录下的页面文件）。

        Returns: (applied: bool, message: str)
        """
        page_key = args.get('page_key', '')
        old_string = args.get('old_string', '')
        new_string = args.get('new_string', '')
        replace_all = args.get('replace_all', False)

        if not page_key:
            return False, "page_key 不能为空"
        if not old_string:
            return False, "old_string 不能为空"

        pages_dir = os.path.join(self.project_folder, 'pages')
        if not os.path.isdir(pages_dir):
            return False, "pages/ 目录不存在，当前为单文件项目，请使用 edit_current_file。"

        page_file = None
        for pf in os.listdir(pages_dir):
            if pf.endswith('.html') and page_key in pf:
                page_file = pf
                break

        if not page_file:
            return False, f"未找到 page_key '{page_key}' 对应的文件"

        page_path = os.path.join(pages_dir, page_file)
        try:
            with open(page_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception as e:
            return False, f"读取文件出错: {e}"

        # 精确匹配
        idx = content.find(old_string)
        if idx == -1:
            return False, f"未找到 old_string。首行: '{old_string.split(chr(10))[0][:60]}'"

        second_idx = content.find(old_string, idx + 1)
        if second_idx != -1 and not replace_all:
            return False, "old_string matches multiple locations. Provide more context or set replace_all=true."

        if replace_all:
            updated = content.replace(old_string, new_string)
        else:
            updated = content[:idx] + new_string + content[idx + len(old_string):]
        try:
            with open(page_path, 'w', encoding='utf-8') as f:
                f.write(updated)
            return True, f"编辑成功: {page_file}"
        except Exception as e:
            return False, f"写入文件出错: {e}"

    def _execute_read_current_file(self, html_path, args):
        """执行 read_current_file 工具调用。"""
        try:
            with open(html_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception as e:
            return f"读取文件出错: {e}"

        start_line = args.get('start_line')
        end_line = args.get('end_line')

        lines = content.split('\n')
        total_lines = len(lines)
        MAX_READ_CHARS = 150000

        if start_line and end_line:
            s = max(0, start_line - 1)
            e = min(len(lines), end_line)
            selected = lines[s:e]
            numbered = '\n'.join(
                f'{start_line + i:>6}→{line}'
                for i, line in enumerate(selected)
            )
            return (
                f"[index.html] lines {start_line}-{end_line} "
                f"({e - s + 1} lines, {total_lines} total):\n"
                f"{numbered}"
            )

        # 无行范围：返回带行号的全文或摘要
        if len(content) <= MAX_READ_CHARS:
            numbered = '\n'.join(f'{i+1:>6}→{line}' for i, line in enumerate(lines))
            return (
                f"[index.html] full content "
                f"({len(content)} chars, {total_lines} lines):\n"
                f"{numbered}"
            )
        else:
            result_lines = []
            used = 0
            for i, line in enumerate(lines):
                if used + len(line) + 10 > MAX_READ_CHARS:
                    result_lines.append(
                        f"\n[截断] 共 {total_lines} 行，已返回前 {i} 行。"
                        f"使用 start_line={i+1} 继续。"
                    )
                    break
                result_lines.append(f'{i+1:>6}→{line}')
                used += len(line) + 10
            return (
                f"[index.html] ({len(content)} chars, {total_lines} lines):\n"
                + '\n'.join(result_lines)
            )

    def _execute_edit_page(self, current_html, args):
        """执行 edit_page 工具调用。3层匹配逻辑。

        Returns: (updated_html, applied, message)
        """
        old_string = args.get('old_string', '')
        new_string = args.get('new_string', '')
        replace_all = args.get('replace_all', False)

        if not old_string:
            return current_html, False, "old_string 不能为空"

        # 第1层：精确匹配
        idx = current_html.find(old_string)
        if idx != -1:
            second_idx = current_html.find(old_string, idx + 1)
            if second_idx != -1 and not replace_all:
                return current_html, False, (
                    "old_string matches multiple locations. "
                    "Provide more context for unique match, "
                    "or set replace_all=true."
                )
            if replace_all:
                updated = current_html.replace(old_string, new_string)
            else:
                updated = current_html[:idx] + new_string + current_html[idx + len(old_string):]
            return updated, True, "Edit applied successfully."

        # 第2层：归一化空白匹配
        norm_old = re.sub(r'\s+', ' ', old_string).strip()
        norm_html = re.sub(r'\s+', ' ', current_html)
        norm_idx = norm_html.find(norm_old)
        if norm_idx != -1:
            # 尝试通过字符计数映射回原始位置
            # 简化方法：在原始 HTML 中找到包含首尾关键词的区域
            old_words = norm_old.split()
            if len(old_words) >= 2:
                first_word = old_words[0]
                last_word = old_words[-1]
                for i, line in enumerate(current_html.split('\n')):
                    if first_word in line:
                        for j, line2 in enumerate(current_html.split('\n')[i:], i):
                            if last_word in line2:
                                # 找到候选范围
                                lines = current_html.split('\n')
                                candidate = '\n'.join(lines[i:j+1])
                                norm_candidate = re.sub(r'\s+', ' ', candidate).strip()
                                if norm_candidate == norm_old:
                                    updated = (
                                        '\n'.join(lines[:i]) + '\n'
                                        + new_string + '\n'
                                        + '\n'.join(lines[j+1:])
                                    )
                                    return updated, True, "编辑成功应用（归一化匹配）。"
                # 直接在归一化文本上替换，然后尝试反向映射
                updated_norm = norm_html.replace(norm_old, new_string, 1)
                if updated_norm != norm_html:
                    # 简化：使用行级替换
                    pass

        # 第3层：逐行去除空白匹配
        old_lines = old_string.split('\n')
        html_lines = current_html.split('\n')
        stripped_old = [l.strip() for l in old_lines]
        stripped_html = [l.strip() for l in html_lines]

        match_start = -1
        for i in range(len(stripped_html) - len(stripped_old) + 1):
            if stripped_html[i:i + len(stripped_old)] == stripped_old:
                # 检查唯一性
                has_dup = False
                for j in range(i + 1, len(stripped_html) - len(stripped_old) + 1):
                    if stripped_html[j:j + len(stripped_old)] == stripped_old:
                        has_dup = True
                        break
                if not has_dup:
                    match_start = i
                break

        if match_start != -1:
            match_end = match_start + len(old_lines)
            updated = (
                '\n'.join(html_lines[:match_start]) + '\n'
                + new_string + '\n'
                + '\n'.join(html_lines[match_end:])
            )
            return updated, True, "编辑成功应用（去除空白匹配）。"

        # 所有层失败：诊断信息
        old_stripped_lines = old_string.strip().split('\n')
        first_line = old_stripped_lines[0].strip() if old_stripped_lines else ''
        last_line = old_stripped_lines[-1].strip() if old_stripped_lines else ''

        diag = ["编辑失败：在文件中未找到 old_string。"]
        first_found = first_line in current_html if first_line else False
        last_found = last_line in current_html if last_line else False
        if first_found and last_found:
            diag.append("首行和尾行都存在于文件中，但整体不匹配。可能是空白/缩进不同。")
        elif first_found:
            diag.append(f"首行存在 ('{first_line[:60]}')，但整体不匹配。")
        elif last_found:
            diag.append(f"尾行存在 ('{last_line[:60]}')，但整体不匹配。")
        else:
            diag.append(f"首行 '{first_line[:60]}' 未在文件中找到。")
        diag.append("请调用 read_current_file 查看当前文件内容，然后重试。")

        return current_html, False, ' '.join(diag)

    def _get_structure_summary(self, html, max_chars=2000):
        """生成 HTML 文件结构的简明摘要。"""
        lines = html.split('\n')
        total_lines = len(lines)
        parts = [f"文件有 {total_lines} 行 ({len(html)} 字符)。"]

        if '<div class="sidebar"' in html or "class='sidebar'" in html:
            parts.append("- 有侧边栏导航 div。")
        if '<div class="main-content"' in html:
            parts.append("- 有 main-content 内容区 div。")
        if 'v-show="currentPage ===' in html:
            page_divs = re.findall(r"v-show=\"currentPage === '(\w+)'\"", html)
            if page_divs:
                parts.append(f"- 已有页面: {list(set(page_divs))}")
        if 'createApp' in html:
            parts.append("- Vue 3 应用已挂载。")

        return '\n'.join(parts)[:max_chars]

    def _compact_loop_messages(self, messages, max_chars=240000):
        """压缩 agentic loop 消息以控制上下文大小。"""
        total = sum(
            len(m.get('content', '')) if isinstance(m.get('content'), str)
            else 0
            for m in messages
        )
        if total <= max_chars:
            return messages

        logger.info(f"[Agent] 上下文压缩: {total} 字符 → 目标 {max_chars}")

        # 压缩旧的 read_current_file 结果
        compacted = []
        for m in messages:
            if m.get('name') == 'read_current_file' and m.get('role') == 'tool':
                content = m.get('content', '')
                if len(content) > 3000:
                    # 保留前500字符 + 截断提示
                    m = {**m, 'content': content[:3000] + '\n\n[已压缩 - 原始内容过长]'}
            compacted.append(m)

        return compacted

    def _generate_business_page_standalone(self, page_spec, page_idx, page_name,
                                           design_system, global_config,
                                           spec_summary, page_images, style_card):
        """并行生成单个业务页面（Route B 独立文件模式）。

        作为 ThreadPoolExecutor 的 worker 单元，不依赖其他页面的生成结果。
        线程安全：所有共享状态只读，文件写入到独立路径。

        Args:
            page_spec: 页面规格 dict
            page_idx: 业务页面索引（从 0 开始，不含框架页）
            page_name: 页面名称（来自 pages_data，确保与侧边栏一致）
            design_system: 设计系统 dict（只读）
            global_config: 全局配置（只读）
            spec_summary: 跨页规格摘要（只读）
            page_images: 页面图片列表
            style_card: 第 0 页的样式参考卡

        Returns:
            tuple: (success: bool, page_name: str, page_idx: int, file_path: str or None)
        """
        try:
            self._update_phase(2, f'page_{page_idx}', 'running', label=page_name,
                               progress={'current': page_idx + 1, 'total': 0})

            if self._is_cancelled():
                return (False, page_name, page_idx, None)

            prompt = build_single_page_prompt(
                page_spec=page_spec,
                design_system=design_system,
                page_index=page_idx,
                total_pages=0,  # 未知总数，但不影响 prompt 质量
                global_config=global_config,
                cross_page_spec_summary=spec_summary,
                template_style_card=style_card,
                include_full_template=False,
                standalone=True,
                iframe_context=True,
            )

            page_response = self._call_ai_streaming(prompt, page_images)
            page_html = extract_complete_page_html(page_response)

            if not page_html or len(page_html) < 100:
                logger.warning(f"[并行] 页面 {page_name} 内容过短或为空")
                return (False, page_name, page_idx, None)

            # 保存到 pages/ 目录
            page_filename = f"page_{page_idx}_{page_name}.html"
            page_path = os.path.join(self.project_folder, 'pages', page_filename)
            with open(page_path, 'w', encoding='utf-8') as f:
                f.write(page_html)

            # 页面审查 + 自动修复
            try:
                fixed_html, edit_count, _ = self._review_and_fix_page(page_html, page_name, page_spec=self._build_page_spec(page_name))
                if edit_count > 0:
                    with open(page_path, 'w', encoding='utf-8') as rf:
                        rf.write(fixed_html)
                    logger.info(f"[并行] {page_name} 审查修复 {edit_count} 处")
            except Exception as review_ex:
                logger.warning(f"[并行] {page_name} 审查异常: {review_ex}")

            logger.info(f"[并行] {page_name} ({page_filename}) 生成完成 ({len(page_html)} 字符)")
            return (True, page_name, page_idx, page_path)

        except Exception as e:
            logger.error(f"[并行] 页面 {page_name} 生成失败: {e}")
            return (False, page_name, page_idx, None)

    def _run_agentic_pages(self, pages_to_generate, design_system, global_config,
                           existing_html, html_path, page_images_map, spec_summaries,
                           total_pages, pages_data, page_offset=0):
        """Agentic loop：AI 通过 tool_calls 逐页生成内容。

        Args:
            pages_to_generate: pages_data[1:] 待生成的页面列表
            design_system: 设计系统 dict
            global_config: 全局配置
            existing_html: 第 1 页生成后的当前 index.html 内容
            html_path: index.html 路径
            page_images_map: {page_index: [images]} 参考图片
            spec_summaries: {page_index: summary_str} 跨页规格
            total_pages: 总页数
            pages_data: 完整页面数据列表（用于构建所有页面名称）
            page_offset: 批次偏移量，分批处理时保持全局页面编号连续

        Returns:
            str: 最终 HTML 内容
        """
        # 构建待生成页面信息
        pending_pages = []
        for i, page in enumerate(pages_to_generate):
            page_idx = i + 1 + page_offset  # 支持批次偏移，保持全局编号连续
            page_key = f'page_{page_idx}'
            pending_pages.append({
                'index': page_idx,
                'key': page_key,
                'name': page.get('name', f'页面{page_idx}'),
                'spec': page,
                'images': page_images_map.get(page_idx, []),
                'spec_summary': spec_summaries.get(page_idx, '')
            })

        if not pending_pages:
            return existing_html

        # 大量页面分批处理：每批最多 BATCH_SIZE 页，避免上下文溢出
        BATCH_SIZE = 8
        if len(pending_pages) > BATCH_SIZE:
            logger.info(f"[Agent] 页面数 {len(pending_pages)} > {BATCH_SIZE}，分批处理")
            current_html = existing_html
            for batch_start in range(0, len(pending_pages), BATCH_SIZE):
                batch = pending_pages[batch_start:batch_start + BATCH_SIZE]
                logger.info(f"[Agent] 批次 {batch_start // BATCH_SIZE + 1}: "
                            f"页面 {batch_start + 1}-{batch_start + len(batch)} "
                            f"({len(batch)} 页)")
                # 为当前批次构建 pages_data 子集
                batch_pages_data = [pages_data[0]]  # 保留第一页（已完成）
                for pp in batch:
                    batch_pages_data.append(pp['spec'])

                current_html = self._run_agentic_pages(
                    pages_to_generate=[pp['spec'] for pp in batch],
                    design_system=design_system,
                    global_config=global_config,
                    existing_html=current_html,
                    html_path=html_path,
                    page_images_map={pp['index']: pp['images'] for pp in batch},
                    spec_summaries={pp['index']: pp['spec_summary'] for pp in batch},
                    total_pages=total_pages,  # 使用全局总页数，确保进度正确
                    pages_data=batch_pages_data,
                    page_offset=batch_start  # 传递批次偏移量，保持全局编号连续
                )
                # 更新中间状态
                self._save_incremental_state()
            return current_html

        # 构建系统提示
        first_page_name = pages_data[0].get('name', '页面1') if pages_data else '页面1'
        structure_summary = self._get_structure_summary(existing_html)
        page_list_for_prompt = [
            {'key': pp['key'], 'name': pp['name']}
            for pp in pending_pages
        ]

        system_prompt = build_incremental_system_prompt(
            design_system, page_list_for_prompt, first_page_name, structure_summary,
            global_config=global_config
        )

        # 注入已生成页面的样式参考卡（保持跨批次风格一致）
        style_card = build_generated_style_card(existing_html)
        if style_card:
            system_prompt += f"\n\n## 已生成页面的样式参考（新页面必须保持一致）\n{style_card}"

        # 构建初始用户消息（包含所有页面规格）
        user_parts = []
        for pp in pending_pages:
            spec = pp['spec']
            part = f'## 要生成的页面：{pp["name"]} (page_key: "{pp["key"]}"))\n'
            if spec.get('description'):
                part += f'**描述**：{spec["description"]}\n'
            if spec.get('features'):
                part += f'**UI 组件**：\n{spec["features"]}\n'
            if spec.get('dataStructure'):
                part += f'**数据字段**（生成真实示例数据）：\n{spec["dataStructure"]}\n'
            if spec.get('interaction'):
                part += f'**交互行为**：\n{spec["interaction"]}\n'
            if spec.get('layout'):
                part += f'**布局**：\n{spec["layout"]}\n'
            if pp['spec_summary']:
                part += f'\n{pp["spec_summary"]}\n'
            user_parts.append(part)

        user_message_text = (
            f"请按顺序生成以下 {len(pending_pages)} 个页面。"
            f"每个页面调用一次 add_page。\n\n"
            + '\n'.join(user_parts)
        )

        # 初始化消息列表
        loop_messages = []

        # 如果有参考图片，附加到第一个用户消息
        first_images = pending_pages[0]['images'] if pending_pages else []
        if first_images:
            user_content = [{"type": "text", "text": user_message_text}]
            for img in first_images:
                user_content.append({"type": "image_url", "image_url": {"url": img}})
            loop_messages.append({'role': 'user', 'content': user_content})
        else:
            loop_messages.append({'role': 'user', 'content': user_message_text})

        current_html = existing_html
        completed_keys = set()
        MAX_ROUNDS = 30
        consecutive_empty = 0

        for agent_round in range(MAX_ROUNDS):
            if self._is_cancelled():
                logger.info(f"[Agent] 在第 {agent_round} 轮被取消")
                return current_html

            pending_count = len(pending_pages) - len(completed_keys)
            logger.info(f"[Agent] 第 {agent_round} 轮，待生成页面: {pending_count}")

            # === 调用 AI（带 tools） ===
            accumulated = ""
            tool_calls_result = None
            _streaming_tool_html_sent = 0  # 上次推送流式 HTML 的字节位置

            gen = self.server.call_ai_model_streaming(
                loop_messages, [],
                cancellable_project_id=self.project_id,
                tools=incremental_tools
            )
            try:
                had_reasoning = False
                for chunk_text, full_content, done, tc, *_rest in gen:
                    accumulated = full_content
                    # 提取 had_reasoning 标志（第 5 个元素）
                    if _rest and len(_rest) > 0:
                        had_reasoning = bool(_rest[0])
                    if chunk_text and not chunk_text.startswith('[think]'):
                        # 流式推送到 SSE（显示 AI 的状态文字，过滤思考 token）
                        self._send_event('chat', {
                            'role': 'assistant',
                            'content': chunk_text
                        })
                    # 捕获 tool call 参数流式增量，提取部分 HTML 推送到前端
                    if tc and not done:
                        for _tc_key, _tc_val in tc.items():
                            if _tc_val.get('name') == 'add_page' and _tc_val.get('arguments'):
                                _args = _tc_val['arguments']
                                _html = _extract_partial_html_from_args(_args)
                                if _html and len(_html) > (_streaming_tool_html_sent + 500):
                                    _streaming_tool_html_sent = len(_html)
                                    self._send_event('streaming_html', {
                                        'html': _html
                                    })
                    if done:
                        tool_calls_result = tc
                        break
            finally:
                try:
                    gen.close()
                except RuntimeError:
                    pass

            # === 无 tool_calls：纯文本响应 ===
            if not tool_calls_result:
                if not accumulated.strip():
                    # 模型只返回了 reasoning_content（推理）但没有实际内容
                    # 不应计入连续空响应——模型可能正在思考或上下文过大
                    if had_reasoning:
                        logger.info(f"[Agent] 模型有推理但无输出，视为上下文压力信号 "
                                    f"(连续 {consecutive_empty + 1} 次)")
                        consecutive_empty += 1
                        if consecutive_empty >= 3:  # 推理模式给更多机会
                            logger.warning("[Agent] 连续推理但无输出，退出循环")
                            break
                        # 不追加空消息，直接重试
                        continue
                    consecutive_empty += 1
                    if consecutive_empty >= 2:
                        logger.warning("[Agent] 连续空响应，退出循环")
                        break
                    continue

                consecutive_empty = 0

                # 检查是否所有页面已完成
                if len(completed_keys) >= len(pending_pages):
                    logger.info("[Agent] 所有页面已完成，AI 确认")
                    break

                # 提醒 AI 继续
                loop_messages.append({'role': 'assistant', 'content': accumulated})
                remaining = [pp for pp in pending_pages if pp['key'] not in completed_keys]
                loop_messages.append({
                    'role': 'user',
                    'content': (
                        f"还有 {len(remaining)} 个页面未生成。"
                        f"请为以下页面调用 add_page："
                        + '、'.join(f'"{pp["name"]}" ({pp["key"]})' for pp in remaining)
                    )
                })
                continue

            # === 有 tool_calls：构建 assistant 消息 ===
            consecutive_empty = 0

            # 构建 assistant 消息（带 tool_calls）
            assistant_tc_list = [
                {
                    "id": tc['id'],
                    "type": "function",
                    "function": {
                        "name": tc['name'],
                        "arguments": json.dumps(tc['arguments'], ensure_ascii=False)
                    }
                }
                for tc in tool_calls_result
            ]
            loop_messages.append({
                "role": "assistant",
                "content": accumulated if accumulated else None,
                "tool_calls": assistant_tc_list
            })

            # === 逐个执行 tool_calls ===
            for tc in tool_calls_result:
                tc_id = tc['id']
                tool_name = tc.get('name', '')
                args = tc.get('arguments', {})

                if tool_name == 'add_page':
                    page_key = args.get('page_key', '')

                    # 找到 page_index（同时获取可靠的页面名称）
                    page_idx = None
                    resolved_name = None
                    for pp in pending_pages:
                        if pp['key'] == page_key:
                            page_idx = pp['index']
                            resolved_name = pp['name']
                            break

                    if page_idx is None:
                        valid_keys = [pp['key'] for pp in pending_pages]
                        loop_messages.append({
                            'role': 'tool',
                            'tool_call_id': tc_id,
                            'name': 'add_page',
                            'content': f"错误：未知的 page_key '{page_key}'。有效的 key 为：{valid_keys}"
                        })
                        continue

                    self._update_phase(2, f'page_{page_idx}', 'running',
                                       label=resolved_name,
                                       progress={'current': page_idx, 'total': total_pages})

                    # 判断项目类型：Route B (pages/ 目录存在) vs sidebar (单文件注入)
                    pages_dir = os.path.join(self.project_folder, 'pages')
                    is_route_b = os.path.isdir(pages_dir)

                    if is_route_b:
                        # Route B: 保存完整独立 HTML 到 pages/ 目录
                        html_content = args.get('html_content', '')
                        # 使用 resolved_name（来自 pages_data）而非 AI 返回的 page_name，
                        # 确保文件名与侧边栏引用一致
                        page_filename = f"page_{page_idx}_{resolved_name}.html"
                        page_path = os.path.join(pages_dir, page_filename)

                        # 确保 html_content 是完整 HTML
                        if '<!DOCTYPE' not in html_content and '<html' not in html_content:
                            html_content = _wrap_as_standalone(html_content)

                        with open(page_path, 'w', encoding='utf-8') as f:
                            f.write(html_content)

                        completed_keys.add(page_key)
                        current_html = html_content  # 保持兼容

                        self._send_event('page_written', {
                            'page': resolved_name,
                            'index': page_idx,
                            'path': page_path,
                            'size': len(html_content)
                        })

                        # 发送 preview 事件让前端立即渲染页面到画布
                        self._send_event('preview', {
                            'page': resolved_name,
                            'html_fragment': html_content
                        })

                        # 页面审查 + 自动修复
                        try:
                            fixed_route_b, review_edits_rb, _ = self._review_and_fix_page(
                                html_content, resolved_name, page_spec=self._build_page_spec(resolved_name))
                            if review_edits_rb > 0:
                                html_content = fixed_route_b
                                current_html = fixed_route_b
                                with open(page_path, 'w', encoding='utf-8') as rf:
                                    rf.write(fixed_route_b)
                                logger.info(f"[Agent] 页面 {resolved_name} 审查修复 {review_edits_rb} 处")
                        except Exception as review_ex:
                            logger.warning(f"[Agent] 页面 {resolved_name} 审查异常: {review_ex}")

                        logger.info(f"[Agent] Route B 页面已保存: {resolved_name} ({page_key}), "
                                    f"文件: {page_filename}, 大小: {len(html_content)}")

                        loop_messages.append({
                            'role': 'tool',
                            'tool_call_id': tc_id,
                            'name': 'add_page',
                            'content': f"页面「{resolved_name}」已保存为独立文件 {page_filename} ({len(html_content)} 字符)"
                        })
                    else:
                        # Sidebar 模式：注入到单文件 index.html
                        updated_html, success, message = self._execute_add_page(
                            current_html, args, page_idx, total_pages
                        )

                        if success:
                            current_html = updated_html
                            completed_keys.add(page_key)

                            # 立即写入磁盘
                            with open(html_path, 'w', encoding='utf-8') as f:
                                f.write(current_html)

                            self._send_event('page_written', {
                                'page': resolved_name,
                                'index': page_idx,
                                'path': html_path,
                                'size': len(current_html)
                            })

                            # 发送 preview 事件让前端立即渲染页面到画布
                            self._send_event('preview', {
                                'page': resolved_name,
                                'html_fragment': current_html
                            })

                            # 页面审查 + 自动修复
                            try:
                                fixed_sidebar, review_edits_sb, _ = self._review_and_fix_page(
                                    current_html, resolved_name, page_spec=self._build_page_spec(resolved_name))
                                if review_edits_sb > 0:
                                    current_html = fixed_sidebar
                                    with open(html_path, 'w', encoding='utf-8') as rf:
                                        rf.write(fixed_sidebar)
                                    logger.info(f"[Agent] Sidebar 页面 {resolved_name} 审查修复 {review_edits_sb} 处")
                            except Exception as review_ex:
                                logger.warning(f"[Agent] Sidebar 页面 {resolved_name} 审查异常: {review_ex}")

                            logger.info(f"[Agent] Sidebar 页面已注入: {resolved_name} ({page_key})")
                        else:
                            logger.warning(f"[Agent] add_page 失败: {message}")

                        loop_messages.append({
                            'role': 'tool',
                            'tool_call_id': tc_id,
                            'name': 'add_page',
                            'content': message
                        })

                    self._update_phase(2, f'page_{page_idx}', 'done',
                                       label=resolved_name,
                                       progress={'current': page_idx, 'total': total_pages})

                elif tool_name == 'read_page':
                    pages_dir = os.path.join(self.project_folder, 'pages')
                    if os.path.isdir(pages_dir):
                        content = self._execute_read_page(args)
                    else:
                        content = self._execute_read_current_file(html_path, args)
                    loop_messages.append({
                        'role': 'tool',
                        'tool_call_id': tc_id,
                        'name': 'read_page',
                        'content': content
                    })

                elif tool_name == 'read_current_file':
                    # 向后兼容：根据项目类型重定向
                    pages_dir = os.path.join(self.project_folder, 'pages')
                    if os.path.isdir(pages_dir):
                        content = self._execute_read_page(args)
                    else:
                        content = self._execute_read_current_file(html_path, args)
                    loop_messages.append({
                        'role': 'tool',
                        'tool_call_id': tc_id,
                        'name': 'read_page',
                        'content': content
                    })

                elif tool_name == 'edit_page':
                    pages_dir = os.path.join(self.project_folder, 'pages')
                    if os.path.isdir(pages_dir):
                        applied, message = self._execute_edit_page_file(args)
                    else:
                        # Sidebar 模式：在单文件 HTML 上编辑
                        updated_html, applied, message = self._execute_edit_page(current_html, args)
                        if applied:
                            current_html = updated_html
                            with open(html_path, 'w', encoding='utf-8') as f:
                                f.write(current_html)

                    if applied:
                        logger.info(f"[Agent] edit_page 成功")

                    loop_messages.append({
                        'role': 'tool',
                        'tool_call_id': tc_id,
                        'name': 'edit_page',
                        'content': message
                    })

                else:
                    loop_messages.append({
                        'role': 'tool',
                        'tool_call_id': tc_id,
                        'name': tool_name,
                        'content': f"未知工具: {tool_name}"
                    })

            # 检查所有页面是否完成
            if len(completed_keys) >= len(pending_pages):
                logger.info("[Agent] 所有页面已通过 tool_calls 成功生成")
                break

            # 上下文管理
            loop_messages = self._compact_loop_messages(loop_messages)

        # 最终检查
        if len(completed_keys) < len(pending_pages):
            missing = [pp['name'] for pp in pending_pages if pp['key'] not in completed_keys]
            logger.warning(f"[Agent] 未完成页面: {missing}")

        return current_html

    # ---- 内部方法 ----

    def _call_ai_streaming(self, prompt, images):
        """调用 AI 流式接口，返回累积文本"""
        accumulated = ""
        _last_streaming_push = 0
        gen = self.server.call_ai_model_streaming(
            prompt, images,
            cancellable_project_id=self.project_id
        )
        try:
            for chunk_text, full_content, done, *rest in gen:
                accumulated = full_content

                # 推送到 SSE 缓冲区
                pid = self.project_id
                srv = _get_server_module()
                if pid and pid in srv.generating_tasks:
                    with srv.tasks_lock:
                        task = srv.generating_tasks[pid]
                        task['accumulated_content'] = accumulated
                        if chunk_text and not chunk_text.startswith('[think]'):
                            sl = task.get('stream_lock')
                            if sl:
                                with sl:
                                    task['stream_chunks'].append(chunk_text)
                            se = task.get('stream_event')
                            if se:
                                se.set()
                            # 进度启发式
                            estimated = min(80, 20 + len(accumulated) // 100)
                            task['progress'] = estimated

                    # ---- 流式 HTML 实时预览推送 ----
                    _last_streaming_push = srv._maybe_push_streaming_html(
                        pid, accumulated, _last_streaming_push,
                        getattr(threading.current_thread(), '_page_name', '') or ''
                    )
                if done:
                    break
        finally:
            try:
                gen.close()
            except RuntimeError:
                pass

        return accumulated

    def _call_ai_streaming_with_history(self, messages, images=None):
        """带对话历史的 AI 调用（多轮对话模式）

        images 只附加在最后一条 user message 上。
        """
        # 如果有图片，附加到最后一条 user message
        if images:
            msgs_copy = list(messages)
            for j in range(len(msgs_copy) - 1, -1, -1):
                if msgs_copy[j].get('role') == 'user':
                    content = msgs_copy[j]['content']
                    if isinstance(content, str):
                        msgs_copy[j] = {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": content},
                                *[{"type": "image_url", "image_url": {"url": img}} for img in images]
                            ]
                        }
                    break
            messages = msgs_copy

        accumulated = ""
        _last_streaming_push = 0
        gen = self.server.call_ai_model_streaming(
            messages,  # 直接传 messages list 作为 prompt_or_messages
            cancellable_project_id=self.project_id
        )
        try:
            for chunk_text, full_content, done, *rest in gen:
                accumulated = full_content
                pid = self.project_id
                srv = _get_server_module()
                if pid and pid in srv.generating_tasks:
                    with srv.tasks_lock:
                        task = srv.generating_tasks[pid]
                        task['accumulated_content'] = accumulated
                        if chunk_text and not chunk_text.startswith('[think]'):
                            sl = task.get('stream_lock')
                            if sl:
                                with sl:
                                    task['stream_chunks'].append(chunk_text)
                            se = task.get('stream_event')
                            if se:
                                se.set()
                            estimated = min(80, 20 + len(accumulated) // 100)
                            task['progress'] = estimated

                    # ---- 流式 HTML 实时预览推送 ----
                    _last_streaming_push = srv._maybe_push_streaming_html(
                        pid, accumulated, _last_streaming_push,
                        getattr(threading.current_thread(), '_page_name', '') or ''
                    )
                if done:
                    break
        finally:
            try:
                gen.close()
            except RuntimeError:
                pass

        return accumulated

    def _maybe_compact(self):
        """检查是否需要压缩对话历史"""
        max_context = self._get_max_context_tokens()
        compress_threshold = max_context * 0.65  # 65% 时触发

        current_tokens = estimate_messages_tokens(self.conversation_messages)

        if current_tokens > compress_threshold:
            logger.info(f"[多轮] context {current_tokens} 超过阈值 {int(compress_threshold)}，触发压缩")
            self._compact_conversation()

    def _get_max_context_tokens(self):
        """获取当前模型的 max_context_tokens"""
        try:
            models_config = getattr(self.server, 'models_config', {})
            if isinstance(models_config, dict):
                selected_id = models_config.get('selected_model_id', '')
                for m in models_config.get('models', []):
                    if m.get('id') == selected_id:
                        return m.get('max_context_tokens', 128000)
        except Exception:
            pass
        return 128000

    def _compact_conversation(self):
        """压缩对话历史（用于 AI 上下文，不丢失原始数据）

        将早期页面的 assistant response 替换为 AI 结构化摘要。
        保留最近 2 页完整对话。
        原始消息保存到 _full_conversation_messages 供持久化使用。
        """
        if len(self.conversation_messages) < 4:
            return

        logger.info(f"[多轮] 压缩对话历史: {len(self.conversation_messages)} 条消息")

        # 保存原始消息（用于持久化到 multi_round_state.json）
        if not hasattr(self, '_full_conversation_messages') or not self._full_conversation_messages:
            self._full_conversation_messages = list(self.conversation_messages)

        # 找出需要压缩的 assistant messages
        assistant_indices = [
            idx for idx, msg in enumerate(self.conversation_messages)
            if msg.get('role') == 'assistant'
        ]

        # 保留最近 2 条 assistant response 不压缩
        to_compress = assistant_indices[:-2] if len(assistant_indices) > 2 else []

        for idx in to_compress:
            original_content = self.conversation_messages[idx].get('content', '')
            if len(original_content) > 500:
                # 找对应的页面名
                page_name = "页面"
                if idx > 0:
                    prev_msg = self.conversation_messages[idx - 1].get('content', '')
                    if isinstance(prev_msg, str):
                        name_match = re.search(r'页面需求[：:]\s*(.+)', prev_msg)
                        if not name_match:
                            name_match = re.search(r'## 页面需求[：:]\s*(.+)', prev_msg)
                        if name_match:
                            page_name = name_match.group(1).strip()

                summary = self._compress_page_context(original_content, page_name)
                self.conversation_messages[idx] = {
                    "role": "assistant",
                    "content": f"[已压缩] 页面「{page_name}」结构摘要:\n{summary}"
                }

        logger.info(f"[多轮] 压缩完成，{len(to_compress)} 个页面已替换为摘要")

    def _compress_page_context(self, page_html, page_name):
        """用 AI 压缩已生成页面的上下文"""
        compact_prompt = f"""请将以下已生成的 HTML 页面压缩为结构化摘要。
这个摘要将被注入到后续页面的生成上下文中，确保新页面与已生成页面保持一致。

## 已生成页面「{page_name}」的 HTML 代码
```html
{page_html[:6000]}
```

## 输出要求
请输出以下结构化摘要（不要输出 HTML 代码，只输出摘要）：

1. **组件清单**: 使用了哪些 UI 组件
2. **CSS class 命名**: 关键自定义 class 名
3. **CSS 变量**: 使用的颜色值、字体、间距等设计 token
4. **数据字段**: 数据模型字段名和示例值
5. **导航结构**: 页面导航/标签栏的实现方式
6. **交互逻辑**: 关键的 Vue 交互
7. **样式特征**: 表格斑马纹、卡片阴影、按钮颜色等视觉特征

摘要要简洁精准。"""

        return self._call_ai_streaming(compact_prompt, [])

    def _get_page_images(self, page_spec, all_images):
        """获取某页面的参考图片"""
        img_count = page_spec.get('imageCount', 0)
        if img_count <= 0 or not all_images:
            return []
        # 简单截取：按页面顺序分配图片
        # TODO: 更精确的图片分配逻辑
        return all_images[:img_count]

    def _is_cancelled(self):
        """检查任务是否被取消"""
        srv = _get_server_module()
        with srv.tasks_lock:
            return (self.project_id in srv.generating_tasks and
                    srv.generating_tasks[self.project_id].get('status') == 'cancelled')

    def _update_phase(self, round_num, step, status, label='', progress=None):
        """更新当前阶段信息到 generating_tasks"""
        srv = _get_server_module()
        with srv.tasks_lock:
            if self.project_id in srv.generating_tasks:
                task = srv.generating_tasks[self.project_id]
                task['round'] = round_num
                task['phase_description'] = label or step
                if progress:
                    task['page_progress'] = progress

        phase_data = {'round': round_num, 'step': step, 'status': status, 'label': label}
        if progress:
            phase_data['progress'] = progress
        self._send_event('phase', phase_data)

    def _send_event(self, event_type, data):
        """发送结构化 SSE 事件"""
        srv = _get_server_module()

        event = json.dumps({
            'type': event_type,
            'data': data,
            'timestamp': time.time()
        }, ensure_ascii=False)

        with srv.tasks_lock:
            task = srv.generating_tasks.get(self.project_id)
            if task:
                sl = task.get('stream_lock')
                if sl:
                    with sl:
                        task['stream_chunks'].append(event)
                se = task.get('stream_event')
                if se:
                    se.set()
                logger.debug(f"[多轮] SSE 事件已推送: {event_type} (队列 {len(task.get('stream_chunks', []))})")
            else:
                logger.warning(f"[多轮] SSE 事件推送失败: 任务 {self.project_id[:30]} 不存在")

    def _build_page_spec(self, page_name):
        """从 pages_data 中提取页面规格文本，供审查 AI 参考。"""
        page_data = None
        for pd in getattr(self, 'pages_data', []):
            if pd.get('name') == page_name:
                page_data = pd
                break
        if not page_data:
            return ''
        parts = []
        if page_data.get('features'):
            parts.append(f"### 页面功能\n{page_data['features']}")
        if page_data.get('dataStructure'):
            parts.append(f"### 数据结构\n{page_data['dataStructure']}")
        if page_data.get('interaction'):
            parts.append(f"### 交互逻辑\n{page_data['interaction']}")
        return '\n\n'.join(parts)

    def _review_and_fix_page(self, page_html, page_name, page_spec=''):
        """对生成的页面执行审查+自动修复（确保页面能正常打开、无报错）

        委托给 server._run_code_review() 执行。
        """
        def _push_event(data):
            self._send_event('code_review', data)

        try:
            fixed_html, total_edits, summary = self.server._run_code_review(
                page_html, page_name, self.project_id, self.project_folder,
                push_event_fn=_push_event,
                page_spec=page_spec
            )
            return fixed_html, total_edits, summary
        except Exception as e:
            logger.warning(f"[多轮] 页面 {page_name} 审查失败: {e}")
            return page_html, 0, f'审查失败: {e}'

    def _save_intermediate(self):
        """保存中间状态 + 写入可预览的 index.html（参考 Claude Code 每步写磁盘）

        Claude Code 每次编辑后立即 writeTextContent 写磁盘。
        我们每页生成完后也把当前已有页面组装写入 index.html，
        这样即使中途崩溃，用户也能看到已生成的部分页面。
        """
        state = {
            'design_system': self.design_system,
            'page_fragments': self.page_fragments,
            'page_names': self.page_names,
            'cross_page_spec': self.cross_page_spec,
            # 保存完整的原始对话（压缩前的版本），而非内存中被压缩的版本
            'conversation_messages': getattr(self, '_full_conversation_messages', None)
                                     or self.conversation_messages,
        }
        state_path = os.path.join(self.project_folder, 'multi_round_state.json')
        try:
            with open(state_path, 'w', encoding='utf-8') as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[多轮] 保存中间状态失败: {e}")

        # 同时写入可预览的 index.html（每页完成后即时更新）
        if not self.page_fragments:
            return

        try:
            partial_html = assemble_multi_page_html(
                page_fragments=self.page_fragments,
                design_system_css=self.design_system.get('css_variables', ''),
                page_names=self.page_names[:len(self.page_fragments)],
                layout_type=getattr(self, '_layout_type', 'plain'),
                project_dir=self.project_folder,
                output_format='dual'
            )
            html_path = os.path.join(self.project_folder, 'index.html')
            if os.path.exists(os.path.dirname(html_path)):
                with open(html_path, 'w', encoding='utf-8') as f:
                    f.write(partial_html)
                logger.info(f"[多轮] 已写入中间 index.html（{len(self.page_fragments)} 页，{len(partial_html)} 字符）")
        except Exception as e:
            logger.warning(f"[多轮] 写入中间 index.html 失败: {e}")

    def _save_incremental_state(self):
        """保存增量模式的中间状态到 multi_round_state.json。

        与 _save_intermediate 不同，增量模式的 index.html 由 agentic loop
        直接管理，这里只保存 multi_round_state.json 供对话模式加载。
        """
        # 从磁盘读取当前 index.html 作为最新快照
        html_path = os.path.join(self.project_folder, 'index.html')
        current_html = self.base_html or ''
        if os.path.exists(html_path):
            try:
                with open(html_path, 'r', encoding='utf-8') as f:
                    current_html = f.read()
            except Exception:
                pass

        state = {
            'mode': 'incremental',
            'design_system': getattr(self, 'design_system', {}),
            'page_fragments': getattr(self, 'page_fragments', []),
            'page_names': getattr(self, 'page_names', []),
            'cross_page_spec': getattr(self, 'cross_page_spec', {}),
            'conversation_messages': getattr(self, '_full_conversation_messages', None)
                                     or getattr(self, 'conversation_messages', []),
            'base_html': current_html,
        }
        state_path = os.path.join(self.project_folder, 'multi_round_state.json')
        try:
            with open(state_path, 'w', encoding='utf-8') as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            logger.info(f"[增量] 已保存中间状态 ({len(current_html)} 字符)")
        except Exception as e:
            logger.warning(f"[增量] 保存中间状态失败: {e}")
