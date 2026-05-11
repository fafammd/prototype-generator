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

logger = logging.getLogger('prototype')


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
      {"id": "page_id", "name": "页面名称", "is_entry": true}
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
    """为 Round 2 的某页生成浓缩版 spec 摘要

    输出限制在 ~3000 字符内。
    优先展示导航链接（最关键的行动项），数据模型用精简格式。
    """
    if not spec:
        return ''

    lines = []
    lines.append("## 跨页规格约束（所有页面必须遵循）")

    # 当前页面信息
    pages = spec.get('pages', [])
    current_page = pages[page_index] if page_index < len(pages) else None
    current_page_id = current_page.get('id', '') if current_page else ''
    current_page_name = current_page.get('name', '') if current_page else ''

    # ===== 优先级 1：当前页面的导航按钮（最重要，不能被截断） =====
    nav = spec.get('navigation', {})
    links = nav.get('links', [])

    # 当前页面的出站导航（当前页必须实现的跳转）
    outbound_links = [l for l in links if l.get('from') == current_page_id]
    # 当前页面的入站导航（其他页面跳转到当前页）
    inbound_links = [l for l in links if l.get('to') == current_page_id]

    if outbound_links:
        lines.append(f"\n### ⚠️ 当前页面（{current_page_name}）必须实现的跳转按钮")
        lines.append("**以下导航按钮必须在你的 HTML 中实现，不能遗漏：**")
        for link in outbound_links:
            target_page = link.get('to', '?')
            trigger = link.get('trigger', '')
            # 找到目标页面的索引（用于 navigateTo）
            target_idx = next((i for i, p in enumerate(pages) if p.get('id') == target_page), -1)
            target_name = next((p.get('name', target_page) for p in pages if p.get('id') == target_page), target_page)
            if target_idx >= 0:
                lines.append(f"- **{trigger}** → 使用 `navigateTo('page_{target_idx}')` 或 `currentPage = 'page_{target_idx}'` 跳转到「{target_name}」")
            else:
                lines.append(f"- **{trigger}** → 跳转到「{target_name}」")

    if inbound_links:
        lines.append(f"\n### 其他页面跳转到本页的入口（供参考）")
        for link in inbound_links:
            source_page = link.get('from', '?')
            source_name = next((p.get('name', source_page) for p in pages if p.get('id') == source_page), source_page)
            lines.append(f"- 「{source_name}」→ 本页: {link.get('trigger', '')}")

    if not outbound_links and not inbound_links and links:
        lines.append("\n### 跨页导航（全局参考）")
        for link in links:
            lines.append(f"- {link.get('from', '?')} → {link.get('to', '?')}: {link.get('trigger', '')}")

    # ===== 优先级 2：当前页面需要的数据模型（精简格式） =====
    # 只列出当前页面 data_sources 引用的模型
    current_data_sources = current_page.get('data_sources', []) if current_page else []
    models = spec.get('shared_data_models', [])
    relevant_models = [m for m in models if m.get('name') in current_data_sources]
    other_model_names = [m.get('name', '') for m in models if m.get('name') not in current_data_sources]

    if relevant_models:
        lines.append("\n### 当前页面使用的数据模型")
        for m in relevant_models:
            fields_str = ', '.join(
                f"{f['name']}({f.get('type', 'string')})"
                for f in m.get('fields', [])
            )
            lines.append(f"- **{m['name']}**: {fields_str}")

    if other_model_names:
        lines.append(f"\n### 其他数据模型（仅名称）\n{', '.join(other_model_names)}")

    # ===== 优先级 3：共享组件 =====
    components = spec.get('shared_components', [])
    if components:
        lines.append("\n### 共享组件")
        for c in components:
            lines.append(f"- **{c['name']}** ({', '.join(c.get('variants', []))}): {c.get('usage', '')}")

    # ===== 优先级 4：其他页面简述 =====
    if len(pages) > 1:
        lines.append("\n### 其他页面")
        for i, p in enumerate(pages):
            if i != page_index:
                lines.append(f"- **{p.get('name', '?')}** (page_{i}, id={p.get('id', '?')}): 使用 {', '.join(p.get('data_sources', []))}")

    # 样式一致性
    if current_page:
        refs = current_page.get('cross_references', {})
        match_pages = refs.get('must_match_style_of', [])
        if match_pages:
            lines.append(f"\n**必须与以下页面保持样式一致**: {', '.join(match_pages)}")

    # 用户调整意见（如存在，追加到末尾）
    adjustment = spec.get('_adjustment_note', '')
    if adjustment:
        lines.append(f"\n## 用户补充要求（请优先遵循）\n{adjustment}")

    result = '\n'.join(lines)
    if len(result) > 3000:
        result = result[:3000] + "\n...(已截断)"
    return result


def estimate_messages_tokens(messages):
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


