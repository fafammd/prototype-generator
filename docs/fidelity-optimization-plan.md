# 优化原型保真度 — HTML 模板上传 + 参考图增强

## Context

用户的核心痛点：**生成的原型与现有系统差距大**。两种场景都存在这个问题：
1. **图片参考**：用户给截图，AI 生成结果与截图差距大
2. **HTML 模板**：用户有现有系统，希望基于其样式去新增/优化页面，但无法把现有系统的页面"搬"过来

用户提出的新方案：**允许用户上传包含现有系统 HTML 静态页面的 ZIP 包**，AI 基于这些 HTML 的样式和结构来生成新原型。核心难点是：**用户如何从现有系统中获取静态 HTML 页面？**

---

## 方案概览

分三大模块实施：

| 模块 | 内容 | 解决的问题 |
|------|------|-----------|
| **A. ZIP 模板上传** | 上传含现有系统 HTML 的 ZIP，AI 基于此生成 | 基于现有系统样式生成新页面 |
| **B. 页面捕获工具** | 提供浏览器书签小程序(Bookmarklet)一键捕获页面 | 帮用户从现有系统获取 HTML |
| **C. Prompt 增强** | 优化参考图 + HTML 模板的 prompt 指令 | 提升 AI 还原精度 |

---

## 模块 A: ZIP 模板上传

### A1. 前端 — 新增模板上传区域

**文件**: `src/script.js` + `src/index.html`

在页面创建表单的全局设置区域（配色方案上方），新增「设计模板」上传区：

```
┌─────────────────────────────────────────────┐
│ 📐 设计模板（可选）                            │
│ ┌─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┐ │
│ │  📎 点击或拖拽上传 ZIP 压缩包              │ │
│ │     包含现有系统的 HTML 页面文件            │ │
│ └─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┘ │
│ ✅ template.zip (3个HTML文件)                 │
│    ├ index.html (首页)                        │
│    ├ user-list.html (用户管理)               │
│    └ dashboard.html (数据面板)               │
│                                              │
│ 💡 如何获取？ [使用页面捕获工具]               │
└─────────────────────────────────────────────┘
```

**实现要点**:
- `src/script.js` 新增全局变量 `templateZip = null`（ZIP 的 base64）
- 新增 `templateHtmlFiles = []`（解压后的 HTML 文件名列表）
- 上传区支持拖拽和点击，只接受 `.zip` 文件
- 上传后立即调用 `POST /api/template/parse` 解析 ZIP，返回 HTML 文件列表
- 在每个页面卡片的 similarity 选项旁新增「参考模板」下拉框，可选择模板中的哪个 HTML 文件作为参考

**关键函数**:
- `handleTemplateZip(file)` — 读取 ZIP 为 base64，发送到后端解析
- `renderTemplateInfo(fileList)` — 显示解析结果
- `removeTemplate()` — 清除已上传模板

### A2. 后端 — ZIP 解析与存储

**文件**: `server.py`

新增 API 端点:

**`POST /api/template/parse`**
- 接收 ZIP 文件的 base64
- 使用 Python 标准库 `zipfile` 解压到临时目录
- 扫描其中的 `.html` / `.htm` 文件
- 对每个 HTML 文件提取:
  - 文件名和相对路径
  - `<title>` 内容
  - 估算大小（字符数）
- 返回文件列表供前端展示

**实现**:
```python
def handle_template_parse(self):
    data = self._read_json_body()
    zip_base64 = data.get('zipData', '')
    # 去掉 data:application/zip;base64, 前缀
    zip_bytes = base64.b64decode(zip_base64.split(',')[-1])

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        html_files = []
        for name in zf.namelist():
            if name.endswith(('.html', '.htm')) and not name.startswith('__MACOSX'):
                content = zf.read(name).decode('utf-8', errors='ignore')
                title = extract_title(content)  # 正则提取 <title>
                html_files.append({
                    'name': os.path.basename(name),
                    'path': name,
                    'title': title,
                    'size': len(content)
                })
    return {'success': True, 'files': html_files}
```

