/**
 * 原型生成器前端脚本
 * 简化版：AI调用在后端完成
 */

// ==================== 状态管理 ====================
let pages = [];
let pageFiles = {};
let pageEnums = {};   // 存储从PRD提取的页面级枚举数据: { pageId: { enumName: [...] } }
let globalEnums = {}; // 存储从PRD提取的全局枚举数据: { enumName: [...] }
let allProjects = [];
let searchQuery = '';
let currentRecordProject = null; // 当前查看的记录项目

// ==================== 模型管理 ====================
let modelsList = [];
let currentModel = null;
let selectedModelId = '';

// ==================== 增量更新相关 ====================
let sourceProjectId = null;       // 来源项目ID（用于增量更新）
let originalFormData = null;      // 原始表单数据快照
let originalImageHashes = {};     // 原始图片哈希 { pageIndex: hash }

// ==================== 模板上传相关 ====================
let templateZip = null;           // ZIP 文件的 base64 数据
let templateHtmlFiles = [];       // 解析后的 HTML 文件列表

// ==================== 模板处理函数 ====================

// 处理模板 ZIP 上传
function handleTemplateZip(file) {
    if (!file) return;
    if (!file.name.endsWith('.zip')) {
        showToast('请上传 ZIP 格式的文件', 'error');
        return;
    }

    const reader = new FileReader();
    reader.onload = async (e) => {
        templateZip = e.target.result; // data:application/zip;base64,...

        try {
            const response = await fetch('/api/template/parse', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ zipData: templateZip })
            });
            const result = await response.json();

            if (result.success && result.files && result.files.length > 0) {
                templateHtmlFiles = result.files;
                renderTemplateInfo(file.name, result.files);
                showToast(`已解析 ${result.files.length} 个 HTML 文件`, 'success');
            } else {
                showToast('ZIP 中未找到 HTML 文件', 'error');
                templateZip = null;
            }
        } catch (err) {
            console.error('[模板解析失败]', err);
            showToast('模板解析失败: ' + err.message, 'error');
            templateZip = null;
        }
    };
    reader.readAsDataURL(file);
}

// 渲染模板文件信息
function renderTemplateInfo(fileName, files) {
    $('templateUploadContent').innerHTML = `
        <i class="fas fa-check-circle text-3xl text-green-400 mb-2"></i>
        <p class="text-sm text-gray-600">点击重新选择文件</p>
    `;

    $('templateFileInfo').classList.remove('hidden');
    $('templateFileName').textContent = `${fileName} (${files.length}个HTML文件)`;

    const listHtml = files.map(f => {
        const sizeKb = (f.size / 1024).toFixed(1);
        const title = f.title ? ` — ${f.title}` : '';
        return `<div class="flex items-center gap-1.5 py-0.5">
            <i class="fas fa-file-code text-purple-400"></i>
            <span>${f.name}${title}</span>
            <span class="text-gray-400">(${sizeKb}KB)</span>
        </div>`;
    }).join('');
    $('templateFileList').innerHTML = listHtml;
}

// 移除已上传模板
function removeTemplate() {
    templateZip = null;
    templateHtmlFiles = [];
    $('templateUploadContent').innerHTML = `
        <i class="fas fa-file-archive text-3xl text-gray-300 mb-2"></i>
        <p class="text-sm text-gray-500">点击或拖拽上传 ZIP 压缩包</p>
        <p class="text-xs text-gray-400 mt-1">包含现有系统的 HTML 页面文件，AI 将基于其样式生成原型</p>
    `;
    $('templateFileInfo').classList.add('hidden');
    $('templateZipInput').value = '';
}

// 显示/关闭捕获帮助模态框
function showCaptureHelp() {
    const modal = $('captureHelpModal');
    modal.classList.remove('hidden');
    modal.classList.add('flex');
    initBookmarklet();
}

function closeCaptureHelp() {
    const modal = $('captureHelpModal');
    modal.classList.add('hidden');
    modal.classList.remove('flex');
}

// 初始化 Bookmarklet 链接
function initBookmarklet() {
    const bookmarkletCode = `javascript:void(function(){var e=["color","backgroundColor","borderColor","fontFamily","fontSize","fontWeight","lineHeight","textAlign","display","flexDirection","justifyContent","alignItems","gap","borderRadius","boxShadow","opacity","overflow","gridTemplateColumns"],t={color:"rgb(0, 0, 0)",backgroundColor:"rgba(0, 0, 0, 0)",fontSize:"16px",fontWeight:"400",lineHeight:"normal",display:"block",opacity:"1",overflow:"visible",borderRadius:"0px"},n=document.body.cloneNode(!0),o=document.body.querySelectorAll("*"),r=n.querySelectorAll("*"),l="";document.querySelectorAll("style").forEach(function(e){l+=e.textContent+"\\n"});for(var i=0;i<Math.min(o.length,r.length,500);i++){var a=getComputedStyle(o[i]),s=[];e.forEach(function(e){var n=a.getPropertyValue(e);n&&n!==t[e]&&s.push(e+":"+n)}),s.length>0&&r[i].setAttribute("style",s.join(";")),r[i].removeAttribute("class"),r[i].removeAttribute("id")}var d='<!DOCTYPE html><html><head><meta charset="utf-8"><title>'+(document.title||"page")+" (captured)</title>";l&&(d+="<style>"+l+"</style>");d+="</head>"+n.outerHTML+"</html>";var c=new Blob([d],{type:"text/html;charset=utf-8"}),u=document.createElement("a");u.href=URL.createObjectURL(c),u.download=(document.title||"page")+".html",document.body.appendChild(u),u.click(),u.remove()})();`;

    const link = $('bookmarkletLink');
    if (link) {
        link.href = bookmarkletCode;
    }
}

// ==================== DOM 元素 ====================
const $ = (id) => document.getElementById(id);

// ==================== 初始化 ====================
document.addEventListener('DOMContentLoaded', () => {
    setupEventListeners();
    loadProjects();
    loadModels();
    addPage(); // 默认添加一个页面
});

// ==================== 事件监听 ====================
function setupEventListeners() {
    // 新建项目
    $('createNewBtn').onclick = createNewProject;

    // 添加页面
    $('addPageBtn').onclick = addPage;

    // AI生成
    $('aiGenerateBtn').onclick = generateWithAI;

    // 复制Prompt
    $('copyPromptBtn').onclick = copyPromptToClipboard;

    // 颜色选择器
    $('primaryColor').oninput = (e) => $('primaryColorValue').textContent = e.target.value;
    $('secondaryColor').oninput = (e) => $('secondaryColorValue').textContent = e.target.value;

    // 搜索
    $('projectSearch').oninput = (e) => {
        searchQuery = e.target.value.toLowerCase();
        renderProjectList();
    };

    // 记录模态框关闭
    $('closeRecordModal').onclick = closeRecordModal;
    $('recordModalOverlay').onclick = closeRecordModal;

    // 从记录重新生成
    $('regenerateFromRecord').onclick = regenerateFromRecord;
    $('loadAsNewProject').onclick = loadAsNewProject;
    $('copyPromptFromRecord').onclick = copyPromptFromRecord;

    // 回收站
    $('recycleBinBtn').onclick = openRecycleBin;
    $('closeRecycleBinModal').onclick = closeRecycleBinModal;
    $('recycleBinOverlay').onclick = closeRecycleBinModal;

    // 编辑标题弹窗
    $('editTitleOverlay').onclick = closeEditTitleModal;
    $('editTitleInput').onkeydown = (e) => {
        if (e.key === 'Enter') saveProjectTitle();
        if (e.key === 'Escape') closeEditTitleModal();
    };
}

// ==================== 项目管理 ====================
function createNewProject() {
    // 重置表单
    $('primaryColor').value = '#004fff';
    $('primaryColorValue').textContent = '#004fff';
    $('secondaryColor').value = '#10B981';
    $('secondaryColorValue').textContent = '#10B981';
    $('backgroundMode').value = 'light';
    $('componentStyle').value = 'Ant Design';

    // 清空页面
    pages = [];
    pageFiles = {};
    pageEnums = {};
    globalEnums = {};
    $('pageCardsContainer').innerHTML = '';
    addPage();

    $('headerTitle').textContent = '请输入您的设计灵感';
    currentRecordProject = null;

    // 清空增量更新状态
    sourceProjectId = null;
    originalFormData = null;
    originalImageHashes = {};
}

function loadProjects() {
    fetch('/data/projects.json?t=' + Date.now())
        .then(res => res.json())
        .then(data => {
            allProjects = data || [];
            renderProjectList();
        })
        .catch(() => {
            $('projectList').innerHTML = '<div class="text-center py-8 text-gray-400 text-sm">暂无项目</div>';
        });
}

function renderProjectList() {
    const container = $('projectList');
    let filtered = allProjects;

    if (searchQuery) {
        filtered = allProjects.filter(p => p.name.toLowerCase().includes(searchQuery));
    }

    if (filtered.length === 0) {
        container.innerHTML = '<div class="text-center py-8 text-gray-400 text-sm">暂无项目</div>';
        return;
    }

    container.innerHTML = filtered.map(p => {
        // 状态标签
        let statusHTML = '';
        if (p.status === 'generating') {
            statusHTML = `
                <span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-blue-50 text-blue-600 animate-pulse">
                    <i class="fas fa-spinner fa-spin text-[10px]"></i>生成中
                </span>`;
        } else if (p.status === 'failed') {
            statusHTML = `
                <span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-red-50 text-red-500">
                    <i class="fas fa-times-circle text-[10px]"></i>失败
                </span>`;
        } else if (p.status === 'pending_external') {
            statusHTML = `
                <span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-amber-50 text-amber-600">
                    <i class="fas fa-clock text-[10px]"></i>待生成
                </span>`;
        } else if (p.status === 'stopped') {
            statusHTML = `
                <span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-gray-100 text-gray-500">
                    <i class="fas fa-stop-circle text-[10px]"></i>已停止
                </span>`;
        } else {
            statusHTML = `
                <span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-green-50 text-green-600">
                    <i class="fas fa-check-circle text-[10px]"></i>已完成
                </span>`;
        }

        const safeName = p.name.replace(/'/g, "\\'").replace(/"/g, '&quot;');

        return `
        <div class="project-card group bg-white border border-gray-200 rounded-xl overflow-hidden hover:shadow-md hover:border-indigo-200 transition-all duration-200 cursor-pointer"
             onclick="window.open('viewer.html?project=${encodeURIComponent(p.id)}', '_blank')">
            <div class="p-3.5">
                <div class="flex items-start justify-between gap-2 mb-2">
                    <p class="text-sm font-semibold text-gray-800 line-clamp-2 leading-snug flex-1" title="${p.name}">${p.name}</p>
                    ${statusHTML}
                </div>
                <p class="text-xs text-gray-400">${p.date}</p>
                ${p.model_name ? `<p class="text-[11px] text-indigo-400 mt-0.5 flex items-center gap-1"><i class="fas fa-robot text-[9px]"></i>${p.model_name}</p>` : ''}
            </div>
            <div class="flex items-center border-t border-gray-100 bg-gray-50/50 px-2 py-1.5 transition-opacity duration-150"
                 onclick="event.stopPropagation()">
                ${p.status === 'generating' ? `
                <button onclick="canvasStudio.reopen('${p.id}')"
                        class="flex-1 flex items-center justify-center gap-1 py-1 text-xs text-violet-500 hover:text-violet-700 rounded hover:bg-violet-50 transition-colors font-medium" title="打开画布">
                    <i class="fas fa-th-large"></i>
                </button>
                <button onclick="stopGeneration('${p.id}', '${safeName}')"
                        class="flex-1 flex items-center justify-center gap-1 py-1 text-xs text-orange-500 hover:text-orange-700 rounded hover:bg-orange-50 transition-colors font-medium" title="停止生成">
                    <i class="fas fa-stop"></i>
                </button>
                <button onclick="editProjectTitle('${p.id}', '${safeName}')"
                        class="flex-1 flex items-center justify-center gap-1 py-1 text-xs text-gray-400 hover:text-blue-600 rounded hover:bg-blue-50 transition-colors" title="编辑">
                    <i class="fas fa-edit"></i>
                </button>
                <button onclick="copyProject('${p.id}', '${safeName}')"
                        class="flex-1 flex items-center justify-center gap-1 py-1 text-xs text-gray-400 hover:text-purple-600 rounded hover:bg-purple-50 transition-colors" title="复制">
                    <i class="fas fa-copy"></i>
                </button>
                <button onclick="deleteProject('${p.id}', '${safeName}')"
                        class="flex-1 flex items-center justify-center gap-1 py-1 text-xs text-gray-400 hover:text-red-500 rounded hover:bg-red-50 transition-colors" title="删除">
                    <i class="fas fa-trash-alt"></i>
                </button>
                ` : p.status === 'failed' ? `
                <button onclick="canvasStudio.reopen('${p.id}')"
                        class="flex-1 flex items-center justify-center gap-1 py-1 text-xs text-violet-500 hover:text-violet-700 rounded hover:bg-violet-50 transition-colors font-medium" title="打开画布">
                    <i class="fas fa-th-large"></i>
                </button>
                <button onclick="resumeGeneration('${p.id}', '${safeName}')"
                        class="flex-1 flex items-center justify-center gap-1 py-1 text-xs text-green-500 hover:text-green-700 rounded hover:bg-green-50 transition-colors font-medium" title="继续生成">
                    <i class="fas fa-redo"></i>继续
                </button>
                <button onclick="editProjectTitle('${p.id}', '${safeName}')"
                        class="flex-1 flex items-center justify-center gap-1 py-1 text-xs text-gray-400 hover:text-blue-600 rounded hover:bg-blue-50 transition-colors" title="编辑">
                    <i class="fas fa-edit"></i>
                </button>
                <button onclick="copyProject('${p.id}', '${safeName}')"
                        class="flex-1 flex items-center justify-center gap-1 py-1 text-xs text-gray-400 hover:text-purple-600 rounded hover:bg-purple-50 transition-colors" title="复制">
                    <i class="fas fa-copy"></i>
                </button>
                <button onclick="deleteProject('${p.id}', '${safeName}')"
                        class="flex-1 flex items-center justify-center gap-1 py-1 text-xs text-gray-400 hover:text-red-500 rounded hover:bg-red-50 transition-colors" title="删除">
                    <i class="fas fa-trash-alt"></i>
                </button>
                ` : `
                <button onclick="canvasStudio.reopen('${p.id}')"
                        class="flex-1 flex items-center justify-center gap-1 py-1 text-xs text-gray-400 hover:text-violet-600 rounded hover:bg-violet-50 transition-colors" title="画布模式">
                    <i class="fas fa-th-large"></i>
                </button>
                <button onclick="editProjectTitle('${p.id}', '${safeName}')"
                        class="flex-1 flex items-center justify-center gap-1 py-1 text-xs text-gray-400 hover:text-blue-600 rounded hover:bg-blue-50 transition-colors" title="编辑">
                    <i class="fas fa-edit"></i>
                </button>
                <button onclick="openChatAdjust('${p.id}')"
                        class="flex-1 flex items-center justify-center gap-1 py-1 text-xs text-gray-400 hover:text-teal-600 rounded hover:bg-teal-50 transition-colors" title="对话调整">
                    <i class="fas fa-comments"></i>
                </button>
                `}
                <button onclick="copyProject('${p.id}', '${safeName}')" 
                        class="flex-1 flex items-center justify-center gap-1 py-1 text-xs text-gray-400 hover:text-purple-600 rounded hover:bg-purple-50 transition-colors" title="复制">
                    <i class="fas fa-copy"></i>
                </button>
                <button onclick="window.open('viewer.html?project=${encodeURIComponent(p.id)}', '_blank')" 
                        class="flex-1 flex items-center justify-center gap-1 py-1 text-xs text-gray-400 hover:text-indigo-600 rounded hover:bg-indigo-50 transition-colors" title="预览">
                    <i class="fas fa-eye"></i>
                </button>
                <button onclick="viewRecord('${p.id}')" 
                        class="flex-1 flex items-center justify-center gap-1 py-1 text-xs text-gray-400 hover:text-green-600 rounded hover:bg-green-50 transition-colors" title="记录">
                    <i class="fas fa-history"></i>
                </button>
                <button onclick="openExportModal('${p.id}', '${safeName}')" 
                        class="flex-1 flex items-center justify-center gap-1 py-1 text-xs text-gray-400 hover:text-orange-500 rounded hover:bg-orange-50 transition-colors" title="导出 & 分享">
                    <i class="fas fa-paper-plane"></i>
                </button>
                <button onclick="deleteProject('${p.id}', '${safeName}')" 
                        class="flex-1 flex items-center justify-center gap-1 py-1 text-xs text-gray-400 hover:text-red-500 rounded hover:bg-red-50 transition-colors" title="删除">
                    <i class="fas fa-trash-alt"></i>
                </button>
            </div>
        </div>
    `;
    }).join('');
}

async function stopGeneration(id, name) {
    if (!confirm(`确定要停止生成 "${name}" 吗？项目将保留在列表中。`)) return;

    try {
        const res = await fetch('/api/stop-generation', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id })
        });
        const data = await res.json();
        if (data.success) {
            showToast('已停止生成');
            const project = allProjects.find(p => p.id === id);
            if (project) project.status = 'stopped';
            renderProjectList();
        } else {
            showToast(data.message || '停止失败', 'error');
        }
    } catch (e) {
        showToast('停止失败', 'error');
    }
}

function deleteProject(id, name) {
    if (!confirm(`确定要将 "${name}" 移到回收站吗？`)) return;

    fetch('/delete-project', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id })
    })
        .then(res => res.json())
        .then(data => {
            if (data.success) {
                showToast('已移到回收站');
                allProjects = allProjects.filter(p => p.id !== id);
                renderProjectList();
            }
        })
        .catch(err => showToast('删除失败', 'error'));
}

async function copyProject(id, name) {
    const newName = prompt('请输入新项目名称:', name + ' - 副本');
    if (!newName || !newName.trim()) return;

    try {
        showToast('正在复制项目...');
        const response = await fetch('/copy-project', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                sourceProjectId: id,
                newProjectName: newName.trim()
            })
        });

        const data = await response.json();
        if (data.success && data.project) {
            showToast('项目复制成功');
            allProjects.unshift(data.project);
            renderProjectList();
        } else {
            showToast('复制失败: ' + (data.error || '未知错误'), 'error');
        }
    } catch (err) {
        console.error('复制项目失败:', err);
        showToast('复制失败', 'error');
    }
}

// ==================== 标题编辑功能 ====================
function editProjectTitle(id, currentName) {
    $('editProjectId').value = id;
    $('editTitleInput').value = currentName;
    $('editTitleModal').classList.remove('hidden');
    $('editTitleModal').classList.add('flex');
    setTimeout(() => $('editTitleInput').focus(), 100);
}

function closeEditTitleModal() {
    $('editTitleModal').classList.add('hidden');
    $('editTitleModal').classList.remove('flex');
}

async function saveProjectTitle() {
    const id = $('editProjectId').value;
    const newName = $('editTitleInput').value.trim();

    if (!newName) {
        showToast('标题不能为空', 'error');
        return;
    }

    try {
        const response = await fetch('/rename-project', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id, newName })
        });

        const data = await response.json();
        if (data.success) {
            showToast('标题已更新（文件夹已同步重命名）');
            // 使用后端返回的完整项目对象替换本地项目（包含新的id和url）
            const oldIndex = allProjects.findIndex(p => p.id === id);
            if (oldIndex !== -1 && data.project) {
                allProjects[oldIndex] = data.project;
            }
            renderProjectList();
            closeEditTitleModal();
        } else {
            showToast('更新失败: ' + (data.error || '未知错误'), 'error');
        }
    } catch (err) {
        showToast('更新失败', 'error');
    }
}

