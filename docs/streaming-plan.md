# 实施计划：AI 流式响应 + 部分保存 + 断点续传

## 背景

当前 AI 生成（`POST /generate-async`）在后端是完全阻塞的 —— AI API 调用需要等待完整响应才能获取任何内容。如果生成在几分钟后失败，所有进度都将丢失。前端每 3 秒轮询一次，但只能看到粗粒度进度（0/10/20/80/100），无法了解 AI 实际正在生成什么。

**目标：**
1. 实时流式传输 AI 响应，让用户看到内容逐步生成
2. 在失败/超时/取消时保存部分内容
3. 支持利用对话上下文恢复失败的生成

---

## 阶段 1：服务器基础设施

### 1.1 ThreadingTCPServer（关键前置条件）

**文件：** `server.py`（约第 3868 行）

将 `socketserver.TCPServer` 改为 `socketserver.ThreadingTCPServer`。SSE 连接是长连接，会阻塞单线程服务器。代码库已使用线程 + 锁，因此这是安全的。

### 1.2 扩展 `generating_tasks` 增加流式字段

**文件：** `server.py`（第 53 行，注册处在第 896 行）

为每个任务条目添加字段：
- `accumulated_content`（str）—— 目前已接收的完整文本
- `stream_chunks`（list）—— 自上次 SSE 读取以来的新数据块（环形缓冲区）
- `stream_event`（threading.Event）—— 通知有新数据可用
- `stream_lock`（threading.Lock）—— 保护 stream_chunks

### 1.3 新 SSE 端点：`GET /api/generation-stream?id=PROJECT_ID`

**文件：** `server.py`（新路由 + 处理函数）

SSE 端点从 `stream_chunks` 缓冲区读取并推送到客户端。关键设计：SSE 端点从共享缓冲区（由后台线程写入）读取，而非直接从 AI API 流式传输。这种解耦意味着：
- SSE 客户端可以随时连接/断开
- 后台线程不依赖 SSE 连接的存在
- 轮询仍可作为降级方案

---

## 阶段 2：流式 AI 调用

### 2.1 新方法：`call_ai_model_streaming()`

**文件：** `server.py`（在 `call_ai_model` 之后添加，约第 1313 行）

与 `call_ai_model()` 相同的 payload 构建，但增加 `"stream": true`，使用 `requests.post(stream=True)` 配合 `response.iter_lines()` 解析 SSE `data: {...}` 行。产出 `(chunk_text, accumulated_content, done)` 元组。

**降级策略：** 如果流式传输失败（API 不支持），自动降级为非流式调用，将完整响应作为单个数据块返回。确保向后兼容。

### 2.2 新方法：`_call_ai_for_async_streaming()`

**文件：** `server.py`（替换 `_call_ai_for_async`，第 1046 行）

包装 `call_ai_model_streaming()`，将每个数据块推送到 `generating_tasks[project_id]['stream_chunks']`。基于内容长度启发式更新进度。

### 2.3 修改 `generate_in_background()`

**文件：** `server.py`（第 903 行）

将非流式的 `_call_ai_for_async()` 调用替换为 `_call_ai_for_async_streaming()`。完成后对完整累积内容调用 `extract_html()`（与现有逻辑相同）。在失败/取消时调用 `_save_partial_content()`。

### 2.4 新方法：`_save_partial_content()`

**文件：** `server.py`（新增）

保存以下内容：
- `partial_content.txt` —— AI 原始文本（用于续传上下文）
- `index.html` —— 尝试提取 HTML（用于预览）
- 更新 `record.json` 状态为 `'partial'`

---

## 阶段 3：断点续传

### 3.1 新端点：`POST /api/resume-generation`

**文件：** `server.py`（新路由 + 处理函数）

构建多轮对话上下文：
```
[系统提示, 原始用户提示, 部分AI响应, "请继续完成未完成的HTML代码生成..."]
```

使用 `call_ai_model_streaming()` 传入预构建的 messages。采用与初始生成相同的后台线程 + SSE 模式。成功后清理 `partial_content.txt`。

---

## 阶段 4：前端变更

### 4.1 在 `script.js` 中用 SSE 替换轮询

**文件：** `src/script.js`（第 1345 行）

新增 `streamGenerationStatus(projectId)` 函数，使用 `EventSource`。如果 SSE 连接失败，自动降级到现有的 `pollGenerationStatus()`。

### 4.2 增强 `#loadingModal` 用于流式预览

**文件：** `src/index.html`（第 372 行）

改造现有的未使用 `#loadingModal`，展示：
- 旋转加载图标 + "AI 正在生成..." 标题
- 进度文本（字符数、行数）
- 可滚动的代码预览区域，显示最近 3000 字符的流式内容
- 关闭按钮

### 4.3 为失败项目添加"继续"按钮

**文件：** `src/script.js`（`renderProjectList` 约第 140 行）

失败的项目卡片显示绿色的"继续"按钮，点击后调用 `resumeGeneration(projectId)`。该函数发送 `POST /api/resume-generation` 并启动 SSE 流式监听。

### 4.4 更新 `generateWithAI()` 触发逻辑

**文件：** `src/script.js`（第 1330 行）

将 `pollGenerationStatus(result.project.id)` 改为 `streamGenerationStatus(result.project.id)`。

---

## 阶段 5：需求导入流式化（可选，与主流程同模式）

对 `call_ai_for_requirements()`（第 3538 行）应用相同的流式方案：
- 添加流式变体
- SSE 端点 `GET /api/requirements/stream?id=task_id`
- 前端在导入按钮区域展示流式 JSON

结构与主生成流程完全一致，可作为后续迭代。

---

## 需要修改的文件

| 文件 | 修改内容 |
|------|----------|
| `server.py` | ThreadingTCPServer、`call_ai_model_streaming()`、SSE 端点、续传端点、`_save_partial_content()`、修改 `generate_in_background()`、扩展 `generating_tasks` |
| `src/script.js` | `streamGenerationStatus()`、`resumeGeneration()`、`updateStreamingPreview()`、修改 `renderProjectList()`、更改 `generateWithAI()` 触发 |
| `src/index.html` | 增强 `#loadingModal` 添加流式预览面板 |

## 验证清单

1. 启动服务器，触发生成 —— 验证 SSE 数据块在浏览器 DevTools Network 标签中出现
2. 验证流式弹窗实时显示内容
3. 中途停止生成 —— 验证 `partial_content.txt` 已保存
4. 对失败项目点击"继续" —— 验证利用对话上下文恢复生成
5. 测试不支持流式的 API —— 验证降级为非流式模式正常工作
6. 测试并发生成 —— 验证 SSE 数据块路由到正确的客户端