def estimate_prompt_tokens(prompt, image_count=0):
    """估算完整 prompt 的 token 数（文本 + 图片）"""
    text_tokens = estimate_tokens(prompt)
    image_tokens = image_count * 1000
    return text_tokens + image_tokens


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

    prompt = f"""你是一个 UI 设计系统专家。请根据以下需求，生成一套完整的 CSS 设计系统。

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
## 输出要求

生成以下两部分：

### 1. CSS 变量（放在 ```css 代码块中）
```css
:root {
  /* 颜色系统 */
  --color-primary: ...;
  --color-primary-light: ...;
  --color-primary-dark: ...;
  --color-secondary: ...;
  --color-bg-page: ...;
  --color-bg-card: ...;
  --color-bg-sidebar: ...;
  --color-text-primary: ...;
  --color-text-secondary: ...;
  --color-text-muted: ...;
  --color-border: ...;
  --color-success: ...;
  --color-warning: ...;
  --color-danger: ...;

  /* 字体系统 */
  --font-family: ...;
  --font-size-xs: ...;
  --font-size-sm: ...;
  --font-size-base: ...;
  --font-size-lg: ...;
  --font-size-xl: ...;
  --font-size-2xl: ...;

  /* 间距系统 */
  --spacing-xs: ...;
  --spacing-sm: ...;
  --spacing-md: ...;
  --spacing-lg: ...;
  --spacing-xl: ...;

  /* 圆角 */
  --radius-sm: ...;
  --radius-md: ...;
  --radius-lg: ...;

  /* 阴影 */
  --shadow-sm: ...;
  --shadow-md: ...;
  --shadow-lg: ...;
}
```

### 2. 组件规格描述
简要描述以下组件的视觉规格（文字描述即可）：
- 按钮：主按钮、次按钮、文字按钮的样式
- 输入框：边框、聚焦状态
- 表格：表头、行高、斑马纹
- 卡片：背景、圆角、阴影、内边距
- 导航：高度、激活项样式
- 标签页：样式、选中状态
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
                             cross_page_spec_summary=''):
    """构建 Round 2 单页生成 prompt

    Args:
        page_spec: 页面规格 dict {name, description, layout, features, ...}
        design_system: Round 1 产出的设计系统 dict
        page_index: 页面索引（从 0 开始）
        total_pages: 总页数
        global_config: 全局设计配置
        template_css_path: 模板 CSS 路径
        is_iframe_layout: 是否为 iframe 布局
        template_design_tokens: 模板 CSS 设计令牌
        template_html_summary: 模板 HTML 结构摘要
        template_frame_html: 模板框架 HTML（iframe/sidebar）
        template_layout_type: 布局类型 'iframe'|'sidebar'|'plain'
    """
    primary = global_config.get('primaryColor', '#004fff') if global_config else '#004fff'
    secondary = global_config.get('secondaryColor', '#10b981') if global_config else '#10b981'
    bg_mode = 'light' if (global_config or {}).get('backgroundMode', 'light') == 'light' else 'dark'
    component_style = global_config.get('componentStyle', 'Ant Design') if global_config else 'Ant Design'

    prompt = f"""你是一个专业的前端开发者。请生成一个页面的 HTML 代码。

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

        if is_iframe_layout and template_frame_html:
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

        if is_sidebar_mode:
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
```html
<style>/* 页面特有样式 */</style>
<div>
    <!-- 页面内容 -->
</div>
<script src="https://unpkg.com/vue@3/dist/vue.global.prod.js"></script>
<script>
// Vue 3 应用
</script>
```
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
```html
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
```
"""
    # ===== 非模板模式：多页面布局约束 =====
    if not has_template and total_pages > 1:
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
```html
<style>/* 页面特有样式 */</style>
<div>
    <!-- 页面核心内容：表格、卡片、表单、图表等 -->
</div>
<script>
// Vue 3 应用逻辑（如需要）
</script>
```
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
```html
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
```
"""
    return prompt


def extract_page_fragment(ai_response, page_name=''):
    """从 Round 2 AI 响应中提取页面 HTML

    处理 AI 可能输出完整 HTML 文档或带 markdown 包裹的情况。
    """
    # 安全网：先清除 AI 思考标记（防止 [think] 泄漏到 HTML）
    cleaned = re.sub(r'\[think\]', '', ai_response)

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


# ==================== Phase 4: 组装 (Round 3) ====================