// ==================== 回收站功能 ====================
async function openRecycleBin() {
    $('recycleBinModal').classList.remove('hidden');
    $('recycleBinModal').classList.add('flex');
    $('recycleBinContent').innerHTML = '<div class="text-center py-8 text-gray-400 text-sm">加载中...</div>';

    try {
        const response = await fetch('/deleted-projects', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({})
        });

        const data = await response.json();
        if (data.success) {
            renderRecycleBin(data.projects || []);
        } else {
            $('recycleBinContent').innerHTML = '<div class="text-center py-8 text-red-400 text-sm">加载失败</div>';
        }
    } catch (err) {
        $('recycleBinContent').innerHTML = '<div class="text-center py-8 text-red-400 text-sm">加载失败</div>';
    }
}

function renderRecycleBin(projects) {
    if (projects.length === 0) {
        $('recycleBinContent').innerHTML = '<div class="text-center py-8 text-gray-400 text-sm">回收站为空</div>';
        return;
    }

    $('recycleBinContent').innerHTML = projects.map(p => `
        <div class="flex items-start justify-between p-3 rounded-lg hover:bg-gray-50 transition-all border-b border-gray-100 last:border-0">
            <div class="flex-1 min-w-0">
                <p class="text-sm font-medium text-gray-900 line-clamp-2" title="${p.name}">${p.name}</p>
                <p class="text-xs text-gray-400 mt-1">删除于: ${p.deletedAt || p.date}</p>
            </div>
            <button onclick="restoreProject('${p.id}')" 
                    class="flex-shrink-0 ml-2 px-3 py-1.5 text-sm text-indigo-600 hover:bg-indigo-50 rounded-lg transition flex items-center gap-1">
                <i class="fas fa-undo"></i> 恢复
            </button>
        </div>
    `).join('');
}

function closeRecycleBinModal() {
    $('recycleBinModal').classList.add('hidden');
    $('recycleBinModal').classList.remove('flex');
}

async function restoreProject(id) {
    try {
        const response = await fetch('/restore-project', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id })
        });

        const data = await response.json();
        if (data.success) {
            showToast('项目已恢复');
            // 添加到本地列表
            if (data.project) {
                allProjects.unshift(data.project);
                renderProjectList();
            }
            // 重新加载回收站
            openRecycleBin();
        } else {
            showToast('恢复失败: ' + (data.error || '未知错误'), 'error');
        }
    } catch (err) {
        showToast('恢复失败', 'error');
    }
}

// ==================== 记录查看功能 ====================
async function viewRecord(projectId) {
    try {
        // 获取项目记录
        const response = await fetch(`/projects/${projectId}/record.json?t=${Date.now()}`);
        if (!response.ok) {
            showToast('该项目没有保存记录', 'error');
            return;
        }

        const record = await response.json();
        currentRecordProject = { id: projectId, record };

        // 显示模态框
        renderRecordModal(record, projectId);
        $('recordModal').classList.remove('hidden');
        $('recordModal').classList.add('flex');

    } catch (error) {
        showToast('加载记录失败', 'error');
        console.error(error);
    }
}

function renderRecordModal(record, projectId) {
    const container = $('recordContent');

    // 全局设置
    let html = `
        <div class="mb-6">
            <h4 class="font-bold text-gray-700 mb-3 flex items-center gap-2">
                <i class="fas fa-palette text-indigo-500"></i> 全局设置
            </h4>
            <div class="grid grid-cols-2 gap-3 text-sm">
                <div class="flex items-center gap-2">
                    <span class="text-gray-500">主色调:</span>
                    <span class="w-5 h-5 rounded" style="background:${record.global?.primaryColor || '#004fff'}"></span>
                    <span class="font-mono">${record.global?.primaryColor || '#004fff'}</span>
                </div>
                <div class="flex items-center gap-2">
                    <span class="text-gray-500">强调色:</span>
                    <span class="w-5 h-5 rounded" style="background:${record.global?.secondaryColor || '#10B981'}"></span>
                    <span class="font-mono">${record.global?.secondaryColor || '#10B981'}</span>
                </div>
                <div><span class="text-gray-500">背景模式:</span> ${record.global?.backgroundMode || 'light'}</div>
                <div><span class="text-gray-500">组件风格:</span> ${record.global?.componentStyle || 'Ant Design'}</div>
            </div>
        </div>
    `;

    // 页面列表
    if (record.pages && record.pages.length > 0) {
        html += `<h4 class="font-bold text-gray-700 mb-3 flex items-center gap-2">
            <i class="fas fa-file-alt text-green-500"></i> 页面信息
        </h4>`;

        record.pages.forEach((page, index) => {
            html += `
                <div class="bg-gray-50 rounded-lg p-4 mb-4">
                    <h5 class="font-bold text-gray-800 mb-2">页面 ${index + 1}: ${page.name || '未命名'}</h5>
                    <div class="space-y-2 text-sm">
                        ${page.layout ? `<div><span class="text-gray-500">布局描述:</span><p class="mt-1 text-gray-700">${page.layout}</p></div>` : ''}
                        ${page.features ? `<div><span class="text-gray-500">核心功能:</span><p class="mt-1 text-gray-700">${page.features}</p></div>` : ''}
                        ${page.interaction ? `<div><span class="text-gray-500">交互说明:</span><p class="mt-1 text-gray-700">${page.interaction}</p></div>` : ''}
                        <div><span class="text-gray-500">参考相似度:</span> ${page.similarity || 'layout'}</div>
                    </div>
            `;

            // 参考图片
            if (page.images && page.images.length > 0) {
                html += `
                    <div class="mt-3">
                        <span class="text-gray-500 text-sm">参考图片:</span>
                        <div class="grid grid-cols-4 gap-2 mt-2">
                            ${page.images.map(img => `
                                <img src="/projects/${projectId}/reference/${img}" 
                                     class="w-full aspect-square object-cover rounded border cursor-pointer hover:opacity-80"
                                     onclick="previewImage('/projects/${projectId}/reference/${img}')">
                            `).join('')}
                        </div>
                    </div>
                `;
            }

            html += `</div>`;
        });
    }

    container.innerHTML = html;
}

function closeRecordModal() {
    $('recordModal').classList.add('hidden');
    $('recordModal').classList.remove('flex');
}

// 从 record 恢复表单数据的共享逻辑（供"加载并重新生成"和"作为新项目加载"复用）
async function restoreFormDataFromRecord(record, projectId) {
    // 清空当前表单
    pages = [];
    pageFiles = {};
    pageEnums = {};
    globalEnums = {};
    $('pageCardsContainer').innerHTML = '';

    if (record.global) {
        $('primaryColor').value = record.global.primaryColor || '#004fff';
        $('primaryColorValue').textContent = record.global.primaryColor || '#004fff';
        $('secondaryColor').value = record.global.secondaryColor || '#10B981';
        $('secondaryColorValue').textContent = record.global.secondaryColor || '#10B981';
        $('backgroundMode').value = record.global.backgroundMode || 'light';
        $('componentStyle').value = record.global.componentStyle || 'Ant Design';
        // 恢复全局枚举数据
        if (record.global.enums && typeof record.global.enums === 'object') {
            globalEnums = record.global.enums;
        }
    }

    // 恢复页面
    if (record.pages && record.pages.length > 0) {
        for (const pageRecord of record.pages) {
            const id = Date.now().toString() + Math.random().toString(36).substr(2, 5);
            pages.push(id);
            pageFiles[id] = [];

            const index = pages.length;
            const html = createPageCardHtml(id, index);

            const div = document.createElement('div');
            div.id = `page-${id}`;
            div.className = 'bg-white rounded-xl shadow-sm border border-gray-200 overflow-hidden';
            div.innerHTML = html;
            $('pageCardsContainer').appendChild(div);
            setupPageListeners(id);

            // 填入数据
            await new Promise(r => setTimeout(r, 50)); // 等待DOM更新
            if (pageRecord.name) $(`pageName_${id}`).value = ensureString(pageRecord.name);
            if (pageRecord.description) $(`description_${id}`).value = ensureString(pageRecord.description);
            if (pageRecord.layout) $(`layout_${id}`).value = ensureString(pageRecord.layout);
            if (pageRecord.features) $(`features_${id}`).value = ensureString(pageRecord.features);
            if (pageRecord.dataStructure) $(`dataStructure_${id}`).value = ensureString(pageRecord.dataStructure);
            if (pageRecord.interaction) $(`interaction_${id}`).value = ensureString(pageRecord.interaction);
            if (pageRecord.userFlow) $(`userFlow_${id}`).value = ensureString(pageRecord.userFlow);
            if (pageRecord.similarity) {
                const radio = document.querySelector(`input[name="similarity_${id}"][value="${pageRecord.similarity}"]`);
                if (radio) {
                    radio.checked = true;
                    // 更新 active 样式
                    const group = $(`similarityGroup_${id}`);
                    if (group) {
                        group.querySelectorAll('.similarity-btn').forEach(btn => btn.classList.remove('active'));
                        radio.closest('.similarity-btn').classList.add('active');
                    }
                }
            }

            // 恢复页面级枚举数据
            if (pageRecord.enums && typeof pageRecord.enums === 'object') {
                pageEnums[id] = pageRecord.enums;
            }

            // 加载参考图片（从服务器）- 使用Promise确保等待完成
            if (pageRecord.images && pageRecord.images.length > 0) {
                const imageLoadPromises = pageRecord.images.map(imgName => {
                    return new Promise(async (resolve) => {
                        try {
                            const imgUrl = `/projects/${projectId}/reference/${imgName}`;
                            const response = await fetch(imgUrl);
                            const blob = await response.blob();
                            const reader = new FileReader();
                            reader.onload = (e) => {
                                pageFiles[id].push({
                                    name: imgName,
                                    base64: e.target.result
                                });
                                renderPreviews(id);
                                resolve();
                            };
                            reader.onerror = () => resolve(); // 失败也继续
                            reader.readAsDataURL(blob);
                        } catch (e) {
                            console.error('加载参考图失败:', e);
                            resolve(); // 失败也继续
                        }
                    });
                });
                // 等待该页面所有图片加载完成
                await Promise.all(imageLoadPromises);
            }
        }
    } else {
        addPage();
    }
}

// 加载历史记录并重新生成（增量模式）
async function regenerateFromRecord() {
    if (!currentRecordProject) return;

    const record = currentRecordProject.record;
    const projectId = currentRecordProject.id;
    closeRecordModal();

    // 保存来源项目ID（用于增量更新）
    sourceProjectId = projectId;
    console.log('[增量更新] 开始加载历史记录，sourceProjectId:', sourceProjectId);

    await restoreFormDataFromRecord(record, projectId);

    // 图片已全部加载完成，保存原始快照
    originalFormData = collectFormData();
    originalImageHashes = computeAllImageHashes();

    console.log('[增量更新] 已保存原始快照:', {
        sourceProjectId,
        originalFormData,
        originalImageHashes,
        pagesCount: pages.length,
        imagesCounts: pages.map(id => pageFiles[id]?.length || 0)
    });

    $('headerTitle').textContent = '已加载历史记录 - 可修改后重新生成（支持智能更新）';
    showToast('已加载历史记录，修改后将智能更新');
}

// 作为新项目加载（不触发增量模式）
async function loadAsNewProject() {
    if (!currentRecordProject) return;

    const record = currentRecordProject.record;
    const projectId = currentRecordProject.id;
    closeRecordModal();

    // 不设置 sourceProjectId，不走增量模式
    sourceProjectId = null;
    originalFormData = null;
    originalImageHashes = {};

    await restoreFormDataFromRecord(record, projectId);

    $('headerTitle').textContent = '新项目 - 已加载历史配置';
    showToast('已加载历史配置，生成时将作为全新项目处理');
}

// 复制历史项目的提示词到剪贴板
async function copyPromptFromRecord() {
    if (!currentRecordProject) return;
    const projectId = currentRecordProject.id;

    try {
        // 优先从服务器读取保存的 prompt.txt
        const response = await fetch(`/projects/${projectId}/prompt.txt?t=${Date.now()}`);
        if (response.ok) {
            const promptText = await response.text();
            await copyToClipboard(promptText);
            showToast('提示词已复制到剪贴板');
            return;
        }

        // fallback: 如果 prompt.txt 不存在，临时恢复表单并生成 prompt
        const record = currentRecordProject.record;
        // 保存当前表单状态
        const savedPages = [...pages];
        const savedPageFiles = JSON.parse(JSON.stringify(pageFiles));
        const savedGlobalEnums = JSON.parse(JSON.stringify(globalEnums));
        const savedPageEnums = JSON.parse(JSON.stringify(pageEnums));

        await restoreFormDataFromRecord(record, projectId);
        const promptText = generatePrompt();
        await copyToClipboard(promptText);

        // 还原表单状态
        pages = savedPages;
        pageFiles = savedPageFiles;
        globalEnums = savedGlobalEnums;
        pageEnums = savedPageEnums;
        $('pageCardsContainer').innerHTML = '';
        for (const id of pages) {
            const index = pages.indexOf(id) + 1;
            const html = createPageCardHtml(id, index);
            const div = document.createElement('div');
            div.id = `page-${id}`;
            div.className = 'bg-white rounded-xl shadow-sm border border-gray-200 overflow-hidden';
            div.innerHTML = html;
            $('pageCardsContainer').appendChild(div);
            setupPageListeners(id);
        }

        showToast('提示词已复制到剪贴板（从记录重建）');
    } catch (err) {
        console.error('复制提示词失败:', err);
        showToast('复制提示词失败: ' + err.message, 'error');
    }
}

// 剪贴板写入的通用方法
async function copyToClipboard(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
    } else {
        // fallback for non-HTTPS contexts
        const textarea = document.createElement('textarea');
        textarea.value = text;
        textarea.style.cssText = 'position:fixed;opacity:0';
        document.body.appendChild(textarea);
        textarea.select();
        document.execCommand('copy');
        document.body.removeChild(textarea);
    }
}

// ==================== 页面卡片管理 ====================
function addPage() {
    const id = Date.now().toString();
    pages.push(id);
    pageFiles[id] = [];

    const index = pages.length;
    const html = createPageCardHtml(id, index);

    const div = document.createElement('div');
    div.id = `page-${id}`;
    div.className = 'bg-white rounded-xl shadow-sm border border-gray-200 overflow-hidden';
    div.innerHTML = html;

    $('pageCardsContainer').appendChild(div);
    setupPageListeners(id);
}

function createPageCardHtml(id, index) {
    return `
        <div class="border-b border-gray-200 px-6 py-4 bg-gray-50 flex justify-between items-center">
            <div class="flex items-center gap-3 flex-1">
                <span class="w-8 h-8 rounded-lg bg-indigo-600 text-white flex items-center justify-center text-sm font-bold">${index}</span>
                <input type="text" id="pageName_${id}" class="flex-1 bg-transparent border-none focus:ring-0 font-bold text-lg placeholder-gray-400" placeholder="页面名称（如：首页、用户列表）">
            </div>
            <button onclick="removePage('${id}')" class="text-gray-400 hover:text-red-500 p-2" title="删除页面">
                <i class="fas fa-trash-alt"></i>
            </button>
        </div>

        <div class="p-6 space-y-4">
            <!-- 页面用途简述 -->
            <div>
                <label class="block text-sm font-medium text-gray-700 mb-1">页面用途 <span class="text-gray-400 font-normal">（一句话说明这个页面做什么）</span></label>
                <input type="text" id="description_${id}" class="w-full rounded-lg border-gray-200 border p-2.5 text-sm" placeholder="如：管理和查看系统中的所有用户信息">
            </div>

            <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                <!-- 布局描述 -->
                <div>
                    <label class="block text-sm font-medium text-gray-700 mb-1">布局描述</label>
                    <textarea id="layout_${id}" rows="4" class="w-full rounded-lg border-gray-200 border p-3 text-sm resize-none" placeholder="描述页面的布局结构...&#10;如：顶部标题栏 + 左侧筛选面板(1/4宽) + 右侧数据表格(3/4宽)&#10;右上角有搜索框和'新建'按钮"></textarea>
                </div>

                <!-- 参考图上传 -->
                <div>
                    <div class="flex justify-between items-center mb-1">
                        <label class="text-sm font-medium text-gray-700">参考图</label>
                        <div class="flex gap-1" id="similarityGroup_${id}">
                            <label class="similarity-btn active" data-value="layout">
                                <input type="radio" name="similarity_${id}" value="layout" checked class="hidden">
                                仅参考布局
                            </label>
                            <label class="similarity-btn" data-value="style">
                                <input type="radio" name="similarity_${id}" value="style" class="hidden">
                                仅参考风格
                            </label>
                            <label class="similarity-btn" data-value="pixel">
                                <input type="radio" name="similarity_${id}" value="pixel" class="hidden">
                                像素级还原
                            </label>
                        </div>
                    </div>
                    <div id="dropZone_${id}" tabindex="0" class="border-2 border-dashed border-gray-200 rounded-lg h-24 flex items-center justify-center text-center hover:border-indigo-500 hover:bg-indigo-50/50 transition-all cursor-pointer focus:outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-200">
                        <div class="text-gray-400 text-sm">
                            <i class="fas fa-image mr-2"></i>点击、拖拽或粘贴上传
                        </div>
                        <input type="file" id="fileInput_${id}" class="hidden" accept="image/*" multiple>
                    </div>
                    <div id="preview_${id}" class="grid grid-cols-5 gap-2 mt-2 hidden"></div>
                </div>
            </div>

            <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                <!-- UI 组件描述 -->
                <div>
                    <label class="block text-sm font-medium text-gray-700 mb-1">UI 组件 <span class="text-gray-400 font-normal">（页面包含的组件和配置）</span></label>
                    <textarea id="features_${id}" rows="4" class="w-full rounded-lg border-gray-200 border p-3 text-sm resize-none" placeholder="列出页面中的UI组件：&#10;- 搜索栏：关键词搜索 + 状态筛选下拉&#10;- 数据表格：列（姓名/邮箱/角色/状态/操作），支持排序&#10;- 操作按钮：新建、批量删除&#10;- 分页：每页10条"></textarea>
                </div>

                <!-- 数据字段 -->
                <div>
                    <label class="block text-sm font-medium text-gray-700 mb-1">数据字段 <span class="text-gray-400 font-normal">（核心数据对象及其属性）</span></label>
                    <textarea id="dataStructure_${id}" rows="4" class="w-full rounded-lg border-gray-200 border p-3 text-sm resize-none" placeholder="列出核心数据字段（用于生成真实示例数据）：&#10;用户对象：ID、姓名、邮箱、角色(管理员/编辑/访客)、状态(启用/禁用)、创建时间"></textarea>
                </div>
            </div>

            <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                <!-- 交互行为 -->
                <div>
                    <label class="block text-sm font-medium text-gray-700 mb-1">交互行为</label>
                    <textarea id="interaction_${id}" rows="4" class="w-full rounded-lg border-gray-200 border p-3 text-sm resize-none" placeholder="描述用户操作和页面响应：&#10;- 点击'新建'→弹出表单弹窗&#10;- 每行有编辑/删除操作，删除前需确认&#10;- 选择部门后自动过滤用户列表&#10;- 搜索实时过滤，300ms防抖"></textarea>
                </div>

                <!-- 用户流程 -->
                <div>
                    <label class="block text-sm font-medium text-gray-700 mb-1">用户流程 <span class="text-gray-400 font-normal">（可选）</span></label>
                    <textarea id="userFlow_${id}" rows="4" class="w-full rounded-lg border-gray-200 border p-3 text-sm resize-none" placeholder="描述典型操作步骤：&#10;1. 进入页面查看数据概览&#10;2. 使用筛选条件缩小范围&#10;3. 点击某条记录查看详情&#10;4. 执行编辑或审批操作"></textarea>
                </div>
            </div>
        </div>
    `;
}

function removePage(id) {
    if (pages.length <= 1) {
        showToast('至少需要一个页面', 'error');
        return;
    }

    const el = $(`page-${id}`);
    el.remove();
    pages = pages.filter(p => p !== id);
    delete pageFiles[id];
    delete pageEnums[id];

    // 更新序号
    pages.forEach((pid, i) => {
        const badge = document.querySelector(`#page-${pid} .bg-indigo-600`);
        if (badge) badge.textContent = i + 1;
    });
}

