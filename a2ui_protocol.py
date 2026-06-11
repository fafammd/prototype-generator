"""
A2UI 组件树协议 — JSONL 格式定义、流式解析、HTML 转换

协议规范：
  AI 输出 JSONL（每行一个 JSON 对象），两种行类型：
  1. 标记行：{"type":"marker","marker":"page-start|page-end","page":"<name>",...}
  2. 组件行：{"type":"component","id":"<unique>","component":"<Type>","style":"<tailwind>","props":{...},"children":[...]}

流式解析：
  parse_jsonl_stream(buffer) 从流缓冲区提取完整 JSONL 行

HTML 转换：
  jsonl_to_html(lines) 将完整 JSONL 组件树转为独立 HTML 文档
"""

import json
import re

# ==================== 系统提示 ====================

A2UI_SYSTEM_PROMPT = """You are a UI component tree generator. You output a JSON Lines (JSONL) component tree that describes a complete web page.

OUTPUT FORMAT — JSON Lines (one JSON object per line):

Line 1: {"type":"marker","marker":"page-start","page":"<PageName>","index":<0-based>,"total":<totalPages>,"designTokens":{"primaryColor":"<hex>","secondaryColor":"<hex>","bgMode":"light|dark"}}
Lines 2..N-1: {"type":"component","id":"<uniqueId>","component":"<ComponentType>","style":"<tailwind classes>","props":{...},"children":["<childId1>","<childId2>"]}
Last line: {"type":"marker","marker":"page-end","page":"<PageName>"}

RULES:
1. Every line is a complete JSON object on a SINGLE line (no newlines inside JSON)
2. Root component MUST have id "root"
3. All child IDs referenced in "children" MUST be defined as separate component lines
4. Use Tailwind CSS classes for styling (via CDN, available at runtime)
5. Use FontAwesome icons (fa-* classes)
6. Use REAL Chinese data — never use Lorem ipsum or placeholder text
7. Output ONLY JSONL lines — no explanation, no markdown, no code fences

COMPONENT TYPES:

Layout:
  Page: root container. style: tailwind for background/layout
  FlexRow: flex row container. style: flex-related classes. props: gap, wrap, align, justify
  FlexColumn: flex column container. style: flex-col classes. props: gap, align
  Grid: grid layout. props: {cols: number, gap: number}

Navigation:
  NavBar: top navigation bar. props: {title: string, logo: "fa-icon", breadcrumbs: [{label}]}
  SideBar: side navigation. props: {width: "w-56", items: [{label, icon: "fa-*", active: bool}]}
  Tabs: tab bar. props: {items: [{label, active: bool}]}

Content:
  Card: card container. props: {title: string, extra: "<html>"}
  Table: data table. props: {columns: [{key, label, render?: "badge"|"tag"}], data: [{key: value}], stripe, scrollX, pagination: {total, pageSize}}
  Form: form with fields. props: {fields: [{label, type: "text"|"select"|"textarea", placeholder, required, options: []}]}
  List: item list. props: {items: [{label, description?}]}
  StatRow: statistic card. props: {label, value, icon: "fa-*", trend: "+12.5%", trendUp: bool}
  Chart: chart placeholder. props: {chartType: "bar"|"line"|"pie", height: number}

Basic:
  Text: text content. props: {content: string, tag: "p"|"h1"|"h2"|"h3"|"span"|"div"}
  Button: button. props: {label, variant: "primary"|"secondary"|"danger"|"ghost"|"success", icon: "fa-*", size: "sm"|"lg"}
  Input: text input. props: {placeholder, type, prepend: "fa-icon"}
  Badge: status badge. props: {text, variant: "success"|"warning"|"danger"|"info"}
  Tag: tag label. props: {text, color: "border-gray-300 text-gray-600"}
  Icon: icon. props: {name: "fa-*", size: number, color}
  Avatar: user avatar. props: {src?, text?, alt?}
  Modal: dialog. props: {title, visible: bool, width: "max-w-lg"}
  Progress: progress bar. props: {percent: 0-100, status: "success"|"exception"}
  Empty: empty state. props: {description, icon: "fa-*"}

Escape hatch:
  RawHtml: raw HTML content for complex components. props: {html: "<raw html string>"}

EXAMPLE — Dashboard page:
{"type":"marker","marker":"page-start","page":"Dashboard","index":0,"total":1,"designTokens":{"primaryColor":"#4f46e5","secondaryColor":"#10b981","bgMode":"light"}}
{"type":"component","id":"root","component":"Page","style":"min-h-screen bg-gray-50","props":{},"children":["nav1","body1"]}
{"type":"component","id":"nav1","component":"NavBar","style":"bg-white shadow-sm px-6 py-3 flex items-center justify-between","props":{"title":"Dashboard","logo":"fa-chart-line"},"children":["nav-r"]}
{"type":"component","id":"nav-r","component":"FlexRow","style":"items-center gap-3","props":{},"children":["av1"]}
{"type":"component","id":"av1","component":"Avatar","style":"w-8 h-8 text-sm bg-indigo-500","props":{"text":"A","alt":"Admin"},"children":[]}
{"type":"component","id":"body1","component":"FlexColumn","style":"flex-1 p-6 gap-6 max-w-7xl mx-auto w-full","props":{},"children":["stats1","card1"]}
{"type":"component","id":"stats1","component":"FlexRow","style":"gap-4 flex-wrap","props":{},"children":["s1","s2","s3","s4"]}
{"type":"component","id":"s1","component":"StatRow","style":"flex-1 min-w-[200px]","props":{"label":"Total Users","value":"12,846","icon":"fa-users","trend":"+12.5%","trendUp":true},"children":[]}
{"type":"component","id":"s2","component":"StatRow","style":"flex-1 min-w-[200px]","props":{"label":"Active","value":"8,492","icon":"fa-play-circle","trend":"+5.7%","trendUp":true},"children":[]}
{"type":"component","id":"s3","component":"StatRow","style":"flex-1 min-w-[200px]","props":{"label":"Revenue","value":"¥2.4M","icon":"fa-yen-sign","trend":"-2.1%","trendUp":false},"children":[]}
{"type":"component","id":"s4","component":"StatRow","style":"flex-1 min-w-[200px]","props":{"label":"Uptime","value":"99.97%","icon":"fa-shield-alt","trend":"+0.02%","trendUp":true},"children":[]}
{"type":"component","id":"card1","component":"Card","style":"","props":{"title":"Recent Tasks","extra":"<button class='px-3 py-1.5 text-sm bg-indigo-500 text-white rounded-lg'>New Task</button>"},"children":["tbl1"]}
{"type":"component","id":"tbl1","component":"Table","style":"","props":{"columns":[{"key":"name","label":"Name"},{"key":"status","label":"Status","render":"badge"},{"key":"date","label":"Date"}],"data":[{"name":"Task A","status":"success","date":"2024-01-15"},{"name":"Task B","status":"warning","date":"2024-02-20"}],"pagination":{"total":50,"pageSize":10}},"children":[]}
{"type":"marker","marker":"page-end","page":"Dashboard"}

CRITICAL: Output ONLY JSONL. No markdown. No explanation. Start with page-start marker, end with page-end marker.
"""