def assemble_multi_page_html(page_fragments, design_system_css, page_names,
                              global_config=None, template_css_path=None,
                              layout_type='plain'):
    """将多个页面 HTML 片段组装为带 Vue 导航的完整 HTML

    关键：使用与 inject_page_navigation_listener() 兼容的 currentPage 变量。

    Args:
        layout_type: 'iframe'|'sidebar'|'plain'
            - 'sidebar': 返回内容块（无外层 HTML 包装），用于注入框架
            - 'iframe'/'plain': 返回完整独立 HTML 页面
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

    # ===== 非 sidebar 模式：生成侧边栏 + 内容区的单页应用 =====
    primary = (global_config or {}).get('primaryColor', '#004fff')
    sidebar_bg = '#304156'
    sidebar_active_bg = '#1890ff'

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

    # 构建侧边栏菜单
    sidebar_items = []
    for i, (safe_name, display_name, _) in enumerate(page_sections):
        icon_map = {
            0: 'fa-tachometer-alt', 1: 'fa-database', 2: 'fa-folder-open',
            3: 'fa-table', 4: 'fa-edit', 5: 'fa-chart-line',
            6: 'fa-hdd', 7: 'fa-clock', 8: 'fa-users', 9: 'fa-upload'
        }
        icon = icon_map.get(i, 'fa-file')
        sidebar_items.append(
            f'<li @click="currentPage = \'{safe_name}\'"\n'
            f'    :class="[\'sidebar-menu-item\', currentPage === \'{safe_name}\' ? \'active\' : \'\']"\n'
            f'    :style="currentPage === \'{safe_name}\' ? {{backgroundColor: \'{sidebar_active_bg}\'}} : {{}}">\n'
            f'  <i class="fas {icon}"></i>\n'
            f'  <span>{display_name}</span>\n'
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
    title_str = page_sections[0][1] if page_sections else '原型'
    first_page_id = page_sections[0][0] if page_sections else 'page'

    template_link = ''
    if template_css_path:
        template_link = f'    <link rel="stylesheet" href="{template_css_path}">\n'

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
        '.sidebar-menu-item.active { color: #fff; background: ' + sidebar_active_bg + '; }',
        '.sidebar-menu-item i { width: 18px; text-align: center; font-size: 14px; }',
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
    for scripts in collected_scripts:
        if scripts:
            parts.append(scripts)

    parts.extend([
        '</body>',
        '</html>'
    ])
    return '\n'.join(parts)


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


# ==================== Phase 4.5: Edit 增量生成工具 ====================

def find_actual_string(base_content, search_string):
    """查找实际匹配的字符串（参考 Claude Code findActualString）

    utils.ts:73-93 的 Python 翻译：
    1. 先精确匹配
    2. 失败则归一化引号后匹配
    3. 返回 base_content 中的原始字符串（保持引号风格）
    """
    if not search_string or not base_content:
        return None

    # 1. 精确匹配
    if search_string in base_content:
        return search_string

    # 2. 引号归一化匹配（curly quotes -> straight quotes）
    def normalize_quotes(s):
        return (s
                .replace('\u2018', "'").replace('\u2019', "'")   # 单引号
                .replace('\u201c', '"').replace('\u201d', '"'))   # 双引号

    normalized_search = normalize_quotes(search_string)
    normalized_base = normalize_quotes(base_content)

    idx = normalized_base.find(normalized_search)
    if idx != -1:
        return base_content[idx:idx + len(search_string)]

    return None


def build_edit_prompt(page_spec, page_index, total_pages,
                      cross_page_spec_summary='', layout_type='plain'):
    """构建 edit 式页面生成的 prompt

    AI 已经在对话历史中看到了第一页的完整 HTML。
    这个 prompt 让 AI 只输出需要修改的部分。
    """
    prompt = f"""基于你刚才生成的页面，现在生成第 {page_index+1}/{total_pages} 个页面。

## 页面需求：{page_spec.get('name', f'页面{page_index+1}')}
"""
    if page_spec.get('description'):
        prompt += f"**用途**: {page_spec['description']}\n\n"
    if page_spec.get('features'):
        prompt += f"**UI 组件**:\n{page_spec['features']}\n\n"
    if page_spec.get('dataStructure'):
        prompt += f"**数据字段**:\n{page_spec['dataStructure']}\n\n"
    if page_spec.get('interaction'):
        prompt += f"**交互行为**:\n{page_spec['interaction']}\n\n"

    if cross_page_spec_summary:
        prompt += f"\n{cross_page_spec_summary}\n"

    prompt += """
## 输出格式（严格遵守）

不要输出完整 HTML。使用以下 EDIT 格式输出修改指令：

```
EDIT_START
FIND: 要查找的原文字符串（必须与之前生成的 HTML 精确匹配）
REPLACE: 替换后的新字符串
EDIT_END
```

你可以输出多个 EDIT 块来修改不同区域。

**规则**：
1. FIND 字符串必须是之前生成的 HTML 中的精确子串（逐字符匹配）
2. FIND 字符串必须唯一（不能在页面中出现两次）
3. 保持 CSS、导航栏、侧边栏等框架部分不变，只修改内容区域
4. 保持与之前页面完全一致的样式风格和数据字段命名
5. 如果某区域完全不需要修改，不要输出对应的 EDIT 块