function setupPageListeners(id) {
    const dropZone = $(`dropZone_${id}`);
    const fileInput = $(`fileInput_${id}`);

    // 鼠标悬停时自动聚焦，使粘贴无需点击
    dropZone.onmouseenter = () => dropZone.focus();

    dropZone.onclick = () => fileInput.click();

    dropZone.ondragover = (e) => {
        e.preventDefault();
        dropZone.classList.add('border-indigo-500', 'bg-indigo-50');
    };

    dropZone.ondragleave = () => {
        dropZone.classList.remove('border-indigo-500', 'bg-indigo-50');
    };

    dropZone.ondrop = (e) => {
        e.preventDefault();
        dropZone.classList.remove('border-indigo-500', 'bg-indigo-50');
        handleFiles(id, e.dataTransfer.files);
    };

    fileInput.onchange = (e) => {
        handleFiles(id, e.target.files);
        fileInput.value = '';
    };

    // 支持粘贴剪切板中的图片
    dropZone.addEventListener('paste', (e) => {
        e.preventDefault();
        const items = e.clipboardData?.items;
        if (!items) return;

        const imageFiles = [];
        for (const item of items) {
            if (item.type.startsWith('image/')) {
                const file = item.getAsFile();
                if (file) imageFiles.push(file);
            }
        }

        if (imageFiles.length > 0) {
            handleFiles(id, imageFiles);
            showToast(`已粘贴 ${imageFiles.length} 张图片`);
        }
    });

    // 参考图相似度选项切换
    const similarityGroup = $(`similarityGroup_${id}`);
    if (similarityGroup) {
        similarityGroup.querySelectorAll('.similarity-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                similarityGroup.querySelectorAll('.similarity-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
            });
        });
    }
}

function handleFiles(id, files) {
    Array.from(files).filter(f => f.type.startsWith('image/')).forEach(file => {
        const reader = new FileReader();
        reader.onload = (e) => {
            pageFiles[id].push({
                name: file.name,
                base64: e.target.result
            });
            renderPreviews(id);
        };
        reader.readAsDataURL(file);
    });
}

function renderPreviews(id) {
    const container = $(`preview_${id}`);
    const files = pageFiles[id];

    if (files.length === 0) {
        container.classList.add('hidden');
        return;
    }

    container.classList.remove('hidden');
    container.innerHTML = files.map((f, i) => `
        <div class="relative aspect-square bg-gray-100 rounded overflow-hidden group">
            <img src="${f.base64}" class="w-full h-full object-cover cursor-zoom-in" onclick="previewImage('${f.base64}')">
            <button onclick="removeFile('${id}', ${i})" class="absolute top-1 right-1 w-5 h-5 bg-black/60 text-white rounded-full text-xs opacity-0 group-hover:opacity-100 transition-opacity">×</button>
        </div>
    `).join('');
}

function removeFile(id, index) {
    pageFiles[id].splice(index, 1);
    renderPreviews(id);
}

function previewImage(src) {
    $('fullSizeImage').src = src;
    $('imagePreviewModal').classList.remove('hidden');
    $('imagePreviewModal').classList.add('flex');
}

// ==================== AI 生成 ====================
// 构建参考图的详细 prompt 指令（根据 similarity 模式生成不同级别的还原要求）
function buildImagePromptInstructions(pageName, imageCount, similarity, globalColors) {
    let instructions = `**参考图**: 已附加${imageCount}张参考图，这是页面"${pageName}"的目标设计。\n\n`;

    // 构建色调优先级声明：用户指定的色调始终为最高优先级
    const colorPriority = globalColors
        ? `\n**色调优先级（最高约束）**: 你必须严格使用用户指定的全局色调，不可被参考图的颜色覆盖：
- 主色必须使用: ${globalColors.primaryColor}
- 强调色必须使用: ${globalColors.secondaryColor}
- 背景模式: ${globalColors.backgroundMode === 'light' ? '浅色' : '深色'}
参考图中的配色仅作为辅助参考，当参考图颜色与用户指定色调冲突时，以用户指定色调为准。\n\n`
        : '';

    if (similarity === 'pixel') {
        instructions += `**参考图还原要求（最高优先级）**:
你必须先仔细分析参考图，然后尽可能精确地还原。请按以下步骤：
1. **颜色提取**: 从参考图中识别所有使用的颜色值（辅色、背景色、文字色、边框色、阴影色），在HTML中精确复现。
2. **布局还原**: 准确还原参考图的区域划分、元素位置、间距比例。注意header/body/footer/侧边栏的精确位置关系。
3. **组件识别**: 识别参考图中的每个UI组件类型（按钮、输入框、表格、卡片、导航、标签页等），用对应的HTML+Tailwind代码还原。
4. **字体与排版**: 还原标题/正文的字号层级、字重、行高、对齐方式。
5. **间距与留白**: 精确还原元素之间的间距、内边距、外边距。
6. **图标与装饰**: 还原参考图中的图标位置和装饰性元素。

**还原精度要求**:
- 配色方案以用户指定的主色和强调色为基准，其他颜色从参考图中提取
- 元素的相对位置、大小比例必须与参考图一致
- 使用与参考图相同或极为相似的UI组件结构
- 如果参考图中有数据表格/列表，保持列数和内容类型一致\n\n`;
        instructions += colorPriority;
    } else if (similarity === 'style') {
        instructions += `**风格参考要求**:
1. **配色方案**: 主色和强调色严格使用用户指定的全局色调（${globalColors ? globalColors.primaryColor + ' / ' + globalColors.secondaryColor : '见全局设计规范'}），其他辅助色从参考图的色系范围中提取，保持氛围匹配。
2. **质感与氛围**: 还原参考图的视觉质感（扁平/拟物/毛玻璃/渐变等）和整体氛围（专业/活泼/简约/奢华等）。
3. **字体风格**: 参考图的字号层级和字重风格。
4. **圆角与阴影**: 参考参考图的圆角大小和阴影深浅。
5. **组件风格**: 参考图中按钮、卡片、输入框等组件的视觉风格。

布局和内容可以自由发挥，但视觉风格必须与参考图高度一致。\n\n`;
        instructions += colorPriority;
    } else {
        instructions += `**布局参考要求**:
1. **区域划分**: 参考图的整体区域划分（header/nav/main/sidebar/footer的比例和位置）。
2. **元素位置**: 参考图中主要功能区域的位置关系（上下、左右、嵌套关系）。
3. **栅格/比例**: 参考图的列数和各区域宽度比例。
4. **层次结构**: 参考图的视觉层次（哪些元素突出，哪些是次要的）。

配色必须严格遵循用户指定的全局色调**（主色和强调色），区域划分和元素位置关系应与参考图一致。\n\n`;
        instructions += colorPriority;
    }

    return instructions;
}

function generatePrompt() {
    const global = {
        primaryColor: $('primaryColor').value,
        secondaryColor: $('secondaryColor').value,
        backgroundMode: $('backgroundMode').value,
        componentStyle: $('componentStyle').value
    };

    // 检测是否为多页面项目
    const isMultiPage = pages.length > 1;

    let prompt = `你是一位资深的前端工程师和UI/UX设计师，擅长创建**精美、专业、高保真**的可交互HTML原型。
你的设计风格参考 Ant Design / Element Plus 等成熟 UI 框架的视觉标准。
请根据以下设计规范和需求，生成一个视觉效果出色的HTML原型。

# 技术栈
- Tailwind CSS (CDN)
- Vue 3 (CDN, 可选)
- FontAwesome (CDN)
- ECharts (如需图表)
- Google Fonts (Inter)

# 全局设计规范
- 主色: ${global.primaryColor}
- 强调色: ${global.secondaryColor}
- 背景模式: ${global.backgroundMode === 'light' ? '浅色' : '深色'}
- 组件风格: ${global.componentStyle}
- 圆角: 0.5rem
- 阴影: 使用柔和现代的阴影
- 字体: 系统默认或 Inter
- 所有文字使用中文
- 使用真实、有意义的示例数据（不要使用 Lorem ipsum）
`;

    // 多页面时添加导航说明
    if (isMultiPage) {
        prompt += `
# 页面导航（重要）
这是一个包含 ${pages.length} 个页面的多页面原型。
请使用**标签页(Tabs)或侧边导航**来组织多个页面，确保用户可以在页面间切换。
每个页面对应一个独立的标签页/导航项，切换时显示对应内容，隐藏其他页面内容。
`;

        // 列出所有页面名作为导航项
        const pageNames = pages.map((id, index) => {
            const name = $(`pageName_${id}`).value || `页面${index + 1}`;
            return `  ${index + 1}. ${name}`;
        });
        prompt += `导航菜单项：\n${pageNames.join('\n')}\n`;

        prompt += `
# 跨页面一致性要求（非常重要！）

多页面原型中，以下组件在所有页面必须使用完全统一的样式，禁止每个页面各自定义不同的样式：

## 面包屑导航规范
所有包含面包屑导航的页面必须使用以下统一结构：
\`\`\`html
<nav class="breadcrumb">
  <a href="#" class="breadcrumb-link">父级页面</a>
  <span class="breadcrumb-sep">/</span>
  <span class="breadcrumb-current">当前页面</span>
</nav>
\`\`\`
- **样式必须只定义一次**（在全局 <style> 中），禁止在每个页面的 scoped 样式中重复定义
- 链接文字颜色: #666，悬停时变为主色（${global.primaryColor}）
- 当前页文字颜色: #333，font-weight: 600
- 分隔符: "/"，颜色 #ccc，两侧各 4px 间距
- 字号: 14px，行高与页面正文一致
- 面包屑位置: 统一在页面内容区最顶部，与下方内容保持 16px 间距

## 侧边栏/顶部导航规范
- 所有页面共享同一套导航栏结构和样式
- 当前页在导航中高亮显示
- 不要在不同页面中使用不同的导航样式

## 通用组件规范
- 按钮、表格、表单、卡片等基础组件样式在所有页面保持一致
- 颜色、圆角、阴影、字号等使用统一的 CSS 变量
- 不要在不同页面中使用不同风格的同一类组件
`;
    }

    prompt += `\n# 页面需求\n`;

    pages.forEach((id, index) => {
        const name = $(`pageName_${id}`).value || `页面${index + 1}`;
        const description = $(`description_${id}`) ? $(`description_${id}`).value : '';
        const layout = $(`layout_${id}`).value;
        const features = $(`features_${id}`).value;
        const dataStructure = $(`dataStructure_${id}`) ? $(`dataStructure_${id}`).value : '';
        const interaction = $(`interaction_${id}`).value;
        const userFlow = $(`userFlow_${id}`) ? $(`userFlow_${id}`).value : '';
        const similarity = (document.querySelector(`input[name="similarity_${id}"]:checked`) || {}).value || 'layout';
        const hasImages = pageFiles[id].length > 0;

        prompt += `
## 页面${index + 1}: ${name}
`;

        if (description) {
            prompt += `**用途**: ${description}\n\n`;
        }

        if (layout) {
            prompt += `**布局结构**:\n${layout}\n\n`;
        }

        if (features) {
            prompt += `**UI组件**:\n${features}\n\n`;
        }

        if (dataStructure) {
            prompt += `**数据字段**（请据此生成真实示例数据）:\n${dataStructure}\n\n`;
        }

        // 添加枚举值定义（页面级 + 全局级）
        const pageEnumData = pageEnums[id] || {};
        const mergedEnums = { ...globalEnums };
        // 页面级枚举覆盖同名的全局枚举
        for (const [k, v] of Object.entries(pageEnumData)) {
            mergedEnums[k] = v;
        }
        if (Object.keys(mergedEnums).length > 0) {
            prompt += `**枚举值定义**（用于下拉选项、状态标签、筛选器等，请严格使用这些值）:\n`;
            for (const [enumName, enumData] of Object.entries(mergedEnums)) {
                if (Array.isArray(enumData)) {
                    prompt += `- ${enumName}: ${enumData.join(', ')}\n`;
                } else if (enumData && enumData.values) {
                    prompt += `- ${enumName}: ${enumData.values.join(', ')}`;
                    if (enumData.description) {
                        prompt += `（${enumData.description}）`;
                    }
                    prompt += `\n`;
                }
            }
            prompt += `\n`;
        }

        if (interaction) {
            prompt += `**交互行为**:\n${interaction}\n\n`;
        }

        if (userFlow) {
            prompt += `**用户操作流程**:\n${userFlow}\n\n`;
        }

        if (hasImages) {
            prompt += buildImagePromptInstructions(name, pageFiles[id].length, similarity, global);
        }

        // 如果没有填写任何详细信息，给出基本指引
        if (!layout && !features && !interaction && !dataStructure && !hasImages) {
            prompt += `请根据页面名称"${name}"设计一个常见且合理的页面布局和功能。\n\n`;
        }
    });

    prompt += `# 输出要求（重要！）

请输出一个**完整的、独立的HTML文件**。

## 代码质量要求
1. 所有CSS放在<style>标签中，优先使用Tailwind CSS类名
2. 所有JS放在<script>标签中
3. 语义化HTML标签（header, main, nav, section, article等）
4. 完整的响应式设计（移动端适配）
5. 使用真实、有意义的中文示例数据
6. 每个交互元素都有真实的行为（按钮可点击、表单可填写、列表可排序）

## 视觉质量要求（必须达到专业水准）

### 整体质感
1. 页面背景使用 ${global.backgroundMode === 'light' ? '#f5f7fa 或 #f0f2f5 浅灰色' : '深色渐变'}，**不要纯白/纯黑**。卡片用白色/近白背景，与页面底色形成对比层次
2. 使用 CSS 变量统一管理颜色、间距、圆角、阴影，确保全局一致
3. 图标使用 FontAwesome 增强信息表达，不要纯文字堆砌

### 阴影与深度
4. 卡片使用**多层复合阴影**：box-shadow: 0 1px 2px rgba(0,0,0,0.04), 0 4px 12px rgba(0,0,0,0.08)
5. 悬停时阴影加深过渡：transition: box-shadow 0.3s ease
6. 弹窗/模态框使用更强阴影：0 8px 30px rgba(0,0,0,0.15)
7. **禁止**扁平无阴影的设计，也避免过于浓重的阴影

### 圆角与边框
8. 卡片/面板：border-radius: 12px；按钮/输入框：6-8px；标签/徽章：4px 或 999px（胶囊型）
9. 边框使用浅色：1px solid rgba(0,0,0,0.06) 或 #e8e8e8

### 微交互（所有可交互元素必须有过渡动画）
10. 所有可点击元素必须有 hover 过渡（transition: all 0.2s ease）
11. 按钮 hover：背景色变化 + 轻微上浮（translateY(-1px)）；active：轻微下压（scale(0.98)）
12. 卡片 hover：阴影加深 + 轻微上浮（translateY(-2px)）
13. 输入框 focus：边框变主色 + 外发光（box-shadow: 0 0 0 3px rgba(主色, 0.15)）
14. 列表行 hover：背景微变（rgba(0,0,0,0.02)~rgba(0,0,0,0.04)）

### 色彩层次
15. 主色只用于关键操作按钮、激活态、重要标记，**不要大面积使用**
16. 使用 rgba 透明度变体：主色 8% 透明度做背景、15% 做悬停
17. 文字层次：标题 #1f1f1f → 正文 #333 → 辅助 #666 → 禁用/提示 #999

### 间距与排版
18. 基于 4px 网格的间距系统：页面内边距 24-32px、卡片内边距 20-24px、卡片间距 16-24px
19. 标题字重 600、正文字重 400。字号梯度：页面标题 20-24px、区块标题 16-18px、正文 14px、辅助 12px
20. 行高 1.5-1.7，表格行高 54px

### 组件精修
21. **按钮**：内边距 8px 16px、hover 变色、active 缩放、禁用半透明+cursor:not-allowed
22. **输入框**：高度 32-36px、placeholder #bfbfbf、focus 发光边框
23. **表格**：表头 #fafafa 背景、斑马纹、hover 行高亮
24. **空状态**：居中大图标 + 灰色说明文字 + 操作按钮
25. 状态色彩明确：成功 #52c41a、警告 #faad14、错误 #ff4d4f、信息 #1890ff

## 交互质量要求
1. 所有按钮有 hover/active 效果
2. 表格支持排序（点击表头）
3. 搜索/筛选有即时响应效果
4. 弹窗/模态框有遮罩和 fade+scale 动画
5. 表单有基本验证提示
6. 页面切换平滑无闪烁

输出格式：
\`\`\`html
<!DOCTYPE html>
<html lang="zh-CN">
...完整代码...
</html>
\`\`\`
`;

    return prompt;
}

// 收集用户输入数据用于保存记录
function collectFormData() {
    const global = {
        primaryColor: $('primaryColor').value,
        secondaryColor: $('secondaryColor').value,
        backgroundMode: $('backgroundMode').value,
        componentStyle: $('componentStyle').value
    };

    const pagesData = pages.map((id, index) => ({
        name: $(`pageName_${id}`).value || `页面${index + 1}`,
        description: $(`description_${id}`) ? $(`description_${id}`).value : '',
        layout: $(`layout_${id}`).value,
        features: $(`features_${id}`).value,
        dataStructure: $(`dataStructure_${id}`) ? $(`dataStructure_${id}`).value : '',
        interaction: $(`interaction_${id}`).value,
        userFlow: $(`userFlow_${id}`) ? $(`userFlow_${id}`).value : '',
        similarity: (document.querySelector(`input[name="similarity_${id}"]:checked`) || {}).value || 'layout',
        imageCount: pageFiles[id].length
    }));

    const generationConfig = {};

    return { global, pages: pagesData, generationConfig };
}

// ==================== 增量更新功能 ====================

// 计算简单的字符串哈希
function simpleHash(str) {
    let hash = 0;
    for (let i = 0; i < str.length; i++) {
        const char = str.charCodeAt(i);
        hash = ((hash << 5) - hash) + char;
        hash = hash & hash;
    }
    return hash.toString(16);
}

// 计算所有页面的图片哈希
function computeAllImageHashes() {
    const hashes = {};
    pages.forEach((id, index) => {
        const images = pageFiles[id] || [];
        const imageData = images.map(f => f.base64.substring(0, 100)).join('|');
        hashes[index] = simpleHash(imageData);
    });
    return hashes;
}

// ==================== 输入验证 ====================
/**
 * 检查是否有任何用户输入
 * @returns {boolean} 如果有任何输入返回true，否则返回false
 */
function hasAnyInput() {
    // 检查是否有页面名称
    const hasPageName = pages.some(id => {
        const name = $(`pageName_${id}`);
        return name && name.value && name.value.trim() !== '';
    });

    // 检查是否有布局描述
    const hasLayout = pages.some(id => {
        const layout = $(`pageLayout_${id}`);
        return layout && layout.value && layout.value.trim() !== '';
    });

    // 检查是否有功能点描述
    const hasFeatures = pages.some(id => {
        const features = $(`pageFeatures_${id}`);
        return features && features.value && features.value.trim() !== '';
    });

    // 检查是否有交互方式描述
    const hasInteraction = pages.some(id => {
        const interaction = $(`pageInteraction_${id}`);
        return interaction && interaction.value && interaction.value.trim() !== '';
    });

    // 检查是否有参考图片
    const hasImages = pages.some(id => pageFiles[id] && pageFiles[id].length > 0);

    // 检查全局配置是否被修改过（这些有默认值，检查是否与默认值不同）
    const globalChanged = (
        $('primaryColor').value !== '#004fff' ||
        $('secondaryColor').value !== '#10b981' ||
        $('backgroundMode').value !== 'light' ||
        $('componentStyle').value !== 'Ant Design'
    );

    // 只要有任何一项输入就返回true
    return hasPageName || hasLayout || hasFeatures || hasInteraction || hasImages || globalChanged;
}

/**
 * 显示Toast提示
 * @param {string} message - 提示消息
 * @param {string} type - 类型: 'success', 'error', 'info'
 */
