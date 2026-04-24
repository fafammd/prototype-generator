# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

原型生成器 (Prototype Generator) 是一个 AI 驱动的高保真原型设计工具。用户通过自然语言描述 UI 需求，AI 生成可交互的 HTML 原型。支持参考图驱动、多模型切换、微调模式、PRD 文档、GitHub Pages 发布等功能。

## 常用命令

```bash
# 安装依赖（唯一的外部依赖）
pip install requests

# 启动开发服务器（默认端口 8080）
python server.py

# 打包为 Windows 可执行文件
build_exe.bat

# 导出项目（独立 HTML，无需服务器运行）
python export_project.py <project_id> <mode>
# mode: preview（纯预览）, dev（含 PRD/流程图）, embedded（单文件内嵌）

# 恢复备份
python restore_backup.py
```

## 核心架构

### 技术栈
- **后端**: Python 3.8+ 标准库 (`http.server`)，无 Flask/Django
- **前端**: 原生 HTML/JS，CDN 加载 Tailwind CSS、FontAwesome、Vue 3
- **存储**: 文件系统 JSON 文件，无数据库
- **AI API**: OpenAI 兼容接口，支持多模型配置

### 关键文件
| 文件 | 行数 | 职责 |
|------|------|------|
| `server.py` | ~3000 | 后端主服务，所有 API 路由、AI 调用、项目管理 |
| `src/index.html` | ~800 | 主界面，项目列表、创建表单、模型管理 |
| `src/script.js` | ~1900 | 前端逻辑，项目管理、图片上传、AI 生成触发 |
| `src/viewer.html` | ~3200 | 预览器，多模式（预览/编辑/开发/微调）、iframe 渲染 |
| `src/viewer_standalone.html` | ~500 | 独立预览器，用于导出后的本地运行 |

### iframe 通信协议 (关键)

预览器 (`viewer.html`) 通过 iframe 加载生成的原型，使用 `postMessage` 通信：

```
Viewer → iframe: { type: 'navigateTo', page: 'pagename' }
iframe → Viewer: { type: 'pageChange', page: 'pagename' }
```

**修改 viewer.html 或注入脚本时，绝对不能破坏此消息监听逻辑。**

### 异步生成流程

1. 用户点击生成 → `POST /generate-async`
2. 项目立即出现在列表，状态为 "generating"
3. 后台线程调用 AI API
4. 前端每 3 秒轮询 `GET /api/generation-status`
5. 完成后自动刷新预览

### AI 调用容错

`call_ai_model()` 函数先尝试 Python `requests` 库（3 次重试），失败后降级到系统 `curl` 命令。用于处理 SSL/网络问题。

## 开发规范

### 核心原则

1. **增量修改，严禁重写**：除非明确指令，禁止重写整个文件（尤其是 `viewer.html` 和 `server.py`）
2. **保护 iframe 通信**：postMessage 是系统神经中枢，修改时不能破坏现有消息监听
3. **兼容 file:// 协议**：导出后的项目在本地运行，严禁使用 `/` 开头的绝对路径

### 文件操作

- **修改前备份**：核心逻辑修改前，复制到 `backups/YYYYMMDD_TaskName/`，在 `docs/backup_log.md` 登记
- **路径处理**：Python 中使用 `os.path.join`，不硬编码路径分隔符

### UI/UX

- 保持界面美观，使用半透明、圆角、阴影等现代 UI 元素
- 新功能入口融合到现有「悬浮工具栏」，不随意增加顶部/侧边栏

### 调试

- 服务端日志：`server.py` 控制台输出
- 前端日志：`viewer.html` 使用 `console.log('[Viewer] ...')` 格式

### 任务记录

较大开发任务时：
1. 在 `docs/archive/` 创建任务记录文件
2. 记录 Implementation Plan 和 Verification Report
3. 更新 `docs/changelog.md`

## 配置文件

| 文件 | 用途 | Git 状态 |
|------|------|----------|
| `config.json` | 服务器端口、AI 参数、GitHub 配置 | 已忽略 |
| `models.json` | AI 模型配置（API key、base_url） | 已忽略 |
| `models.example.json` | 模型配置示例模板 | 已提交 |

## 模板文件

- `templates/base.md` - 页面设计需求模板（颜色、字体、布局、响应式）
- `templates/components.md` - UI 组件模板库（卡片、按钮、表单、列表、模态框等）

## API 端点概览

主要端点（详见 `docs/api.md`）：

- `POST /generate`, `POST /generate-async` - AI 生成
- `POST /save-project`, `POST /delete-project`, `POST /rename-project` - 项目管理
- `GET /api/generation-status` - 轮询生成状态
- `POST /api/inspector/apply` - 微调模式修改
- `POST /api/github/publish`, `POST /api/github/unpublish` - GitHub Pages 发布
- `GET/POST /api/models/*` - 模型配置管理