### A3. 生成时集成模板 HTML

**文件**: `src/script.js` + `server.py`

**前端改动**:
- `generateWithAI()` 在构建 `requestData` 时，加入模板相关字段:
  ```javascript
  if (templateZip) {
      requestData.templateZip = templateZip;       // ZIP base64
      requestData.templateMapping = {};             // 页面→模板HTML映射
      pages.forEach(id => {
          const tpl = $(`templateSelect_${id}`)?.value;
          if (tpl) requestData.templateMapping[id] = tpl;
      });
  }
  ```

**后端改动**:
- `handle_generate_async()` 接收模板数据后:
  1. 解压 ZIP 到项目目录 `projects/{id}/template/`
  2. 读取模板 HTML 内容
  3. 提取关键设计信息（CSS、配色、布局模式）
  4. 将模板 HTML 片段注入 AI prompt

**Prompt 注入方式**（在 `generatePrompt()` 或服务端增强）:
```
# 现有系统设计模板（必须严格遵循其视觉风格）

以下是用户现有系统的 HTML 页面。新生成的原型必须与其保持一致的设计语言。

## 模板页面: index.html
```html
{template_html_content (截取前 8000 字符)}
```

## 要求
1. 配色方案必须与模板完全一致（提取模板中使用的颜色值）
2. 组件样式（按钮、表格、表单、卡片）必须与模板一致
3. 布局结构（导航栏、侧边栏、内容区比例）参考模板
4. 字体、字号、间距与模板保持统一
5. 你可以在模板基础上添加新功能，但视觉风格不能偏离
```

**截断策略**: 每个 HTML 文件最多 8000 字符（约 2000 tokens），避免超出上下文窗口。

优先保留顺序：
1. `<style>` 块和内联 `style` 属性（设计信息最密集）
2. `<head>` 中的 meta、link、字体声明
3. `<body>` 中的主要布局结构（header、nav、main、sidebar、footer）
4. 表格、表单、卡片等组件示例（各保留一个代表性组件即可）
5. 重复元素截断（如长列表只保留前3项 + "..." 注释）

如果 ZIP 中有多个 HTML，总共不超过 20000 字符。

**服务端智能截断函数** `extract_design_from_html(html_content, max_chars=8000)`:
```python
def extract_design_from_html(html, max_chars=8000):
    """从 HTML 中提取对 AI 最有价值的设计信息"""
    # 1. 提取所有 <style> 块内容（最高优先级）
    style_blocks = re.findall(r'<style[^>]*>(.*?)</style>', html, re.DOTALL)
    styles_text = '\n'.join(style_blocks)

    # 2. 提取 body 中的布局结构
    body_match = re.search(r'<body[^>]*>(.*)</body>', html, re.DOTALL)
    body_html = body_match.group(1) if body_match else ''

    # 3. 截断重复元素（列表项只保留前3个）
    body_html = truncate_repeated_elements(body_html, max_repeated=3)

    # 4. 组合，优先保证样式信息完整
    result = ''
    if styles_text:
        result += '<style>' + styles_text[:max_chars // 3] + '</style>\n'
    remaining = max_chars - len(result)
    result += '<body>\n' + body_html[:remaining] + '\n</body>'
    return result
```

---

## 模块 B: 页面捕获工具

### 问题分析：为什么 CSS/JS 下载是难点

现有系统的 CSS 和 JS 通常来自多种来源：

| 资源类型 | 来源示例 | 捕获难点 |
|---------|---------|---------|
| 同源 CSS | `/static/css/app.css` | 无 CORS 问题，可直接读取 |
| CDN CSS | `cdn.jsdelivr.net/npm/tailwindcss` | **CORS 限制，无法读取 cssRules** |
| 内联 CSS | `<style>...</style>` | 直接在 HTML 中，无问题 |
| JS 文件 | 各种来源 | AI 不需要 JS 功能，不需要捕获 |
| 图片/字体 | CDN 或内部服务 | VPN 场景下可能无法离线访问 |