function showToast(message, type = 'success') {
    console.log('[Toast] 显示提示:', message, '类型:', type);

    const toast = $('toast');
    const toastIcon = $('toastIcon');
    const toastMessage = $('toastMessage');

    if (!toast || !toastIcon || !toastMessage) {
        console.error('[Toast] DOM元素未找到!', { toast, toastIcon, toastMessage });
        return;
    }

    // 设置消息
    toastMessage.textContent = message;

    // 设置图标和颜色
    if (type === 'success') {
        toastIcon.className = 'fas fa-check-circle text-green-400';
    } else if (type === 'error') {
        toastIcon.className = 'fas fa-exclamation-circle text-red-400';
    } else if (type === 'info') {
        toastIcon.className = 'fas fa-info-circle text-blue-400';
    }

    // 移除隐藏状态
    toast.classList.remove('hidden');

    // 强制重绘以触发动画
    requestAnimationFrame(() => {
        toast.classList.remove('translate-y-20', 'opacity-0');
        toast.classList.add('translate-y-0', 'opacity-100');
    });

    // 3秒后隐藏
    setTimeout(() => {
        toast.classList.remove('translate-y-0', 'opacity-100');
        toast.classList.add('translate-y-20', 'opacity-0');

        // 动画结束后完全隐藏
        setTimeout(() => {
            toast.classList.add('hidden');
        }, 300); // 等待transition完成
    }, 3000);
}

// 检测变更
function detectChanges(original, current, origImgHashes, currImgHashes) {
    const changes = {
        hasChanges: false,
        globalChanged: false,
        pagesChanged: [],      // 变化的页面索引
        pagesUnchanged: [],    // 未变化的页面索引
        newPages: [],          // 新增的页面索引
        deletedPages: []       // 删除的页面索引
    };

    if (!original || !current) {
        changes.hasChanges = true;
        return changes;
    }

    // 对比全局设置
    if (JSON.stringify(original.global) !== JSON.stringify(current.global)) {
        changes.globalChanged = true;
        changes.hasChanges = true;
    }

    // 对比页面数量
    const origLen = original.pages.length;
    const currLen = current.pages.length;

    // 对比每个页面
    current.pages.forEach((page, i) => {
        if (i >= origLen) {
            // 新增的页面
            changes.newPages.push(i);
            changes.hasChanges = true;
        } else {
            const origPage = original.pages[i];
            const origImgHash = origImgHashes[i] || '';
            const currImgHash = currImgHashes[i] || '';

            // 对比页面内容和图片
            const pageContentSame = (
                origPage.name === page.name &&
                origPage.layout === page.layout &&
                origPage.features === page.features &&
                origPage.interaction === page.interaction &&
                origPage.similarity === page.similarity
            );
            const imagesSame = (origImgHash === currImgHash);

            if (pageContentSame && imagesSame) {
                changes.pagesUnchanged.push(i);
            } else {
                changes.pagesChanged.push(i);
                changes.hasChanges = true;
            }
        }
    });

    // 检查删除的页面
    for (let i = currLen; i < origLen; i++) {
        changes.deletedPages.push(i);
        changes.hasChanges = true;
    }

    return changes;
}

async function generateWithAI() {
    // 验证是否有任何输入
    const hasInput = hasAnyInput();
    console.log('[验证] hasAnyInput 返回:', hasInput);

    if (!hasInput) {
        console.log('[验证] 没有输入，显示提示');
        alert('请先输入内容'); // 临时使用alert确保能看到
        showToast('请先输入内容', 'error');
        return;
    }

    // 收集当前表单数据
    const formData = collectFormData();
    const currentImageHashes = computeAllImageHashes();

    // 检测变更（如果有来源项目）
    let changes = null;
    let useIncremental = false;

    console.log('[增量更新] 检测状态:', {
        hasSourceProjectId: !!sourceProjectId,
        hasOriginalFormData: !!originalFormData,
        sourceProjectId,
        currentFormData: formData,
        originalFormData,
        currentImageHashes,
        originalImageHashes
    });

    if (sourceProjectId && originalFormData) {
        changes = detectChanges(originalFormData, formData, originalImageHashes, currentImageHashes);
        console.log('[增量更新] 变更检测结果:', changes);

        if (!changes.hasChanges) {
            // 无变化，直接复制项目
            showToast('内容未变化，将复制原项目', 'info');
            useIncremental = true;
        } else if (changes.pagesUnchanged.length > 0) {
            // 部分页面未变化，使用增量更新
            console.log(`[增量更新] ${changes.pagesUnchanged.length}个页面未变化，将复用`);
            useIncremental = true;
        }
    } else {
        console.log('[增量更新] 非增量模式：sourceProjectId或originalFormData为空');
    }

    // 生成prompt
    const prompt = generatePrompt();
    console.log('=== Prompt ===');
    console.log(prompt);

    // 收集所有图片
    const allImages = [];
    pages.forEach(id => {
        pageFiles[id].forEach(f => allImages.push(f.base64));
    });

    // 项目名称
    const projectName = pages.map(id => $(`pageName_${id}`).value).filter(Boolean).join(' + ') || '未命名项目';

    // 所有项目统一走画布模式
    return startCanvasGeneration(prompt, formData, allImages, projectName, {
        useIncremental, changes, sourceProjectId
    });
}

// ==================== Canvas Agent 模式 ====================

async function startCanvasGeneration(prompt, formData, allImages, projectName, opts) {
    const { useIncremental, changes, sourceProjectId: optsSourceProjectId } = opts || {};
    try {
        const requestData = {
            prompt: prompt,
            images: allImages,
            projectName: projectName,
            formData: formData
        };

        // 如果使用增量更新，添加额外信息
        if (useIncremental && changes) {
            requestData.incremental = true;
            requestData.sourceProjectId = optsSourceProjectId;
            requestData.changes = changes;
        }

        // 如果上传了模板 ZIP，添加模板数据
        if (templateZip) {
            requestData.templateZip = templateZip;
        }

        showToast('🚀 画布模式启动...', 'info');

        const response = await fetch('/generate-async', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(requestData)
        });

        const result = await response.json();

        if (result.error) {
            showToast('生成失败: ' + result.error, 'error');
            return;
        }

        if (result.success && result.project) {
            allProjects.unshift(result.project);
            renderProjectList();

            // 启动 Canvas Studio 而非普通流式监听
            canvasStudio.open(result.project.id, result.project.name, formData);

            sourceProjectId = null;
            originalFormData = null;
            originalImageHashes = {};
        }

    } catch (error) {
        showToast('请求失败: ' + error.message, 'error');
        console.error(error);
    }
}

// ==================== Spec 确认模式 ====================

async function showSpecConfirmationChoice() {
    return new Promise((resolve) => {
        // 引导式流程：默认先进入页面规划，可跳过
        const overlay = document.createElement('div');
        overlay.className = 'fixed inset-0 bg-black/50 flex items-center justify-center z-50';
        overlay.innerHTML = `
            <div class="bg-white rounded-2xl shadow-2xl p-8 max-w-md w-full mx-4">
                <div class="text-center mb-6">
                    <div class="w-14 h-14 rounded-full bg-indigo-50 flex items-center justify-center mx-auto mb-4">
                        <i class="fas fa-map text-indigo-500 text-xl"></i>
                    </div>
                    <h3 class="text-lg font-semibold text-gray-800 mb-2">你的产品有多个页面</h3>
                    <p class="text-sm text-gray-500">先看看页面规划，确认后再逐页制作原型</p>
                </div>
                <div class="space-y-3">
                    <button id="specConfirmBtn" class="w-full px-4 py-3 bg-indigo-600 text-white rounded-xl text-sm font-medium hover:bg-indigo-700 transition-colors shadow-sm">
                        <i class="fas fa-map mr-2"></i>先看页面规划
                    </button>
                    <button id="canvasAgentBtn" class="w-full px-4 py-3 bg-violet-600 text-white rounded-xl text-sm font-medium hover:bg-violet-700 transition-colors shadow-sm">
                        <i class="fas fa-layer-group mr-2"></i>画布模式（逐页制作）
                    </button>
                    <button id="directGenBtn" class="w-full px-4 py-3 border border-gray-200 text-gray-600 rounded-xl text-sm hover:bg-gray-50 transition-colors">
                        跳过规划，直接制作全部页面
                    </button>
                </div>
            </div>
        `;
        document.body.appendChild(overlay);

        overlay.querySelector('#specConfirmBtn').onclick = () => {
            document.body.removeChild(overlay);
            resolve('spec');
        };
        overlay.querySelector('#canvasAgentBtn').onclick = () => {
            document.body.removeChild(overlay);
            resolve('canvas');
        };
        overlay.querySelector('#directGenBtn').onclick = () => {
            document.body.removeChild(overlay);
            resolve('direct');
        };
    });
}

// SpecStudio 桥接函数（供 HTML onclick 调用）
function closeSpecStudio() {
    if (window.specStudio) {
        window.specStudio.close();
    }
}

// SpecStudio 集成版规格生成
async function _generateSpecWithStudio(prompt, formData, allImages, projectName, adjustmentNote, previousSpec) {
    console.log('[Spec] 使用 SpecStudio 生成规格, projectName:', projectName);

    try {
        const requestData = {
            prompt: prompt,
            formData: formData,
            images: allImages,
            projectName: projectName,
            templateZip: templateZip || null,
            adjustmentNote: adjustmentNote || '',
            previousSpec: previousSpec || null
        };

        // 1. 打开 SpecStudio 面板
        if (!window.specStudio) {
            window.specStudio = new SpecStudio();
        }
        window.specStudio.open(prompt, formData, allImages, projectName);

        // 2. 发起请求
        const response = await fetch('/generate-spec', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(requestData)
        });

        if (!response.ok) {
            throw new Error('HTTP ' + response.status + ': ' + response.statusText);
        }

        const result = await response.json();
        if (result.error || !result.success) {
            window.specStudio.close();
            showToast('规划失败: ' + (result.error || '未知错误'), 'error');
            return;
        }

        // 3. 通过 SpecStudio 连接 SSE
        window.specStudio.connectSSE(result.projectId);

    } catch (error) {
        console.error('[Spec] 请求失败:', error);
        if (window.specStudio) window.specStudio.close();
        showToast('规划失败: ' + error.message, 'error');
    }
}

async function regenerateSpec(prompt, formData, allImages, projectName, previousSpec, adjustmentNote) {
    showToast('正在根据你的反馈重新规划...', 'info');
    await generateSpecForConfirmation(prompt, formData, allImages, projectName, adjustmentNote, previousSpec);
}

async function generateSpecForConfirmation(prompt, formData, allImages, projectName, adjustmentNote, previousSpec) {
    console.log('[Spec] 开始生成规格, projectName:', projectName);

    // 优先使用 SpecStudio（需 D3.js），否则 fallback 到旧弹窗
    if (typeof SpecStudio !== 'undefined' && typeof d3 !== 'undefined') {
        return _generateSpecWithStudio(prompt, formData, allImages, projectName, adjustmentNote, previousSpec);
    }

    try {
        const requestData = {
            prompt: prompt,
            formData: formData,
            images: allImages,
            projectName: projectName,
            templateZip: templateZip || null,
            adjustmentNote: adjustmentNote || '',
            previousSpec: previousSpec || null
        };

        // 1. 立即弹出对话框（加载状态）
        const overlay = createSpecDialog(projectName);

        // 2. 发起请求（后端立即返回 projectId）
        const response = await fetch('/generate-spec', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(requestData)
        });

        if (!response.ok) {
            throw new Error('HTTP ' + response.status + ': ' + response.statusText);
        }

        const result = await response.json();
        if (result.error || !result.success) {
            closeSpecDialog();
            showToast('规划失败: ' + (result.error || '未知错误'), 'error');
            return;
        }

        const projectId = result.projectId;

        // 3. 连接 SSE 流，实时显示 AI 输出
        connectSpecSSE(overlay, projectId, prompt, formData, allImages, projectName);

    } catch (error) {
        console.error('[Spec] 请求失败:', error);
        closeSpecDialog();
        showToast('规划失败: ' + error.message, 'error');
    }
}

function createSpecDialog(projectName) {
    const overlay = document.createElement('div');
    overlay.id = 'specConfirmationModal';
    overlay.className = 'fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50';
    overlay.innerHTML = `
        <div class="bg-white rounded-xl shadow-2xl max-w-2xl w-full mx-4 max-h-[85vh] flex flex-col">
            <div class="px-6 py-4 border-b flex items-center justify-between">
                <div>
                    <h3 class="text-lg font-semibold text-gray-800">页面规划</h3>
                    <p id="specStatusText" class="text-xs text-gray-500 mt-1">正在理解你的产品需求...</p>
                </div>
                <button id="specModalClose" class="text-gray-400 hover:text-gray-600">
                    <i class="fas fa-times text-lg"></i>
                </button>
            </div>
            <div id="specStreamArea" class="px-6 py-4 overflow-y-auto flex-1">
                <div id="specStreamContent" class="text-sm text-gray-700 whitespace-pre-wrap font-mono leading-relaxed"
                     style="max-height: 50vh; overflow-y: auto;">
                </div>
                <div id="specStreamCursor" class="inline-block w-2 h-4 bg-indigo-500 animate-pulse ml-0.5"></div>
            </div>
            <div id="specConfirmArea" class="px-6 py-4 border-t bg-gray-50 rounded-b-xl hidden">
                <!-- 确认按钮区域，spec 完成后显示 -->
            </div>
        </div>
    `;
    document.body.appendChild(overlay);

    overlay.querySelector('#specModalClose').onclick = () => closeSpecDialog();

    return overlay;
}

function closeSpecDialog() {
    const modal = document.getElementById('specConfirmationModal');
    if (modal) document.body.removeChild(modal);
}

function connectSpecSSE(overlay, projectId, prompt, formData, allImages, projectName) {
    const streamArea = overlay.querySelector('#specStreamArea');
    const streamCursor = overlay.querySelector('#specStreamCursor');
    const statusText = overlay.querySelector('#specStatusText');

    let thinkingText = '';
    let outputText = '';
    let specResult = null;

    // 创建思考区域和输出区域
    let thinkBlock = null;
    let outputBlock = null;

    function ensureLayout() {
        if (thinkBlock) return;
        // 移除原始的 specStreamContent
        const old = overlay.querySelector('#specStreamContent');
        if (old) old.remove();

        thinkBlock = document.createElement('div');
        thinkBlock.id = 'specThinkingBlock';
        thinkBlock.style.cssText = 'display:none; margin-bottom:8px;';
        thinkBlock.innerHTML =
            '<div style="display:flex;align-items:center;gap:6px;padding:4px 0;cursor:pointer;font-size:12px;color:#9ca3af;" onclick="this.nextElementSibling.style.display=this.nextElementSibling.style.display===\'none\'?\'block\':\'none\'">' +
            '  <span style="width:16px;height:16px;border-radius:50%;background:#f3e8ff;display:inline-flex;align-items:center;justify-content:center;font-size:10px;color:#8b5cf6;font-weight:700;">?</span>' +
            '  <span>AI 思考过程</span>' +
            '  <span id="specThinkMeta" style="font-size:11px;color:#d1d5db;"></span>' +
            '</div>' +
            '<pre id="specThinkContent" style="margin:4px 0 8px 0;padding:8px 12px;background:#faf5ff;border-radius:6px;font-size:11px;color:#7c3aed;white-space:pre-wrap;word-break:break-all;max-height:200px;overflow-y:auto;font-family:Menlo,Consolas,monospace;line-height:1.5;"></pre>';

        outputBlock = document.createElement('div');
        outputBlock.id = 'specOutputContent';
        outputBlock.style.cssText = 'font-size:13px;color:#374151;white-space:pre-wrap;font-family:Menlo,Consolas,\'Courier New\',monospace;line-height:1.6;';

        if (streamArea) {
            streamArea.appendChild(thinkBlock);
            streamArea.appendChild(outputBlock);
        }
    }

    function updateDisplay() {
        ensureLayout();

        // 更新思考内容
        if (thinkingText) {
            thinkBlock.style.display = 'block';
            const thinkContent = document.getElementById('specThinkContent');
            const thinkMeta = document.getElementById('specThinkMeta');
            if (thinkContent) {
                const display = thinkingText.length > 800
                    ? thinkingText.slice(0, 400) + '\n...\n' + thinkingText.slice(-400)
                    : thinkingText;
                thinkContent.textContent = display;
            }
            if (thinkMeta) {
                thinkMeta.textContent = thinkingText.length.toLocaleString() + ' 字符';
            }
        }

        // 更新输出内容
        if (outputBlock) {
            outputBlock.textContent = outputText;
            outputBlock.scrollTop = outputBlock.scrollHeight;
        }

        // 自动滚动
        if (streamArea) {
            streamArea.scrollTop = streamArea.scrollHeight;
        }
    }

    const evtSource = new EventSource(
        `/api/generation-stream?id=${encodeURIComponent(projectId)}`
    );

    evtSource.onmessage = function(event) {
        try {
            const data = JSON.parse(event.data);

            // 原始文本流（AI 输出）
            if (data.content) {
                if (data.content.startsWith('[think]')) {
                    // 思考内容
                    thinkingText += data.content.slice(7);
                    if (statusText) {
                        statusText.textContent = `AI 思考中... (${thinkingText.length.toLocaleString()} 字符)`;
                    }
                } else {
                    // 实际输出
                    outputText += data.content;
                    if (statusText) {
                        statusText.textContent = 'AI 正在规划你的产品...';
                    }
                }
                updateDisplay();
            }

            // 结构化事件
            if (data.type && data.data) {
                handleSpecSSEEvent(data.type, data.data);
            }
        } catch (e) {
            console.error('[Spec SSE] 解析错误:', e);
        }
    };

    // 监听 status 事件（completed / failed）
    evtSource.addEventListener('status', function(event) {
        const data = JSON.parse(event.data);
        if (data.status === 'completed' || data.status === 'failed') {
            evtSource.close();
        }
    });

    function handleSpecSSEEvent(type, d) {
        switch (type) {
            case 'phase':
                if (d.status === 'running') {
                    if (statusText) statusText.textContent = 'AI 正在理解你的产品需求...';
                }
                break;

            case 'spec_complete':
                // spec 生成完成 — 切换到确认界面
                specResult = d;
                if (streamCursor) streamCursor.style.display = 'none';
                if (statusText) statusText.textContent = '页面规划完成，请确认';

                // 将原始流式内容折叠，显示结构化 spec
                renderSpecConfirmation(overlay, d, prompt, formData, allImages, projectName);
                break;

            case 'spec_error':
                evtSource.close();
                closeSpecDialog();
                showToast('规划失败: ' + (d.error || '未知错误'), 'error');
                break;
        }
    }
}

function _findPageNameById(spec, pageId) {
    if (!spec || !spec.pages) return pageId;
    var page = spec.pages.find(function(p) { return p.id === pageId; });
    return page ? (page.name || page.id) : pageId;
}