# ==================== 流式 JSONL 解析 ====================

def parse_jsonl_stream(buffer):
    """从流缓冲区提取完整的 JSONL 行。

    Args:
        buffer: 累积的文本缓冲区（可能包含多行和不完整行）

    Returns:
        (complete_lines, remaining_buffer):
            complete_lines: 完整行的列表
            remaining_buffer: 剩余的不完整文本
    """
    if not buffer:
        return [], ''

    lines = buffer.split('\n')
    complete_lines = []

    # 最后一行可能不完整（没有换行符结束），保留在缓冲区
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue

        # 尝试解析为 JSON
        try:
            json.loads(stripped)
            complete_lines.append(stripped)
        except json.JSONDecodeError:
            # 可能是不完整的行 — 如果是最后一行则保留
            if i == len(lines) - 1:
                return complete_lines, stripped
            # 非最后一行的解析失败 — 跳过（可能是非 JSONL 文本）
            continue

    return complete_lines, ''


# ==================== JSONL → HTML 转换 ====================

def jsonl_to_html(jsonl_text_or_lines, design_tokens=None):
    """将 JSONL 组件树转换为完整独立 HTML 文档。

    Args:
        jsonl_text_or_lines: JSONL 文本（str）或行列表（list[str]）
        design_tokens: 额外设计令牌（可选）

    Returns:
        完整 HTML 文档字符串，如果输入无效则返回空字符串
    """
    if isinstance(jsonl_text_or_lines, str):
        lines = [l.strip() for l in jsonl_text_or_lines.split('\n') if l.strip()]
    else:
        lines = [l.strip() for l in jsonl_text_or_lines if l and l.strip()]

    components = {}
    page_start = None
    page_end = None

    for line in lines:
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue

        if parsed.get('type') == 'marker':
            marker = parsed.get('marker')
            if marker == 'page-start':
                page_start = parsed
            elif marker == 'page-end':
                page_end = parsed
        elif parsed.get('type') == 'component':
            comp_id = parsed.get('id')
            if comp_id:
                components[comp_id] = parsed

    root = components.get('root')
    if not root:
        return ''

    tokens = {}
    if page_start and page_start.get('designTokens'):
        tokens.update(page_start['designTokens'])
    if design_tokens:
        tokens.update(design_tokens)

    title = page_start.get('page', 'Page') if page_start else 'Page'
    bg_mode = tokens.get('bgMode', 'light')
    bg_color = '#1a1a2e' if bg_mode == 'dark' else '#f5f5f5'
    primary_color = tokens.get('primaryColor', '#004fff')
    secondary_color = tokens.get('secondaryColor', '#10b981')

    body_html = _render_component(root, components)

    return (
        '<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n'
        '  <meta charset="UTF-8">\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        f'  <title>{_esc(title)}</title>\n'
        '  <script src="https://cdn.tailwindcss.com"></script>\n'
        '  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">\n'
        '  <style>\n'
        f'    :root {{ --primary-color: {primary_color}; --secondary-color: {secondary_color}; }}\n'
        f'    body {{ margin: 0; background: {bg_color}; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}\n'
        '    .stripe tbody tr:nth-child(even) { background: #f9fafb; }\n'
        '  </style>\n'
        '</head>\n<body>\n'
        f'{body_html}\n'
        '</body>\n</html>'
    )