**关键认知**：AI 生成原型不需要 JS 功能，只需要**视觉设计信息**（颜色、字号、间距、布局、组件结构）。因此目标是把「页面长什么样」完整捕获下来，而不是让页面能运行。

### B1. 捕获方案（三种，按推荐度排序）

#### 方案一：SingleFile 扩展（最推荐）

**推荐理由**：Chrome 扩展有更高权限，不受 CORS 限制，能内联所有 CSS 和图片。

**优势**：
- 跨域 CSS **全部内联**（扩展权限绕过 CORS）
- 图片转 data URL，**完全自包含**
- 生成单个 HTML 文件，直接可用于 ZIP 打包
- VPN 场景：用户在 VPN 内捕获，文件离线也可用

**用户操作流程**：
1. 安装 Chrome 扩展 [SingleFile](https://chrome.google.com/webstore/detail/singlefile)
2. 在现有系统中打开目标页面（需连 VPN 的页面先连上 VPN）
3. 点击 SingleFile 扩展图标，等待保存完成
4. 自动下载为单个自包含 HTML 文件
5. 多个页面重复 2-4 步，打包成 ZIP 上传

#### 方案二：计算样式内联 Bookmarklet（零安装，推荐作为补充）

**原理**：不尝试下载 CSS 文件，而是用 `getComputedStyle()` 读取每个元素**浏览器已渲染的最终样式**，内联到 `style` 属性中。

**为什么这样可行**：
- `getComputedStyle()` 没有 CORS 限制，浏览器已经把所有 CSS（包括 CDN 上的）应用完毕
- 不需要下载任何外部文件，只要用户能**看到**页面，就能捕获
- VPN 场景：用户在 VPN 内打开页面，Bookmarklet 读取渲染结果，保存后离线可用
- 图片 URL 保留原始地址（AI 主要参考布局和样式，不需要图片精确还原）

**Bookmarklet 核心逻辑**：
```javascript
javascript:(function(){
    /* 只捕获对 AI 有用的设计属性，避免输出过大 */
    var DESIGN_PROPS = [
        'color', 'backgroundColor', 'borderColor', 'borderTopColor',
        'fontFamily', 'fontSize', 'fontWeight', 'lineHeight', 'textAlign',
        'margin', 'padding', 'gap',
        'display', 'flexDirection', 'justifyContent', 'alignItems',
        'width', 'height', 'maxWidth', 'minHeight',
        'position', 'top', 'left', 'right', 'bottom',
        'borderRadius', 'boxShadow', 'opacity',
        'overflow', 'gap', 'gridTemplateColumns'
    ];

    /* 默认值表：与默认值相同的属性跳过，减少体积 */
    var DEFAULTS = {
        'color': 'rgb(0, 0, 0)', 'backgroundColor': 'rgba(0, 0, 0, 0)',
        'fontSize': '16px', 'fontWeight': '400', 'lineHeight': 'normal',
        'textAlign': 'start', 'display': 'block', 'position': 'static',
        'opacity': '1', 'overflow': 'visible', 'borderRadius': '0px'
    };

    /* 克隆 body 并内联计算样式 */
    var clone = document.body.cloneNode(true);

    /* 只处理可见元素（跳过 script, style, svg 等） */
    var originals = document.body.querySelectorAll('*');
    var clones = clone.querySelectorAll('*');
    for (var i = 0; i < originals.length && i < clones.length; i++) {
        var computed = getComputedStyle(originals[i]);
        var styles = [];
        for (var j = 0; j < DESIGN_PROPS.length; j++) {
            var prop = DESIGN_PROPS[j];
            var val = computed.getPropertyValue(prop);
            if (val && val !== DEFAULTS[prop]) {
                styles.push(prop + ':' + val);
            }
        }
        if (styles.length > 0) {
            clones[i].setAttribute('style', styles.join(';'));
        }
        /* 清除 class 和 id（避免 AI 误解为依赖外部 CSS） */
        clones[i].removeAttribute('class');
        clones[i].removeAttribute('id');
    }

    /* 收集内联 style 和 内嵌 style 标签 */
    var inlineStyles = '';
    var styleTags = document.querySelectorAll('style');
    for (var k = 0; k < styleTags.length; k++) {
        inlineStyles += styleTags[k].textContent + '\n';
    }

    /* 构建自包含 HTML */
    var html = '<!DOCTYPE html><html><head><meta charset="utf-8">';
    html += '<title>' + document.title + ' (captured)</title>';
    if (inlineStyles) html += '<style>' + inlineStyles + '</style>';
    html += '</head>' + clone.outerHTML + '</html>';

    /* 下载 */
    var blob = new Blob([html], {type: 'text/html;charset=utf-8'});
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = (document.title || 'page') + '.html';
    document.body.appendChild(a);
    a.click();
    a.remove();
})();
```

**局限性**：
- 不捕获 `:hover`、`:focus`、`@media` 等动态/响应式样式
- 图片保持为 URL 引用（如果图片需 VPN 访问，离线后看不到，但 AI 不依赖图片精确显示）
- DOM 过大时可能较慢（限制最多处理前 500 个元素）

#### 方案三：浏览器 Ctrl+S（最简单但文件杂乱）

- 保存类型选择「网页，完整」
- 浏览器会下载所有 CSS、JS、图片到本地文件夹
- VPN 场景下可用（在 VPN 内保存，文件下载到本地）
- 缺点：每个页面生成一个 HTML + 一个 `_files` 文件夹，ZIP 打包时需包含两者

### B2. 捕获工具 UI 部署

**文件**: `src/index.html` + `src/script.js`

在主界面添加「如何获取现有系统页面」帮助入口，点击弹出模态框，展示三种方案的使用说明。

**模态框内容结构**：

```
┌──────────────────────────────────────────────────────┐
│  📋 如何捕获现有系统页面                               │
│                                                      │
│  方法一：SingleFile 扩展（推荐，最完整）                │
│  ┌──────────────────────────────────────────────┐    │
│  │ 1. 安装 Chrome 扩展 SingleFile               │    │
│  │ 2. 打开目标页面 → 点击扩展图标 → 自动下载      │    │
│  │ 3. 将下载的 HTML 文件打包成 ZIP 上传           │    │
│  │                                              │    │
│  │ ✅ 跨域 CSS 全部内联  ✅ 图片转为内嵌           │    │
│  │ ✅ VPN 页面可用       ✅ 完全离线可用           │    │
│  └──────────────────────────────────────────────┘    │
│                                                      │
│  方法二：页面捕获书签（零安装，快速）                    │
│  ┌──────────────────────────────────────────────┐    │
│  │ [📎 拖拽到书签栏] ← 捕获页面                   │    │
│  │                                              │    │
│  │ 1. 将上方按钮拖到浏览器书签栏                  │    │
│  │ 2. 在目标页面点击书签 → 自动下载 HTML           │    │
│  │ 3. 将多个 HTML 打包成 ZIP 上传                 │    │
│  │                                              │    │
│  │ ✅ 无需安装    ✅ VPN 页面可用                  │    │
│  │ ⚠️ 图片不内嵌  ⚠️ 不含动态样式                  │    │
│  └──────────────────────────────────────────────┘    │
│                                                      │
│  方法三：浏览器另存为 Ctrl+S                           │
│  ┌──────────────────────────────────────────────┐    │
│  │ 保存类型选「网页，完整」，打包保存的文件上传      │    │
│  └──────────────────────────────────────────────┘    │
│                                                      │
│                              [关闭]                   │
└──────────────────────────────────────────────────────┘
```

**实现要点**：
- SingleFile 方案提供安装链接
- Bookmarklet 以 `<a href="javascript:...">` 形式展示，用户可拖拽到书签栏
- Ctrl+S 方案简要说明即可

---

## 模块 C: Prompt 增强

### C1. 增强参考图 Prompt 指令

**文件**: `src/script.js`
**位置**: `generatePrompt()` 函数, 行 970-978

将当前的简单条件分支替换为根据 similarity 模式生成的详细结构化指令：

- **pixel 模式**: 6 步视觉分析指令（颜色提取→布局还原→组件识别→字体排版→间距留白→图标装饰）+ 精度要求
- **style 模式**: 5 步风格参考指令（配色方案→质感氛围→字体风格→圆角阴影→组件风格）
- **layout 模式**: 4 步布局参考指令（区域划分→元素位置→栅格比例→层次结构）

**实现**: 新建 `buildImagePromptInstructions(pageName, imageCount, similarity)` 辅助函数。

### C2. 优化 System Prompt

**文件**: `server.py` 行 1288-1289, 1413-1414 + `config.json`

将默认 system prompt 增强为:
```
You are a professional UI/UX Developer specializing in high-fidelity HTML prototype generation. When reference images or HTML templates are provided, you must FIRST carefully analyze every visual detail (colors, typography, spacing, layout, components), then reproduce the design as accurately as possible using HTML + Tailwind CSS. When an existing system HTML template is provided, match its design language exactly. Always respond with complete HTML code, not explanations.
```

### C3. 提升图片压缩质量

**文件**: `server.py` `compress_image_for_api()` 行 1193

- `max_size`: 1024 → 1536
- `quality`: 75 → 85
- `max_bytes`: 1MB → 2MB
- pixel 模式下进一步: `max_size=2048, quality=90, max_bytes=3MB`

### C4. 增量模式传入已有 HTML

**文件**: `server.py` 行 968-971

增量更新时将源 HTML 片段（前 15000 字符）注入 prompt，并附带保持风格一致的指令。

---

## 实施顺序

| 顺序 | 内容 | 文件 | 改动量 |
|------|------|------|--------|
| 1 | C2: System Prompt | server.py, config.json | 极小 |
| 2 | C1: 增强参考图 Prompt | script.js | 小 |
| 3 | C3: 压缩参数 | server.py | 极小 |
| 4 | B1-B2: Bookmarklet 捕获工具 | index.html / script.js | 小 |
| 5 | A2: ZIP 解析 API | server.py | 中等 |
| 6 | A1: ZIP 上传前端 | script.js, index.html | 中等 |
| 7 | A3: 模板集成到 prompt | script.js, server.py | 中等 |
| 8 | C4: 增量 HTML | server.py | 小 |

建议分两批实施：
- **第一批 (1-4)**：Prompt 增强 + 压缩参数 + 捕获工具，快速提升效果
- **第二批 (5-8)**：ZIP 模板上传的完整流程

---

## 关键文件清单

| 文件 | 修改内容 |
|------|----------|
| `src/script.js` | `buildImagePromptInstructions()` 新函数；`generatePrompt()` 行 970-978 增强指令；`generateWithAI()` 行 1287-1310 添加模板数据；新增模板上传处理函数 |
| `server.py` | system prompt 更新(行 1288, 1414)；`compress_image_for_api` 参数(行 1193)；新增 `/api/template/parse` 端点；`handle_generate_async` 模板集成(行 809+)；增量 prompt 增强(行 968-971) |
| `src/index.html` | 模板上传区域 UI；Bookmarklet 说明模态框 |
| `config.json` | 更新 `system_prompt` 字段 |

## 向后兼容

- 所有新功能均为**增量添加**，不影响现有流程
- ZIP 模板为可选功能，不上传则走原有逻辑
- Bookmarklet 为独立工具，与生成流程解耦
- Prompt 增强仅修改文本内容，不改变 API 接口

## 验证方式

1. **无模板无图片** — 确认生成结果与之前一致
2. **上传 ZIP 模板** — 解析返回 HTML 文件列表，生成结果匹配模板样式
3. **ZIP + 参考图同时使用** — 模板提供基础样式，图片提供具体布局参考
4. **Bookmarklet** — 在任意网页点击后下载 HTML，上传到系统验证可用
5. **增量更新** — 基于已有项目迭代时风格保持一致
6. **大 ZIP (10+ HTML 文件)** — 截断策略正确，不超出 token 限制