function renderSpecConfirmation(overlay, specResult, prompt, formData, allImages, projectName) {
    const streamArea = overlay.querySelector('#specStreamArea');
    const confirmArea = overlay.querySelector('#specConfirmArea');

    // 折叠流式内容
    if (streamArea) {
        streamArea.style.maxHeight = '120px';
        streamArea.style.overflow = 'hidden';
        streamArea.style.borderBottom = '1px solid #e5e7eb';
        streamArea.style.cursor = 'pointer';
        streamArea.title = '点击展开原始输出';
        streamArea.onclick = () => {
            streamArea.style.maxHeight = streamArea.style.maxHeight === '120px' ? '50vh' : '120px';
            streamArea.style.overflow = 'auto';
        };
    }

    // 提取 spec 数据
    const spec = specResult.spec;
    const specPages = (spec && spec.pages) || [];
    const navLinks = (spec && spec.navigation && spec.navigation.links) || [];
    const dataModels = (spec && spec.shared_data_models) || [];
    const components = (spec && spec.shared_components) || [];

    // 构建入口页面集合（用于标记"首页"）
    const entryPageIds = new Set();
    if (spec && spec.navigation && spec.navigation.pages) {
        spec.navigation.pages.forEach(function(p) {
            if (p.is_entry) entryPageIds.add(p.id);
        });
    }
    if (spec && spec.navigation && spec.navigation.default_page) {
        entryPageIds.add(spec.navigation.default_page);
    }

    // ---- 引导清单 ----
    const guidanceHtml = `
        <div class="p-3 bg-blue-50 border border-blue-200 rounded-lg">
            <div class="flex items-center gap-2 mb-2">
                <i class="fas fa-clipboard-check text-blue-500"></i>
                <span class="text-sm font-medium text-blue-800">检查 AI 是否理解了你的产品</span>
            </div>
            <div class="grid grid-cols-1 gap-1.5 text-xs text-blue-700">
                <div class="flex items-center gap-1.5">
                    <i class="far fa-check-square text-blue-400"></i>
                    <span>页面是否齐全？</span>
                </div>
                <div class="flex items-center gap-1.5">
                    <i class="far fa-check-square text-blue-400"></i>
                    <span>用户导航路径是否正确？</span>
                </div>
            </div>
        </div>`;

    // ---- 页面跳转关系图 ----
    let navigationHtml = '';
    if (navLinks.length > 0) {
        let linksHtml = '';
        navLinks.forEach(function(link) {
            const fromName = _findPageNameById(spec, link.from || '');
            const toName = _findPageNameById(spec, link.to || '');
            const trigger = link.trigger || '点击跳转';
            linksHtml += `
                <div class="flex items-center gap-2 text-xs bg-gray-50 rounded-lg px-3 py-2 flex-wrap">
                    <span class="px-2 py-1 bg-white rounded border border-gray-200 font-medium text-gray-700">${fromName}</span>
                    <div class="flex items-center gap-1 text-indigo-500 flex-shrink-0">
                        <i class="fas fa-arrow-right text-[10px]"></i>
                        <span class="text-[11px] text-indigo-600 italic">${trigger}</span>
                        <i class="fas fa-arrow-right text-[10px]"></i>
                    </div>
                    <span class="px-2 py-1 bg-white rounded border border-gray-200 font-medium text-gray-700">${toName}</span>
                </div>`;
        });
        navigationHtml = `
            <div>
                <div class="flex items-center gap-2 mb-2">
                    <i class="fas fa-route text-indigo-500"></i>
                    <span class="text-sm font-semibold text-gray-700">用户导航路径</span>
                </div>
                <div class="space-y-2">${linksHtml}</div>
            </div>`;
    } else {
        navigationHtml = `
            <div>
                <div class="flex items-center gap-2 mb-2">
                    <i class="fas fa-route text-indigo-500"></i>
                    <span class="text-sm font-semibold text-gray-700">用户导航路径</span>
                </div>
                <div class="text-xs text-gray-400 italic">暂无页面跳转</div>
            </div>`;
    }

    // ---- 页面卡片 ----
    let pagesHtml = '';
    if (specPages.length > 0) {
        let cardsHtml = '';
        specPages.forEach(function(page, i) {
            const pageId = page.id || '';
            const pageName = page.name || ('页面' + (i + 1));
            const isEntry = entryPageIds.has(pageId);
            const dataSources = page.data_sources || [];
            const compsNeeded = page.components_needed || [];
            const navsTo = (page.cross_references && page.cross_references.navigates_to) || [];

            let detailLines = '';
            if (dataSources.length > 0) {
                detailLines += `
                    <div class="flex items-center gap-1.5 mt-1.5 flex-wrap">
                        <i class="fas fa-database text-[10px] text-amber-500"></i>
                        <span class="text-[11px] text-gray-600">页面数据: ${dataSources.join(', ')}</span>
                    </div>`;
            }
            if (compsNeeded.length > 0) {
                detailLines += `
                    <div class="flex items-center gap-1.5 mt-1 flex-wrap">
                        <i class="fas fa-puzzle-piece text-[10px] text-purple-500"></i>
                        <span class="text-[11px] text-gray-600">功能: ${compsNeeded.join(', ')}</span>
                    </div>`;
            }
            if (navsTo.length > 0) {
                const navNames = navsTo.map(function(id) { return _findPageNameById(spec, id); });
                detailLines += `
                    <div class="flex items-center gap-1.5 mt-1 flex-wrap">
                        <i class="fas fa-arrow-right text-[10px] text-indigo-400"></i>
                        <span class="text-[11px] text-gray-600">可跳转: ${navNames.join(', ')}</span>
                    </div>`;
            }

            cardsHtml += `
                <div class="flex items-start gap-3 p-3 bg-gray-50 rounded-lg border-l-[3px] border-indigo-400">
                    <span class="flex-shrink-0 w-6 h-6 rounded-full bg-indigo-100 text-indigo-700 flex items-center justify-center text-xs font-bold">${i + 1}</span>
                    <div class="flex-1 min-w-0">
                        <div class="flex items-center gap-2 flex-wrap">
                            <span class="font-medium text-gray-800 text-sm">${pageName}</span>
                            ${isEntry ? '<span class="px-1.5 py-0.5 bg-green-100 text-green-700 rounded text-[10px] font-medium"><i class="fas fa-flag text-[8px] mr-0.5"></i>首页</span>' : ''}
                        </div>
                        ${detailLines}
                    </div>
                </div>`;
        });
        pagesHtml = `
            <div>
                <div class="flex items-center gap-2 mb-2">
                    <i class="fas fa-file-alt text-indigo-500"></i>
                    <span class="text-sm font-semibold text-gray-700">页面列表</span>
                    <span class="text-xs text-gray-400">（共 ${specPages.length} 个）</span>
                </div>
                <div class="space-y-2">${cardsHtml}</div>
            </div>`;
    }

    // ---- 组装 spec 内容 ----
    const hasSpec = specPages.length > 0 || navLinks.length > 0;
    const specContentHtml = hasSpec
        ? guidanceHtml + navigationHtml + pagesHtml
        : '<div class="text-xs text-gray-400 text-center py-4">（未生成页面规划，将直接制作原型）</div>';

    // ---- 调整文本框 ----
    const adjustmentHtml = `
        <div class="mt-3 mb-1">
            <div id="adjustmentToggle" class="flex items-center gap-2 cursor-pointer select-none py-1.5" onclick="
                var ta = document.getElementById('adjustmentInputArea');
                var arrow = document.getElementById('adjustmentArrow');
                if (ta.style.display === 'none') {
                    ta.style.display = 'block';
                    arrow.style.transform = 'rotate(90deg)';
                } else {
                    ta.style.display = 'none';
                    arrow.style.transform = 'rotate(0deg)';
                }
            ">
                <i id="adjustmentArrow" class="fas fa-chevron-right text-[10px] text-gray-400 transition-transform duration-200"></i>
                <i class="fas fa-comment-dots text-gray-400 text-sm"></i>
                <span class="text-xs text-gray-500">想调整页面规划？点击补充说明</span>
            </div>
            <div id="adjustmentInputArea" style="display:none">
                <textarea id="adjustmentInput"
                    class="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm text-gray-700 placeholder-gray-400 resize-none focus:outline-none focus:border-indigo-400 focus:ring-1 focus:ring-indigo-200"
                    rows="3"
                    placeholder="比如：增加一个用户权限管理页&#10;或者：数据列表页不需要批量删除&#10;或者：大屏页面希望用深色主题"></textarea>
            </div>
        </div>`;

    // ---- 显示确认区域 ----
    if (confirmArea) {
        confirmArea.classList.remove('hidden');
        const estimatedPages = specResult.estimatedPages || specPages.length || '?';
        confirmArea.innerHTML = `
            <div class="max-h-[45vh] overflow-y-auto pr-1 space-y-4 mb-3">
                ${specContentHtml}
            </div>
            ${adjustmentHtml}
            <div class="flex items-center justify-between pt-2 border-t border-gray-100">
                <div>
                    <div class="text-xs text-gray-500">
                        共 <span class="font-bold text-gray-700">${estimatedPages}</span> 个页面
                    </div>
                    <div class="text-[11px] text-gray-400 mt-0.5">确认规划后，将逐页制作原型</div>
                </div>
                <div class="flex gap-2">
                    <button id="specCancelBtn" class="px-4 py-1.5 border border-gray-300 rounded-lg text-sm text-gray-600 hover:bg-gray-100 transition-colors">
                        取消
                    </button>
                    <button id="specRegenerateBtn" class="px-4 py-1.5 border border-indigo-300 text-indigo-600 rounded-lg text-sm hover:bg-indigo-50 transition-colors">
                        <i class="fas fa-sync-alt mr-1 text-xs"></i>换个思路
                    </button>
                    <button id="specConfirmBtn" class="px-4 py-1.5 bg-indigo-600 text-white rounded-lg text-sm hover:bg-indigo-700 transition-colors">
                        确认规划，开始制作
                    </button>
                </div>
            </div>
        `;

        confirmArea.querySelector('#specCancelBtn').onclick = () => {
            closeSpecDialog();
            showToast('已取消生成', 'info');
        };

        confirmArea.querySelector('#specRegenerateBtn').onclick = () => {
            const adjustmentNote = (overlay.querySelector('#adjustmentInput') || {}).value || '';
            if (!adjustmentNote.trim()) {
                showToast('请先输入你想调整的内容', 'info');
                return;
            }
            closeSpecDialog();
            regenerateSpec(prompt, formData, allImages, projectName, specResult.spec, adjustmentNote.trim());
        };

        confirmArea.querySelector('#specConfirmBtn').onclick = () => {
            const adjustmentNote = (overlay.querySelector('#adjustmentInput') || {}).value || '';
            closeSpecDialog();
            startIncrementalGeneration(
                prompt, formData, allImages, projectName,
                specResult.spec, specResult.projectId,
                adjustmentNote.trim()
            );
        };
    }
}

async function startIncrementalGeneration(prompt, formData, allImages, projectName, confirmedSpec, projectId, adjustmentNote) {
    showToast('开始逐页制作...', 'info');

    try {
        const requestData = {
            prompt: prompt,
            formData: formData,
            images: allImages,
            projectName: projectName,
            templateZip: templateZip || null,
            confirmedSpec: confirmedSpec,
            projectId: projectId,
            adjustmentNote: adjustmentNote || ''
        };

        const response = await fetch('/generate-async', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(requestData)
        });

        const result = await response.json();

        if (result.error) {
            showToast('生成失败: ' + result.error, 'error');
            return;
        }

        if (result.success && result.project) {
            allProjects.unshift(result.project);
            renderProjectList();
            showToast('已开始制作 "' + result.project.name + '"');
            streamGenerationStatus(result.project.id);

            sourceProjectId = null;
            originalFormData = null;
            originalImageHashes = {};
        }

        window._pendingSpecData = null;

    } catch (error) {
        showToast('请求失败: ' + error.message, 'error');
        console.error(error);
    }
}

async function startDirectGeneration(prompt, formData, allImages, projectName) {
    try {
        const requestData = {
            prompt: prompt,
            formData: formData,
            images: allImages,
            projectName: projectName,
            templateZip: templateZip || null
        };

        const response = await fetch('/generate-async', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(requestData)
        });

        const result = await response.json();

        if (result.error) {
            showToast('生成失败: ' + result.error, 'error');
            return;
        }

        if (result.success && result.project) {
            allProjects.unshift(result.project);
            renderProjectList();
            showToast('已开始生成 "' + result.project.name + '"');
            streamGenerationStatus(result.project.id);
        }
    } catch (error) {
        showToast('请求失败: ' + error.message, 'error');
        console.error(error);
    }
}

// ==================== 异步状态轮询 ====================
function pollGenerationStatus(projectId) {
    const POLL_INTERVAL = 3000; // 每3秒轮询一次
    const MAX_POLLS = 120; // 最多轮询120次（6分钟超时）
    let pollCount = 0;

    const poll = async () => {
        pollCount++;
        console.log(`[轮询] 第${pollCount}次检查项目状态: ${projectId}`);

        try {
            const response = await fetch(`/api/generation-status?id=${encodeURIComponent(projectId)}`);
            const data = await response.json();

            console.log('[轮询] 状态:', data);

            // 更新列表中的项目状态
            const projectIndex = allProjects.findIndex(p => p.id === projectId);
            if (projectIndex !== -1) {
                if (data.status === 'completed') {
                    // 生成完成
                    allProjects[projectIndex].status = null; // 清除 generating 状态
                    renderProjectList();
                    showToast('✅ "' + allProjects[projectIndex].name + '" 生成完成！');

                    // 如果有 PRD 讨论生成的 PRD，回填到项目
                    if (prdDiscMarkdown) {
                        savePrdToProject(projectId, prdDiscMarkdown);
                        prdDiscMarkdown = '';  // 清空，只保存一次
                    }

                    // 自动打开预览
                    setTimeout(() => {
                        window.open(`/projects/${projectId}/index.html`, '_blank');
                    }, 500);
                    return; // 停止轮询

                } else if (data.status === 'failed') {
                    // 生成失败
                    allProjects[projectIndex].status = 'failed';
                    renderProjectList();
                    showToast('❌ "' + allProjects[projectIndex].name + '" 生成失败: ' + (data.error || '未知错误'), 'error');
                    return; // 停止轮询
                } else if (data.status === 'cancelled') {
                    // 任务已停止
                    allProjects[projectIndex].status = 'stopped';
                    renderProjectList();
                    showToast('"' + allProjects[projectIndex].name + '" 已停止生成');
                    return; // 停止轮询
                }
            }

            // 继续轮询
            if (pollCount < MAX_POLLS) {
                setTimeout(poll, POLL_INTERVAL);
            } else {
                showToast('⚠️ 生成超时，请刷新页面查看状态', 'error');
            }

        } catch (error) {
            console.error('[轮询错误]', error);
            // 网络错误时继续轮询
            if (pollCount < MAX_POLLS) {
                setTimeout(poll, POLL_INTERVAL);
            }
        }
    };

    // 首次轮询延迟3秒开始（给后端一点启动时间）
    setTimeout(poll, POLL_INTERVAL);
}

// ==================== 流式生成状态监听（SSE） ====================
function streamGenerationStatus(projectId) {
    // 优先使用新的 GenerationPanel
    if (typeof GenerationPanel !== 'undefined') {
        try {
            if (!window._gp) window._gp = new GenerationPanel();
            window._gp.connect(projectId);
            return;
        } catch (e) {
            console.warn('[GenerationPanel] 加载失败，降级到原有弹窗:', e);
        }
    }

    // 降级：使用原有 loadingModal
    showStreamingModal();

    try {
        const evtSource = new EventSource(
            `/api/generation-stream?id=${encodeURIComponent(projectId)}`
        );

        let accumulatedContent = '';
        let thinkingText = '';

        evtSource.onmessage = function(event) {
            try {
                const data = JSON.parse(event.data);
                if (data.content) {
                    if (data.content.startsWith('[think]')) {
                        // 思考内容，用不同样式展示
                        thinkingText += data.content.slice(7);
                        updateStreamingPreview(accumulatedContent, thinkingText);
                    } else {
                        accumulatedContent += data.content;
                        updateStreamingPreview(accumulatedContent, thinkingText);
                    }
                }
            } catch (e) {
                console.error('[SSE 解析错误]', e);
            }
        };

        // 监听终态事件
        evtSource.addEventListener('status', function(event) {
            const data = JSON.parse(event.data);

            if (data.status === 'completed') {
                evtSource.close();
                hideStreamingModal();

                const projectIndex = allProjects.findIndex(p => p.id === projectId);
                if (projectIndex !== -1) {
                    allProjects[projectIndex].status = null;
                    renderProjectList();
                    showToast('"' + allProjects[projectIndex].name + '" 生成完成！');

                    // 如果有 PRD 讨论生成的 PRD，回填到项目
                    if (prdDiscMarkdown) {
                        savePrdToProject(projectId, prdDiscMarkdown);
                        prdDiscMarkdown = '';
                    }

                    setTimeout(() => {
                        window.open(`/projects/${projectId}/index.html`, '_blank');
                    }, 500);
                }

            } else if (data.status === 'failed') {
                evtSource.close();
                hideStreamingModal();

                const projectIndex = allProjects.findIndex(p => p.id === projectId);
                if (projectIndex !== -1) {
                    allProjects[projectIndex].status = 'failed';
                    renderProjectList();
                    showToast('"' + allProjects[projectIndex].name + '" 生成失败: ' + (data.error || '未知错误'), 'error');
                }

            } else if (data.status === 'cancelled') {
                evtSource.close();
                hideStreamingModal();

                const projectIndex = allProjects.findIndex(p => p.id === projectId);
                if (projectIndex !== -1) {
                    allProjects[projectIndex].status = 'stopped';
                    renderProjectList();
                    showToast('"' + allProjects[projectIndex].name + '" 已停止生成');
                }
            }
        });

        evtSource.onerror = function() {
            evtSource.close();
            console.log('[SSE] 连接失败，降级为轮询模式');
            hideStreamingModal();
            // 降级到原有轮询
            pollGenerationStatus(projectId);
        };

    } catch (e) {
        console.error('[SSE] 不支持，降级为轮询:', e);
        hideStreamingModal();
        pollGenerationStatus(projectId);
    }
}

// ==================== 流式预览弹窗控制 ====================
function showStreamingModal() {
    const modal = document.getElementById('loadingModal');
    modal.classList.remove('hidden');
    modal.classList.add('flex');
    const preview = document.getElementById('streamPreview');
    if (preview) preview.textContent = '等待AI响应...';
    const progress = document.getElementById('streamProgress');
    if (progress) progress.textContent = '准备中';
}

function hideStreamingModal() {
    const modal = document.getElementById('loadingModal');
    modal.classList.add('hidden');
    modal.classList.remove('flex');
}

function updateStreamingPreview(content, thinking) {
    const preview = document.getElementById('streamPreview');
    const progress = document.getElementById('streamProgress');

    if (preview) {
        let display = '';
        // 思考内容（灰色斜体，只显示最后 500 字符）
        if (thinking) {
            const thinkShort = thinking.length > 500
                ? '...\n' + thinking.slice(-500)
                : thinking;
            display += `[思考中] ${thinkShort}\n\n`;
        }
        // 正式内容（只显示最后 3000 字符）
        if (content) {
            const contentShort = content.length > 3000
                ? '...\n' + content.slice(-3000)
                : content;
            display += contentShort;
        }
        if (!display) display = '等待AI响应...';
        preview.textContent = display;
        preview.scrollTop = preview.scrollHeight;
    }

    if (progress) {
        const phase = thinking && !content ? 'AI 思考中' : '已生成';
        const chars = content.length;
        const lines = content.split('\n').length;
        progress.textContent = thinking && !content
            ? `AI 思考中... (${thinking.length.toLocaleString()} 字符)`
            : `${phase} ${chars.toLocaleString()} 字符, ${lines} 行`;
    }
}

// ==================== 断点续传 ====================
async function resumeGeneration(projectId, name) {
    try {
        showToast('正在继续生成 "' + name + '"...', 'info');
        const response = await fetch('/api/resume-generation', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                projectId: projectId,
                instructions: '请继续完成上一轮未完成的HTML代码生成。从上次中断的地方继续，不要重复已生成的内容。'
            })
        });

        const result = await response.json();

        if (result.error) {
            showToast('继续失败: ' + result.error, 'error');
            return;
        }

        if (result.success) {
            const projectIndex = allProjects.findIndex(p => p.id === projectId);
            if (projectIndex !== -1) {
                allProjects[projectIndex].status = 'generating';
                renderProjectList();
            }
            streamGenerationStatus(projectId);
        }
    } catch (error) {
        showToast('继续失败: ' + error.message, 'error');
    }
}

// ==================== 复制Prompt功能 ====================
async function copyPromptToClipboard() {
    // 验证是否有任何输入
    const hasInput = hasAnyInput();
    console.log('[复制Prompt验证] hasAnyInput 返回:', hasInput);

    if (!hasInput) {
        showToast('请先输入内容', 'error');
        return;
    }

    try {
        const prompt = generatePrompt();
        await copyToClipboard(prompt);
        showToast('Prompt已复制到剪贴板');
    } catch (err) {
        console.error('复制失败:', err);
        showToast('复制失败: ' + err.message, 'error');
    }
}

