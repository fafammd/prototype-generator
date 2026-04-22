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
                <button onclick="stopGeneration('${p.id}', '${safeName}')"
                        class="flex-1 flex items-center justify-center gap-1 py-1 text-xs text-orange-500 hover:text-orange-700 rounded hover:bg-orange-50 transition-colors font-medium" title="停止生成">
                    <i class="fas fa-stop"></i>停止
                </button>
                ` : p.status === 'failed' ? `
                <button onclick="resumeGeneration('${p.id}', '${safeName}')"
                        class="flex-1 flex items-center justify-center gap-1 py-1 text-xs text-green-500 hover:text-green-700 rounded hover:bg-green-50 transition-colors font-medium" title="继续生成">
                    <i class="fas fa-redo"></i>继续
                </button>
                ` : `
                <button onclick="editProjectTitle('${p.id}', '${safeName}')"
                        class="flex-1 flex items-center justify-center gap-1 py-1 text-xs text-gray-400 hover:text-blue-600 rounded hover:bg-blue-50 transition-colors" title="编辑">
                    <i class="fas fa-edit"></i>
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

async function regenerateFromRecord() {
    if (!currentRecordProject) return;

    const record = currentRecordProject.record;
    closeRecordModal();

    // 保存来源项目ID（用于增量更新）
    sourceProjectId = currentRecordProject.id;
    console.log('[增量更新] 开始加载历史记录，sourceProjectId:', sourceProjectId);

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
            if (pageRecord.layout) $(`layout_${id}`).value = ensureString(pageRecord.layout);
            if (pageRecord.features) $(`features_${id}`).value = ensureString(pageRecord.features);
            if (pageRecord.interaction) $(`interaction_${id}`).value = ensureString(pageRecord.interaction);
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
                            const imgUrl = `/projects/${currentRecordProject.id}/reference/${imgName}`;
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

    $('headerTitle').textContent = '已加载历史记录 - 可修改后重新生成（支持增量更新）';
    showToast('已加载历史记录，修改后将智能增量生成');
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
function generatePrompt() {
    const global = {
        primaryColor: $('primaryColor').value,
        secondaryColor: $('secondaryColor').value,
        backgroundMode: $('backgroundMode').value,
        componentStyle: $('componentStyle').value
    };

    // 检测是否为多页面项目
    const isMultiPage = pages.length > 1;

    let prompt = `你是一位资深的前端工程师和UI/UX设计师，擅长创建高保真、可交互的HTML原型。
请根据以下设计规范和需求，生成一个高保真的HTML原型。

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
            prompt += `**参考图**: 已附加${pageFiles[id].length}张参考图。`;
            if (similarity === 'pixel') {
                prompt += `请尽可能像素级还原参考图的设计。\n\n`;
            } else if (similarity === 'style') {
                prompt += `请参考其视觉风格（配色、质感、氛围）。\n\n`;
            } else {
                prompt += `请参考其布局结构（元素位置、区域划分）。\n\n`;
            }
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

## 视觉质量要求
1. 现代化设计风格，精致的视觉效果
2. 合理的间距、对齐、层次感
3. 统一的配色方案（遵循上方全局设计规范）
4. 状态反馈：悬停效果、选中状态、焦点样式
5. 空状态和加载状态的优雅处理
6. 图标使用 FontAwesome

## 交互质量要求
1. 所有按钮有 hover/active 效果
2. 表格支持排序（点击表头）
3. 搜索/筛选有即时响应效果
4. 弹窗/模态框有遮罩和动画
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

    return { global, pages: pagesData };
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

    try {
        // 构建请求数据
        const requestData = {
            prompt: prompt,
            images: allImages,
            projectName: projectName,
            formData: formData
        };

        // 如果使用增量更新，添加额外信息
        if (useIncremental && changes) {
            requestData.incremental = true;
            requestData.sourceProjectId = sourceProjectId;
            requestData.changes = changes;
        }

        // ==================== 异步生成模式 ====================
        showToast('🚀 开始生成，请稍候...', 'info');

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
            // 立即添加带 generating 状态的项目到列表
            allProjects.unshift(result.project);
            renderProjectList();
            showToast('🔵 已开始生成 "' + result.project.name + '"，请查看左侧列表');

            // 开始流式监听状态
            streamGenerationStatus(result.project.id);

            // 重置增量更新状态
            sourceProjectId = null;
            originalFormData = null;
            originalImageHashes = {};
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
    // 显示流式预览弹窗
    showStreamingModal();

    try {
        const evtSource = new EventSource(
            `/api/generation-stream?id=${encodeURIComponent(projectId)}`
        );

        let accumulatedContent = '';

        evtSource.onmessage = function(event) {
            try {
                const data = JSON.parse(event.data);
                if (data.content) {
                    accumulatedContent += data.content;
                    updateStreamingPreview(accumulatedContent);
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

function updateStreamingPreview(content) {
    const preview = document.getElementById('streamPreview');
    const progress = document.getElementById('streamProgress');

    if (preview) {
        // 只显示最后 3000 字符，避免 DOM 过大
        const display = content.length > 3000
            ? '...\n' + content.slice(-3000)
            : content;
        preview.textContent = display;
        preview.scrollTop = preview.scrollHeight;
    }

    if (progress) {
        const chars = content.length;
        const lines = content.split('\n').length;
        progress.textContent = `已生成 ${chars.toLocaleString()} 字符, ${lines} 行`;
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
        if (navigator.clipboard && navigator.clipboard.writeText) {
            await navigator.clipboard.writeText(prompt);
        } else {
            // Fallback for non-HTTPS contexts (HTTP, file://)
            const textarea = document.createElement('textarea');
            textarea.value = prompt;
            textarea.style.cssText = 'position:fixed;opacity:0';
            document.body.appendChild(textarea);
            textarea.select();
            document.execCommand('copy');
            document.body.removeChild(textarea);
        }
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
                    ${m.multimodal ? '<span class="text-xs bg-purple-50 text-purple-600 px-1.5 py-0.5 rounded">多模态</span>' : ''}
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
    $('modelFormProvider').value = m.provider || '';
    $('modelFormModel').value = m.model || '';
    $('modelFormBaseUrl').value = m.base_url || '';
    $('modelFormApiKey').value = m.api_key || '';
    $('modelFormMultimodal').checked = !!m.multimodal;
    $('modelFormMaxTokens').value = m.max_tokens || '';
    $('modelFormTimeout').value = m.timeout || '';
    $('modelFormTitle').textContent = '编辑模型: ' + m.name;
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
        model: m.model || '',
        base_url: m.base_url || '',
        api_key: m.api_key || '',
        multimodal: !!m.multimodal,
        max_tokens: m.max_tokens || null,
        timeout: m.timeout || null
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
    const maxTokens = $('modelFormMaxTokens').value ? parseInt($('modelFormMaxTokens').value) : null;
    const timeout = $('modelFormTimeout').value ? parseInt($('modelFormTimeout').value) : null;

    const modelData = { id, name, provider, model, base_url: baseUrl, api_key: apiKey, multimodal };
    if (maxTokens) modelData.max_tokens = maxTokens;
    if (timeout) modelData.timeout = timeout;

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
    $('modelFormProvider').value = '';
    $('modelFormModel').value = '';
    $('modelFormBaseUrl').value = '';
    $('modelFormApiKey').value = '';
    $('modelFormMultimodal').checked = false;
    $('modelFormMaxTokens').value = '';
    $('modelFormTimeout').value = '';
    $('modelFormTitle').textContent = '添加新模型';
    // 刷新列表取消高亮
    if ($('modelManagerModal') && !$('modelManagerModal').classList.contains('hidden')) {
        renderModelManagerList();
    }
}


// ==================== 导出 & 分享弹窗 ====================

let currentExportProjectId = '';
let currentExportProjectName = '';
let currentExportMode = 'preview';

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
        let resolved = false;

        evtSource.onmessage = function(event) {
            try {
                const data = JSON.parse(event.data);
                if (data.content) {
                    accumulated += data.content;
                    // 更新按钮进度文本
                    const chars = accumulated.length;
                    btn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> AI 提取中 (${chars.toLocaleString()} 字符)...`;
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