# ==================== 组件渲染器 ====================

def _render_component(comp, all_components):
    """递归渲染组件为 HTML 字符串。"""
    comp_type = comp.get('component', '')
    style = comp.get('style', '')
    props = comp.get('props', {})
    children_ids = comp.get('children', [])

    children_html = ''
    for child_id in children_ids:
        child = all_components.get(child_id)
        if child:
            children_html += '\n' + _render_component(child, all_components)

    renderer = COMPONENT_RENDERERS.get(comp_type)
    if renderer:
        return renderer(style, props, children_html)

    # 未知组件 fallback
    if comp_type == 'RawHtml':
        return props.get('html', '')
    return f'<!-- unknown: {comp_type} -->'


def _esc(s):
    """HTML 文本转义。"""
    if not s:
        return ''
    return str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')


def _esc_attr(s):
    """HTML 属性值转义。"""
    if not s:
        return ''
    return str(s).replace('&', '&amp;').replace('"', '&quot;').replace('<', '&lt;')


COMPONENT_RENDERERS = {
    # ---- 布局 ----
    'Page': lambda s, p, c: f'<div class="{s}">{c}</div>',
    'FlexRow': lambda s, p, c: f'<div class="flex {s}">{c}</div>',
    'FlexColumn': lambda s, p, c: f'<div class="flex flex-col {s}">{c}</div>',
    'Grid': lambda s, p, c: f'<div class="grid grid-cols-{p.get("cols",3)} gap-{p.get("gap",4)} {s}">{c}</div>',

    # ---- 导航 ----
    'NavBar': lambda s, p, c: (
        f'<nav class="{s}">'
        f'{_icon(p.get("logo",""), "mr-2", "var(--primary-color)")}'
        f'<span class="font-semibold text-lg">{_esc(p.get("title",""))}</span>'
        f'{_breadcrumbs(p.get("breadcrumbs",[]))}'
        f'{c}</nav>'
    ),
    'SideBar': lambda s, p, c: (
        f'<aside class="{p.get("width","w-56")} {s} flex-shrink-0">'
        f'{_sidebar_items(p.get("items",[]))}'
        f'{c}</aside>'
    ),
    'Tabs': lambda s, p, c: (
        '<div class="flex border-b ' + s + '">'
        + ''.join(
            f'<button class="px-4 py-2.5 text-sm {"border-b-2 border-blue-500 text-blue-600 font-medium" if t.get("active") else "text-gray-500 hover:text-gray-700"} transition-colors">{_esc(t.get("label",""))}</button>'
            for t in p.get('items', [])
        )
        + f'{c}</div>'
    ),

    # ---- 内容 ----
    'Card': lambda s, p, c: (
        f'<div class="bg-white rounded-xl shadow-sm p-5 {s}">'
        f'{_card_header(p)}'
        f'{c}</div>'
    ),
    'Table': lambda s, p, c: _render_table(s, p, c),
    'Form': lambda s, p, c: f'<form class="{s}">{_form_fields(p.get("fields",[]))}{c}</form>',
    'List': lambda s, p, c: f'<div class="{s}">{_list_items(p.get("items",[]))}{c}</div>',
    'StatRow': lambda s, p, c: (
        f'<div class="bg-white rounded-xl shadow-sm p-5 {s}">'
        f'<div class="flex items-center justify-between">'
        f'{_icon_box(p.get("icon",""))}'
        f'<div class="text-right">{_trend(p.get("trend"), p.get("trendUp", True))}</div></div>'
        f'<div class="mt-3"><p class="text-sm text-gray-500">{_esc(p.get("label",""))}</p>'
        f'<p class="text-2xl font-bold text-gray-900 mt-1">{_esc(p.get("value",""))}</p></div></div>'
    ),
    'Chart': lambda s, p, c: (
        f'<div class="bg-white rounded-xl shadow-sm p-5 {s}" style="min-height:{p.get("height",250)}px">'
        f'<div class="flex items-center justify-center h-full text-gray-300">'
        f'<i class="fas fa-chart-{"pie" if p.get("chartType")=="pie" else "bar"} text-4xl"></i></div>{c}</div>'
    ),

    # ---- 基础 ----
    'Text': lambda s, p, c: f'<{p.get("tag","p")} class="{s}">{p.get("content","")}</{p.get("tag","p")}>',
    'Button': lambda s, p, c: (
        f'<button class="rounded-lg font-medium transition-colors '
        f'{{"primary":"bg-blue-500 text-white hover:bg-blue-600 shadow-sm","secondary":"bg-gray-100 text-gray-700 hover:bg-gray-200",'
        f'"danger":"bg-red-500 text-white hover:bg-red-600","ghost":"text-gray-600 hover:bg-gray-100",'
        f'"success":"bg-green-500 text-white hover:bg-green-600"}}.get(p.get("variant"),"primary") '
        f'{"px-3 py-1.5 text-xs" if p.get("size")=="sm" else "px-6 py-3 text-base" if p.get("size")=="lg" else "px-4 py-2 text-sm"} '
        f'{s}">{_icon_in(p.get("icon"),"mr-1")}{_esc(p.get("label",""))}</button>'
    ),
    'Input': lambda s, p, c: (
        f'<div class="relative">'
        f'{_input_prepend(p.get("prepend",""))}'
        f'<input type="{p.get("type","text")}" placeholder="{_esc_attr(p.get("placeholder",""))}" '
        f'class="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500 '
        f'{"pl-10 " if p.get("prepend") else ""}{s}"></div>'
    ),
    'Badge': lambda s, p, c: (
        f'<span class="px-2.5 py-0.5 rounded-full text-xs font-medium '
        f'{{"success":"bg-green-100 text-green-700","warning":"bg-yellow-100 text-yellow-700",'
        f'"danger":"bg-red-100 text-red-700","info":"bg-blue-100 text-blue-700"}}'
        f'.get(p.get("variant"),"bg-gray-100 text-gray-700") {s}">{_esc(p.get("text",""))}</span>'
    ),
    'Tag': lambda s, p, c: f'<span class="inline-flex items-center px-2 py-0.5 rounded text-xs border {p.get("color","border-gray-300 text-gray-600")} {s}">{_esc(p.get("text",""))}</span>',
    'Icon': lambda s, p, c: f'<i class="fas {p.get("name","")} {s}" style="font-size:{p.get("size",16)}px;{"color:"+p["color"]+";" if p.get("color") else ""}"></i>',
    'Avatar': lambda s, p, c: (
        f'<img src="{_esc_attr(p.get("src",""))}" alt="{_esc_attr(p.get("alt",""))}" class="rounded-full {s}">' if p.get('src')
        else f'<div class="rounded-full flex items-center justify-center text-white font-medium {s}">{_esc(p.get("text","?"))}</div>'
    ),
    'Modal': lambda s, p, c: '' if not p.get('visible') else (
        f'<div class="fixed inset-0 bg-black/50 flex items-center justify-center z-50">'
        f'<div class="bg-white rounded-xl shadow-2xl {p.get("width","max-w-lg")} {s}">'
        f'<div class="px-6 py-4 border-b flex items-center justify-between">'
        f'<h3 class="font-semibold text-lg">{_esc(p.get("title",""))}</h3>'
        f'<button class="text-gray-400 hover:text-gray-600"><i class="fas fa-times"></i></button></div>'
        f'<div class="px-6 py-4">{c}</div></div></div>'
    ),
    'Progress': lambda s, p, c: (
        f'<div class="w-full bg-gray-200 rounded-full h-2 {s}">'
        f'<div class="{"bg-red-500" if p.get("status")=="exception" else "bg-green-500" if p.get("status")=="success" else p.get("strokeColor","bg-blue-500")} h-2 rounded-full transition-all" '
        f'style="width:{p.get("percent",0)}%"></div></div>'
    ),
    'Empty': lambda s, p, c: (
        f'<div class="flex flex-col items-center justify-center py-12 {s}">'
        f'<i class="fas {p.get("icon","fa-inbox")} text-4xl text-gray-300 mb-3"></i>'
        f'<p class="text-gray-400 text-sm">{_esc(p.get("description","暂无数据"))}</p></div>'
    ),
    'RawHtml': lambda s, p, c: f'<div class="{s}">{p.get("html","")}</div>',
    'Image': lambda s, p, c: f'<img src="{_esc_attr(p.get("src",""))}" alt="{_esc_attr(p.get("alt",""))}" class="{s}">',
}


