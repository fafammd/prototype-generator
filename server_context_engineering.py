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
                             template_frame_html='', template_layout_type='plain'):
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
    return prompt


def extract_page_fragment(ai_response, page_name=''):
    """从 Round 2 AI 响应中提取页面 HTML

    处理 AI 可能输出完整 HTML 文档或带 markdown 包裹的情况。
    """
    # 尝试提取 ```html 代码块
    html_match = re.search(r'```(?:html|HTML)?\s*\n([\s\S]*?)```', ai_response)
    if html_match:
        fragment = html_match.group(1).strip()
        if '<!DOCTYPE' in fragment or '<html' in fragment:
            return fragment
        return fragment

    # 尝试直接找完整 HTML
    doctype_match = re.search(r'(<!DOCTYPE[\s\S]*</html>)', ai_response, re.IGNORECASE)
    if doctype_match:
        return doctype_match.group(1).strip()

    # 尝试找 <html> 到 </html>
    html_tag_match = re.search(r'(<html[\s\S]*?</html>)', ai_response, re.IGNORECASE)
    if html_tag_match:
        return html_tag_match.group(1).strip()

    # 降级：返回全部内容
    return ai_response.strip()


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
        # 多页：首个页面作为主页面返回（后续可扩展多页切换）
        return page_sections[0][2]

    # ===== 非 sidebar 模式：原有逻辑，返回完整独立 HTML（带 Vue 导航） =====
    # 构建导航标签
    nav_items = []
    for safe_name, display_name, _ in page_sections:
        nav_items.append(
            '            <button @click="currentPage = \'' + safe_name + '\'"\n'
            '                    :class="[\'px-4 py-2 rounded-lg text-sm font-medium transition-all\',\n'
            '                             currentPage === \'' + safe_name + '\' ? \'bg-white shadow-sm text-indigo-600\' : \'text-gray-500 hover:text-gray-700 hover:bg-white/50\'\n'
            '                    ]">\n'
            '                ' + display_name + '\n'
            '            </button>'
        )

    # 构建页面内容区（用 iframe + srcdoc 嵌入每个页面）
    page_divs = []
    for safe_name, display_name, content in page_sections:
        page_divs.append(
            '    <div v-if="currentPage === \'' + safe_name + '\'" class="page-container">\n'
            '        <!-- Page: ' + display_name + ' -->\n'
            '        <iframe :srcdoc="pageData[\'' + safe_name + '\']" \n'
            '                class="w-full border-0" \n'
            '                :style="{ minHeight: \'calc(100vh - 80px)\' }"\n'
            '                @load="onIframeLoad($event, \'' + safe_name + '\')"></iframe>\n'
            '    </div>'
        )

    # 构建页面数据（转义 HTML 用于 srcdoc）
    page_data_entries = []
    for safe_name, _, content in page_sections:
        escaped = (content
                   .replace('\\', '\\\\')
                   .replace("'", "\\'")
                   .replace('\n', '\\n')
                   .replace('\r', '')
                   .replace('</script>', '<\\/script>'))
        page_data_entries.append("            '" + safe_name + "': '" + escaped + "'")

    page_data_str = ',\n'.join(page_data_entries)
    nav_str = '\n'.join(nav_items)
    divs_str = '\n'.join(page_divs)
    title_str = ' | '.join(n for _, n, _ in page_sections)
    first_page_id = page_sections[0][0] if page_sections else 'page'

    template_link = ''
    if template_css_path:
        template_link = '    <link rel="stylesheet" href="' + template_css_path + '">\n'

    parts = [
        '<!DOCTYPE html>',
        '<html lang="zh-CN">',
        '<head>',
        '    <meta charset="UTF-8">',
        '    <meta name="viewport" content="width=device-width, initial-scale=1.0">',
        '    <title>' + title_str + '</title>',
        '    <script src="https://unpkg.com/vue@3/dist/vue.global.prod.js"></script>',
        '    <script src="https://cdn.tailwindcss.com"></script>',
        '    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">',
        template_link,
        '    <style>',
        design_system_css,
        '',
        '/* 页面导航 */',
        '.page-nav {',
        '    display: flex;',
        '    gap: 4px;',
        '    padding: 8px;',
        '    background: rgba(0,0,0,0.03);',
        '    border-radius: 12px;',
        '    margin-bottom: 16px;',
        '}',
        '.page-container {',
        '    animation: fadeIn 0.2s ease;',
        '}',
        '@keyframes fadeIn {',
        '    from { opacity: 0; transform: translateY(4px); }',
        '    to { opacity: 1; transform: translateY(0); }',
        '}',
        '    </style>',
        '</head>',
        '<body class="' + bg_class + ' min-h-screen">',
        '<div id="app" class="max-w-[1400px] mx-auto p-4">',
        '    <!-- 页面导航 -->',
        '    <div class="page-nav">',
        nav_str,
        '    </div>',
        '',
        '    <!-- 页面内容 -->',
        divs_str,
        '</div>',
        '',
        '<script>',
        'const { createApp, ref, onMounted } = Vue;',
        '',
        'createApp({',
        '    setup() {',
        "        const currentPage = ref('" + first_page_id + "');",
        '        const pageData = {',
        page_data_str,
        '        };',
        '',
        '        // 页面切换 → 通知父级 viewer',
        '        function notifyPageChange() {',
        '            if (window.parent !== window) {',
        '                window.parent.postMessage({',
        "                    type: 'pageChange',",
        '                    page: currentPage.value',
        "                }, '*');",
        '            }',
        '            window.currentPage = currentPage.value;',
        '        }',
        '',
        '        // iframe 加载完成后自适应高度',
        '        function onIframeLoad(event, pageName) {',
        '            const iframe = event.target;',
        '            try {',
        '                const doc = iframe.contentDocument || iframe.contentWindow.document;',
        "                iframe.style.height = doc.body.scrollHeight + 'px';",
        '            } catch(e) {}',
        '        }',
        '',
        '        // 监听父级导航指令',
        "        window.addEventListener('message', (e) => {",
        "            if (e.data && e.data.type === 'navigateTo') {",
        '                const target = e.data.page;',
        '                if (pageData[target] !== undefined) {',
        '                    currentPage.value = target;',
        '                    notifyPageChange();',
        '                }',
        '            }',
        '        });',
        '',
        '        onMounted(() => {',
        '            window.currentPage = currentPage.value;',
        '            notifyPageChange();',
        '        });',
        '',
        '        return { currentPage, pageData, onIframeLoad };',
        '    }',
        "}).mount('#app');",
        '</script>',
        '</body>',
        '</html>'
    ]
    return '\n'.join(parts)