function generateProjectIdFromName(name) {
    const now = new Date();
    const dateStr = now.toISOString().slice(0, 10).replace(/-/g, '');

    // 使用与服务端一致的12小时制格式: {H}-{MM}-{SS}{am/pm}
    let hour = now.getHours();
    const amPm = hour < 12 ? 'am' : 'pm';
    hour = hour <= 12 ? hour : hour - 12;
    if (hour === 0) hour = 12;

    const minutes = String(now.getMinutes()).padStart(2, '0');
    const seconds = String(now.getSeconds()).padStart(2, '0');
    const timeStr = `${hour}-${minutes}-${seconds}${amPm}`;

    // 处理不安全字符（与服务端一致）
    let safeName = name.replace(/[\\\/:*?"<>|]/g, '').replace(/ /g, '_');
    if (safeName.length > 30) safeName = safeName.slice(0, 30);

    return `${safeName}_${dateStr}_${timeStr}`;
}

// ==================== 工具函数 ====================
function showToast(msg, type = 'success') {
    const toast = $('toast');
    $('toastMessage').textContent = msg;
    $('toastIcon').className = type === 'error'
        ? 'fas fa-exclamation-circle text-red-400'
        : 'fas fa-check-circle text-green-400';

    toast.classList.remove('translate-y-20', 'opacity-0');
    setTimeout(() => toast.classList.add('translate-y-20', 'opacity-0'), 3000);
}

// ==================== 模型管理 ====================

async function loadModels() {
    try {
        const res = await fetch('/api/models?t=' + Date.now());
        const data = await res.json();
        modelsList = data.models || [];
        selectedModelId = data.selected_model_id || '';

        // 找到当前选中的模型
        currentModel = modelsList.find(m => m.id === selectedModelId) || modelsList[0] || null;

        // 更新顶栏显示
        $('currentModelName').textContent = currentModel ? currentModel.name : '未配置';

        // 渲染下拉列表
        renderModelDropdown();
    } catch (e) {
        console.error('加载模型列表失败:', e);
        $('currentModelName').textContent = '加载失败';
    }
}

function renderModelDropdown() {
    const container = $('modelDropdownList');
    if (!modelsList.length) {
        container.innerHTML = '<div class="px-4 py-3 text-sm text-gray-400 text-center">暂无模型</div>';
        return;
    }
    container.innerHTML = modelsList.map(m => `
        <div class="model-dropdown-item ${m.id === selectedModelId ? 'active' : ''}" onclick="selectModel('${m.id}')">
            <span class="check">${m.id === selectedModelId ? '<i class="fas fa-check"></i>' : ''}</span>
            <div class="flex-1 min-w-0">
                <div class="font-medium truncate flex items-center gap-1.5">
                    ${m.name}
                    ${m.multimodal ? '<span class="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium bg-purple-50 text-purple-600 leading-none">多模态</span>' : ''}
                </div>
                <div class="text-xs text-gray-400 truncate">${m.provider || ''} · ${m.model}</div>
            </div>
        </div>
    `).join('');
}

function toggleModelDropdown(e) {
    e.stopPropagation();
    const dropdown = $('modelDropdown');
    dropdown.classList.toggle('show');
}

// 点击外部关闭下拉
document.addEventListener('click', (e) => {
    const dropdown = $('modelDropdown');
    if (dropdown && !e.target.closest('#modelSelector')) {
        dropdown.classList.remove('show');
    }
});

async function selectModel(id) {
    try {
        const res = await fetch('/api/models/select', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id })
        });
        const data = await res.json();
        if (data.success) {
            selectedModelId = id;
            currentModel = modelsList.find(m => m.id === id);
            $('currentModelName').textContent = currentModel ? currentModel.name : id;
            renderModelDropdown();
            $('modelDropdown').classList.remove('show');
            // 如果模型管理弹窗打开中，刷新列表以更新"当前"标签
            if ($('modelManagerModal') && !$('modelManagerModal').classList.contains('hidden')) {
                renderModelManagerList();
            }
            showToast('已切换到: ' + (currentModel?.name || id));
        }
    } catch (e) {
        showToast('切换失败', 'error');
    }
}

function openModelManager() {
    $('modelDropdown').classList.remove('show');
    $('modelManagerModal').classList.remove('hidden');
    $('modelManagerModal').classList.add('flex');
    renderModelManagerList();
    resetModelForm();
}

function closeModelManager() {
    $('modelManagerModal').classList.add('hidden');
    $('modelManagerModal').classList.remove('flex');
}

function renderModelManagerList() {
    const container = $('modelManagerList');
    const editingId = $('editModelId').value;
    if (!modelsList.length) {
        container.innerHTML = '<div class="text-center py-6 text-gray-400 text-sm">暂无模型配置</div>';
        return;
    }
    container.innerHTML = modelsList.map(m => `
        <div class="flex items-center gap-3 p-3 rounded-lg border ${m.id === editingId ? 'border-indigo-300 bg-indigo-50/70 ring-1 ring-indigo-200' : m.id === selectedModelId ? 'border-indigo-200 bg-indigo-50/50' : 'border-gray-100 bg-white'} hover:border-indigo-200 transition cursor-pointer"
             onclick="editModel('${m.id}')">
            <div class="flex-1 min-w-0">
                <div class="flex items-center gap-2 flex-wrap">
                    <span class="font-medium text-sm text-gray-900 truncate">${m.name}</span>
                    ${m.id === selectedModelId ? '<span class="text-xs bg-indigo-100 text-indigo-600 px-1.5 py-0.5 rounded">当前</span>' : ''}
                    ${(m.api_format === 'claude') ? '<span class="text-xs bg-orange-50 text-orange-600 px-1.5 py-0.5 rounded">Claude</span>' : ''}
                    ${m.multimodal ? '<span class="text-xs bg-purple-50 text-purple-600 px-1.5 py-0.5 rounded">多模态</span>' : ''}
                    ${m.thinking_mode ? '<span class="text-xs bg-amber-50 text-amber-600 px-1.5 py-0.5 rounded">思考</span>' : ''}
                </div>
                <div class="text-xs text-gray-400 mt-0.5 truncate">${m.provider || '—'} · ${m.model}${m.max_tokens ? ' · ' + m.max_tokens + ' tokens' : ''}${m.timeout ? ' · ' + m.timeout + 's' : ''}</div>
            </div>
            <div class="flex items-center gap-1 flex-shrink-0" onclick="event.stopPropagation()">
                ${m.id !== selectedModelId ? `<button onclick="selectModel('${m.id}')" class="p-1.5 text-gray-400 hover:text-indigo-600 rounded hover:bg-indigo-50" title="选用"><i class="fas fa-check-circle"></i></button>` : ''}
                <button onclick="duplicateModel('${m.id}')" class="p-1.5 text-gray-400 hover:text-teal-600 rounded hover:bg-teal-50" title="复制"><i class="fas fa-copy"></i></button>
                <button onclick="editModel('${m.id}')" class="p-1.5 text-gray-400 hover:text-blue-600 rounded hover:bg-blue-50" title="编辑"><i class="fas fa-edit"></i></button>
                <button onclick="deleteModel('${m.id}')" class="p-1.5 text-gray-400 hover:text-red-500 rounded hover:bg-red-50" title="删除"><i class="fas fa-trash-alt"></i></button>
            </div>
        </div>
    `).join('');
}

function editModel(id) {
    const m = modelsList.find(x => x.id === id);
    if (!m) return;
    $('editModelId').value = m.id;
    $('modelFormName').value = m.name || '';
    $('modelFormApiFormat').value = m.api_format || 'openai';
    $('modelFormProvider').value = m.provider || '';
    $('modelFormModel').value = m.model || '';
    $('modelFormBaseUrl').value = m.base_url || '';
    $('modelFormApiKey').value = m.api_key || '';
    $('modelFormMultimodal').checked = !!m.multimodal;
    $('modelFormThinkingMode').checked = !!m.thinking_mode;
    $('modelFormMaxTokens').value = m.max_tokens || '';
    $('modelFormTimeout').value = m.timeout || '';
    $('modelFormMaxContextTokens').value = m.max_context_tokens || '';
    $('modelFormMaxOutputTokens').value = m.max_output_tokens || '';
    $('modelFormTitle').textContent = '编辑模型: ' + m.name;
    onApiFormatChange();
    // 刷新列表以高亮当前编辑项
    renderModelManagerList();
}

async function duplicateModel(id) {
    const m = modelsList.find(x => x.id === id);
    if (!m) return;

    const newId = m.name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/(^-|-$)/g, '') + '-' + Date.now().toString(36);
    const newModel = {
        id: newId,
        name: m.name + ' (副本)',
        provider: m.provider || '',
        api_format: m.api_format || 'openai',
        model: m.model || '',
        base_url: m.base_url || '',
        api_key: m.api_key || '',
        multimodal: !!m.multimodal,
        thinking_mode: !!m.thinking_mode,
        max_tokens: m.max_tokens || null,
        timeout: m.timeout || null,
        max_context_tokens: m.max_context_tokens || null,
        max_output_tokens: m.max_output_tokens || null
    };

    try {
        const res = await fetch('/api/models/save', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(newModel)
        });
        const data = await res.json();
        if (data.success) {
            showToast('模型已复制: ' + newModel.name);
            await loadModels();
            renderModelManagerList();
        } else {
            showToast('复制失败: ' + (data.error || ''), 'error');
        }
    } catch (e) {
        showToast('复制失败', 'error');
    }
}

async function saveModelForm() {
    const existingId = $('editModelId').value;
    const name = $('modelFormName').value.trim();
    const provider = $('modelFormProvider').value.trim();
    const apiFormat = $('modelFormApiFormat').value;
    const model = $('modelFormModel').value.trim();
    const baseUrl = $('modelFormBaseUrl').value.trim();
    const apiKey = $('modelFormApiKey').value.trim();

    if (!name || !model || !baseUrl || !apiKey) {
        showToast('请填写所有必填字段', 'error');
        return;
    }

    // 生成 ID：编辑时沿用，新增时自动生成
    const id = existingId || name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/(^-|-$)/g, '') + '-' + Date.now().toString(36);

    const multimodal = $('modelFormMultimodal').checked;
    const thinkingMode = $('modelFormThinkingMode').checked;
    const maxTokens = $('modelFormMaxTokens').value ? parseInt($('modelFormMaxTokens').value) : null;
    const timeout = $('modelFormTimeout').value ? parseInt($('modelFormTimeout').value) : null;
    const maxContextTokens = $('modelFormMaxContextTokens').value ? parseInt($('modelFormMaxContextTokens').value) : null;
    const maxOutputTokens = $('modelFormMaxOutputTokens').value ? parseInt($('modelFormMaxOutputTokens').value) : null;

    const modelData = { id, name, provider, api_format: apiFormat, model, base_url: baseUrl, api_key: apiKey, multimodal, thinking_mode: thinkingMode };
    if (maxTokens) modelData.max_tokens = maxTokens;
    if (timeout) modelData.timeout = timeout;
    if (maxContextTokens) modelData.max_context_tokens = maxContextTokens;
    if (maxOutputTokens) modelData.max_output_tokens = maxOutputTokens;

    try {
        const res = await fetch('/api/models/save', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(modelData)
        });
        const data = await res.json();
        if (data.success) {
            showToast(existingId ? '模型已更新' : '模型已添加');
            await loadModels();
            renderModelManagerList();
            resetModelForm();
        } else {
            showToast('保存失败: ' + (data.error || ''), 'error');
        }
    } catch (e) {
        showToast('保存失败', 'error');
    }
}

async function deleteModel(id) {
    const m = modelsList.find(x => x.id === id);
    if (!confirm(`确定要删除模型 "${m?.name || id}" 吗？`)) return;

    try {
        const res = await fetch('/api/models/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id })
        });
        const data = await res.json();
        if (data.success) {
            showToast('模型已删除');
            await loadModels();
            renderModelManagerList();
            // 如果删除的是当前编辑的，重置表单
            if ($('editModelId').value === id) {
                resetModelForm();
            }
        } else {
            showToast(data.error || '删除失败', 'error');
        }
    } catch (e) {
        showToast('删除失败', 'error');
    }
}

function resetModelForm() {
    $('editModelId').value = '';
    $('modelFormName').value = '';
    $('modelFormApiFormat').value = 'openai';
    $('modelFormProvider').value = '';
    $('modelFormModel').value = '';
    $('modelFormBaseUrl').value = '';
    $('modelFormApiKey').value = '';
    $('modelFormMultimodal').checked = false;
    $('modelFormThinkingMode').checked = false;
    $('modelFormMaxTokens').value = '';
    $('modelFormTimeout').value = '';
    $('modelFormMaxContextTokens').value = '';
    $('modelFormMaxOutputTokens').value = '';
    $('modelFormTitle').textContent = '添加新模型';
    onApiFormatChange();
    // 刷新列表取消高亮
    if ($('modelManagerModal') && !$('modelManagerModal').classList.contains('hidden')) {
        renderModelManagerList();
    }
}

function onApiFormatChange() {
    const format = $('modelFormApiFormat').value;
    const baseUrlInput = $('modelFormBaseUrl');
    const hint = $('modelFormBaseUrlHint');
    if (format === 'claude') {
        baseUrlInput.placeholder = '如: https://api.anthropic.com/v1';
        hint.classList.remove('hidden');
    } else {
        baseUrlInput.placeholder = '如: https://api.openai.com/v1';
        hint.classList.add('hidden');
    }
}

// ==================== 模型测试 ====================

async function testModel(testType) {
    const name = $('modelFormName').value.trim();
    const model = $('modelFormModel').value.trim();
    const baseUrl = $('modelFormBaseUrl').value.trim();
    const apiKey = $('modelFormApiKey').value.trim();
    const apiFormat = $('modelFormApiFormat').value;

    if (!model || !baseUrl || !apiKey) {
        showToast('请先填写模型标识、Base URL 和 API Key', 'error');
        return;
    }

    const spinner = $('testSpinner');
    const resultBox = $('testResultBox');
    const resultContent = $('testResultContent');
    const buttons = document.querySelectorAll('#modelManagerModal button[onclick^="testModel"]');

    // 显示 loading
    spinner.classList.remove('hidden');
    resultBox.classList.remove('hidden');
    buttons.forEach(b => b.disabled = true);

    const typeLabels = { text: '文本', multimodal: '多模态', tools: '工具调用' };
    resultContent.textContent = `[${typeLabels[testType]}] 正在测试...`;

    const modelConfig = {
        provider: $('modelFormProvider').value.trim(),
        api_format: apiFormat,
        model,
        base_url: baseUrl,
        api_key: apiKey,
        multimodal: $('modelFormMultimodal').checked,
        max_tokens: $('modelFormMaxTokens').value ? parseInt($('modelFormMaxTokens').value) : 256,
        timeout: $('modelFormTimeout').value ? parseInt($('modelFormTimeout').value) : 30
    };

    try {
        const res = await fetch('/api/models/test', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ test_type: testType, model_config: modelConfig })
        });
        const data = await res.json();

        if (!data.success) {
            resultContent.innerHTML = `<span class="text-red-600">✗ 请求失败</span>\n${data.error || '未知错误'}`;
            return;
        }

        const results = data.results;
        const formatLabel = results.format === 'claude' ? 'Claude API' : 'OpenAI 兼容';
        let output = `API 格式: ${formatLabel}\n`;
        output += '─'.repeat(32) + '\n\n';

        for (const [key, info] of Object.entries(results.tests)) {
            const label = typeLabels[key] || key;
            if (info.success) {
                output += `<span class="text-green-600 font-semibold">✓ ${label}</span>  (${info.detail.elapsed}s)`;
                if (info.detail.finish_reason) {
                    output += `  finish_reason: ${info.detail.finish_reason}`;
                }
                output += '\n';
                if (info.detail.content) {
                    // 转义 HTML，保留换行
                    const escaped = info.detail.content.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
                    output += `  回复: ${escaped}\n`;
                }
                if (key === 'tools' && info.detail.tool_calls) {
                    for (const tc of info.detail.tool_calls) {
                        output += `  工具调用: ${tc.name}(${JSON.stringify(tc.input)})\n`;
                    }
                }
            } else {
                output += `<span class="text-red-600 font-semibold">✗ ${label}</span>  (${info.detail.elapsed}s)\n`;
                output += `  错误: ${info.detail.message}\n`;
            }
            output += '\n';
        }

        resultContent.innerHTML = output;
    } catch (e) {
        resultContent.innerHTML = `<span class="text-red-600">✗ 网络错误</span>\n${e.message}`;
    } finally {
        spinner.classList.add('hidden');
        buttons.forEach(b => b.disabled = false);
    }
}


// ==================== 导出 & 分享弹窗 ====================

let currentExportProjectId = '';
let currentExportProjectName = '';
let currentExportMode = 'preview';

function openChatAdjust(id) {
    /**打开对话调整面板 */
    if (typeof GenerationPanel !== 'undefined') {
        if (!window._gp) window._gp = new GenerationPanel();
        window._gp.connectChat(id);
    } else {
        showToast('对话调整功能不可用', 'error');
    }
}

async function openExportModal(id, name) {
    currentExportProjectId = id;
    currentExportProjectName = name;
    currentExportMode = 'preview';

    // 重置 UI
    selectExportMode('preview');
    $('exportModalProjectName').textContent = name;
    $('githubPublishedInfo').classList.add('hidden');
    $('githubNotConfigured').classList.add('hidden');
    $('githubConfigured').classList.add('hidden');
    if ($('githubUnpublishBtn')) $('githubUnpublishBtn').classList.add('hidden');
    if ($('githubPublishBtnText')) $('githubPublishBtnText').textContent = '立即发布';

    // 显示弹窗
    $('exportModal').classList.remove('hidden');
    $('exportModal').classList.add('flex');

    // 加载 GitHub 配置状态
    await loadGithubStatus(id);
}

function closeExportModal() {
    $('exportModal').classList.add('hidden');
    $('exportModal').classList.remove('flex');
}

function selectExportMode(mode) {
    currentExportMode = mode;
    ['preview', 'dev', 'embedded', 'figma'].forEach(m => {
        const btn = $(`exportMode${m.charAt(0).toUpperCase() + m.slice(1)}`);
        if (btn) btn.classList.toggle('active', m === mode);
    });
}

async function doLocalExport() {
    const btn = $('localExportBtn');
    const originalText = btn.innerHTML;
    btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> 导出中...';
    btn.disabled = true;

    try {
        const resp = await fetch('/api/export', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ projectId: currentExportProjectId, mode: currentExportMode })
        });
        const data = await resp.json();
        if (data.success) {
            // 触发下载
            if (data.downloadUrl) {
                const link = document.createElement('a');
                link.href = data.downloadUrl;
                link.download = '';
                document.body.appendChild(link);
                link.click();
                document.body.removeChild(link);
            }
            showToast(`✅ 导出完成`);
            closeExportModal();
        } else {
            showToast('导出失败: ' + (data.error || '未知错误'), 'error');
        }
    } catch (e) {
        showToast('导出失败: ' + e.message, 'error');
    } finally {
        btn.innerHTML = originalText;
        btn.disabled = false;
    }
}

async function loadGithubStatus(projectId) {
    try {
        const resp = await fetch('/api/github/config');
        const data = await resp.json();

        if (!data.success || !data.hasToken || !data.username) {
            $('githubNotConfigured').classList.remove('hidden');
            return;
        }

        $('githubConfigured').classList.remove('hidden');
        $('githubRepoDisplay').textContent = `${data.username}/${data.repo}`;

        // 检查项目是否已发布（从 record.json 读取）
        try {
            const recResp = await fetch(`/projects/${projectId}/record.json?t=${Date.now()}`);
            if (recResp.ok) {
                const record = await recResp.json();
                if (record.github_url) {
                    $('githubPublishedUrl').value = record.github_url;
                    $('githubPublishedAt').textContent = record.github_published_at || '';
                    $('githubPublishedInfo').classList.remove('hidden');
                    $('githubPublishBtnText').textContent = '重新发布 / 更新';
                    if ($('githubUnpublishBtn')) $('githubUnpublishBtn').classList.remove('hidden');

                    // 如果有记录的模式，尝试恢复选中状态
                    if (record.github_publish_mode) {
                        const radio = document.querySelector(`input[name="githubPublishMode"][value="${record.github_publish_mode}"]`);
                        if (radio) radio.checked = true;
                    }
                }
            }
        } catch (_) { }

    } catch (e) {
        $('githubNotConfigured').classList.remove('hidden');
    }
}

