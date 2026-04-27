# Plan: 历史项目提示词复制与作为新项目加载

## Context

用户想复用历史项目的提示词/表单配置来创建新项目，但当前的"加载并重新生成"按钮会设置增量模式（`sourceProjectId`、`originalFormData`、`originalImageHashes`），导致生成时走增量逻辑而非全新生成。需要一种方式加载历史项目的完整配置但不触发增量模式，同时支持复制原始 prompt 文本。

## 修改文件

- `src/script.js` - 新增 2 个函数，修改 1 个函数，修改 1 处事件绑定
- `src/index.html` - 修改记录模态框 footer，新增 2 个按钮

## 改动详情

### 1. 记录模态框增加两个按钮 (`src/index.html:473-482`)

在现有"关闭"和"加载并重新生成"按钮之间插入：

- **"作为新项目加载"** - 绿色按钮，加载表单数据但不设增量标志
- **"复制提示词"** - 灰色/紫色按钮，读取服务器 `prompt.txt` 并复制到剪贴板

修改后的 footer 按钮顺序：关闭 | 复制提示词 | 作为新项目加载 | 加载并重新生成

### 2. 新增 `loadAsNewProject()` 函数 (`src/script.js`)

复用 `regenerateFromRecord()` 的表单恢复逻辑（清空表单 → 恢复全局设置 → 逐页恢复 → 加载图片），但**跳过**：
- 不设 `sourceProjectId`
- 不设 `originalFormData`
- 不设 `originalImageHashes`

同时修复数据丢失 bug：补全 `description`、`dataStructure`、`userFlow` 三个字段的恢复。

提取共享逻辑：将 `regenerateFromRecord()` 中恢复表单数据的代码提取为 `restoreFormDataFromRecord(record, projectId, isIncremental)` 内部函数，两个按钮共用恢复逻辑，仅增量标志不同。

### 3. 新增 `copyPromptFromRecord()` 函数 (`src/script.js`)

- 从服务器读取 `/projects/{id}/prompt.txt`
- 复制到剪贴板（复用已有的 `navigator.clipboard` + fallback 模式）
- 显示 toast 提示

### 4. 事件绑定 (`src/script.js` ~line 163)

为两个新按钮绑定 onclick 事件。

## 验证方式

1. 启动 `python server.py`
2. 生成一个项目（带参考图和详细表单数据）
3. 点击项目卡片的"记录"按钮
4. 验证三个功能：
   - "复制提示词" → 剪贴板包含完整 prompt 文本
   - "作为新项目加载" → 表单恢复完整数据，标题显示普通新建状态，生成时不走增量逻辑
   - "加载并重新生成" → 行为不变，仍走增量逻辑
5. 确认 `description`、`dataStructure`、`userFlow` 三个字段在"作为新项目加载"后也被正确恢复