# ==================== 渲染辅助函数 ====================

def _icon(name, extra_class='', color=''):
    """FontAwesome 图标。"""
    if not name:
        return ''
    style = f' style="color:{color}"' if color else ''
    return f'<i class="fas {name} {extra_class}"{style}></i>'


def _icon_in(name, extra_class=''):
    if not name:
        return ''
    return f'<i class="fas {name} {extra_class}"></i>'


def _icon_box(name):
    """统计卡片的图标盒子。"""
    if not name:
        return ''
    return f'<div class="w-12 h-12 rounded-xl bg-blue-50 flex items-center justify-center"><i class="fas {name} text-blue-500 text-lg"></i></div>'


def _trend(text, is_up):
    """趋势指标。"""
    if not text:
        return ''
    color = 'text-green-500' if is_up else 'text-red-500'
    arrow = 'fa-arrow-up' if is_up else 'fa-arrow-down'
    return f'<span class="{color} text-sm font-medium"><i class="fas {arrow} text-xs"></i> {_esc(text)}</span>'


def _breadcrumbs(items):
    """面包屑导航。"""
    if not items:
        return ''
    parts = []
    for i, item in enumerate(items):
        label = item.get('label', '')
        if i < len(items) - 1:
            parts.append(f'<a href="#" class="text-gray-500 hover:text-gray-700">{_esc(label)}</a>')
        else:
            parts.append(f'<span class="text-gray-900 font-medium">{_esc(label)}</span>')
    return '<nav class="text-sm ml-4">' + ' <span class="mx-1 text-gray-400">/</span> '.join(parts) + '</nav>'