async function doGitHubPublish() {
    const btn = $('githubPublishBtn');
    const btnText = $('githubPublishBtnText');
    const originalText = btnText ? btnText.textContent : '立即发布';
    btn.disabled = true;
    btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> <span>发布中，请稍候...</span>';

    let finalText = originalText;

    const modeRadio = document.querySelector('input[name="githubPublishMode"]:checked');
    const publishMode = modeRadio ? modeRadio.value : 'preview';

    try {
        const resp = await fetch('/api/github/publish', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ projectId: currentExportProjectId, mode: publishMode })
        });
        const data = await resp.json();

        if (data.success) {
            $('githubPublishedUrl').value = data.url;
            $('githubPublishedAt').textContent = '刚刚';
            $('githubPublishedInfo').classList.remove('hidden');
            finalText = '重新发布 / 更新';
            if ($('githubUnpublishBtn')) $('githubUnpublishBtn').classList.remove('hidden');
            showToast('🚀 发布成功！约 1-3 分钟后链接生效');
        } else {
            showToast('发布失败: ' + (data.error || '未知错误'), 'error');
        }
    } catch (e) {
        showToast('发布失败: ' + e.message, 'error');
    } finally {
        btn.disabled = false;
        btn.innerHTML = `<i class="fab fa-github"></i> <span id="githubPublishBtnText">${finalText}</span>`;
    }
}

function doGitHubUnpublish() {
    $('unpublishConfirmModal').classList.remove('hidden');
    $('unpublishConfirmModal').classList.add('flex');
}

function closeUnpublishConfirmModal() {
    $('unpublishConfirmModal').classList.add('hidden');
    $('unpublishConfirmModal').classList.remove('flex');
}

async function executeGitHubUnpublish() {
    const confirmBtn = $('confirmUnpublishBtn');
    const originalConfirmText = confirmBtn.innerHTML;
    confirmBtn.disabled = true;
    confirmBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> 取消中...';

    const btn = $('githubUnpublishBtn');
    const originalText = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> 取消中...';

    try {
        const resp = await fetch('/api/github/unpublish', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ projectId: currentExportProjectId })
        });
        const data = await resp.json();

        if (data.success) {
            showToast('✅ 已取消发布并删除 GitHub 上的文件');
            // 更新 UI 状态
            $('githubPublishedInfo').classList.add('hidden');
            $('githubPublishBtnText').textContent = '立即发布';
            btn.classList.add('hidden');
            closeUnpublishConfirmModal(); // 成功后关闭确认弹窗
        } else {
            showToast('取消发布失败: ' + (data.error || '未知错误'), 'error');
        }
    } catch (e) {
        showToast('请求失败: ' + e.message, 'error');
    } finally {
        btn.disabled = false;
        btn.innerHTML = originalText;
        confirmBtn.disabled = false;
        confirmBtn.innerHTML = originalConfirmText;
    }
}


function copyGithubUrl() {
    const url = $('githubPublishedUrl').value;
    if (!url) return;
    navigator.clipboard.writeText(url).then(() => {
        showToast('✅ 链接已复制到剪贴板');
    }).catch(() => {
        $('githubPublishedUrl').select();
        document.execCommand('copy');
        showToast('✅ 链接已复制');
    });
}

function openGithubUrl() {
    const url = $('githubPublishedUrl').value;
    if (url) window.open(url, '_blank');
}

// ==================== GitHub 配置引导 ====================

function openGithubSetup() {
    // 预加载已有配置
    fetch('/api/github/config').then(r => r.json()).then(data => {
        if (data.success) {
            $('setupUsername').value = data.username || '';
            $('setupRepo').value = data.repo || 'my-prototypes';
            $('setupToken').value = ''; // Token 不回显，保持空
            if (data.tokenMasked) {
                $('setupToken').placeholder = data.tokenMasked + ' （不修改则留空）';
            }
        }
    }).catch(() => { });

    $('githubTestResult').classList.add('hidden');
    $('githubSetupModal').classList.remove('hidden');
    $('githubSetupModal').classList.add('flex');
}

function closeGithubSetup() {
    $('githubSetupModal').classList.add('hidden');
    $('githubSetupModal').classList.remove('flex');
}

async function testGithubConnection() {
    const token = $('setupToken').value.trim();
    const btn = $('testConnBtn');
    btn.disabled = true;
    btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> 验证中...';

    try {
        const resp = await fetch('/api/github/test', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ token })
        });
        const data = await resp.json();
        const resultEl = $('githubTestResult');
        resultEl.classList.remove('hidden', 'bg-green-50', 'text-green-700', 'bg-red-50', 'text-red-700');

        if (data.success) {
            resultEl.classList.add('bg-green-50', 'text-green-700');
            resultEl.textContent = data.message;
            // 自动填充用户名
            if (data.username && !$('setupUsername').value) {
                $('setupUsername').value = data.username;
            }
        } else {
            resultEl.classList.add('bg-red-50', 'text-red-700');
            resultEl.textContent = data.message || data.error || '验证失败';
        }
    } catch (e) {
        const resultEl = $('githubTestResult');
        resultEl.classList.remove('hidden');
        resultEl.classList.add('bg-red-50', 'text-red-700');
        resultEl.textContent = '连接失败: ' + e.message;
    } finally {
        btn.disabled = false;
        btn.innerHTML = '<i class="fas fa-plug"></i> 测试连接';
    }
}

async function saveGithubConfig() {
    const token = $('setupToken').value.trim();
    const username = $('setupUsername').value.trim();
    const repo = ($('setupRepo').value.trim()) || 'my-prototypes';

    if (!username) {
        showToast('请填写 GitHub 用户名', 'error');
        return;
    }

    const btn = $('saveGithubBtn');
    btn.disabled = true;
    btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> 保存中...';

    try {
        const resp = await fetch('/api/github/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ token, username, repo })
        });
        const data = await resp.json();

        if (data.success) {
            showToast('✅ GitHub 配置已保存');
            closeGithubSetup();
            // 刷新导出弹窗的 GitHub 状态
            await loadGithubStatus(currentExportProjectId);
        } else {
            showToast('保存失败: ' + (data.error || '未知错误'), 'error');
        }
    } catch (e) {
        showToast('保存失败: ' + e.message, 'error');
    } finally {
        btn.disabled = false;
        btn.innerHTML = '<i class="fas fa-save"></i> 保存配置';
    }
}

// ==================== 需求文档导入 ====================

async function importRequirementsDoc() {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = '.docx,.md,.txt';

    input.onchange = async (e) => {
        const file = e.target.files[0];
        if (!file) return;

        if (file.size > 60 * 1024 * 1024) {
            showToast('文件过大，请上传小于 60MB 的文件', 'error');
            return;
        }

        console.log('[需求导入] 选择文件:', file.name, file.size, 'bytes');

        const btn = $('importDocBtn');
        const originalHtml = btn.innerHTML;
        btn.disabled = true;

        try {
            // 上传文件，获取 task_id
            const formData = new FormData();
            formData.append('file', file);

            btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> 上传中...';

            const uploadResp = await fetch('/api/requirements/import', {
                method: 'POST',
                body: formData
            });
            const uploadResult = await uploadResp.json();

            if (uploadResult.error) {
                showToast('上传失败: ' + uploadResult.error, 'error');
                return;
            }

            if (!uploadResult.success || !uploadResult.taskId) {
                showToast('上传失败: 服务器返回异常', 'error');
                return;
            }

            // 轮询等待处理完成
            const taskId = uploadResult.taskId;
            console.log('[需求导入] 任务已创建:', taskId);
            streamImportStatus(taskId, btn, originalHtml);

        } catch (error) {
            console.error('[需求导入] 错误:', error);
            showToast('导入失败: ' + error.message, 'error');
            btn.disabled = false;
            btn.innerHTML = originalHtml;
        }
    };

    input.click();
}

// ==================== 需求导入 SSE 流式监听 ====================
function streamImportStatus(taskId, btn, originalHtml) {
    try {
        const evtSource = new EventSource(
            `/api/requirements/stream?id=${encodeURIComponent(taskId)}`
        );

        let accumulated = '';
        let thinkingLen = 0;
        let resolved = false;

        evtSource.onmessage = function(event) {
            try {
                const data = JSON.parse(event.data);
                if (data.content) {
                    if (data.content.startsWith('[think]')) {
                        thinkingLen += data.content.length - 7;
                        btn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> AI 思考中 (${thinkingLen.toLocaleString()} 字符)...`;
                    } else {
                        accumulated += data.content;
                        const chars = accumulated.length;
                        btn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> AI 提取中 (${chars.toLocaleString()} 字符)...`;
                    }
                }
            } catch (e) {
                // ignore parse errors
            }
        };

        evtSource.addEventListener('status', function(event) {
            if (resolved) return;
            resolved = true;
            evtSource.close();

            const data = JSON.parse(event.data);

            if (data.status === 'completed' && data.data) {
                fillFormWithImportedData(data.data);
                const pageCount = data.data.pages ? data.data.pages.length : 0;
                showToast(`导入成功！已提取 ${pageCount} 个页面，请检查并调整`, 'success');
                btn.disabled = false;
                btn.innerHTML = originalHtml;
            } else if (data.status === 'failed') {
                showToast('导入失败: ' + (data.error || '未知错误'), 'error');
                btn.disabled = false;
                btn.innerHTML = originalHtml;
            }
        });

        evtSource.onerror = function() {
            evtSource.close();
            if (!resolved) {
                console.log('[需求SSE] 连接失败，降级为轮询');
                pollImportStatus(taskId, btn, originalHtml);
            }
        };

        // 初始按钮状态
        btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> 解析文档中...';

    } catch (e) {
        console.error('[需求SSE] 不支持，降级为轮询:', e);
        pollImportStatus(taskId, btn, originalHtml);
    }
}

function pollImportStatus(taskId, btn, originalHtml) {
    const POLL_INTERVAL = 2000;
    const MAX_POLLS = 180; // 6 分钟超时
    let pollCount = 0;
    let timedOut = false;

    const poll = async () => {
        if (timedOut) return;
        pollCount++;

        try {
            const resp = await fetch(`/api/requirements/import-status?id=${encodeURIComponent(taskId)}`);
            const data = await resp.json();

            // 任务已被服务端清理（无数据返回）
            if (!resp.ok && resp.status === 404) {
                showToast('任务已过期，请重新导入', 'error');
                btn.disabled = false;
                btn.innerHTML = originalHtml;
                return;
            }

            // 更新按钮进度
            const progress = data.progress || 0;
            if (progress < 30) {
                btn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> 解析文档 ${progress}%...`;
            } else if (progress < 80) {
                btn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> AI 提取中 ${progress}%...`;
            } else {
                btn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> 即将完成 ${progress}%...`;
            }

            if (data.status === 'completed' && data.data) {
                fillFormWithImportedData(data.data);
                const pageCount = data.data.pages ? data.data.pages.length : 0;
                const imgCount = data.metadata && data.metadata.imageCount ? data.metadata.imageCount : 0;
                const msg = imgCount > 0
                    ? `导入成功！已提取 ${pageCount} 个页面、${imgCount} 张参考图，请检查并调整`
                    : `导入成功！已提取 ${pageCount} 个页面，请检查并调整`;
                showToast(msg, 'success');
                console.log('[需求导入] 提取完成，数据:', data.data);
                btn.disabled = false;
                btn.innerHTML = originalHtml;
                return;

            } else if (data.status === 'failed') {
                showToast('导入失败: ' + (data.error || '未知错误'), 'error');
                btn.disabled = false;
                btn.innerHTML = originalHtml;
                return;
            }

            // 继续轮询
            if (pollCount < MAX_POLLS) {
                setTimeout(poll, POLL_INTERVAL);
            } else {
                // 超时：显示刷新按钮而非直接报错
                timedOut = true;
                btn.disabled = false;
                btn.innerHTML = `<i class="fas fa-sync-alt"></i> AI 仍在处理，点击刷新`;
                btn.onclick = () => {
                    btn.disabled = true;
                    btn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> 检查中...`;
                    // 单次请求检查状态
                    fetch(`/api/requirements/import-status?id=${encodeURIComponent(taskId)}`)
                        .then(r => r.json())
                        .then(data => {
                            if (data.status === 'completed' && data.data) {
                                fillFormWithImportedData(data.data);
                                const pageCount = data.data.pages ? data.data.pages.length : 0;
                                showToast(`导入成功！已提取 ${pageCount} 个页面，请检查并调整`, 'success');
                                btn.disabled = false;
                                btn.innerHTML = originalHtml;
                                btn.onclick = null;
                            } else if (data.status === 'failed') {
                                showToast('导入失败: ' + (data.error || '未知错误'), 'error');
                                btn.disabled = false;
                                btn.innerHTML = originalHtml;
                                btn.onclick = null;
                            } else {
                                // 仍在处理中，恢复刷新按钮
                                btn.disabled = false;
                                btn.innerHTML = `<i class="fas fa-sync-alt"></i> AI 仍在处理，点击刷新`;
                            }
                        })
                        .catch(() => {
                            showToast('网络异常，请稍后再试', 'error');
                            btn.disabled = false;
                            btn.innerHTML = `<i class="fas fa-sync-alt"></i> AI 仍在处理，点击刷新`;
                        });
                };
                showToast('AI 处理时间较长，完成后可点击按钮刷新获取结果', 'info');
            }

        } catch (error) {
            console.error('[需求导入轮询错误]', error);
            if (pollCount < MAX_POLLS) {
                setTimeout(poll, POLL_INTERVAL);
            } else {
                timedOut = true;
                btn.disabled = false;
                btn.innerHTML = `<i class="fas fa-sync-alt"></i> 网络异常，点击重试`;
                btn.onclick = () => {
                    // 重新开始轮询
                    timedOut = false;
                    pollCount = 0;
                    btn.onclick = null;
                    poll();
                };
            }
        }
    };

    poll();
}

/**
 * 确保值是字符串，防止 [object Object] 出现在表单中
 * 如果是对象/数组，转为格式化的 JSON 字符串
 */
function ensureString(val) {
    if (val == null) return '';
    if (typeof val === 'string') return val;
    if (typeof val === 'object') {
        try { return JSON.stringify(val, null, 2); } catch { return String(val); }
    }
    return String(val);
}

function fillFormWithImportedData(data) {
    if (!data) return;

    // 1. 填充全局设置
    if (data.global) {
        if (data.global.primaryColor) {
            $('primaryColor').value = data.global.primaryColor;
            $('primaryColorValue').textContent = data.global.primaryColor;
        }
        if (data.global.secondaryColor) {
            $('secondaryColor').value = data.global.secondaryColor;
            $('secondaryColorValue').textContent = data.global.secondaryColor;
        }
        if (data.global.backgroundMode) {
            $('backgroundMode').value = data.global.backgroundMode;
        }
        if (data.global.componentStyle) {
            $('componentStyle').value = data.global.componentStyle;
        }
        // 存储全局枚举数据
        if (data.global.enums && typeof data.global.enums === 'object' && Object.keys(data.global.enums).length > 0) {
            globalEnums = data.global.enums;
            console.log('[需求导入] 提取到全局枚举:', Object.keys(globalEnums));
        }
    }

    // 2. 清空现有页面
    pages = [];
    pageFiles = {};
    pageEnums = {};
    globalEnums = {};
    $('pageCardsContainer').innerHTML = '';

    // 3. 添加导入的页面
    if (data.pages && data.pages.length > 0) {
        data.pages.forEach((pageData) => {
            const id = Date.now().toString() + Math.random().toString(36).slice(2, 7);
            pages.push(id);
            pageFiles[id] = [];

            const index = pages.length;
            const html = createPageCardHtml(id, index);
            const div = document.createElement('div');
            div.id = `page-${id}`;
            div.className = 'bg-white rounded-xl shadow-sm border border-gray-200 overflow-hidden';
            div.innerHTML = html;
            $('pageCardsContainer').appendChild(div);
            setupPageListeners(id);

            // 填充页面数据（兼容新旧格式，确保字段为字符串防止 [object Object]）
            if (pageData.name) {
                const nameInput = $(`pageName_${id}`);
                if (nameInput) nameInput.value = ensureString(pageData.name);
            }
            if (pageData.description) {
                const descInput = $(`description_${id}`);
                if (descInput) descInput.value = ensureString(pageData.description);
            }
            if (pageData.layout) {
                const layoutInput = $(`layout_${id}`);
                if (layoutInput) layoutInput.value = ensureString(pageData.layout);
            }
            // UI 组件：优先用新字段 components，兼容旧字段 features
            const componentsRaw = pageData.components || pageData.features || '';
            const componentsText = ensureString(componentsRaw);
            if (componentsText) {
                const featuresInput = $(`features_${id}`);
                if (featuresInput) featuresInput.value = componentsText;
            }
            if (pageData.dataStructure) {
                const dsInput = $(`dataStructure_${id}`);
                if (dsInput) dsInput.value = ensureString(pageData.dataStructure);
            }
            // 交互：优先用新字段 interactions，兼容旧字段 interaction
            const interactionRaw = pageData.interactions || pageData.interaction || '';
            const interactionText = ensureString(interactionRaw);
            if (interactionText) {
                const interactionInput = $(`interaction_${id}`);
                if (interactionInput) interactionInput.value = interactionText;
            }
            if (pageData.userFlow) {
                const ufInput = $(`userFlow_${id}`);
                if (ufInput) ufInput.value = ensureString(pageData.userFlow);
            }

            // 存储页面级枚举数据
            if (pageData.enums && typeof pageData.enums === 'object' && Object.keys(pageData.enums).length > 0) {
                pageEnums[id] = pageData.enums;
                console.log(`[需求导入] 页面 "${pageData.name}" 提取到枚举:`, Object.keys(pageData.enums));
            }

            // 填充参考图
            if (pageData.images && pageData.images.length > 0) {
                pageData.images.forEach((img) => {
                    pageFiles[id].push({
                        name: img.name || 'ref.png',
                        base64: img.base64
                    });
                });
                renderPreviews(id);
            }
        });
    } else {
        // 没有提取到页面数据，添加一个空页面
        addPage();
    }

    console.log('[需求导入] 表单填充完成，共', pages.length, '个页面');
}

// ==================== PRD 需求讨论 ====================

let prdDiscSending = false;
let prdDiscDiscussionId = '';
let prdDiscStreamingEl = null;
let prdDiscAccumulatedPrd = '';
let currentSpecTab = 'confirmed';
let prdDiscLastSpecCard = null;
let prdDiscMarkdown = '';  // 讨论生成的 PRD 原文，用于回填到项目
let prdDiscAttachments = []; // 待发送附件 [{type:'image'|'file', name, base64?, previewUrl?}]

// 回车发送
document.addEventListener('DOMContentLoaded', () => {
    const input = $('prdDiscInput');
    if (input) {
        input.addEventListener('keydown', e => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                sendPrdDiscMessage();
            }
        });
    }

    // 需求讨论输入区：拖拽上传
    const inputArea = $('prdDiscInputArea');
    if (inputArea) {
        inputArea.addEventListener('dragover', e => {
            e.preventDefault();
            e.stopPropagation();
            inputArea.classList.add('border-amber-300', 'bg-amber-50');
        });
        inputArea.addEventListener('dragleave', e => {
            e.preventDefault();
            inputArea.classList.remove('border-amber-300', 'bg-amber-50');
        });
        inputArea.addEventListener('drop', e => {
            e.preventDefault();
            e.stopPropagation();
            inputArea.classList.remove('border-amber-300', 'bg-amber-50');
            if (e.dataTransfer && e.dataTransfer.files.length > 0) {
                handlePrdDiscFileSelect(e.dataTransfer.files);
            }
        });
    }

    // 需求讨论输入区：粘贴上传
    const prdModal = $('prdDiscussionModal');
    if (prdModal) {
        prdModal.addEventListener('paste', e => {
            const imageFiles = [];
            for (const item of (e.clipboardData || {}).items || []) {
                if (item.type.startsWith('image/')) {
                    const file = item.getAsFile();
                    if (file) imageFiles.push(file);
                }
            }
            if (imageFiles.length > 0) {
                handlePrdDiscFileSelect(imageFiles);
            }
        });
    }
});

