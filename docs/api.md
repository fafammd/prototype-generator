# API 参考 (API Reference)

服务端地址: `http://localhost:8080`

## 1. 项目管理

### 获取项目列表
`GET /api/projects`

### 创建项目
`POST /generate`
- Body: `{ prompt, images }`

### 删除项目
`POST /delete-project`
- Body: `{ id }`

### 复制项目
`POST /copy-project`
- Body: `{ sourceProjectId, newProjectName }`
- Returns: `{ success: true, project: {...} }`

### 创建占位项目
`POST /create-placeholder`
- Body: `{ projectId, projectName, formData, imageFiles }`
- Returns: `{ success: true, project: {...} }`
- 说明：创建不调用AI的占位项目，用于复制Prompt功能

### 查询生成状态
`GET /api/generation-status`
- Query: `?id=xxx`
- Returns: `{ status: 'pending'|'generating'|'completed'|'failed', progress: 0-100, error: 'xxx' }`
- 说明：查询项目异步生成状态

## 2. PRD 文档

### 保存 PRD
`POST /api/prd/save`
- Body: `{ projectId, pageName, content }`

### 加载 PRD
`GET /api/prd/load`
- Query: `?projectId=xxx&pageName=yyy`

## 3. 研发数据

### 获取页面列表
`GET /api/pages`
- Query: `?projectId=xxx`
- Returns: `{ pages: [{name, label}] }`

### 获取流程图数据
`GET /api/flowchart`
- Query: `?projectId=xxx`
- Returns: `{ pages, transitions, modals, mermaid }`

## 4. 微调模式

### 应用 AI 修改
`POST /api/inspector/apply`
- Body:
  ```json
  {
    "projectId": "xxx",
    "userRequest": "修改背景色为蓝色",
    "elements": [
      { "selector": "#btn", "html": "<button>..." }
    ],
    "prompt": "完整 prompt (可选)"
  }
  ```
- Returns: `{ success: true, message: "...", backupFile: "index.html.bak" }`

## 5. 需求文档导入（异步）

### 上传需求文档
`POST /api/requirements/import`
- Content-Type: `multipart/form-data`
- Body: `file` - 文档文件（支持 .docx、.md、.txt，最大 10MB）
- Returns（立即返回，后台异步处理）:
  ```json
  { "success": true, "taskId": "import_1713700000000", "async": true }
  ```

### 查询导入状态
`GET /api/requirements/import-status?id=<taskId>`
- Returns（处理中）:
  ```json
  { "status": "generating", "progress": 30, "error": "" }
  ```
- Returns（完成）:
  ```json
  {
    "status": "completed",
    "progress": 100,
    "data": {
      "global": {
        "primaryColor": "#004fff",
        "secondaryColor": "#10B981",
        "backgroundMode": "light",
        "componentStyle": "Ant Design"
      },
      "pages": [
        {
          "name": "首页",
          "layout": "顶部导航栏 + 左侧内容区...",
          "features": "用户登录、数据展示...",
          "interaction": "点击导航切换页面...",
          "images": [
            { "name": "image1.png", "base64": "data:image/png;base64,..." }
          ]
        }
      ]
    },
    "metadata": {
      "filename": "需求文档.docx",
      "fileType": "docx",
      "extractedAt": "2026-04-21 10:30:00",
      "contentLength": 3500,
      "imageCount": 3
    }
  }
  ```
- Returns（失败）:
  ```json
  { "status": "failed", "progress": 0, "error": "AI 调用失败: ..." }
  ```

- 进度说明: 0-10 上传中, 10-30 文档解析, 30-80 AI 提取, 80-100 图片分配
  ```
- 说明：上传需求规格说明书，后端解析文档内容（含内嵌图片）后调用 AI 提取结构化数据，返回可直接用于填充表单的 JSON。Word 文档中的图片会按其在文档中的段落位置自动分配到对应页面。文档为自由格式时，AI 会智能识别页面划分和设计规范。未提及的字段使用默认值。