def _make_safe_page_id(name):
    """将页面名转为安全的 Vue 变量名"""
    # 保留中文、字母、数字、下划线
    safe = re.sub(r'[^a-zA-Z0-9\u4e00-\u9fff_-]', '_', name)
    if not safe:
        safe = 'page'
    return safe


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

    def run(self, prompt, pages_data, images, global_config,
            template_tokens='', template_html_summary='',
            template_css_path='', template_is_iframe=False,
            template_frame_html='', template_raw_frame_html='',
            template_layout_type='plain'):
        """执行三轮生成，返回最终 HTML

        Args:
            prompt: 原始完整 prompt（用于参考）
            pages_data: 页面规格列表
            images: 参考图片 base64 列表
            global_config: 全局设计配置
            template_*: 模板相关参数

        Returns:
            str: 最终组装的完整 HTML
        """
        self.page_names = [p.get('name', f'页面{i+1}') for i, p in enumerate(pages_data)]
        self._layout_type = template_layout_type

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

        # ---- Round 2: 逐页生成 ----
        total = len(pages_data)
        for i, page in enumerate(pages_data):
            page_name = page.get('name', f'页面{i+1}')
            logger.info(f"[多轮] Round 2: 页面 {i+1}/{total} — {page_name}")
            self._update_phase(2, f'page_{i}', 'running', label=page_name,
                               progress={'current': i+1, 'total': total})

            if self._is_cancelled():
                logger.info(f"[多轮] 取消于页面 {i+1}/{total}")
                return None

            try:
                # 构建单页 prompt
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
                    template_layout_type=getattr(self, '_layout_type', 'plain')
                )

                # 获取该页面的参考图片
                page_images = self._get_page_images(page, images)

                # 调用 AI
                page_response = self._call_ai_streaming(page_prompt, page_images)
                fragment = extract_page_fragment(page_response, page_name)

                if not fragment or len(fragment) < 50:
                    raise Exception(f"页面「{page_name}」生成失败：AI 未返回有效内容")

                self.page_fragments.append(fragment)
                self._send_event('preview', {'page': page_name, 'html_fragment': fragment})

            except Exception as e:
                logger.error(f"[多轮] 页面 {i+1} 生成失败: {e}")
                # 错误占位符
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
                        if chunk_text:
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
        """保存中间状态（支持部分恢复）"""
        state = {
            'design_system': self.design_system,
            'page_fragments': self.page_fragments,
            'page_names': self.page_names,
        }
        state_path = os.path.join(self.project_folder, 'multi_round_state.json')
        try:
            with open(state_path, 'w', encoding='utf-8') as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[多轮] 保存中间状态失败: {e}")