// 处理文件选择
function handlePrdDiscFileSelect(files) {
    if (!files || files.length === 0) return;
    const maxFiles = 5;
    const maxSize = 10 * 1024 * 1024; // 10MB
    const allowedExts = ['.pdf', '.docx', '.txt', '.md'];
    const allowedImageTypes = ['image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/svg+xml'];

    const remaining = maxFiles - prdDiscAttachments.length;
    if (remaining <= 0) {
        showToast('最多上传 ' + maxFiles + ' 个文件', 'error');
        return;
    }

    const filesToProcess = Array.from(files).slice(0, remaining);
    let processed = 0;

    for (const file of filesToProcess) {
        if (file.size > maxSize) {
            showToast(file.name + ' 超过 10MB 限制', 'error');
            continue;
        }

        const isImage = allowedImageTypes.includes(file.type);
        const ext = '.' + file.name.split('.').pop().toLowerCase();
        const isDoc = allowedExts.includes(ext);

        if (!isImage && !isDoc) {
            showToast(file.name + ' 格式不支持（支持图片/PDF/DOCX/TXT/MD）', 'error');
            continue;
        }

        if (isImage) {
            // 图片：读取为 base64 用于预览和发送
            const reader = new FileReader();
            reader.onload = (e) => {
                prdDiscAttachments.push({
                    type: 'image',
                    name: file.name,
                    base64: e.target.result,
                    previewUrl: e.target.result
                });
                processed++;
                if (processed >= filesToProcess.length) renderPrdDiscAttachments();
            };
            reader.readAsDataURL(file);
        } else {
            // 文档：读取为 base64 发送给后端提取
            const reader = new FileReader();
            reader.onload = (e) => {
                prdDiscAttachments.push({
                    type: 'file',
                    name: file.name,
                    ext: ext,
                    base64: e.target.result
                });
                processed++;
                if (processed >= filesToProcess.length) renderPrdDiscAttachments();
            };
            reader.readAsDataURL(file);
        }
    }
}

// 渲染附件预览条
function renderPrdDiscAttachments() {
    const container = $('prdDiscAttachments');
    if (!container) return;

    if (prdDiscAttachments.length === 0) {
        container.classList.add('hidden');
        container.innerHTML = '';
        return;
    }

    container.classList.remove('hidden');
    container.innerHTML = prdDiscAttachments.map((att, i) => {
        if (att.type === 'image') {
            return '<div class="prd-disc-att-item" style="position:relative;display:inline-flex;align-items:center;gap:4px;padding:4px 8px;background:#fef3c7;border-radius:8px;font-size:11px;color:#92400e;max-width:160px;">' +
                '<img src="' + att.previewUrl + '" style="width:24px;height:24px;object-fit:cover;border-radius:4px;flex-shrink:0;" />' +
                '<span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">' + _escapeHtml(att.name) + '</span>' +
                '<button onclick="removePrdDiscAttachment(' + i + ')" style="margin-left:2px;color:#d97706;font-size:10px;flex-shrink:0;" title="删除">&times;</button>' +
                '</div>';
        } else {
            const icon = att.ext === '.pdf' ? 'fa-file-pdf' : att.ext === '.docx' ? 'fa-file-word' : 'fa-file-alt';
            return '<div class="prd-disc-att-item" style="position:relative;display:inline-flex;align-items:center;gap:4px;padding:4px 8px;background:#e0e7ff;border-radius:8px;font-size:11px;color:#3730a3;max-width:160px;">' +
                '<i class="fas ' + icon + '" style="flex-shrink:0;"></i>' +
                '<span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">' + _escapeHtml(att.name) + '</span>' +
                '<button onclick="removePrdDiscAttachment(' + i + ')" style="margin-left:2px;color:#4f46e5;font-size:10px;flex-shrink:0;" title="删除">&times;</button>' +
                '</div>';
        }
    }).join('');
}

// 删除附件
function removePrdDiscAttachment(index) {
    prdDiscAttachments.splice(index, 1);
    renderPrdDiscAttachments();
}

function openPrdDiscussion() {
    $('prdDiscussionModal').classList.remove('hidden');
    $('prdDiscInput').focus();
    // 如果没有活跃讨论，显示欢迎引导
    if (!prdDiscDiscussionId) {
        $('prdDiscMessages').innerHTML =
            '<div class="prd-disc-bubble prd-disc-system"><div class="prd-disc-text">' +
            '描述你的产品想法，AI 将引导你逐步完善需求<br>' +
            '<span style="font-size:11px;color:#6b7280;">讨论完成后可一键填充到生成表单</span>' +
            '</div></div>';
    }
}

function closePrdDiscussion() {
    $('prdDiscussionModal').classList.add('hidden');
}

function _escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function addDiscBubble(role, content, attachments) {
    const area = $('prdDiscMessages');
    const bubble = document.createElement('div');
    bubble.className = 'prd-disc-bubble prd-disc-' + role;

    let html = '';

    // 渲染附件（图片和文件都在气泡文本上方）
    if (attachments && attachments.length > 0) {
        const images = attachments.filter(a => a.type === 'image');
        const files = attachments.filter(a => a.type !== 'image');

        // 图片网格
        if (images.length > 0) {
            const gridCols = images.length === 1 ? '' : 'grid-template-columns:repeat(' + Math.min(images.length, 3) + ',1fr);';
            html += '<div class="prd-disc-images" style="display:grid;' + gridCols + 'gap:6px;margin-bottom:6px;">';
            for (const img of images) {
                const src = img.previewUrl || img.base64 || img.url || '';
                if (src) {
                    html += '<div class="prd-disc-img-wrap" style="position:relative;border-radius:10px;overflow:hidden;cursor:pointer;background:#f3f4f6;">' +
                        '<img src="' + src + '" style="width:100%;max-height:200px;object-fit:cover;display:block;" />' +
                        '</div>';
                }
            }
            html += '</div>';
        }

        // 文件列表
        if (files.length > 0) {
            html += '<div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:6px;">';
            for (const att of files) {
                const icon = (att.ext === '.pdf') ? 'fa-file-pdf' : (att.ext === '.docx') ? 'fa-file-word' : 'fa-file-alt';
                html += '<div style="display:inline-flex;align-items:center;gap:4px;padding:4px 10px;background:#e0e7ff;border-radius:8px;font-size:11px;color:#3730a3;">' +
                    '<i class="fas ' + icon + '"></i>' +
                    '<span>' + _escapeHtml(att.name) + '</span>' +
                    '</div>';
            }
            html += '</div>';
        }
    }

    if (content) {
        html += '<div class="prd-disc-text">' + _escapeHtml(content) + '</div>';
    }
    bubble.innerHTML = html;

    // 为图片绑定点击预览（避免 inline onclick 中 base64 转义问题）
    bubble.querySelectorAll('.prd-disc-img-wrap').forEach((wrap, i) => {
        const att = attachments.filter(a => a.type === 'image')[i];
        if (att) {
            const src = att.previewUrl || att.base64 || att.url || '';
            wrap.addEventListener('click', () => previewPrdDiscImage(src));
        }
    });

    area.appendChild(bubble);
    area.scrollTop = area.scrollHeight;
    return bubble;
}

// 图片灯箱预览
function previewPrdDiscImage(src) {
    // 移除已有的灯箱
    const existing = document.getElementById('prdDiscLightbox');
    if (existing) { existing.remove(); return; }

    const overlay = document.createElement('div');
    overlay.id = 'prdDiscLightbox';
    overlay.style.cssText = 'position:fixed;inset:0;z-index:100000;background:rgba(0,0,0,0.7);display:flex;align-items:center;justify-content:center;cursor:zoom-out;';
    overlay.onclick = () => overlay.remove();

    const img = document.createElement('img');
    img.src = src;
    img.style.cssText = 'max-width:90vw;max-height:90vh;border-radius:12px;box-shadow:0 8px 32px rgba(0,0,0,0.3);object-fit:contain;';
    img.onclick = (e) => { e.stopPropagation(); };

    overlay.appendChild(img);
    document.body.appendChild(overlay);
}

function addDiscStreamingBubble() {
    const area = $('prdDiscMessages');
    const bubble = document.createElement('div');
    bubble.className = 'prd-disc-bubble prd-disc-ai';
    bubble.innerHTML = '<div class="prd-disc-text"></div>';
    area.appendChild(bubble);
    area.scrollTop = area.scrollHeight;
    return bubble.querySelector('.prd-disc-text');
}

function updateMaturityBar(level) {
    const levels = ['RA0', 'RA1', 'RA2', 'RA3', 'RA4', 'RA5'];
    const labels = {
        'RA0': 'RA0 · 模糊想法', 'RA1': 'RA1 · 可讨论', 'RA2': 'RA2 · 可分析',
        'RA3': 'RA3 · 可设计', 'RA4': 'RA4 · 可实现', 'RA5': 'RA5 · 可交付',
    };
    const idx = levels.indexOf(level);
    document.querySelectorAll('.maturity-step').forEach((step, i) => {
        step.classList.remove('active', 'passed');
        if (i < idx) step.classList.add('passed');
        if (i === idx) step.classList.add('active');
    });
    $('maturityLabel').textContent = labels[level] || level;
}

function updateSpecCard(specCard) {
    if (!specCard) return;
    prdDiscLastSpecCard = specCard;
    const confirmed = specCard.confirmed || [];
    const assumptions = specCard.assumptions || [];
    const questions = specCard.open_questions || [];
    $('specConfirmedCount').textContent = confirmed.length;
    $('specAssumptionsCount').textContent = assumptions.length;
    $('specQuestionsCount').textContent = questions.length;
    renderSpecItems(currentSpecTab, specCard);
    updateMaturityBar(specCard.maturity_level || 'RA0');
}

function switchSpecTab(tab) {
    currentSpecTab = tab;
    document.querySelectorAll('.spec-card-tab').forEach(t => {
        t.classList.toggle('active', t.dataset.tab === tab);
    });
    if (prdDiscLastSpecCard) renderSpecItems(tab, prdDiscLastSpecCard);
}

function renderSpecItems(tab, specCard) {
    const container = $('specCardContent');
    let items = [], dotClass = '';
    if (tab === 'confirmed') { items = specCard.confirmed || []; dotClass = 'confirmed'; }
    else if (tab === 'assumptions') { items = specCard.assumptions || []; dotClass = 'assumption'; }
    else { items = specCard.open_questions || []; dotClass = 'question'; }

    if (items.length === 0) {
        const empty = tab === 'confirmed' ? '暂无已确认需求' : tab === 'assumptions' ? '暂无假设' : '暂无待讨论问题';
        container.innerHTML = '<div style="text-align:center;padding:20px 8px;color:#9ca3af;font-size:12px;">' + empty + '</div>';
        return;
    }
    container.innerHTML = items.map(item =>
        '<div class="spec-item"><div class="spec-item-dot ' + dotClass + '"></div><span>' + _escapeHtml(item) + '</span></div>'
    ).join('');
}

async function sendPrdDiscMessage() {
    const input = $('prdDiscInput');
    const message = input.value.trim();
    const attachments = [...prdDiscAttachments]; // 快照当前附件

    if ((!message && attachments.length === 0) || prdDiscSending) return;
    input.value = '';
    prdDiscAttachments = [];
    renderPrdDiscAttachments();

    prdDiscSending = true;
    $('prdDiscSendBtn').disabled = true;
    $('prdDiscStatus').textContent = attachments.length > 0 ? '处理附件中...' : '思考中...';
    addDiscBubble('user', message || '(上传了附件)', attachments);

    if (!prdDiscDiscussionId) {
        await startDiscussion(message, attachments);
    } else {
        await continueDiscussion(message, attachments);
    }
    prdDiscSending = false;
    $('prdDiscSendBtn').disabled = false;
}

async function startDiscussion(initialMessage, attachments) {
    try {
        const body = { initialIdea: initialMessage };
        if (attachments && attachments.length > 0) {
            body.attachments = attachments.map(a => ({
                type: a.type,
                name: a.name,
                base64: a.base64,
                ext: a.ext
            }));
        }
        const res = await fetch('/api/prd/discussion/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
        if (!res.ok) throw new Error('请求失败: ' + res.status);
        await processSSEStream(res);
    } catch (e) {
        console.error('[PRD讨论] 启动失败:', e);
        addDiscBubble('system', '讨论启动失败: ' + e.message);
        $('prdDiscStatus').textContent = '';
    }
}

async function continueDiscussion(message, attachments) {
    try {
        const body = { discussionId: prdDiscDiscussionId, message: message };
        if (attachments && attachments.length > 0) {
            body.attachments = attachments.map(a => ({
                type: a.type,
                name: a.name,
                base64: a.base64,
                ext: a.ext
            }));
        }
        const res = await fetch('/api/prd/discussion/message', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
        if (!res.ok) throw new Error('请求失败: ' + res.status);
        await processSSEStream(res);
    } catch (e) {
        console.error('[PRD讨论] 发送失败:', e);
        addDiscBubble('system', '发送失败: ' + e.message);
        $('prdDiscStatus').textContent = '';
    }
}

async function processSSEStream(res) {
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop();
        for (const line of lines) {
            if (!line.startsWith('data: ')) continue;
            try {
                handleDiscEvent(JSON.parse(line.substring(6)));
            } catch (e) { /* skip */ }
        }
    }
}

function handleDiscEvent(event) {
    const { type, data } = event;

    if (type === 'discussion_start') {
        prdDiscDiscussionId = data.discussionId;
        prdDiscStreamingEl = addDiscStreamingBubble();
    } else if (type === 'chat') {
        if (!prdDiscStreamingEl) prdDiscStreamingEl = addDiscStreamingBubble();
        prdDiscStreamingEl.textContent += data.content;
        $('prdDiscMessages').scrollTop = $('prdDiscMessages').scrollHeight;
    } else if (type === 'spec_card_update') {
        prdDiscStreamingEl = null;
        updateSpecCard(data);
        $('btnPrdPreview').disabled = false;
        $('btnPrdDelivery').disabled = false;
        $('btnApplyPrd').disabled = false;
    } else if (type === 'done') {
        prdDiscStreamingEl = null;
        $('prdDiscStatus').textContent = '';
    } else if (type === 'error') {
        prdDiscStreamingEl = null;
        addDiscBubble('system', '错误: ' + (data.message || '未知错误'));
        $('prdDiscStatus').textContent = '';
    } else if (type === 'prd_content') {
        if (!prdDiscStreamingEl) prdDiscStreamingEl = addDiscStreamingBubble();
        prdDiscAccumulatedPrd += data.content;
        prdDiscStreamingEl.textContent = prdDiscAccumulatedPrd;
        $('prdDiscMessages').scrollTop = $('prdDiscMessages').scrollHeight;
    } else if (type === 'prd_done') {
        prdDiscStreamingEl = null;
        prdDiscAccumulatedPrd = '';
        const modeText = data.mode === 'delivery' ? '交付版' : '预览版';
        addDiscBubble('system', modeText + ' PRD 已生成 (' + data.length + ' 字符)');
        $('prdDiscStatus').textContent = '';
    } else if (type === 'extract_start') {
        $('prdDiscStatus').textContent = data.message;
    } else if (type === 'extract_done') {
        $('prdDiscStatus').textContent = '';
        if (data.success) {
            addDiscBubble('system', '需求提取成功！正在填充表单...');
            // 存储 PRD markdown，生成完成后回填到项目
            if (data.prd_markdown) {
                prdDiscMarkdown = data.prd_markdown;
            }
            // 填充表单并关闭弹窗
            fillFormWithImportedData(data.requirements);
            setTimeout(() => {
                closePrdDiscussion();
                showToast('需求讨论完成，已填充到生成表单', 'success');
            }, 500);
        } else {
            addDiscBubble('system', '需求提取失败: ' + (data.error || '未知错误'));
        }
    }
}

function newPrdDiscussion() {
    if (prdDiscSending) return;
    prdDiscDiscussionId = '';
    prdDiscLastSpecCard = null;
    prdDiscAccumulatedPrd = '';
    prdDiscAttachments = [];
    renderPrdDiscAttachments();
    $('prdDiscMessages').innerHTML =
        '<div class="prd-disc-bubble prd-disc-system"><div class="prd-disc-text">' +
        '新的讨论已开始，请描述你的产品想法</div></div>';
    updateMaturityBar('RA0');
    currentSpecTab = 'confirmed';
    document.querySelectorAll('.spec-card-tab').forEach(t => t.classList.toggle('active', t.dataset.tab === 'confirmed'));
    $('specCardContent').innerHTML = '<div style="text-align:center;padding:24px 8px;color:#9ca3af;font-size:12px;">开始讨论后<br>需求内容将在这里显示</div>';
    $('specConfirmedCount').textContent = '0';
    $('specAssumptionsCount').textContent = '0';
    $('specQuestionsCount').textContent = '0';
    $('btnPrdPreview').disabled = true;
    $('btnPrdDelivery').disabled = true;
    $('btnApplyPrd').disabled = true;
    $('prdDiscInput').focus();
}

async function generatePrdPreview() {
    if (!prdDiscDiscussionId || prdDiscSending) return;
    prdDiscSending = true;
    $('btnPrdPreview').disabled = true;
    $('prdDiscStatus').textContent = '生成预览版 PRD...';
    prdDiscAccumulatedPrd = '';
    addDiscBubble('system', '正在生成预览版 PRD（允许 TBD 占位符）...');
    try {
        const res = await fetch('/api/prd/discussion/generate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ discussionId: prdDiscDiscussionId, mode: 'preview' }),
        });
        await processSSEStream(res);
    } catch (e) { addDiscBubble('system', '生成失败: ' + e.message); }
    prdDiscSending = false;
    $('btnPrdPreview').disabled = false;
}

async function generatePrdDelivery() {
    if (!prdDiscDiscussionId || prdDiscSending) return;
    prdDiscSending = true;
    $('btnPrdDelivery').disabled = true;
    $('prdDiscStatus').textContent = '生成交付版 PRD...';
    prdDiscAccumulatedPrd = '';
    addDiscBubble('system', '正在生成交付版 PRD（禁止 TBD 占位符）...');
    try {
        const res = await fetch('/api/prd/discussion/generate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ discussionId: prdDiscDiscussionId, mode: 'delivery' }),
        });
        await processSSEStream(res);
    } catch (e) { addDiscBubble('system', '生成失败: ' + e.message); }
    prdDiscSending = false;
    $('btnPrdDelivery').disabled = false;
}

async function applyPrdToProject() {
    if (!prdDiscDiscussionId || prdDiscSending) return;
    prdDiscSending = true;
    $('btnApplyPrd').disabled = true;
    $('prdDiscStatus').textContent = '正在提取需求...';
    addDiscBubble('system', '正在从讨论中提取结构化需求...');

    try {
        const res = await fetch('/api/prd/discussion/apply', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ discussionId: prdDiscDiscussionId, mode: 'delivery' }),
        });
        await processSSEStream(res);
    } catch (e) {
        addDiscBubble('system', '应用失败: ' + e.message);
    }
    prdDiscSending = false;
    $('btnApplyPrd').disabled = false;
}

async function savePrdToProject(projectId, markdown) {
    // 将讨论生成的 PRD 保存到项目
    try {
        // 尝试读取项目信息获取页面列表
        let pageNames = ['主页面'];
        try {
            const res = await fetch(`/api/generation-status?id=${encodeURIComponent(projectId)}`);
            const data = await res.json();
            if (data.page_names && data.page_names.length > 0) {
                pageNames = data.page_names;
            }
        } catch (e) { /* 使用默认值 */ }

        // 为每个页面保存 PRD
        for (const pageName of pageNames) {
            await fetch('/api/prd/save', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    projectId: projectId,
                    pageName: pageName,
                    content: markdown,
                }),
            });
        }
        console.log(`[PRD讨论] PRD 已回填到项目 ${projectId}，${pageNames.length} 个页面`);
    } catch (e) {
        console.error('[PRD讨论] PRD 回填失败:', e);
    }
}