**典型修改区域**：
- 主内容区域的 HTML
- 页面标题
- 侧边栏激活状态（sidebar 模式）

如果你认为两个页面差异太大，无法通过 EDIT 完成，可以输出完整 HTML（以 <!DOCTYPE html> 开头）。
"""
    return prompt


def apply_edits_to_base(base_html, ai_response, page_name=''):
    """将 AI 输出的 edit 指令应用到 base HTML

    参考 Claude Code Edit 工具: applyEditToFile() 就是一行 string.replace

    如果 AI 输出完整 HTML（兜底），直接返回。
    如果解析 edit 失败，回退到从 AI 响应提取完整 HTML。

    Returns:
        tuple: (result_html, edit_feedback)
            result_html: 应用 edit 后的 HTML
            edit_feedback: 反馈信息（用于注入到对话历史）
    """
    # 检查 AI 是否直接返回了完整 HTML（兜底）
    if '<!DOCTYPE' in ai_response or ('<html' in ai_response and '</html>' in ai_response):
        logger.info(f"[Edit] AI 返回了完整 HTML，直接使用")
        fragment = extract_page_fragment(ai_response, page_name)
        return fragment, None

    # 解析 EDIT 块
    edit_pattern = re.compile(
        r'EDIT_START\s*\nFIND:\s*(.*?)\nREPLACE:\s*(.*?)\nEDIT_END',
        re.DOTALL
    )
    edits = edit_pattern.findall(ai_response)

    if not edits:
        # 没有找到 EDIT 块，尝试从响应中提取 HTML 片段
        logger.warning(f"[Edit] 未找到 EDIT 块，尝试提取 HTML 片段")
        fragment = extract_page_fragment(ai_response, page_name)
        if fragment and len(fragment) > 100:
            return fragment, None
        # 最终兜底：返回 base_html 不修改
        logger.error(f"[Edit] 无法解析页面「{page_name}」的生成结果")
        return base_html, None

    # 逐个应用 edit（参考 Claude Code applyEditToFile）
    result = base_html
    edit_results = []
    failed_edits = []

    for find_str, replace_str in edits:
        find_str = find_str.strip()
        replace_str = replace_str.strip()

        actual = find_actual_string(result, find_str)
        if actual is not None:
            result = result.replace(actual, replace_str, 1)
            edit_results.append(f"✓ 替换成功: {find_str[:60]}...")
        else:
            edit_results.append(f"✗ 未找到: {find_str[:60]}...")
            failed_edits.append((find_str, replace_str))

    applied_count = len(edit_results) - len(failed_edits)
    logger.info(f"[Edit] 页面「{page_name}」: 应用了 {applied_count}/{len(edits)} 个编辑")

    # 构建反馈
    feedback = f"编辑结果 ({applied_count}/{len(edits)} 成功):\n"
    feedback += "\n".join(edit_results)

    # 成功时注入变更 snippet
    if len(failed_edits) < len(edits):
        snippet = extract_edit_context_snippet(base_html, result)
        if snippet:
            feedback += f"\n\n变更区域上下文:\n```html\n{snippet}\n```"

    # 失败时注入附近代码
    if failed_edits:
        nearby_contexts = []
        for find_str, _ in failed_edits:
            nearby = find_nearby_code(result, find_str)
            if nearby:
                nearby_contexts.append(nearby)
        if nearby_contexts:
            feedback += "\n\n以下是你尝试编辑区域的实际代码，请基于此重试:\n"
            for ctx in nearby_contexts:
                feedback += f"```html\n{ctx}\n```\n"

    return result, feedback


def find_nearby_code(base_html, failed_find_str, context_chars=500):
    """在 edit 失败时，搜索 base_html 中与 FIND 字符串相似的区域

    策略（从精确到模糊）：
    1. 取 FIND 的前 30 字符做前缀搜索
    2. 提取 FIND 中的 HTML 属性（class="xxx"）搜索
    3. 提取 FIND 中的文本内容搜索
    4. 全部失败返回 None
    """
    # 策略 1: 前 30 字符前缀搜索
    prefix = failed_find_str[:30].strip()
    if len(prefix) > 10 and prefix in base_html:
        idx = base_html.index(prefix)
        start = max(0, idx - context_chars)
        end = min(len(base_html), idx + len(failed_find_str) + context_chars)
        return base_html[start:end]

    # 策略 2: 提取 class/id 属性搜索
    class_match = re.search(r'class=["\']([^"\']+)["\']', failed_find_str)
    if class_match:
        class_val = class_match.group(1).split()[0]
        for quote_style in ['"', "'"]:
            search = f'class={quote_style}{class_val}{quote_style}'
            if search in base_html:
                idx = base_html.index(search)
                start = max(0, idx - context_chars)
                end = min(len(base_html), idx + len(failed_find_str) + context_chars)
                return base_html[start:end]

    # 策略 3: 提取纯文本内容（去掉 HTML 标签）
    text_only = re.sub(r'<[^>]+>', '', failed_find_str).strip()[:20]
    if len(text_only) > 5 and text_only in base_html:
        idx = base_html.index(text_only)
        start = max(0, idx - context_chars)
        end = min(len(base_html), idx + context_chars)
        return base_html[start:end]

    return None


def extract_edit_context_snippet(old_html, new_html, context_lines=8):
    """提取 edit 变更区域的上下文 snippet（参考 Claude Code getSnippetForTwoFileDiff）"""
    old_lines = old_html.split('\n')
    new_lines = new_html.split('\n')

    # 找到第一个不同的行
    diff_start = 0
    for i in range(min(len(old_lines), len(new_lines))):
        if old_lines[i] != new_lines[i]:
            diff_start = i
            break
    else:
        # 内容相同或新内容更长
        if len(new_lines) > len(old_lines):
            diff_start = len(old_lines)
        else:
            return ''

    start = max(0, diff_start - context_lines)
    end = min(len(new_lines), diff_start + context_lines * 2)
    snippet_lines = new_lines[start:end]

    # 添加行号
    numbered = []
    for i, line in enumerate(snippet_lines, start + 1):
        numbered.append(f"  {i} | {line}")

    return '\n'.join(numbered)


# ==================== 增量生成工具定义（Function Calling） ====================

incremental_tools = [
    {
        "type": "function",
        "function": {
            "name": "add_page",
            "description": (
                "向多页原型中添加一个新页面。系统会自动创建带 v-show 绑定的 <div> 区域并插入到正确位置。"
                "你只需提供页面的主体内容 HTML、可选的 CSS 样式和 JS 脚本。"
                "每个待生成的页面调用一次。"
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
                        "description": "页面的主体 HTML 内容。只包含内容区域（表格、卡片、表单、图表等），不得包含 <html>、<head>、<body>、侧边栏或导航。"
                    },
                    "css_content": {
                        "type": "string",
                        "description": "此页面特有的 CSS 样式（不含 <style> 标签）。如果不需要额外样式，可省略。"
                    },
                    "script_content": {
                        "type": "string",
                        "description": "此页面特有的 JavaScript（不含 <script> 标签）。使用原生 JS，不要创建 Vue 实例。如果不需要，可省略。"
                    }
                },
                "required": ["page_key", "page_name", "html_content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_current_file",
            "description": (
                "读取磁盘上当前的 index.html 文件。"
                "如果你需要查看现有结构、侧边栏或之前生成的页面，请调用此工具。"
                "可以指定 start_line 和 end_line 读取特定行范围。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_line": {
                        "type": "integer",
                        "description": "起始行号（从1开始），不指定则返回文件摘要"
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "结束行号（包含），不指定则读到末尾"
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
                "对已生成的页面进行精确搜索替换编辑。"
                "old_string 必须精确匹配文件中的内容（从 read_current_file 结果中复制）。"
                "如果编辑失败，系统会告诉你原因——此时应先 read_current_file 再重试。"
                "用于修正 add_page 之后发现的问题。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "old_string": {
                        "type": "string",
                        "description": "要搜索的精确文本片段（2-5行以确保唯一匹配）"
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
        self.base_html = None             # 第一页完整 HTML（edit 基准）
        self.generation_config = {}       # 前端传来的高级配置

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
        self.conversation_messages = []
        self.base_html = None

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
        self.conversation_messages = []
        self.base_html = None

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

        # 取消检查
        if self._is_cancelled():
            return None

        # ---- Round 2: 逐页生成（多轮对话 + Edit 模式） ----
        total = len(pages_data)
        multi_page_mode = self.generation_config.get('multiPageMode', 'conversation')

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

                if i == 0 or multi_page_mode == 'independent':
                    # 首页 或 独立生成模式：完整生成
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
                        cross_page_spec_summary=spec_summary
                    )

                    if multi_page_mode == 'conversation':
                        # 多轮对话模式：用 messages 数组
                        self.conversation_messages.append({"role": "user", "content": page_prompt})
                        page_response = self._call_ai_streaming_with_history(
                            self.conversation_messages, page_images
                        )
                        self.conversation_messages.append({"role": "assistant", "content": page_response})
                    else:
                        # 独立生成模式：单次调用
                        page_response = self._call_ai_streaming(page_prompt, page_images)

                    fragment = extract_page_fragment(page_response, page_name)

                    if not fragment or len(fragment) < 50:
                        raise Exception(f"页面「{page_name}」生成失败：AI 未返回有效内容")

                    # 首页作为 base_html
                    if i == 0 and multi_page_mode == 'conversation':
                        self.base_html = fragment

                    self.page_fragments.append(fragment)

                else:
                    # 后续页面：Edit 式生成（多轮对话模式）
                    edit_prompt = build_edit_prompt(
                        page_spec=page,
                        page_index=i,
                        total_pages=total,
                        cross_page_spec_summary=spec_summary,
                        layout_type=getattr(self, '_layout_type', 'plain')
                    )

                    self.conversation_messages.append({"role": "user", "content": edit_prompt})

                    # 检查是否需要压缩
                    self._maybe_compact()

                    page_response = self._call_ai_streaming_with_history(
                        self.conversation_messages, page_images
                    )
                    self.conversation_messages.append({"role": "assistant", "content": page_response})

                    # 应用 edit 指令
                    page_html, feedback = apply_edits_to_base(
                        self.base_html, page_response, page_name
                    )

                    # 如果有反馈（含失败），处理重试
                    if feedback:
                        MAX_EDIT_RETRIES = 3
                        for retry in range(MAX_EDIT_RETRIES):
                            has_failure = '✗ 未找到' in feedback

                            self.conversation_messages.append({
                                "role": "user",
                                "content": f"[编辑反馈]\n{feedback}"
                            })

                            if not has_failure:
                                break

                            if retry == MAX_EDIT_RETRIES - 1:
                                # 最终兜底：注入完整 base_html
                                self.conversation_messages.append({
                                    "role": "user",
                                    "content": f"[完整基准代码]\n```html\n{self.base_html[:15000]}\n```\n请基于此输出新的 EDIT 指令。"
                                })
                            else:
                                self.conversation_messages.append({
                                    "role": "assistant",
                                    "content": "收到，我根据实际代码重新输出 EDIT 指令。"
                                })

                            retry_response = self._call_ai_streaming_with_history(
                                self.conversation_messages, []
                            )
                            self.conversation_messages.append({"role": "assistant", "content": retry_response})

                            page_html, feedback = apply_edits_to_base(
                                self.base_html, retry_response, page_name
                            )

                    self.page_fragments.append(page_html)

                self._send_event('preview', {'page': page_name, 'html_fragment': self.page_fragments[-1]})

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

        # ---- Round 3: 组装 ----
        self._update_phase(3, 'assembly', 'running')
        logger.info(f"[多轮] Round 3: 组装 {len(self.page_fragments)} 个页面...")

        final_html = assemble_multi_page_html(
            page_fragments=self.page_fragments,
            design_system_css=self.design_system['css_variables'],
            page_names=self.page_names,
            global_config=global_config,
            template_css_path=template_css_path,
            layout_type=getattr(self, '_layout_type', 'plain')
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

    def _run_incremental(self, prompt, pages_data, images, global_config,
                         template_tokens, template_html_summary,
                         template_css_path, template_is_iframe,
                         template_frame_html, template_raw_frame_html,
                         template_layout_type, template_sidebar_meta,
                         confirmed_spec):
        """增量生成模式：每页完成后立即写入磁盘，不做最终拼装。

        流程：
        1. Round 1: 生成设计系统
        2. 第 1 页：生成完整 HTML（含侧边栏导航 + Vue 路由）→ 写入 index.html
        3. 第 2-N 页：读 index.html → AI 生成内容 → 注入 → 写回 index.html
        """
        html_path = os.path.join(self.project_folder, 'index.html')
        total = len(pages_data)

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

        # ---- Round 2: 逐页增量写入 ----
        for i, page in enumerate(pages_data):
            page_name = page.get('name', f'页面{i+1}')
            logger.info(f"[增量] 页面 {i+1}/{total} — {page_name}")
            self._update_phase(2, f'page_{i}', 'running', label=page_name,
                               progress={'current': i+1, 'total': total})

            if self._is_cancelled():
                logger.info(f"[增量] 取消于页面 {i+1}/{total}")
                return None

            try:
                # 构建当前页面的 spec 摘要
                spec_summary = ''
                if confirmed_spec:
                    spec_summary = build_spec_summary_for_page(confirmed_spec, i)

                page_images = self._get_page_images(page, images)

                if i == 0:
                    # === 第 1 页：生成完整 HTML ===
                    first_page_html = self._generate_first_page_complete(
                        page_spec=page,
                        design_system=self.design_system,
                        page_index=0,
                        total_pages=total,
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

                    # 写入磁盘
                    self._update_phase(2, f'page_{i}', 'writing', label=f'写入 {page_name}',
                                       progress={'current': i+1, 'total': total})
                    with open(html_path, 'w', encoding='utf-8') as f:
                        f.write(first_page_html)

                    logger.info(f"[增量] 已写入首页: {len(first_page_html)} 字符")
                    self._send_event('page_written', {
                        'page': page_name, 'index': 0,
                        'path': html_path, 'size': len(first_page_html)
                    })
                    self.base_html = first_page_html

                    # 保存中间状态（供对话模式加载）
                    self.page_fragments = [first_page_html]
                    self.page_names = [p.get('name', f'页面{i+1}') for i, p in enumerate(pages_data)]
                    self._save_incremental_state()

                else:
                    # === 第 2-N 页：不单独处理，在循环结束后由 agentic loop 统一处理 ===
                    pass

                self._update_phase(2, f'page_{i}', 'done', label=page_name,
                                   progress={'current': i+1, 'total': total})

            except Exception as e:
                logger.error(f"[增量] 页面 {i+1} 生成失败: {e}")
                self._send_event('error', {'message': str(e), 'page': page_name})
                # 继续生成下一页

        # ---- Round 3: Agentic Loop 生成第 2-N 页 ----
        if total > 1:
            self._update_phase(3, 'agentic_pages', 'running',
                               label='Agentic 生成后续页面')

            # 读取第 1 页写入后的文件
            if not os.path.exists(html_path):
                logger.error(f"[增量] 首页文件不存在，跳过后续页面: {html_path}")
                self._send_event('error', {'message': '首页生成失败，无法继续后续页面'})
            else:
                with open(html_path, 'r', encoding='utf-8') as f:
                    existing_html = f.read()

                # 构建参数
                page_images_map = {}
                spec_summaries_map = {}
                for i in range(1, total):
                    page_images_map[i] = self._get_page_images(pages_data[i], images)
                    if confirmed_spec:
                        spec_summaries_map[i] = build_spec_summary_for_page(confirmed_spec, i)

                try:
                    final_html = self._run_agentic_pages(
                        pages_to_generate=pages_data[1:],
                        design_system=self.design_system,
                        global_config=global_config,
                        existing_html=existing_html,
                        html_path=html_path,
                        page_images_map=page_images_map,
                        spec_summaries=spec_summaries_map,
                        total_pages=total,
                        pages_data=pages_data
                    )
                    # 确保最终内容写入磁盘
                    with open(html_path, 'w', encoding='utf-8') as f:
                        f.write(final_html)
                    # 更新中间状态（agentic pages 完成后）
                    self._save_incremental_state()
                except Exception as e:
                    logger.error(f"[增量] Agentic 生成失败: {e}")
                    self._send_event('error', {'message': str(e)})
                    # 即使失败也保存已有状态
                    self._save_incremental_state()

            self._update_phase(3, 'agentic_pages', 'done')

        # 完成
        logger.info(f"[增量] 完成，共 {total} 页")
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

        # 构建页面列表（用于侧边栏菜单）
        page_list_defs = []
        for pi, pn in enumerate(self.page_names):
            page_list_defs.append(f'   - page_key: "page_{pi}", 页面名: "{pn}"')
        page_list_str = '\n'.join(page_list_defs)

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

1. **侧边栏导航**：列出所有 {total_pages} 个页面
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
        如果是静态菜单且缺少此项，则追加。
        """
        # 如果 currentPage 比较已存在于某处，说明已在菜单或页面区域
        if f"currentPage === '{page_key}'" in html:
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
            text = '\n'.join(selected)
            return (
                f"文件 index.html 第 {start_line}-{end_line} 行 "
                f"({len(text)} 字符，共 {total_lines} 行):\n"
                f"```\n{text}\n```"
            )

        # 无行范围：返回带行号的全文或摘要
        if len(content) <= MAX_READ_CHARS:
            numbered = '\n'.join(f"L{i+1}: {line}" for i, line in enumerate(lines))
            return (
                f"文件 index.html 完整内容 "
                f"({len(content)} 字符，{total_lines} 行):\n"
                f"```\n{numbered}\n```"
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
                result_lines.append(f"L{i+1}: {line}")
                used += len(line) + 10
            return (
                f"文件 index.html ({len(content)} 字符，{total_lines} 行):\n"
                f"```\n" + '\n'.join(result_lines) + "\n```"
            )

    def _execute_edit_page(self, current_html, args):
        """执行 edit_page 工具调用。3层匹配逻辑。

        Returns: (updated_html, applied, message)
        """
        old_string = args.get('old_string', '')
        new_string = args.get('new_string', '')

        if not old_string:
            return current_html, False, "old_string 不能为空"

        # 第1层：精确匹配
        idx = current_html.find(old_string)
        if idx != -1:
            second_idx = current_html.find(old_string, idx + 1)
            if second_idx != -1:
                return current_html, False, (
                    "old_string 在文件中匹配了多处。请提供更多上下文行以确保唯一匹配。"
                )
            updated = current_html[:idx] + new_string + current_html[idx + len(old_string):]
            return updated, True, "编辑成功应用。"

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

    def _run_agentic_pages(self, pages_to_generate, design_system, global_config,
                           existing_html, html_path, page_images_map, spec_summaries,
                           total_pages, pages_data):
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

        Returns:
            str: 最终 HTML 内容
        """
        # 构建待生成页面信息
        pending_pages = []
        for i, page in enumerate(pages_to_generate):
            page_idx = i + 1  # 第 1 页索引为 0，从 1 开始
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

            gen = self.server.call_ai_model_streaming(
                loop_messages, [],
                cancellable_project_id=self.project_id,
                tools=incremental_tools
            )
            try:
                for chunk_text, full_content, done, tc, *_ in gen:
                    accumulated = full_content
                    if chunk_text and not chunk_text.startswith('[think]'):
                        # 流式推送到 SSE（显示 AI 的状态文字，过滤思考 token）
                        self._send_event('chat', {
                            'role': 'assistant',
                            'content': chunk_text
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
                    page_name = args.get('page_name', '')

                    # 找到 page_index
                    page_idx = None
                    for pp in pending_pages:
                        if pp['key'] == page_key:
                            page_idx = pp['index']
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
                                       label=page_name,
                                       progress={'current': page_idx, 'total': total_pages})

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
                            'page': page_name,
                            'index': page_idx,
                            'path': html_path,
                            'size': len(current_html)
                        })
                        self._update_phase(2, f'page_{page_idx}', 'done',
                                           label=page_name,
                                           progress={'current': page_idx, 'total': total_pages})

                        logger.info(f"[Agent] 页面已添加: {page_name} ({page_key}), "
                                    f"文件大小: {len(current_html)}")
                    else:
                        logger.warning(f"[Agent] add_page 失败: {message}")

                    loop_messages.append({
                        'role': 'tool',
                        'tool_call_id': tc_id,
                        'name': 'add_page',
                        'content': message
                    })

                elif tool_name == 'read_current_file':
                    content = self._execute_read_current_file(html_path, args)
                    loop_messages.append({
                        'role': 'tool',
                        'tool_call_id': tc_id,
                        'name': 'read_current_file',
                        'content': content
                    })

                elif tool_name == 'edit_page':
                    updated_html, applied, message = self._execute_edit_page(
                        current_html, args
                    )

                    if applied:
                        current_html = updated_html
                        with open(html_path, 'w', encoding='utf-8') as f:
                            f.write(current_html)
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
        threshold_pct = self.generation_config.get('compactThreshold', 65)
        compress_threshold = max_context * (threshold_pct / 100.0)

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
        """压缩对话历史（参考 Claude Code compactConversation）

        1. self.base_html 单独保存，不受压缩影响
        2. 将 messages 中早期页面的 assistant response 替换为 AI 摘要
        3. 保留最近 2 页完整对话
        4. 压缩后重新注入 base_html 作为"附件"消息
        """
        if len(self.conversation_messages) < 4:
            return

        logger.info(f"[多轮] 压缩对话历史: {len(self.conversation_messages)} 条消息")

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

        # 关键步骤：重新注入 base_html
        self._reinject_base_html()

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

    def _reinject_base_html(self):
        """压缩后重新注入 base_html（参考 Claude Code createPostCompactFileAttachments）"""
        if not self.base_html:
            return

        MAX_CHARS = 10000

        # 移除旧的注入
        self.conversation_messages = [
            msg for msg in self.conversation_messages
            if not (msg.get('role') == 'user' and
                    msg.get('content', '').startswith('[基准页面参考]'))
            and not (msg.get('role') == 'assistant' and
                     msg.get('content', '') == '收到，后续将基于此进行 EDIT 操作。')
        ]

        truncated = self.base_html[:MAX_CHARS]
        truncated_note = f"\n\n（已截断，原始共 {len(self.base_html)} 字符）" if len(self.base_html) > MAX_CHARS else ""

        self.conversation_messages.append({
            "role": "user",
            "content": (
                f"[基准页面参考 — 用于 EDIT 的 FIND 匹配，以下是第一页的完整 HTML]\n"
                f"```html\n{truncated}{truncated_note}\n```\n"
                f"后续页面生成时，请基于此 HTML 输出 EDIT 指令。"
            )
        })
        self.conversation_messages.append({
            "role": "assistant",
            "content": "收到，后续将基于此进行 EDIT 操作。"
        })

        logger.info(f"[多轮] 已重新注入 base_html（{len(truncated)} 字符）")

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
            'conversation_messages': self.conversation_messages,
            'base_html': self.base_html,
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
                layout_type=getattr(self, '_layout_type', 'plain')
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
            'conversation_messages': getattr(self, 'conversation_messages', []),
            'base_html': current_html,
        }
        state_path = os.path.join(self.project_folder, 'multi_round_state.json')
        try:
            with open(state_path, 'w', encoding='utf-8') as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            logger.info(f"[增量] 已保存中间状态 ({len(current_html)} 字符)")
        except Exception as e:
            logger.warning(f"[增量] 保存中间状态失败: {e}")