def _sidebar_items(items):
    """侧边栏菜单项。"""
    html = ''
    for item in items:
        active = item.get('active', False)
        cls = 'bg-blue-50 text-blue-600 font-medium' if active else 'text-gray-600 hover:bg-gray-100'
        icon = f'<i class="fas {item["icon"]} w-5 text-center"></i>' if item.get('icon') else ''
        html += f'<a href="#" class="flex items-center px-4 py-2.5 text-sm {cls} transition-colors"><span class="mr-3">{icon}</span>{_esc(item.get("label",""))}</a>'
    return html


def _card_header(props):
    """卡片头部（标题 + 额外内容）。"""
    title = props.get('title')
    if not title:
        return ''
    extra = props.get('extra', '')
    return f'<div class="flex items-center justify-between mb-4"><h3 class="text-lg font-semibold text-gray-800">{_esc(title)}</h3>{extra}</div>'


def _render_table(style, props, children):
    """渲染表格组件。"""
    columns = props.get('columns', [])
    data = props.get('data', [])
    stripe = props.get('stripe', True)
    stripe_cls = 'stripe' if stripe else ''

    th = ''.join(f'<th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">{_esc(c.get("label",""))}</th>' for c in columns)

    rows = ''
    for row in data:
        cells = ''
        for col in columns:
            key = col.get('key', '')
            val = row.get(key, '')
            render = col.get('render')

            if render == 'badge':
                badge_colors = {'success': 'green', 'warning': 'yellow', 'danger': 'red', 'info': 'blue'}
                bc = badge_colors.get(val, 'gray')
                cells += f'<td class="px-4 py-3 text-sm"><span class="px-2.5 py-0.5 rounded-full text-xs font-medium bg-{bc}-100 text-{bc}-700">{_esc(val)}</span></td>'
            elif render == 'tag':
                cells += f'<td class="px-4 py-3 text-sm"><span class="px-2 py-0.5 rounded text-xs border border-gray-300 text-gray-600">{_esc(val)}</span></td>'
            else:
                cells += f'<td class="px-4 py-3 text-sm text-gray-900">{_esc(val)}</td>'

        rows += f'<tr class="hover:bg-gray-50 border-b border-gray-100">{cells}</tr>'

    table = f'<table class="min-w-full {stripe_cls}"><thead class="bg-gray-50"><tr>{th}</tr></thead><tbody>{rows}</tbody></table>'

    # 分页
    pagination = ''
    pag = props.get('pagination')
    if pag:
        total = pag.get('total', 0)
        page_size = pag.get('pageSize', 10)
        pages = max(1, -(-total // page_size))  # ceil division
        pbuttons = ''
        for i in range(1, min(pages + 1, 6)):
            active = 'bg-blue-500 text-white' if i == 1 else 'bg-white text-gray-600 hover:bg-gray-50 border'
            pbuttons += f'<button class="px-3 py-1 rounded text-sm {active}">{i}</button>'
        pagination = (
            f'<div class="flex items-center justify-between mt-4 pt-3 border-t">'
            f'<span class="text-sm text-gray-500">共 {total} 条</span>'
            f'<div class="flex gap-1">{pbuttons}</div></div>'
        )

    scroll = props.get('scrollX', True)
    wrapper = f'<div class="overflow-x-auto">{table}</div>' if scroll else table
    return f'<div class="{style}">{wrapper}{pagination}{children}</div>'


def _form_fields(fields):
    """渲染表单字段。"""
    html = ''
    for f in fields:
        label = f.get('label', '')
        required = ' <span class="text-red-500">*</span>' if f.get('required') else ''
        ft = f.get('type', 'text')
        placeholder = _esc_attr(f.get('placeholder', ''))

        if ft == 'select' and f.get('options'):
            opts = ''.join(f'<option>{_esc(o)}</option>' for o in f['options'])
            html += f'<div class="mb-4"><label class="block text-sm font-medium text-gray-700 mb-1">{label}{required}</label><select class="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm">{opts}</select></div>'
        elif ft == 'textarea':
            html += f'<div class="mb-4"><label class="block text-sm font-medium text-gray-700 mb-1">{label}{required}</label><textarea class="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" rows="3" placeholder="{placeholder}"></textarea></div>'
        else:
            html += f'<div class="mb-4"><label class="block text-sm font-medium text-gray-700 mb-1">{label}{required}</label><input type="{ft}" class="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" placeholder="{placeholder}"></div>'
    return html


def _list_items(items):
    """渲染列表项。"""
    html = ''
    for item in items:
        if isinstance(item, str):
            label = item
            desc = ''
        else:
            label = item.get('label', '')
            desc = f'<p class="text-sm text-gray-500">{_esc(item.get("description",""))}</p>' if item.get('description') else ''
        html += f'<div class="px-4 py-3 border-b border-gray-100 hover:bg-gray-50 transition-colors"><div class="text-sm text-gray-900">{_esc(label)}</div>{desc}</div>'
    return html


def _input_prepend(icon_name):
    """输入框前置图标。"""
    if not icon_name:
        return ''
    return f'<div class="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400"><i class="fas {icon_name}"></i></div>'
