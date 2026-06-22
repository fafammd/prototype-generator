/**
 * CanvasStudio — 画布 Agent 模式
 *
 * 画布式生成体验：
 * - 顶部：项目名称 + 阶段进度
 * - 中间：页面卡片画布（iframe 缩略图）
 * - 底部：对话框（可展开）
 * - 点击卡片：全屏预览 + 页面级对话
 *
 * SSE 事件复用 /api/generation-stream，无需后端修改
 */

// ============================================================================
// StreamingHtmlParser — Generator 状态机流式 HTML 解析器
// 参考 Open Design 的 createArtifactParser，适配 <artifact> 协议 + fallback
// ============================================================================

class StreamingHtmlParser {
    constructor() {
        this._state = 'idle';   // idle | in_think | in_artifact | in_fence | in_html
        this._buffer = '';
        this._currentHtml = '';
    }

    /**
     * 核心方法：喂入 SSE delta，yield 解析事件。
     * 参考 OD parser.ts 的 feed(delta) Generator。
     */
    *feed(delta) {
        this._buffer += delta;
        let progress = true;
        while (this._buffer.length > 0 && progress) {
            progress = false;
            switch (this._state) {
                case 'idle':        progress = yield* this._processIdle(); break;
                case 'in_think':    progress = yield* this._processThink(); break;
                case 'in_artifact': progress = yield* this._processArtifact(); break;
                case 'in_fence':    progress = yield* this._processFence(); break;
                case 'in_html':     progress = yield* this._processDirectHtml(); break;
            }
        }
    }

    // --- State processors ---

    *_processIdle() {
        const buf = this._buffer;

        // 优先级 1：[think]
        const thinkIdx = buf.indexOf('[think]');
        if (thinkIdx !== -1) {
            if (thinkIdx > 0) yield { type: 'text', delta: buf.slice(0, thinkIdx) };
            this._buffer = buf.slice(thinkIdx + 7);
            this._state = 'in_think';
            return true;
        }
        if (this._endsWithPartial(buf, '[think]')) {
            const hb = this._holdbackPosition(buf, '[think]');
            if (hb > 0) { yield { type: 'text', delta: buf.slice(0, hb) }; this._buffer = buf.slice(hb); }
            return false;
        }

        // 优先级 2：<artifact> （主路径！确定性边界）
        const artResult = this._findArtifactOpen(buf);
        if (artResult.found) {
            if (artResult.start > 0) yield { type: 'text', delta: buf.slice(0, artResult.start) };
            this._buffer = buf.slice(artResult.contentStart);
            this._state = 'in_artifact';
            this._currentHtml = '';
            yield { type: 'html:start' };
            return true;
        }
        if (artResult.holdback > 0) {
            const hb = artResult.holdback;
            if (hb > 0) { yield { type: 'text', delta: buf.slice(0, hb) }; this._buffer = buf.slice(hb); }
            return false;
        }

        // 优先级 3：```html fence（fallback 1）
        const fenceResult = this._findFenceOpen(buf);
        if (fenceResult.found) {
            if (fenceResult.start > 0) yield { type: 'text', delta: buf.slice(0, fenceResult.start) };
            this._buffer = buf.slice(fenceResult.contentStart);
            this._state = 'in_fence';
            this._currentHtml = '';
            yield { type: 'html:start' };
            return true;
        }
        if (fenceResult.holdback > 0) {
            const hb = fenceResult.holdback;
            if (hb > 0) { yield { type: 'text', delta: buf.slice(0, hb) }; this._buffer = buf.slice(hb); }
            return false;
        }

        // 优先级 4：直接 HTML 起始（fallback 2）
        const htmlStart = this._detectHtmlStart(buf);
        if (htmlStart !== -1) {
            if (htmlStart > 0) yield { type: 'text', delta: buf.slice(0, htmlStart) };
            this._buffer = buf.slice(htmlStart);
            this._state = 'in_html';
            this._currentHtml = '';
            yield { type: 'html:start' };
            return true;
        }

        // 无匹配 — flush text，holdback 尾部 partial tags
        const flushLen = this._calcIdleFlushLen(buf);
        if (flushLen > 0) {
            yield { type: 'text', delta: buf.slice(0, flushLen) };
            this._buffer = buf.slice(flushLen);
            return true;
        }
        return false;
    }

    *_processThink() {
        const buf = this._buffer;
        const closeIdx = buf.indexOf('[/think]');
        if (closeIdx !== -1) {
            this._buffer = buf.slice(closeIdx + 8);
            this._state = 'idle';
            return true;
        }
        if (this._endsWithPartial(buf, '[/think]')) {
            const hb = this._holdbackPosition(buf, '[/think]');
            this._buffer = buf.slice(hb);
            return false;
        }
        this._buffer = '';
        return true;
    }

    *_processArtifact() {
        const buf = this._buffer;
        const closeIdx = buf.indexOf('</artifact>');
        if (closeIdx !== -1) {
            const chunk = buf.slice(0, closeIdx);
            if (chunk.length > 0) { this._currentHtml += chunk; yield { type: 'html:chunk', delta: chunk }; }
            yield { type: 'html:end', fullContent: this._currentHtml };
            this._buffer = buf.slice(closeIdx + '</artifact>'.length);
            this._state = 'idle';
            this._currentHtml = '';
            return true;
        }
        // Holdback: 保留尾部 11 字符（</artifact> 长度 12 - 1）
        const holdback = Math.max(0, buf.length - 11);
        if (holdback > 0) {
            const chunk = buf.slice(0, holdback);
            this._currentHtml += chunk;
            this._buffer = buf.slice(holdback);
            yield { type: 'html:chunk', delta: chunk };
            return true;
        }
        return false;
    }

    *_processFence() {
        const buf = this._buffer;
        const closeResult = this._findFenceClose(buf);
        if (closeResult.found) {
            const chunk = buf.slice(0, closeResult.start);
            if (chunk.length > 0) { this._currentHtml += chunk; yield { type: 'html:chunk', delta: chunk }; }
            yield { type: 'html:end', fullContent: this._currentHtml };
            this._buffer = buf.slice(closeResult.end);
            this._state = 'idle';
            this._currentHtml = '';
            return true;
        }
        // Holdback: 保留尾部 2 字符（``` 长度 3 - 1）
        const holdback = Math.max(0, buf.length - 2);
        if (holdback > 0) {
            const chunk = buf.slice(0, holdback);
            this._currentHtml += chunk;
            this._buffer = buf.slice(holdback);
            yield { type: 'html:chunk', delta: chunk };
            return true;
        }
        return false;
    }

    *_processDirectHtml() {
        const buf = this._buffer;
        if (buf.length > 0) {
            this._currentHtml += buf;
            yield { type: 'html:chunk', delta: buf };
            this._buffer = '';
            return true;
        }
        return false;
    }

    // --- Tag detection helpers ---

    _findArtifactOpen(buf) {
        const OPEN_PREFIX = '<artifact';
        const len = buf.length;
        let earliestPartial = -1;
        let from = 0;

        while (from < len) {
            const idx = buf.indexOf(OPEN_PREFIX, from);
            if (idx === -1) break;
            const next = buf.charAt(idx + OPEN_PREFIX.length);
            if (next === '') { earliestPartial = idx; break; }
            if (!/[\s>]/.test(next)) { from = idx + OPEN_PREFIX.length; continue; }

            // Valid <artifact — find closing >
            let j = idx + OPEN_PREFIX.length;
            let quote = null;
            while (j < len) {
                const c = buf.charAt(j);
                if (quote !== null) { if (c === quote) quote = null; }
                else if (c === '"' || c === "'") { quote = c; }
                else if (c === '>') {
                    return { found: true, start: idx, end: j + 1, contentStart: j + 1 };
                }
                j++;
            }
            earliestPartial = idx;
            break;
        }

        if (earliestPartial !== -1) return { found: false, holdback: earliestPartial };

        // Check tail for `<art` etc.
        const tailLt = buf.lastIndexOf('<');
        if (tailLt !== -1) {
            const slice = buf.slice(tailLt);
            if (OPEN_PREFIX.startsWith(slice) && slice.length < OPEN_PREFIX.length) {
                return { found: false, holdback: tailLt };
            }
        }
        return { found: false, holdback: 0 };
    }

    _findFenceOpen(buf) {
        const markers = ['```html', '```HTML'];
        for (const marker of markers) {
            const idx = buf.indexOf(marker);
            if (idx !== -1) {
                const nlIdx = buf.indexOf('\n', idx + marker.length);
                if (nlIdx !== -1) return { found: true, start: idx, contentStart: nlIdx + 1 };
                return { found: false, holdback: idx };
            }
            if (this._endsWithPartial(buf, marker)) {
                return { found: false, holdback: this._holdbackPosition(buf, marker) };
            }
        }
        const bareFence = buf.lastIndexOf('```');
        if (bareFence !== -1) {
            const after = buf.slice(bareFence + 3).trim();
            if (after === '' || 'html'.startsWith(after.toLowerCase())) {
                return { found: false, holdback: bareFence };
            }
        }
        return { found: false, holdback: 0 };
    }

    _findFenceClose(buf) {
        const re = /\n```\s*\n/g;
        let match;
        while ((match = re.exec(buf)) !== null) {
            return { found: true, start: match.index, end: match.index + match[0].length };
        }
        if (buf.startsWith('```\n') || buf.startsWith('```\r\n')) {
            return { found: true, start: 0, end: buf.indexOf('\n') + 1 };
        }
        if (buf === '```' || /^```[^\n]*$/.test(buf)) {
            return { found: true, start: 0, end: buf.length };
        }
        return { found: false };
    }

    _detectHtmlStart(buf) {
        let earliest = -1;
        for (const p of [/<!DOCTYPE/i, /<html[\s>]/i, /<head[\s>]/i]) {
            const m = buf.search(p);
            if (m !== -1 && (earliest === -1 || m < earliest)) earliest = m;
        }
        if (earliest === -1) {
            const divIdx = buf.search(/<div[\s>]/i);
            const styleIdx = buf.search(/<style[\s>]/i);
            if (divIdx !== -1 && styleIdx !== -1 && Math.abs(divIdx - styleIdx) < 500) {
                earliest = Math.min(divIdx, styleIdx);
            }
        }
        return earliest;
    }

    _endsWithPartial(buf, target) {
        for (let len = 1; len < target.length && len <= buf.length; len++) {
            if (target.startsWith(buf.slice(-len))) return true;
        }
        return false;
    }

    _holdbackPosition(buf, target) {
        for (let len = Math.min(target.length - 1, buf.length); len >= 1; len--) {
            if (target.startsWith(buf.slice(-len))) return buf.length - len;
        }
        return 0;
    }

    _calcIdleFlushLen(buf) {
        const prefixes = ['<artifact', '<!DOCTYPE', '<html', '<head', '<div', '<body', '```', '[think]'];
        let safeLen = buf.length;
        for (const prefix of prefixes) {
            if (this._endsWithPartial(buf, prefix)) {
                const hb = this._holdbackPosition(buf, prefix);
                if (hb < safeLen) safeLen = hb;
            }
        }
        return Math.min(safeLen, Math.max(0, buf.length - 15));
    }

    *flush() {
        if (this._state === 'in_artifact' || this._state === 'in_fence' || this._state === 'in_html') {
            if (this._buffer.length > 0) {
                this._currentHtml += this._buffer;
                yield { type: 'html:chunk', delta: this._buffer };
                this._buffer = '';
            }
            yield { type: 'html:end', fullContent: this._currentHtml };
        }
        this._state = 'idle';
        this._buffer = '';
        this._currentHtml = '';
    }

    reset() {
        this._state = 'idle';
        this._buffer = '';
        this._currentHtml = '';
    }

    get currentHtml() { return this._currentHtml; }
    get isInsideHtml() { return this._state === 'in_artifact' || this._state === 'in_fence' || this._state === 'in_html'; }
}

class CanvasStudio {
    constructor() {
        this.projectId = null;
        this.projectName = '';
        this.evtSource = null;
        this.pages = {};             // { pageName: { html, status, size, index } }
        this.roundInfo = {};
        this.isGenerating = false;
        this._minimized = false;
        this._startTime = null;
        this._timerInterval = null;
        this.activeConversationId = '';
        this._chatAbortController = null;
        this._chatSending = false;
        this._selectedPage = null;   // 当前预览的页面
        this._chatExpanded = false;
        this._streamBubble = null;  // 当前流式输出的气泡元素
        this._userClosed = false;   // 用户主动关闭标记
        this._templateFrame = null; // 模板框架信息 { layoutType, filename }
        this._totalPlannedPages = 0;  // 计划生成的总页面数
        this._completedPages = 0;     // 已完成页面数
        this._pipelineState = {};     // 各阶段状态 { phaseKey: { status, label } }
        this._currentGeneratingPage = '';  // 当前正在生成的页面名
        this._livePreviewTimer = null;     // 实时预览节流定时器
        this._overlayUpdateTimer = null;   // 预览 Overlay 实时更新定时器
        this._htmlParser = new StreamingHtmlParser();  // Generator 状态机解析器
        this._liveHtml = '';               // 当前解析出的 HTML（用于串行模式实时渲染）
        this._rAFId = null;                // requestAnimationFrame ID
        this._fallbackTimer = null;        // setTimeout 回退 ID
        this._renderScheduled = false;     // 是否已调度渲染
        this._liveHtmlMap = {};            // 并行模式 per-page HTML
        this._renderTimersMap = {};        // 并行模式 per-page 渲染定时器

        // 无限画布状态
        this._canvasEngine = null;
        this._cardPositions = {};           // { pageName: { x, y } }
        this._connections = [];             // [{ from, to, label }]
        this._autoLayoutCursor = { col: 0, row: 0, maxCols: 3 };
        this._layoutSaveTimer = null;
    }

    // ==================== 生命周期 ====================

    open(projectId, projectName, formData) {
        this.projectId = projectId;
        this.projectName = projectName || '未命名项目';
        this.pages = {};
        this.isGenerating = true;
        this._selectedPage = null;
        this._chatExpanded = false;
        this.accumulatedContent = '';
        this._streamBubble = null;
        this._userClosed = false;
        this._templateFrame = null;
        this._totalPlannedPages = 0;
        this._completedPages = 0;
        this._pipelineState = {};
        this._currentGeneratingPage = '';
        this._livePreviewTimer = null;
        this._overlayUpdateTimer = null;

        const modal = document.getElementById('canvasStudioModal');
        if (modal) modal.classList.remove('hidden');

        this._initUI();
        this._startTimer();
        this._connectSSE();
    }

    close() {
        this._userClosed = true;  // 标记为用户主动关闭，防止 onerror 触发轮询
        if (this.evtSource) {
            this.evtSource.close();
            this.evtSource = null;
        }
        this._stopTimer();
        this._flushLivePreview();  // 清理实时预览定时器

        const modal = document.getElementById('canvasStudioModal');
        if (modal) modal.classList.add('hidden');

        this._closePreviewOverlay();
    }

    destroy() {
        this.close();
        this.projectId = null;
        this.pages = {};
    }

    /**
     * 重新打开 Canvas Studio（用于关闭后重新进入）
     * - 生成中的项目：重新连接 SSE 流
     * - 已完成的项目：从 API 加载页面列表，展示画布
     */
    async reopen(projectId) {
        const project = (typeof allProjects !== 'undefined')
            ? allProjects.find(p => p.id === projectId)
            : null;
        const projectName = project ? project.name : projectId;

        this.projectId = projectId;
        this.projectName = projectName;
        this.pages = {};
        this._selectedPage = null;
        this._chatExpanded = false;
        this.accumulatedContent = '';
        this._streamBubble = null;
        this._startTime = null;
        this._userClosed = false;
        this._templateFrame = null;

        const modal = document.getElementById('canvasStudioModal');
        if (modal) modal.classList.remove('hidden');

        this._initUI('Canvas Agent 已就绪，正在加载项目...');

        // 先加载已有页面（无论什么状态都尝试加载，给用户即时反馈）
        await this._loadExistingPages();

        // 加载已保存的画布布局（恢复卡片位置和视口状态）
        await this._loadLayout();

        // 再查询是否仍在生成
        try {
            const statusResp = await fetch('/api/generation-status?id=' + encodeURIComponent(projectId));
            const statusData = await statusResp.json();

            if (statusData.status === 'generating') {
                // 项目仍在生成，重新连接 SSE 接收后续事件
                this.isGenerating = true;
                this._appendChatBubble('system', '正在重新连接生成任务...');
                this._startTimer();
                this._connectSSE();
            } else {
                // 已完成/失败/停止，提示可用操作
                this._appendChatBubble('system', '你可以点击任意页面预览，或在下方输入修改指令。');
            }
        } catch (e) {
            console.error('[CanvasStudio] reopen status check failed:', e);
            this._appendChatBubble('system', '你可以点击任意页面预览，或在下方输入修改指令。');
        }
    }

    /**
     * 从 API 加载已有页面，渲染到画布上
     */
    async _loadExistingPages() {
        if (!this.projectId) return;

        try {
            const resp = await fetch('/api/pages?projectId=' + encodeURIComponent(this.projectId));
            const data = await resp.json();

            if (data.pages && data.pages.length > 0) {
                this._appendChatBubble('system', '已加载 ' + data.pages.length + ' 个页面');

                for (const page of data.pages) {
                    const pageName = page.name || page.label || '页面';
                    const pageIdx = page.index !== undefined ? page.index : 0;

                    this.pages[pageName] = {
                        html: null,
                        status: 'done',
                        index: pageIdx,
                        filename: page.filename
                    };

                    // 添加完成的页面卡片
                    this._addCompletedCard(pageName, pageIdx, page.filename);
                }

                // 检查是否有模板框架文件（仅当存在 template 目录时才检查）
                try {
                    const templateResp = await fetch('/projects/' + this.projectId + '/template/template.css', { method: 'HEAD' });
                    if (templateResp.ok) {
                        const frameResp = await fetch('/projects/' + this.projectId + '/template/frame.html', { method: 'HEAD' });
                        if (frameResp.ok) {
                            this._onTemplateFrame({
                                layout_type: 'unknown',
                                filename: 'template/frame.html'
                            });
                        }
                    }
                } catch (e) {
                    // 无模板框架，忽略
                }

                this._updateProgressBar(100);

                // 显示完成横幅
                const banner = document.getElementById('csCompleteBanner');
                if (banner) {
                    banner.querySelector('.cs-complete-title').textContent = '项目已加载';
                    banner.querySelector('.cs-complete-desc').textContent = '共 ' + data.pages.length + ' 个页面';
                    banner.classList.add('cs-visible');
                }

                // 隐藏空状态
                const empty = document.getElementById('csEmptyState');
                if (empty) empty.style.display = 'none';
            } else {
                this._appendChatBubble('system', '项目暂无页面数据');
            }
        } catch (e) {
            console.error('[CanvasStudio] load pages failed:', e);
        }
    }

    /**
     * 添加已完成的页面卡片（用于 reopen 场景）
     */
    _addCompletedCard(pageName, index, filename) {
        const world = document.getElementById('csCanvasWorld');
        if (!world) return;

        const cardId = this._getCardId(pageName);

        // 避免重复
        if (document.getElementById(cardId)) return;

        const empty = document.getElementById('csEmptyState');
        if (empty) empty.style.display = 'none';

        const pageUrl = '/projects/' + this.projectId + '/' + (filename || 'index.html');

        const card = document.createElement('div');
        card.id = cardId;
        card.className = 'cs-page-card';
        card.dataset.pageName = pageName;

        // 绝对定位
        const pos = this._getNextAutoLayoutPosition();
        this._cardPositions[pageName] = pos;
        card.style.left = pos.x + 'px';
        card.style.top = pos.y + 'px';

        card.innerHTML =
            '<div class="cs-card-header">' +
            '  <span class="cs-card-name">' + this._escapeHtml(pageName) + '</span>' +
            '  <span class="cs-card-status cs-status-done">' +
            '    <span class="cs-status-dot"></span>完成' +
            '  </span>' +
            '</div>' +
            '<div class="cs-card-thumb-wrap">' +
            '  <iframe src="' + pageUrl + '" sandbox="allow-same-origin allow-scripts" loading="lazy"></iframe>' +
            '</div>' +
            '<div class="cs-card-footer">' +
            '  <span>已完成</span>' +
            '  <span>#' + (index + 1) + '</span>' +
            '</div>' +
            '<div class="cs-card-actions">' +
            '  <button class="cs-action-btn cs-preview-btn" onclick="event.stopPropagation(); canvasStudio._openPreviewOverlay(\'' + this._escapeAttr(pageName) + '\')">' +
            '    <i class="fas fa-eye"></i> 预览' +
            '  </button>' +
            '  <button class="cs-action-btn cs-chat-btn" onclick="event.stopPropagation(); canvasStudio._openPreviewWithChat(\'' + this._escapeAttr(pageName) + '\')">' +
            '    <i class="fas fa-comments"></i> 对话' +
            '  </button>' +
            '</div>';

        card.addEventListener('click', () => this._onCardClick(pageName));
        this._makeCardDraggable(card, pageName);
        world.appendChild(card);
    }

    // ==================== UI 初始化 ====================

    _initUI(welcomeMsg) {
        // 项目名称
        const nameEl = document.getElementById('csProjectName');
        if (nameEl) nameEl.textContent = this.projectName;

        // 进度条
        const fill = document.getElementById('csProgressFill');
        if (fill) fill.style.width = '0%';

        // 重置管道可视化
        const pipeline = document.getElementById('csPipelineSteps');
        if (pipeline) {
            pipeline.innerHTML = '';
            pipeline.style.display = 'none';
        }

        // 清空画布
        const world = document.getElementById('csCanvasWorld');
        if (world) world.innerHTML = '';

        // 初始化/重置画布引擎
        const viewport = document.getElementById('csCanvasViewport');
        const svgGroup = document.getElementById('csConnectionLines');
        if (viewport && world) {
            if (!this._canvasEngine) {
                this._canvasEngine = new CanvasEngine(viewport, world, svgGroup);
                this._canvasEngine.onTransformChange((state) => {
                    const zoomEl = document.getElementById('csZoomLevel');
                    if (zoomEl) zoomEl.textContent = Math.round(state.scale * 100) + '%';
                });
                // 窗口 resize 时自动适配所有卡片
                this._canvasEngine.onResize = () => {
                    const positions = this._cardPositions;
                    if (positions && Object.keys(positions).length > 0) {
                        this._canvasEngine.fitAll(positions, 420, 420);
                    }
                };
            } else {
                this._canvasEngine.reset();
            }
        }

        // 重置画布状态
        this._cardPositions = {};
        this._connections = [];
        // 模板框架在 (40,40)，页面从第二列开始避免重叠
        this._autoLayoutCursor = { col: 1, row: 0, maxCols: 3 };

        // 显示空状态
        const empty = document.getElementById('csEmptyState');
        if (empty) empty.style.display = 'flex';

        // 隐藏完成横幅
        const banner = document.getElementById('csCompleteBanner');
        if (banner) banner.classList.remove('cs-visible');

        // 清空左侧对话
        const chatMsgs = document.getElementById('csChatMessages');
        if (chatMsgs) chatMsgs.innerHTML = '';

        // 初始系统消息
        this._appendChatBubble('system', welcomeMsg || 'Canvas Agent 已就绪，正在连接 AI...');

        // 清空预览
        this._closePreviewOverlay();
    }

    // ==================== SSE 连接 ====================

    _connectSSE() {
        if (!this.projectId) return;
        const self = this;

        try {
            const url = '/api/generation-stream?id=' + encodeURIComponent(this.projectId);
            this.evtSource = new EventSource(url);

            this.evtSource.onmessage = function(event) {
                self._handleSSEMessage(event);
            };

            this.evtSource.addEventListener('status', function(event) {
                self._handleSSEStatus(event);
            });

            this.evtSource.onerror = function() {
                console.log('[CanvasStudio] SSE 连接关闭');
                if (self.evtSource) {
                    self.evtSource.close();
                    self.evtSource = null;
                }
                // 只有在非主动关闭（用户没点关闭按钮）且正在生成时才降级到轮询
                if (self.isGenerating && !self._userClosed && typeof pollGenerationStatus === 'function') {
                    self._updatePhaseLabel('SSE 断开，轮询中...');
                    pollGenerationStatus(self.projectId);
                }
            };
        } catch (e) {
            console.error('[CanvasStudio] SSE 不支持:', e);
            if (typeof pollGenerationStatus === 'function') {
                pollGenerationStatus(this.projectId);
            }
        }
    }

    _handleSSEMessage(event) {
        try {
            const data = JSON.parse(event.data);

            // 连接确认
            if (data.type === 'connected') {
                console.log('[CanvasStudio] SSE 已连接');
                this._appendPhaseBubble('已连接，等待 AI 响应...', true);
                return;
            }

            // 结构化事件
            if (data.type && data.data) {
                console.log('[CanvasStudio] SSE 结构化事件:', data.type);
                this._handleStructuredEvent(data);
                return;
            }

            // 原始文本流
            if (data.content) {
                this.accumulatedContent += data.content;

                // 调试：每累积 500 字节打一次日志
                if (this.accumulatedContent.length % 500 < data.content.length + 10) {
                    console.log('[CanvasStudio] 累积内容:', this.accumulatedContent.length, '字节, parser状态:', this._htmlParser._state, 'liveHtml:', this._liveHtml.length);
                }

                // 用 Generator parser 解析（替代原来的 _extractPartialHtml 正则）
                if (this._currentGeneratingPage) {
                    for (const evt of this._htmlParser.feed(data.content)) {
                        this._handleParserEvent(evt);
                    }
                    // Fallback: parser 没检测到 HTML 或 HTML 太少时也调度渲染
                    // _doRender 会用 _extractPartialHtml 兜底提取更完整的内容
                    if (!this._liveHtml || this._liveHtml.length < 500 || !/<body/i.test(this._liveHtml)) {
                        this._scheduleRender(this._currentGeneratingPage);
                    }
                }

                this._showRawStream();
            }
        } catch (e) {
            console.error('[CanvasStudio] SSE 解析错误:', e, event.data);
        }
    }

    _handleSSEStatus(event) {
        try {
            const data = JSON.parse(event.data);

            if (data.status === 'completed') {
                if (this.evtSource) {
                    this.evtSource.close();
                    this.evtSource = null;
                }
                this._onGenerationComplete(data);
            } else if (data.status === 'failed') {
                if (this.evtSource) {
                    this.evtSource.close();
                    this.evtSource = null;
                }
                this._updatePhaseLabel('生成失败: ' + (data.error || '未知错误'));
                this.isGenerating = false;
                this._stopTimer();
                this._updateProjectStatus('failed');
                if (typeof showToast === 'function') {
                    showToast('"' + this.projectName + '" 生成失败: ' + (data.error || '未知错误'), 'error');
                }
            } else if (data.status === 'cancelled') {
                if (this.evtSource) {
                    this.evtSource.close();
                    this.evtSource = null;
                }
                this._updatePhaseLabel('生成已停止');
                this.isGenerating = false;
                this._stopTimer();
            }
        } catch (e) {
            console.error('[CanvasStudio] SSE status 解析错误:', e);
        }
    }

    // ==================== 结构化事件处理 ====================

    _handleStructuredEvent(event) {
        const type = event.type;
        const d = event.data;

        switch (type) {
            case 'phase':
                this._onPhaseEvent(d);
                break;
            case 'preview':
                this._onPagePreview(d);
                break;
            case 'complete':
                this._onPhaseComplete(d);
                break;
            case 'error':
                this._onErrorEvent(d);
                break;
            case 'page_written':
                this._onPageWritten(d);
                break;
            case 'preview_update':
                this._onPreviewUpdate(d);
                break;
            case 'edit_result':
                this._onEditResult(d);
                break;
            case 'tool_call_progress':
                this._onToolCallProgress(d, false);
                break;
            case 'streaming_html':
                this._onStreamingHtml(d);
                break;
            case 'stream_progress':
                this._onStreamProgress(d);
                break;
            case 'artifact':
            case 'chat':
                // 多轮生成时，chat 事件包含 AI 流式输出的文本片段
                if (d && d.content) {
                    this.accumulatedContent += d.content;
                    // 用 Generator parser 解析
                    if (this._currentGeneratingPage) {
                        for (const evt of this._htmlParser.feed(d.content)) {
                            this._handleParserEvent(evt);
                        }
                        // Fallback: parser 没检测到 HTML 或 HTML 太少时也调度渲染
                        // _doRender 会用 _extractPartialHtml 兜底提取更完整的内容
                        if (!this._liveHtml || this._liveHtml.length < 500 || !/<body/i.test(this._liveHtml)) {
                            this._scheduleRender(this._currentGeneratingPage);
                        }
                    }
                    this._showRawStream();
                }
                break;
            case 'diagnostic':
                // 不需要画布级别处理的静默事件
                break;
            case 'template_frame':
                this._onTemplateFrame(d);
                break;
        }
    }

    _onPhaseEvent(data) {
        this.roundInfo = {
            round: data.round,
            step: data.step,
            label: data.label || ''
        };

        if (data.status === 'running') {
            // 新阶段开始，重置流式输出
            // 先完成或移除旧的流式气泡，避免重复创建
            if (this._streamBubble && this._streamBubble.parentNode) {
                this._streamBubble.remove();
            }
            this.accumulatedContent = '';
            this._streamBubble = null;

            let label = '';
            let pipelineKey = '';
            let isParallelPage = false;
            if (data.round === 0) {
                label = '分析项目需求...';
                pipelineKey = 'spec';
            } else if (data.round === 1) {
                label = '创建设计系统...';
                pipelineKey = 'design';
            } else if (data.round === 2) {
                // 记录计划总页面数
                if (data.progress && data.progress.total) {
                    this._totalPlannedPages = data.progress.total;
                }
                const progress = data.progress ? ' (' + data.progress.current + '/' + data.progress.total + ')' : '';
                label = '生成页面: ' + (data.label || '页面') + progress;
                pipelineKey = 'page_' + (data.label || '');
                // 检测是否已有其他页面在 running（并行模式）
                const hasRunningPage = Object.entries(this._pipelineState || {})
                    .some(([k, v]) => k.startsWith('page_') && v.status === 'running' && k !== pipelineKey);
                isParallelPage = hasRunningPage;
                if (data.label) {
                    this._addPlaceholderCard(data.label);
                    if (!hasRunningPage) {
                        // 串行模式：设置当前页面，重置 parser
                        this._currentGeneratingPage = data.label;
                        this._htmlParser.reset();
                        this._liveHtml = '';
                    }
                    // 并行模式：不覆盖 _currentGeneratingPage，不重置 parser
                    // per-page HTML 通过 _liveHtmlMap 独立管理
                }
            } else if (data.round === 3) {
                label = '组装多页导航...';
                pipelineKey = 'assembly';
            }
            if (label) {
                // 并行模式下不自动关闭其他页面的 loading 气泡
                // round 2 页面气泡附带 page 属性，供 stream_progress 就近更新
                const pageAttr = (data.round === 2 && data.label) ? data.label : null;
                this._appendPhaseBubble(label, this.isGenerating, isParallelPage, pageAttr);
            }

            // 更新管道可视化状态
            if (pipelineKey) {
                this._updatePipelineState(pipelineKey, 'running', label || data.label || data.step);
            }

            // 统一进度更新
            const pct = this._calcUnifiedProgress(data.round, data.progress);
            this._updateProgressBar(pct);
        }

        if (data.status === 'done') {
            // 阶段完成，更新管道
            if (data.round === 2 && data.label) {
                this._completedPages++;
                this._updatePipelineState('page_' + data.label, 'done', data.label);
                // 清除当前生成页面标记
                if (this._currentGeneratingPage === data.label) {
                    this._closeStreamDoc();  // 关闭流式文档
                    this._currentGeneratingPage = '';
                    this._flushLivePreview();  // 最后一次渲染
                }
                // 页面完成时清除流式气泡
                if (this._streamBubble && this._streamBubble.parentNode) {
                    this._streamBubble.remove();
                    this._streamBubble = null;
                }
            } else if (data.round === 0) {
                this._updatePipelineState('spec', 'done', '分析需求');
            } else if (data.round === 1) {
                this._updatePipelineState('design', 'done', '设计系统');
            } else if (data.round === 3) {
                this._updatePipelineState('assembly', 'done', '组装导航');
            }

            // 用已完成页面数计算更精确的进度
            if (data.round === 2 && this._totalPlannedPages > 0) {
                const pct = 10 + (this._completedPages / this._totalPlannedPages) * 75;
                this._updateProgressBar(pct);
            } else if (data.round === 3) {
                this._updateProgressBar(95);
            }
        }
    }

    _onPagePreview(data) {
        if (!data.page || !data.html_fragment) return;

        // 隐藏空状态
        const empty = document.getElementById('csEmptyState');
        if (empty) empty.style.display = 'none';

        // 存储页面数据
        const existingData = this.pages[data.page];
        const pageIdx = existingData ? existingData.index : Object.keys(this.pages).length;
        this.pages[data.page] = {
            html: data.html_fragment,
            status: 'done',
            size: existingData ? existingData.size : 0,
            index: pageIdx,
            filename: existingData && existingData.filename
                ? existingData.filename
                : ('pages/page_' + pageIdx + '_' + data.page + '.html')
        };

        // 更新卡片
        this._updatePageCard(data.page, data.html_fragment);

        // 更新进度条（使用计划总页面数）
        const total = this._totalPlannedPages || Object.keys(this.pages).length;
        if (total > 0) {
            const donePages = Object.values(this.pages).filter(p => p.status === 'done' || p.status === 'written').length;
            const pct = 10 + (donePages / total) * 75;
            this._updateProgressBar(Math.min(95, pct));
        }

        console.log('[CanvasStudio] 页面预览:', data.page);
    }

    // ==================== 流式 HTML 渐进渲染 ====================

    _handleParserEvent(evt) {
        switch (evt.type) {
            case 'text':
                // 非 HTML 文本，忽略
                break;
            case 'html:start':
                console.log('[CanvasStudio] parser: html:start, parser state:', this._htmlParser._state);
                this._liveHtml = '';
                break;
            case 'html:chunk':
                this._liveHtml += evt.delta;
                // 每累积 500 字节打一次日志（避免刷屏）
                if (this._liveHtml.length % 500 < evt.delta.length) {
                    console.log('[CanvasStudio] parser: html:chunk 累计:', this._liveHtml.length, 'parser state:', this._htmlParser._state);
                }
                this._scheduleRender(this._currentGeneratingPage);
                break;
            case 'html:end':
                console.log('[CanvasStudio] parser: html:end, 长度:', (evt.fullContent || this._liveHtml).length);
                this._liveHtml = evt.fullContent || this._liveHtml;
                this._scheduleRender(this._currentGeneratingPage);
                break;
        }
    }

    _onStreamingHtml(data) {
        // 服务端预提取的 streaming_html 事件
        if (!data.html || data.html.length < 50) return;
        const pageName = data.page || this._currentGeneratingPage;
        if (!pageName) return;

        // 串行模式 + parser 活跃时，让 parser 优先（避免双路径闪烁）
        const isSerialWithParser = (pageName === this._currentGeneratingPage
                                    && this._htmlParser.isInsideHtml);
        if (isSerialWithParser) return;

        // 其余情况（并行页面、串行 parser 未激活时）都走 per-page 渲染
        this._liveHtmlMap[pageName] = data.html;
        this._scheduleRenderParallel(pageName);
    }

    _scheduleRenderParallel(pageName) {
        // 取消该页面之前的定时器
        if (this._renderTimersMap[pageName]) {
            clearTimeout(this._renderTimersMap[pageName]);
        }
        // 节流调度：800ms 间隔（与串行模式一致）
        this._renderTimersMap[pageName] = setTimeout(() => {
            delete this._renderTimersMap[pageName];
            this._doRenderParallel(pageName);
        }, 800);
    }

    _doRenderParallel(pageName) {
        const html = this._liveHtmlMap[pageName];
        if (!html || html.length < 50) return;

        const renderableHtml = this._makeRenderableHtml(html);
        console.log('[CanvasStudio] _doRenderParallel:', pageName,
                    'html长度:', html.length);
        this._updateCardThumb(pageName, renderableHtml);

        if (this.pages[pageName]) {
            this.pages[pageName].html = renderableHtml;
        }
    }

    _onStreamProgress(data) {
        // 并行 worker 的 per-page 流式进度
        console.log('[CanvasStudio] stream_progress received:', data);
        if (!data.page) return;
        const kb = (data.size / 1024).toFixed(1);
        const msgs = document.getElementById('csChatMessages');
        if (!msgs) return;

        // 优先：找到对应页面的阶段气泡，就地更新文本（内联显示进度）
        const phaseBubble = msgs.querySelector(
            `.cs-chat-phase[data-phase-page="${data.page}"]`);
        if (phaseBubble && phaseBubble.classList.contains('cs-phase-loading')) {
            const baseText = phaseBubble.getAttribute('data-base-text') || '';
            if (baseText) {
                phaseBubble.innerHTML = '<span class="cs-phase-spinner-inline"></span>'
                    + this._escapeHtml(baseText)
                    + ' <span style="opacity:0.6;font-size:0.85em">· 生成中 '
                    + kb + 'KB</span>';
            }
            return;
        }

        // 兜底：如果阶段气泡已不存在（如已完成），跳过
        // 不再创建独立气泡，避免与阶段气泡脱节
    }

    _scheduleRender(pageName) {
        if (this._renderScheduled) return;

        // 最小间隔控制：距上次渲染不足 150ms 时延迟调度
        // 参考 Open Design 的 debounced rebuild 策略
        const now = Date.now();
        const elapsed = now - (this._lastRenderTime || 0);
        const MIN_INTERVAL = 800;  // 增量 body 更新间隔，减少闪烁

        const doSchedule = () => {
            this._renderScheduled = true;
            // rAF 优先（与浏览器渲染同步）
            this._rAFId = requestAnimationFrame(() => {
                this._rAFId = null;
                if (this._fallbackTimer) { clearTimeout(this._fallbackTimer); this._fallbackTimer = null; }
                this._renderScheduled = false;
                this._lastRenderTime = Date.now();
                this._doRender(pageName);
            });

            // setTimeout 回退（标签页不可见时 rAF 暂停）
            this._fallbackTimer = setTimeout(() => {
                this._fallbackTimer = null;
                if (this._rAFId) { cancelAnimationFrame(this._rAFId); this._rAFId = null; }
                this._renderScheduled = false;
                this._lastRenderTime = Date.now();
                this._doRender(pageName);
            }, 900);
        };

        if (elapsed < MIN_INTERVAL) {
            // 延迟到满足最小间隔后再调度
            this._renderScheduled = true;
            this._delayTimer = setTimeout(() => {
                this._delayTimer = null;
                this._renderScheduled = false;
                doSchedule();
            }, MIN_INTERVAL - elapsed);
        } else {
            doSchedule();
        }
    }

    _doRender(pageName) {
        let html = this._liveHtml;
        let usedFallback = false;

        // Fallback 1: parser 没提取到 HTML 时
        if ((!html || html.length < 50) && this.accumulatedContent && this.accumulatedContent.length > 100) {
            html = this._extractPartialHtml(this.accumulatedContent);
            usedFallback = true;
        }

        // Fallback 2: parser 提取的 HTML 太少（没有 body 内容，不可见），尝试从完整累积内容提取
        if (!usedFallback && html && html.length < 500 && !/<body/i.test(html) && this.accumulatedContent && this.accumulatedContent.length > html.length * 3) {
            const extracted = this._extractPartialHtml(this.accumulatedContent);
            if (extracted && extracted.length > html.length) {
                html = extracted;
                usedFallback = true;
            }
        }

        if (!html || html.length < 50) return;

        const renderableHtml = this._makeRenderableHtml(html);
        console.log('[CanvasStudio] _doRender:', pageName, 'html长度:', html.length, 'renderable长度:', renderableHtml.length, 'fallback:', usedFallback, 'hasBody:', /<body/i.test(html));
        this._updateCardThumb(pageName, renderableHtml);

        if (this.pages[pageName]) {
            this.pages[pageName].html = renderableHtml;
        }
    }

    _cancelScheduledRender() {
        if (this._rAFId) { cancelAnimationFrame(this._rAFId); this._rAFId = null; }
        if (this._fallbackTimer) { clearTimeout(this._fallbackTimer); this._fallbackTimer = null; }
        if (this._delayTimer) { clearTimeout(this._delayTimer); this._delayTimer = null; }
        this._renderScheduled = false;
    }

    // ==================== 实时渲染推流 ====================

    /**
     * 注入到页面中的增量更新监听器。
     *
     * 不需要任何外部库。
     * - cs-body-replace: 全量替换 body 内容（保持正确的 DOM 嵌套和布局）
     *   CSS/Tailwind 已在首次 srcdoc 加载，替换 innerHTML 不会重新加载资源
     */
    _getIncrementalScript() {
        return '<script data-incremental-listener>'
            // 注入布局过渡 CSS：将 innerHTML 替换时的布局跳变变为平滑动画
            + 'var cs=document.createElement("style");'
            + 'cs.textContent="body>*{transition:width .2s ease,flex .2s ease,flex-basis .2s ease,margin .2s ease,padding .2s ease,transform .2s ease,top .2s ease,left .2s ease,right .2s ease,bottom .2s ease}";'
            + 'document.head.appendChild(cs);'
            // 队列：body 尚未就绪时暂存
            + 'window._csPending=null;'
            + 'window.addEventListener("message",function(e){'
            + 'if(!e.data||e.data.type!=="cs-body-replace")return;'
            + 'var b=document.body;'
            + 'if(b){'
            // 用 rAF 同步浏览器渲染帧，减少闪烁
            + 'requestAnimationFrame(function(){b.innerHTML=e.data.body;});'
            + '}else{'
            + 'window._csPending=e.data.body;'
            + '}'
            + '},false);'
            // DOMContentLoaded 时冲刷暂存
            + 'document.addEventListener("DOMContentLoaded",function(){'
            + 'if(window._csPending!=null&&document.body){'
            + 'document.body.innerHTML=window._csPending;'
            + 'window._csPending=null;'
            + '}'
            + '},false);'
            + '<\/script>';
    }

    /**
     * 从原始 HTML 中提取 body 内部内容（不含 <body> 标签本身）。
     */
    _extractRawBodyInner(html) {
        const m = html.match(/<body[^>]*>([\s\S]*?)(?:<\/body>|$)/i);
        return m ? m[1] : '';
    }

    /**
     * 推送 HTML 到 iframe。
     *
     * 核心策略：
     * - 首次：srcdoc 加载完整页面（CDN 资源只加载一次）
     * - 后续：通过 postMessage 全量替换 body innerHTML
     *   保持正确的 DOM 嵌套 → 布局正确
     *   不重载 srcdoc → CDN 资源不重新请求 → 无重载闪烁
     *   requestAnimationFrame 同步 → 最小化 DOM 重建的视觉影响
     */
    _pushHtmlToIframe(iframe, html) {
        // 去重
        if (iframe._lastPushedHtml === html) return;
        iframe._lastPushedHtml = html;

        // 阶段 1：首次完整加载
        if (!iframe._pageLoaded) {
            const hasBody = /<body/i.test(html);
            if (!hasBody) return;

            // 注入监听器到 <head>
            let srcHtml = html;
            const script = this._getIncrementalScript();
            if (/<head[^>]*>/i.test(srcHtml)) {
                srcHtml = srcHtml.replace(/(<head[^>]*>)/i, '$1' + script);
            } else {
                srcHtml = script + srcHtml;
            }

            iframe.srcdoc = srcHtml;
            iframe._pageLoaded = true;
            console.log('[CanvasStudio] 首次完整加载, html长度:', html.length);
            return false; // Phase 1，骨架屏保留
        }

        // 阶段 2：全量替换 body（保持布局正确）
        const rawBody = this._extractRawBodyInner(html);
        if (!rawBody || rawBody.length < 10) return false;

        try {
            iframe.contentWindow.postMessage({
                type: 'cs-body-replace',
                body: rawBody
            }, '*');
        } catch (e) {
            // 降级：srcdoc 全量替换
            iframe.srcdoc = html;
        }
        return true; // Phase 2，有增量数据推送
    }

    /**
     * 统一的卡片缩略图更新方法
     */
    _updateCardThumb(pageName, html) {
        if (!html || html.length < 50) return;

        const cardId = this._getCardId(pageName);
        const card = document.getElementById(cardId);
        if (!card) return;

        const thumbWrap = card.querySelector('.cs-card-thumb-wrap');
        if (!thumbWrap) return;

        // 获取或创建 iframe
        let iframe = thumbWrap.querySelector('iframe');
        if (!iframe) {
            // 保留骨架屏，iframe 叠加在上面（初始隐藏）
            // 只移除旧的截图
            const snapshots = thumbWrap.querySelectorAll('.cs-thumb-snapshot');
            snapshots.forEach(el => el.remove());

            iframe = document.createElement('iframe');
            iframe.className = 'cs-card-thumb cs-live-rendering cs-init-loading';
            iframe.setAttribute('sandbox', 'allow-same-origin allow-scripts');
            iframe._pageLoaded = false;
            iframe._sentBodyLen = 0;
            iframe._skeletonRemoved = false;

            thumbWrap.appendChild(iframe);

            thumbWrap.classList.add('cs-thumb-live');
            thumbWrap.classList.remove('cs-thumb-done');
        }

        // 推送 HTML，返回 true 表示有增量 body 数据推送
        const bodyPushed = this._pushHtmlToIframe(iframe, html);

        // 第一次增量推送 body 时：移除骨架屏、显示 iframe 和指示器
        if (bodyPushed && !iframe._skeletonRemoved) {
            iframe._skeletonRemoved = true;
            iframe.classList.remove('cs-init-loading');

            // 淡出骨架屏
            const skeleton = thumbWrap.querySelector('.cs-card-loading');
            if (skeleton) {
                skeleton.style.transition = 'opacity 0.4s ease';
                skeleton.style.opacity = '0';
                setTimeout(() => skeleton.remove(), 400);
            }

            // 此时才添加实时渲染指示器
            if (!thumbWrap.querySelector('.cs-live-indicator')) {
                const indicator = document.createElement('div');
                indicator.className = 'cs-live-indicator';
                indicator.innerHTML = '<span class="cs-live-dot"></span>实时渲染中';
                thumbWrap.appendChild(indicator);
            }

            console.log('[CanvasStudio] 增量数据推送中，骨架屏隐藏');
        }
    }

    /**
     * 将流式渲染的卡片转为最终态（页面生成完成时调用）
     */
    _finalizeCardThumb(pageName) {
        const cardId = this._getCardId(pageName);
        const card = document.getElementById(cardId);
        if (!card) return;

        const thumbWrap = card.querySelector('.cs-card-thumb-wrap');
        if (!thumbWrap) return;

        // 移除实时渲染指示器
        const indicator = thumbWrap.querySelector('.cs-live-indicator');
        if (indicator) indicator.remove();

        // 将 iframe 标记为完成态
        const iframe = thumbWrap.querySelector('iframe');
        if (iframe) {
            iframe.classList.remove('cs-live-rendering');
            iframe._lastPushedHtml = null;
            iframe._sentBodyLen = 0;
        }

        thumbWrap.classList.remove('cs-thumb-live');
        thumbWrap.classList.add('cs-thumb-done');
    }


    _onPageWritten(data) {
        if (!data.page) return;

        // 页面写入完成：执行最后一次渲染
        // 优先检查 per-page 路径（并行页面可能在 _currentGeneratingPage 中）
        if (this._liveHtmlMap[data.page] !== undefined) {
            // 并行模式：取消该页面的定时器并执行最终渲染
            if (this._renderTimersMap[data.page]) {
                clearTimeout(this._renderTimersMap[data.page]);
                delete this._renderTimersMap[data.page];
            }
            this._doRenderParallel(data.page);
        } else if (this._currentGeneratingPage === data.page) {
            // 串行模式：flush parser 并渲染
            for (const evt of this._htmlParser.flush()) {
                this._handleParserEvent(evt);
            }
            this._cancelScheduledRender();
            this._doRender(data.page);
        }

        // 更新页面元数据（不碰 DOM，避免和 preview 事件冲突导致闪烁）
        if (this.pages[data.page]) {
            this.pages[data.page].status = 'written';
            this.pages[data.page].size = data.size || 0;
            this.pages[data.page].index = data.index;
        } else {
            this.pages[data.page] = {
                html: '',
                status: 'written',
                size: data.size || 0,
                index: data.index
            };
        }

        // 在左侧面板记录完成
        // 并行模式下不自动关闭其他页面的 loading 气泡
        const hasOtherRunning = Object.entries(this._pipelineState || {})
            .some(([k, v]) => k.startsWith('page_') && v.status === 'running'
                    && v.label !== data.page);
        // 先显式关闭该页面的阶段 loading 气泡（恢复 base-text）
        const msgs = document.getElementById('csChatMessages');
        if (msgs) {
            const phaseBubble = msgs.querySelector(
                `.cs-chat-phase[data-phase-page="${data.page}"]`);
            if (phaseBubble && phaseBubble.classList.contains('cs-phase-loading')) {
                phaseBubble.classList.remove('cs-phase-loading');
                phaseBubble.classList.add('cs-phase-done');
                const baseText = phaseBubble.getAttribute('data-base-text') || '';
                phaseBubble.innerHTML = '<span class="cs-phase-done-icon">'
                    + '<i class="fas fa-check-circle"></i></span>'
                    + this._escapeHtml(baseText);
            }
        }
        this._appendPhaseBubble(
            '页面完成: ' + data.page + ' (' + this._formatBytes(data.size) + ')',
            false, hasOtherRunning);

        // 定稿卡片缩略图
        this._finalizeCardThumb(data.page);

        // 更新总进度条
        const total = this._totalPlannedPages || Object.keys(this.pages).length;
        if (total > 0) {
            const donePages = Object.values(this.pages).filter(p => p.status === 'done' || p.status === 'written').length;
            const pct = 10 + (donePages / total) * 80;
            this._updateProgressBar(Math.min(95, pct));
        }

        console.log('[CanvasStudio] 页面已写入:', data.page, this._formatBytes(data.size));

        // 清理并行模式的 per-page 状态
        delete this._liveHtmlMap[data.page];
        delete this._renderTimersMap[data.page];
    }

    _onPhaseComplete(data) {
        this._updatePhaseLabel('生成完成!');
        this._updateProgressBar(100);

        // 清除流式气泡
        if (this._streamBubble && this._streamBubble.parentNode) {
            this._streamBubble.remove();
            this._streamBubble = null;
        }

        this._updatePipelineState('assembly', 'done', '组装导航');
    }

    _onErrorEvent(data) {
        if (data.page) {
            this._updateCardStatus(data.page, 'error', null, data.message);
        } else {
            this._updatePhaseLabel('错误: ' + (data.message || '未知错误'));
        }
    }

    _onTemplateFrame(data) {
        if (!data || !data.filename) return;

        // 存储模板框架信息
        this._templateFrame = {
            layoutType: data.layout_type || 'plain',
            filename: data.filename
        };

        // 在画布最前面添加模板框架卡片
        this._addTemplateFrameCard(data);
    }

    _onPreviewUpdate(data) {
        // 对话修改后，刷新对应的卡片缩略图
        if (data && data.page && this.pages[data.page]) {
            // 重新加载页面内容
            this._reloadPageThumbnail(data.page);
        }
    }

    _onEditResult(data) {
        if (!data) return;
        // 显示编辑结果到聊天
        const msg = data.mode === 'full_html'
            ? '完整页面已重新生成'
            : (data.applied || 0) + '/' + ((data.applied || 0) + (data.failed || 0)) + ' 处修改已应用';
        this._appendChatBubble('system', msg);
    }

    // ==================== 生成完成 ====================

    _onGenerationComplete(data) {
        this.isGenerating = false;
        this._stopTimer();
        this._updatePhaseLabel('生成完成');
        this._updateProgressBar(100);

        // 完成所有管道阶段
        Object.keys(this._pipelineState || {}).forEach(key => {
            this._pipelineState[key].status = 'done';
        });
        this._renderPipeline();

        // 清理流式进度指示器，替换为完成消息
        if (this._streamBubble) {
            const totalPages = Object.keys(this.pages).length;
            this._streamBubble.className = 'cs-chat-bubble cs-chat-assistant';
            this._streamBubble.innerHTML = this._renderMarkdown(
                '页面生成完成! 共 ' + (totalPages || 1) + ' 个页面。点击任意页面卡片预览，或在下方输入修改指令。'
            );
            this._streamBubble = null;
        }

        // 更新所有卡片状态为完成
        Object.keys(this.pages).forEach(name => {
            this._updateCardStatus(name, 'done', this.pages[name].size);
        });

        // 显示完成横幅
        const banner = document.getElementById('csCompleteBanner');
        if (banner) {
            const totalPages = Object.keys(this.pages).length;
            banner.querySelector('.cs-complete-title').textContent = '生成完成';
            banner.querySelector('.cs-complete-desc').textContent = '共 ' + totalPages + ' 个页面已生成';
            banner.classList.add('cs-visible');
        }

        // 更新项目列表
        this._updateProjectStatus(null);

        if (typeof showToast === 'function') {
            showToast('"' + this.projectName + '" 生成完成!', 'success');
        }

        // 添加系统消息
        this._appendChatBubble('system', '页面生成完成！你可以点击任意页面预览，或在下方输入修改指令。');
    }

    // ==================== 卡片渲染 ====================

    _addTemplateFrameCard(data) {
        const world = document.getElementById('csCanvasWorld');
        if (!world) return;

        const cardId = 'cs-template-frame-card';
        if (document.getElementById(cardId)) return;

        const empty = document.getElementById('csEmptyState');
        if (empty) empty.style.display = 'none';

        const frameUrl = '/projects/' + this.projectId + '/' + (data.filename || 'template/frame.html');

        const card = document.createElement('div');
        card.id = cardId;
        card.className = 'cs-page-card cs-template-frame-card';
        card.dataset.pageName = '__template_frame__';

        // 固定在左上角
        card.style.left = '40px';
        card.style.top = '40px';
        this._cardPositions['__template_frame__'] = { x: 40, y: 40 };

        card.innerHTML =
            '<div class="cs-card-header">' +
            '  <span class="cs-card-name"><i class="fas fa-columns" style="margin-right:6px;font-size:11px;color:#818cf8"></i>模板框架</span>' +
            '  <span class="cs-card-status cs-status-frame">' +
            '    <span class="cs-status-dot"></span>固定' +
            '  </span>' +
            '</div>' +
            '<div class="cs-card-thumb-wrap">' +
            '  <iframe src="' + frameUrl + '" sandbox="allow-same-origin allow-scripts" loading="lazy"></iframe>' +
            '</div>' +
            '<div class="cs-card-footer">' +
            '  <span>模板框架</span>' +
            '  <span>' + this._escapeHtml(data.layout_type || 'unknown') + '</span>' +
            '</div>';

        card.addEventListener('click', () => this._onCardClick('__template_frame__'));

        world.appendChild(card);
    }

    _addPlaceholderCard(pageName) {
        const cardId = this._getCardId(pageName);
        if (document.getElementById(cardId)) return; // 已存在

        const empty = document.getElementById('csEmptyState');
        if (empty) empty.style.display = 'none';

        const world = document.getElementById('csCanvasWorld');
        if (!world) return;

        const card = document.createElement('div');
        card.id = cardId;
        card.className = 'cs-page-card cs-card-new';
        card.dataset.pageName = pageName;

        // 绝对定位
        const pos = this._getNextAutoLayoutPosition();
        this._cardPositions[pageName] = pos;
        card.style.left = pos.x + 'px';
        card.style.top = pos.y + 'px';

        card.innerHTML =
            '<div class="cs-card-header">' +
            '  <span class="cs-card-name">' + this._escapeHtml(pageName) + '</span>' +
            '  <span class="cs-card-status cs-status-generating">' +
            '    <span class="cs-status-dot"></span>生成中' +
            '  </span>' +
            '</div>' +
            '<div class="cs-card-thumb-wrap">' +
            '  <div class="cs-card-loading">' +
            '    <div class="cs-skeleton cs-skeleton-title"></div>' +
            '    <div class="cs-skeleton cs-skeleton-block"></div>' +
            '    <div class="cs-skeleton cs-skeleton-line"></div>' +
            '    <div class="cs-skeleton cs-skeleton-short"></div>' +
            '    <div class="cs-loading-ai-hint">' +
            '      <span class="cs-ai-dot"></span>' +
            '      <span class="cs-ai-dot"></span>' +
            '      <span class="cs-ai-dot"></span>' +
            '      <span>AI 正在生成...</span>' +
            '    </div>' +
            '  </div>' +
            '</div>' +
            '<div class="cs-card-footer">' +
            '  <span>等待中</span>' +
            '  <span></span>' +
            '</div>';

        // 初始化页面数据，确保点击卡片时能打开预览
        if (!this.pages[pageName]) {
            this.pages[pageName] = {
                html: null,
                status: 'generating',
                size: 0,
                index: Object.keys(this.pages).length
            };
        }

        card.addEventListener('click', () => this._onCardClick(pageName));
        this._makeCardDraggable(card, pageName);
        world.appendChild(card);

        // 摄像机跟随新卡片
        if (this._canvasEngine) {
            this._canvasEngine.centerOn(pos.x + 210, pos.y + 210, true);
        }
    }

    _updatePageCard(pageName, htmlFragment) {
        const cardId = this._getCardId(pageName);
        let card = document.getElementById(cardId);

        if (!card) {
            this._addPlaceholderCard(pageName);
            card = document.getElementById(cardId);
            if (!card) return;
        }

        const thumbWrap = card.querySelector('.cs-card-thumb-wrap');
        if (!thumbWrap) return;

        // 清除流式渲染内容，使用干净的最终 iframe
        thumbWrap.innerHTML = '';
        thumbWrap.classList.remove('cs-thumb-live');
        thumbWrap.classList.add('cs-thumb-done');

        const renderableHtml = this._makeRenderableHtml(htmlFragment);

        const iframe = document.createElement('iframe');
        iframe.className = 'cs-card-thumb';
        iframe.setAttribute('sandbox', 'allow-same-origin allow-scripts');

        // 先隐藏，load 完再显示（避免闪烁）
        iframe.style.opacity = '0';
        iframe.onload = () => {
            iframe.style.opacity = '1';
            iframe.style.transition = 'opacity 0.2s ease';
        };
        iframe.srcdoc = renderableHtml;
        thumbWrap.appendChild(iframe);

        card.classList.add('cs-card-rendered');
        this._updateCardStatus(pageName, 'done', this.pages[pageName] ? this.pages[pageName].size : 0);
        this._addCardActions(card, pageName);
    }

    _updateCardStatus(pageName, status, size, errorMsg) {
        const cardId = this._getCardId(pageName);
        const card = document.getElementById(cardId);
        if (!card) return;

        const statusEl = card.querySelector('.cs-card-status');
        if (!statusEl) return;

        if (status === 'done') {
            statusEl.className = 'cs-card-status cs-status-done';
            statusEl.innerHTML = '<span class="cs-status-dot"></span>完成';
        } else if (status === 'error') {
            statusEl.className = 'cs-card-status cs-status-error';
            statusEl.innerHTML = '<i class="fas fa-exclamation-triangle" style="font-size:10px"></i> ' + (errorMsg || '错误');
        } else {
            statusEl.className = 'cs-card-status cs-status-generating';
            statusEl.innerHTML = '<span class="cs-status-dot"></span>生成中';
        }

        // 更新底部
        const footer = card.querySelector('.cs-card-footer');
        if (footer) {
            const sizeSpan = footer.children[1];
            if (sizeSpan && size) {
                sizeSpan.textContent = this._formatBytes(size);
            }
            const statusSpan = footer.children[0];
            if (statusSpan) {
                statusSpan.textContent = status === 'done' ? '已完成' : (status === 'error' ? '失败' : '生成中');
            }
        }
    }

    _addCardActions(card, pageName) {
        // 移除旧的操作按钮
        const oldActions = card.querySelector('.cs-card-actions');
        if (oldActions) oldActions.remove();

        const actions = document.createElement('div');
        actions.className = 'cs-card-actions';
        actions.innerHTML =
            '<button class="cs-action-btn cs-preview-btn" data-action="preview">' +
            '  <i class="fas fa-eye"></i> 预览' +
            '</button>' +
            '<button class="cs-action-btn cs-chat-btn" data-action="chat">' +
            '  <i class="fas fa-comment"></i> 对话修改' +
            '</button>';

        actions.querySelector('[data-action="preview"]').addEventListener('click', (e) => {
            e.stopPropagation();
            this._openPreviewOverlay(pageName, false);
        });

        actions.querySelector('[data-action="chat"]').addEventListener('click', (e) => {
            e.stopPropagation();
            this._openPreviewOverlay(pageName, true);
        });

        card.appendChild(actions);
    }

    _onCardClick(pageName) {
        if (pageName === '__template_frame__') {
            this._openTemplateFramePreview();
        } else {
            this._openPreviewOverlay(pageName, false);
        }
    }

    // ==================== 预览 Overlay ====================

    _openPreviewOverlay(pageName, openChat) {
        this._selectedPage = pageName;

        const overlay = document.getElementById('csPreviewOverlay');
        if (!overlay) return;

        const titleEl = overlay.querySelector('.cs-preview-title span');
        if (titleEl) titleEl.textContent = pageName;

        // 加载 iframe
        const frame = document.getElementById('csPreviewFrame');
        if (frame) {
            const pageData = this.pages[pageName];
            if (pageData && pageData.filename) {
                // 从 API 返回的 filename 加载（reopen 场景）
                frame.src = '/projects/' + this.projectId + '/' + pageData.filename;
            } else if (pageData && pageData.status === 'written') {
                // 页面已写入磁盘，优先加载组装后的完整页面
                if (this._templateFrame) {
                    frame.src = '/projects/' + this.projectId + '/index.html';
                } else {
                    frame.src = '/projects/' + this.projectId + '/pages/page_' + pageData.index + '_' + encodeURIComponent(pageName) + '.html';
                }
            } else if (pageData && pageData.html) {
                // 生成中显示原始 AI 输出
                frame.srcdoc = this._makeRenderableHtml(pageData.html);
            } else if (pageData && pageData.status === 'generating' && this.accumulatedContent) {
                // 生成中，使用当前累积的内容尝试渲染预览
                const partialHtml = this._extractPartialHtml(this.accumulatedContent);
                if (partialHtml) {
                    frame.srcdoc = this._makeRenderableHtml(partialHtml);
                } else {
                    frame.srcdoc = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:#909399;font-family:sans-serif;">正在生成中...</div>';
                }
            }
        }

        overlay.classList.add('cs-preview-visible');

        // 显示页面对话切换按钮
        const chatToggle = document.getElementById('csPreviewChatToggle');
        if (chatToggle) {
            chatToggle.style.display = 'flex';
            // 移除旧的监听器，防止重复
            chatToggle.onclick = () => {
                const chat = document.getElementById('csPreviewChat');
                if (chat) {
                    if (chat.classList.contains('cs-chat-open')) {
                        chat.classList.remove('cs-chat-open');
                    } else {
                        chat.classList.add('cs-chat-open');
                        this._loadPageChatHistory(pageName);
                    }
                }
            };
        }

        // 打开或关闭侧边聊天
        const chat = document.getElementById('csPreviewChat');
        if (chat) {
            if (openChat) {
                chat.classList.add('cs-chat-open');
                this._loadPageChatHistory(pageName);
            } else {
                chat.classList.remove('cs-chat-open');
            }
        }

        // 如果页面正在生成中，启动实时预览更新
        this._startOverlayLiveUpdate(pageName);
    }

    _openPreviewWithChat(pageName) {
        this._openPreviewOverlay(pageName, true);
    }

    _openTemplateFramePreview() {
        const overlay = document.getElementById('csPreviewOverlay');
        if (!overlay) return;

        const titleEl = overlay.querySelector('.cs-preview-title span');
        if (titleEl) titleEl.textContent = '模板框架';

        const frame = document.getElementById('csPreviewFrame');
        if (frame && this._templateFrame) {
            frame.src = '/projects/' + this.projectId + '/' + (this._templateFrame.filename || 'template/frame.html');
        }

        overlay.classList.add('cs-preview-visible');

        // 模板框架不支持对话，隐藏聊天按钮
        const chatToggle = document.getElementById('csPreviewChatToggle');
        if (chatToggle) chatToggle.style.display = 'none';
        const chat = document.getElementById('csPreviewChat');
        if (chat) chat.classList.remove('cs-chat-open');
    }

    _closePreviewOverlay() {
        const overlay = document.getElementById('csPreviewOverlay');
        if (overlay) overlay.classList.remove('cs-preview-visible');
        this._selectedPage = null;

        // 停止实时预览更新
        this._stopOverlayLiveUpdate();

        // 隐藏页面对话切换按钮
        const chatToggle = document.getElementById('csPreviewChatToggle');
        if (chatToggle) chatToggle.style.display = 'none';

        const chat = document.getElementById('csPreviewChat');
        if (chat) chat.classList.remove('cs-chat-open');
    }

    /**
     * 启动预览 Overlay 的实时更新（生成中每 500ms 刷新一次）
     */
    _startOverlayLiveUpdate(pageName) {
        this._stopOverlayLiveUpdate();

        const pageData = this.pages[pageName];
        // 只在生成中的页面启动实时更新
        if (!pageData || pageData.status === 'done' || pageData.status === 'written') return;

        this._overlayUpdateTimer = setInterval(() => {
            // 检查是否仍在预览且仍在生成
            if (this._selectedPage !== pageName || !this.isGenerating) {
                this._stopOverlayLiveUpdate();
                return;
            }

            const frame = document.getElementById('csPreviewFrame');
            if (!frame) return;

            const partialHtml = this._extractPartialHtml(this.accumulatedContent);
            if (partialHtml) {
                frame.srcdoc = this._makeRenderableHtml(partialHtml);
            }
        }, 500);
    }

    /**
     * 停止预览 Overlay 的实时更新
     */
    _stopOverlayLiveUpdate() {
        if (this._overlayUpdateTimer) {
            clearInterval(this._overlayUpdateTimer);
            this._overlayUpdateTimer = null;
        }
    }

    _reloadPageThumbnail(pageName) {
        // 重新加载指定页面的缩略图
        const cardId = this._getCardId(pageName);
        const card = document.getElementById(cardId);
        if (!card) return;

        // 如果有文件路径，从文件重新加载
        const pageData = this.pages[pageName];
        if (pageData && pageData.status === 'written' && pageData.index !== undefined) {
            const thumbWrap = card.querySelector('.cs-card-thumb-wrap');
            if (thumbWrap) {
                const iframe = thumbWrap.querySelector('iframe');
                if (iframe) {
                    iframe.src = '/projects/' + this.projectId + '/pages/page_' + pageData.index + '_' + encodeURIComponent(pageName) + '.html';
                }
            }
        }
    }

    // ==================== 底部聊天栏 ====================

    _onChatSubmit() {
        const input = document.getElementById('csChatInput');
        if (!input || !input.value.trim()) return;

        const message = input.value.trim();
        input.value = '';

        this._appendChatBubble('user', message);
        this._sendChatMessage(message, null);
    }

    _onPageChatSubmit() {
        const input = document.getElementById('csPreviewChatInput');
        if (!input || !input.value.trim() || !this._selectedPage) return;

        const message = input.value.trim();
        input.value = '';

        this._appendPageChatBubble('user', message);
        this._sendChatMessage(message, this._selectedPage);
    }

    async _sendChatMessage(message, pageName) {
        if (!this.projectId || this._chatSending) return;
        this._chatSending = true;

        const self = this;
        const isPageChat = !!pageName;
        const appendFn = isPageChat
            ? (role, content) => this._appendPageChatBubble(role, content)
            : (role, content) => this._appendChatBubble(role, content);

        try {
            this._chatAbortController = new AbortController();

            const body = {
                projectId: this.projectId,
                message: message,
                targetPage: pageName || ''
            };
            if (this.activeConversationId) {
                body.conversationId = this.activeConversationId;
            }

            const response = await fetch('/api/chat', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
                signal: this._chatAbortController.signal
            });

            if (!response.ok) {
                appendFn('system', '请求失败: ' + response.status);
                this._chatSending = false;
                return;
            }

            // 流式响应
            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            let assistantBubble = null;
            let fullContent = '';

            while (true) {
                const result = await reader.read();
                if (result.done) break;

                const chunk = decoder.decode(result.value, { stream: true });
                const lines = chunk.split('\n');

                for (let i = 0; i < lines.length; i++) {
                    const line = lines[i];
                    if (line.startsWith('data: ')) {
                        try {
                            const evt = JSON.parse(line.slice(6));

                            // 格式1: { type: "chat", data: { role, content } }
                            if (evt.type === 'chat' && evt.data && evt.data.content) {
                                fullContent += evt.data.content;
                                if (!assistantBubble) {
                                    assistantBubble = appendFn('assistant', fullContent);
                                } else {
                                    assistantBubble.innerHTML = self._renderMarkdown(fullContent);
                                }
                            }

                            // 格式2: { content: "..." } (简单格式)
                            else if (evt.content) {
                                fullContent += evt.content;
                                if (!assistantBubble) {
                                    assistantBubble = appendFn('assistant', fullContent);
                                } else {
                                    assistantBubble.innerHTML = self._renderMarkdown(fullContent);
                                }
                            }

                            // tool call 进度（卡片式展示）
                            if (evt.type === 'tool_call_progress' && evt.data) {
                                self._onToolCallProgress(evt.data, isPageChat);
                            }

                            // edit 结果（带 diff 展示）
                            if (evt.type === 'edit_result' && evt.data) {
                                self._onEditResult(evt.data, isPageChat);
                                const editPage = evt.data.page || pageName;
                                if (editPage) {
                                    setTimeout(() => self._reloadPageThumbnail(editPage), 1000);
                                }
                            }

                            // conversation_id
                            if (evt.conversation_id) {
                                self.activeConversationId = evt.conversation_id;
                            }
                            if (evt.data && evt.data.conversation_id) {
                                self.activeConversationId = evt.data.conversation_id;
                            }

                        } catch (e) {
                            // 非 JSON 数据，忽略
                        }
                    }
                }
            }

            if (!assistantBubble && fullContent === '') {
                appendFn('system', 'AI 未返回内容');
            }

        } catch (e) {
            if (e.name !== 'AbortError') {
                appendFn('system', '发送失败: ' + e.message);
            }
        } finally {
            this._chatSending = false;
            this._chatAbortController = null;
        }
    }

    _appendChatBubble(role, content) {
        const msgs = document.getElementById('csChatMessages');
        if (!msgs) return null;

        const bubble = document.createElement('div');
        bubble.className = 'cs-chat-bubble cs-chat-' + role;
        bubble.innerHTML = this._renderMarkdown(content);
        msgs.appendChild(bubble);
        this._smartScroll(msgs);

        return bubble;
    }

    // ==================== Tool Call / Edit Result 卡片展示 ====================

    _getChatContainer(isPageChat) {
        return isPageChat
            ? document.getElementById('csPreviewChatMessages')
            : document.getElementById('csChatMessages');
    }

    /**
     * 实时显示 AI 的 tool call 操作过程（带 diff 展示）
     * 参考 GenerationPanel._onToolCallProgress
     */
    _onToolCallProgress(data, isPageChat) {
        const container = this._getChatContainer(isPageChat);
        if (!container) return;

        const tcId = 'cs-tc-' + (data.tool_call_id || Date.now());
        let el = document.getElementById(tcId);

        if (!el) {
            el = document.createElement('div');
            el.id = tcId;
            el.className = 'cs-chat-bubble cs-chat-tool';
            container.appendChild(el);
        }

        const status = data.status || 'pending';
        const page = data.page || '';
        const snippet = (data.old_string || data.search_snippet || '').substring(0, 100);
        const isFullPage = !!data.full_page;
        const toolName = data.tool_name || 'edit_file';
        const isReadPage = toolName === 'read_page';

        let iconClass = '';
        let statusText = '';
        let statusColor = '';

        if (status === 'pending' || status === 'running') {
            iconClass = 'fas fa-circle-notch fa-spin';
            if (isReadPage) {
                const rPage = data.page || page || '';
                const rLine = data.line_desc || '';
                statusText = rPage ? '读取 ' + rPage + (rLine ? ' ' + rLine : '') + '...' : '读取页面中...';
            } else {
                statusText = isFullPage ? '整页替换中...' : '编辑中...';
            }
            statusColor = '#818cf8';
        } else if (status === 'applied') {
            iconClass = 'fas fa-check-circle';
            statusText = isReadPage ? '已读取' : '已应用';
            statusColor = '#4ade80';
        } else if (status === 'failed') {
            iconClass = 'fas fa-times-circle';
            statusText = data.error || '失败';
            statusColor = '#f87171';
        }

        let html = '<div class="cs-tool-call">';
        html += '<div class="cs-tool-header">';
        html += '<i class="fas ' + (isReadPage ? 'fa-eye' : 'fa-file-code') + '" style="color:#818cf8;margin-right:4px;"></i> ';
        if (isReadPage) {
            const lineDesc = data.line_desc || '';
            const fileName = page || data.page || '';
            let readLabel = '读取文件';
            if (fileName) readLabel += ' ' + this._escapeHtml(fileName);
            if (lineDesc) readLabel += ' ' + this._escapeHtml(lineDesc);
            html += '<span class="cs-tool-name" style="color:#c4b5fd;">' + readLabel + '</span>';
        } else {
            html += '<span class="cs-tool-name">edit_file</span>';
        }
        html += ' <span style="color:' + statusColor + ';font-size:11px;">';
        html += '<i class="' + iconClass + '"></i> ' + statusText;
        html += '</span>';
        html += '</div>';

        if (isReadPage) {
            const contentLen = data.content_length || 0;
            const lineDesc = data.line_desc || '';
            if (page || lineDesc) {
                html += '<div class="cs-tool-detail" style="color:#9ca3af;font-size:11px;">';
                if (page && !lineDesc) html += this._escapeHtml(page);
                if (contentLen > 0) html += (page ? ' · ' : '') + contentLen + ' 字符';
                html += '</div>';
            }
        } else if (page) {
            html += '<div class="cs-tool-detail">';
            html += '<span class="cs-tool-page">' + this._escapeHtml(page) + '</span>';
            if (!isFullPage && snippet) {
                html += ' <span class="cs-tool-snippet">' + this._escapeHtml(snippet) + '</span>';
            } else if (isFullPage) {
                html += ' <span class="cs-tool-snippet">完整页面替换</span>';
            }
            html += '</div>';
        }

        // diff 展示
        const oldText = data.old_text || data.old_snippet || '';
        const newText = data.new_text || data.new_snippet || '';
        if (oldText || newText) {
            const uid = 'uvd-cs-' + tcId.replace(/[^a-zA-Z0-9]/g, '');
            html += (typeof DiffViewer !== 'undefined')
                ? DiffViewer.render(oldText, newText, { id: uid })
                : '<div class="cs-diff"><div class="cs-diff-old"><div class="cs-diff-label">- Original</div>'
                  + this._escapeHtml(oldText) + '</div>'
                  + '<div class="cs-diff-new"><div class="cs-diff-label">+ Modified</div>'
                  + this._escapeHtml(newText) + '</div></div>';
        }

        html += '</div>';
        el.innerHTML = html;
        this._smartScroll(container);
    }

    /**
     * 显示编辑结果摘要（带 diff 和逐条修改状态）
     * 参考 GenerationPanel._onEditResult
     */
    _onEditResult(data, isPageChat) {
        const container = this._getChatContainer(isPageChat);
        if (!container || !data) return;

        const bubble = document.createElement('div');
        bubble.className = 'cs-chat-bubble cs-chat-edit-result';

        let html = '<div class="cs-edit-summary">';
        if (data.mode === 'edit' || data.mode === 'tool_call') {
            html += '<div class="cs-edit-header">';
            html += '<i class="fas fa-code"></i> ';
            html += data.applied + '/' + (data.applied + data.failed) + ' 处修改已应用';
            html += '</div>';
        } else if (data.mode === 'full_html') {
            html += '<div class="cs-edit-header">';
            html += '<i class="fas fa-file-code"></i> ';
            html += '完整页面已重新生成';
            html += '</div>';
        } else if (data.message) {
            html += '<div class="cs-edit-header">';
            html += '<i class="fas fa-info-circle"></i> ';
            html += this._escapeHtml(data.message);
            html += '</div>';
        } else {
            html += '<div class="cs-edit-header">';
            html += '<i class="fas fa-file-code"></i> ';
            html += '完整页面已重新生成';
            html += '</div>';
        }

        if (data.edit_results) {
            for (const edit of data.edit_results) {
                const cls = edit.applied ? 'cs-edit-applied' : 'cs-edit-failed';
                const icon = edit.applied ? 'fa-check' : 'fa-times';
                html += '<div class="cs-edit-item ' + cls + '">';
                html += '<i class="fas ' + icon + '"></i> ';
                html += '[' + this._escapeHtml(edit.page || '') + '] ';
                html += this._escapeHtml((edit.search_snippet || '').substring(0, 80));
                if (!edit.applied && edit.error) {
                    const errorMap = {
                        'not_found': '未找到匹配',
                        'multiple_matches': '匹配多处，需更多上下文',
                        'page_not_found': '页面不存在'
                    };
                    html += ' <span class="cs-edit-error">(' +
                            (errorMap[edit.error] || edit.error) + ')</span>';
                }
                html += '</div>';

                // 每条 edit result 的 diff 展示
                const editOld = edit.old_text || edit.old_snippet || '';
                const editNew = edit.new_text || edit.new_snippet || '';
                if (editOld || editNew) {
                    const uid = 'uvd-cs-er-' + (edit.tool_call_id || Date.now() + Math.random()).toString().replace(/[^a-zA-Z0-9]/g, '');
                    html += (typeof DiffViewer !== 'undefined')
                        ? DiffViewer.render(editOld, editNew, { id: uid })
                        : '<div class="cs-diff"><div class="cs-diff-old"><div class="cs-diff-label">- Original</div>'
                          + this._escapeHtml(editOld) + '</div>'
                          + '<div class="cs-diff-new"><div class="cs-diff-label">+ Modified</div>'
                          + this._escapeHtml(editNew) + '</div></div>';
                }
            }
        }

        html += '</div>';
        bubble.innerHTML = html;
        container.appendChild(bubble);
        this._smartScroll(container);
    }

    _appendPhaseBubble(text, isLoading, skipAutoClose = false, pageAttr = null) {
        const msgs = document.getElementById('csChatMessages');
        if (!msgs) return;

        // 把之前还在 loading 的气泡标记为完成（并行模式下跳过，避免误关其他页面）
        if (!skipAutoClose) {
            const loadingPhases = msgs.querySelectorAll('.cs-phase-loading');
            loadingPhases.forEach(el => {
                el.classList.remove('cs-phase-loading');
                el.classList.add('cs-phase-done');
                const spinner = el.querySelector('.cs-phase-spinner-inline');
                if (spinner) {
                    spinner.outerHTML = '<span class="cs-phase-done-icon"><i class="fas fa-check-circle"></i></span>';
                }
            });
        }

        // 创建新的独立阶段气泡
        const bubble = document.createElement('div');
        bubble.className = 'cs-chat-bubble cs-chat-phase' + (isLoading ? ' cs-phase-loading' : ' cs-phase-done');
        if (isLoading) {
            bubble.innerHTML = '<span class="cs-phase-spinner-inline"></span>' + this._escapeHtml(text);
        } else {
            bubble.innerHTML = '<span class="cs-phase-done-icon"><i class="fas fa-check-circle"></i></span>' + this._escapeHtml(text);
        }
        if (pageAttr) {
            bubble.setAttribute('data-phase-page', pageAttr);
            bubble.setAttribute('data-base-text', text);
        }
        msgs.appendChild(bubble);
        this._smartScroll(msgs);
        return bubble;
    }

    /**
     * 展示原始流式输出（单次调用模式）
     * 创建/更新一个 assistant 气泡，持续追加点入的文本
     */
    _showRawStream() {
        const msgs = document.getElementById('csChatMessages');
        if (!msgs) return;

        if (!this._streamBubble) {
            this._streamBubble = document.createElement('div');
            this._streamBubble.className = 'cs-chat-bubble cs-chat-assistant cs-stream-indicator';
            msgs.appendChild(this._streamBubble);
        }

        // 不显示原始 HTML 代码，只显示简洁的生成进度
        const len = this.accumulatedContent.length;
        const kb = (len / 1024).toFixed(1);

        // 根据 round 显示正确的阶段名称
        const round = (this.roundInfo && this.roundInfo.round != null) ? this.roundInfo.round : 2;
        let stage;
        if (round === 0) {
            stage = '需求分析';
        } else if (round === 1) {
            stage = '设计系统';
        } else if (round === 3) {
            stage = '导航组装';
        } else {
            // Round 2: 解析流式 HTML 结构判断真实阶段
            stage = this._detectHtmlStage(this._liveHtml || this.accumulatedContent);
        }

        // 进度条：基于实际 HTML 结构进度 + 已完成页数
        const total = this._totalPlannedPages || 1;
        const doneCount = Object.values(this.pages).filter(p => p.status === 'done' || p.status === 'written').length;
        const pageBasePct = (doneCount / total) * 100;
        const currentPagePct = round === 2 ? this._detectHtmlProgress(this._liveHtml || this.accumulatedContent) : 0;
        const streamPct = Math.min(95, pageBasePct + (currentPagePct / total));

        this._streamBubble.innerHTML =
            '<div class="cs-stream-progress">' +
            '  <div class="cs-stream-spinner"></div>' +
            '  <div class="cs-stream-info">' +
            '    <span class="cs-stream-stage">正在生成: ' + stage + '</span>' +
            '    <span class="cs-stream-size">' + kb + ' KB</span>' +
            '  </div>' +
            '  <div class="cs-stream-bar">' +
            '    <div class="cs-stream-bar-fill" style="width:' + streamPct + '%"></div>' +
            '  </div>' +
            '</div>';
        this._smartScroll(msgs);
    }

    _appendPageChatBubble(role, content) {
        const msgs = document.getElementById('csPreviewChatMessages');
        if (!msgs) return null;

        const bubble = document.createElement('div');
        bubble.className = 'cs-chat-bubble cs-chat-' + role;
        bubble.innerHTML = this._renderMarkdown(content);
        msgs.appendChild(bubble);
        this._smartScroll(msgs);

        return bubble;
    }

    async _loadPageChatHistory(pageName) {
        const msgs = document.getElementById('csPreviewChatMessages');
        if (!msgs || !this.projectId) return;

        msgs.innerHTML = '';

        try {
            let url = '/api/chat-history?projectId=' + encodeURIComponent(this.projectId);
            if (this.activeConversationId) url += '&conversationId=' + encodeURIComponent(this.activeConversationId);
            const resp = await fetch(url);
            const data = await resp.json();

            if (data.success && data.messages && data.messages.length > 0) {
                this._appendPageChatBubble('system', '已加载历史对话（' + data.messages.length + ' 条消息）');
                for (const msg of data.messages) {
                    this._appendPageChatBubble(msg.role, msg.content);
                }
            }
        } catch (e) {
            console.warn('[CanvasStudio] 加载聊天历史失败:', e);
        }
    }

    // ==================== UI 辅助方法 ====================

    /**
     * 智能滚动：仅在用户已经位于底部附近时自动滚动
     * 如果用户向上滚动查看历史内容，则不强制拉到底部
     */
    _smartScroll(container) {
        if (!container) return;
        const threshold = 80; // 距底部 80px 以内视为"在底部"
        const atBottom = container.scrollHeight - container.scrollTop - container.clientHeight < threshold;
        if (atBottom) {
            container.scrollTop = container.scrollHeight;
        }
    }

    _updatePhaseLabel(text) {
        if (!text) return;
        // 在左侧面板中显示为阶段进度气泡
        this._appendPhaseBubble(text, this.isGenerating);
    }

    // ==================== 生成管道可视化 ====================

    _updatePipelineState(key, status, label) {
        if (!this._pipelineState) this._pipelineState = {};
        this._pipelineState[key] = { status, label };
        this._renderPipeline();
    }

    _renderPipeline() {
        const container = document.getElementById('csPipelineSteps');
        if (!container) return;

        // 构建有序的阶段列表
        const phases = [];
        const state = this._pipelineState || {};

        // 固定的前两个阶段
        if (state['spec']) {
            phases.push({ key: 'spec', label: state['spec'].label || '分析需求', status: state['spec'].status });
        }
        if (state['design']) {
            phases.push({ key: 'design', label: state['design'].label || '设计系统', status: state['design'].status });
        }

        // 动态页面阶段（按顺序）
        const pageKeys = Object.keys(state).filter(k => k.startsWith('page_'));
        pageKeys.forEach(k => {
            phases.push({ key: k, label: state[k].label || k.replace('page_', ''), status: state[k].status });
        });

        // 固定的最后阶段
        if (state['assembly']) {
            phases.push({ key: 'assembly', label: state['assembly'].label || '组装导航', status: state['assembly'].status });
        }

        if (phases.length === 0) return;

        container.innerHTML = phases.map((p, i) => {
            const isLast = i === phases.length - 1;
            const iconClass = p.status === 'done'
                ? 'cs-pipe-done'
                : p.status === 'running'
                    ? 'cs-pipe-running'
                    : 'cs-pipe-pending';
            const icon = p.status === 'done'
                ? '<i class="fas fa-check"></i>'
                : p.status === 'running'
                    ? '<span class="cs-pipe-spinner"></span>'
                    : '<span class="cs-pipe-dot"></span>';
            const connector = isLast ? '' : '<div class="cs-pipe-connector ' + (p.status === 'done' ? 'cs-pipe-conn-done' : '') + '"></div>';
            return '<div class="cs-pipe-step ' + iconClass + '">' +
                   '  <div class="cs-pipe-icon">' + icon + '</div>' +
                   '  <span class="cs-pipe-label">' + this._escapeHtml(p.label) + '</span>' +
                   '</div>' + connector;
        }).join('');

        container.style.display = 'block';
    }

    // ==================== 流式实时预览渲染 ====================

    /**
     * 从累积的原始文本中提取可渲染的 HTML 片段
     * 处理 markdown 代码块包裹和 think 标签
     */
    _extractPartialHtml(rawContent) {
        if (!rawContent || rawContent.length < 10) return '';

        // 去除 [think]...[/think] 思考内容（含未闭合的 [think] 块）
        let cleaned = rawContent.replace(/\[think\][\s\S]*?\[\/think\]/g, '');
        // 去除未闭合的 [think] 块（流式输出时没有 [/think]）
        cleaned = cleaned.replace(/\[think\][\s\S]*$/g, '');

        // 策略 1：尝试提取闭合的 ```html 代码块
        const codeBlockMatch = cleaned.match(/```(?:html|HTML)?\s*\n([\s\S]*?)```/);
        if (codeBlockMatch) {
            return codeBlockMatch[1].trim();
        }

        // 策略 2：提取未闭合的代码块（正在流式生成中）
        const openCodeMatch = cleaned.match(/```(?:html|HTML)?\s*\n([\s\S]+)$/);
        if (openCodeMatch) {
            return openCodeMatch[1].trim();
        }

        // 策略 3：内容直接以 HTML 标签开头（无代码块包裹）
        const trimmed = cleaned.trim();
        if (trimmed.startsWith('<!DOCTYPE') || trimmed.startsWith('<html') || trimmed.startsWith('<div') || trimmed.startsWith('<head')) {
            return trimmed;
        }

        // 策略 4：内容中包含 HTML 标签但前面有解释文本（AI 先说后写）
        // 找到第一个重要 HTML 标签的位置，提取其后所有内容
        const htmlStart = trimmed.search(/<![Dd]OCTYPE|<html|<head|<body|<div[\s>]/i);
        if (htmlStart > 0) {
            return trimmed.substring(htmlStart);
        }

        // 策略 5：兜底 — 找任意常见 HTML 标签
        const tagMatch = trimmed.match(/<(?:!DOCTYPE|html|head|body|div|table|form|section|main|header|nav|article|ul|ol|style|script)\b/i);
        if (tagMatch && tagMatch.index >= 0) {
            return trimmed.substring(tagMatch.index);
        }

        return '';
    }

    /**
     * 解析流式 HTML 内容，判断 AI 当前写到了哪个区段
     *
     * HTML 生成顺序：head → <style> → </head> → <body> 内容 → <script> → </body>
     * 通过检测最后出现的标志性标签来判断真实阶段
     */
    _detectHtmlStage(html) {
        if (!html || html.length < 20) return '页面结构';

        const pageName = this._currentGeneratingPage || this.roundInfo.label || '';

        // 检测 <script 出现（交互逻辑阶段）
        if (/<script\b/i.test(html)) {
            return (pageName ? pageName + ' · ' : '') + '交互逻辑';
        }

        // 检测 </head> 或 <body 出现（进入内容布局阶段）
        if (/<body\b/i.test(html) || /<\/head>/i.test(html)) {
            return (pageName ? pageName + ' · ' : '') + '内容布局';
        }

        // 检测 <style 出现（样式阶段）
        if (/<style\b/i.test(html)) {
            return (pageName ? pageName + ' · ' : '') + '样式定义';
        }

        // 还在 <head> 或更早
        if (/<head\b/i.test(html) || /<html\b/i.test(html)) {
            return (pageName ? pageName + ' · ' : '') + '页面结构';
        }

        return (pageName ? pageName + ' · ' : '') + '页面结构';
    }

    /**
     * 根据流式 HTML 结构计算当前页面的生成进度（0~100）
     *
     * 典型完整 HTML 的结构权重：
     *   <head> 结构  ~5%
     *   <style> 样式  ~20%
     *   <body> 内容   ~55%
     *   <script> 交互 ~20%
     */
    _detectHtmlProgress(html) {
        if (!html || html.length < 20) return 2;

        let pct = 0;

        // <head> 结构开始
        if (/<head\b/i.test(html)) pct += 3;
        if (/<\/head>/i.test(html)) pct += 5;

        // <style> 样式（含完成和进行中）
        if (/<style\b/i.test(html)) pct += 8;
        if (/<\/style>/i.test(html)) pct += 12;

        // <body> 内容
        if (/<body\b/i.test(html)) pct += 10;
        if (/<\/body>/i.test(html)) pct += 45;

        // <script> 交互
        if (/<script\b/i.test(html)) pct += 8;
        if (/<\/script>/i.test(html)) pct += 9;

        return Math.min(98, pct);
    }

    /**
     * 确保部分 HTML 可以安全渲染（补全缺失的闭合标签）
     * 参考 OD srcdoc.ts 的 transform pipeline（简化版）
     */
    _makeRenderableHtml(partialHtml) {
        if (!partialHtml) return '';

        // Step 1: 注入 <base> 标签
        if (this.projectId) {
            const baseHref = '/projects/' + this.projectId + '/';
            if (!/<base/i.test(partialHtml)) {
                if (/<head[^>]*>/i.test(partialHtml)) {
                    partialHtml = partialHtml.replace(
                        /(<head[^>]*>)/i,
                        '$1<base href="' + baseHref + '">'
                    );
                } else {
                    partialHtml = '<head><base href="' + baseHref + '"></head>' + partialHtml;
                }
            }
        }

        // Step 2: Sandbox shim — 拦截 localStorage/sessionStorage 报错
        if (!/<script[^>]*data-sandbox-shim/.test(partialHtml)) {
            const shim = '<script data-sandbox-shim>'
                + 'try{Object.defineProperty(window,"localStorage",{value:new Proxy({},{get:(t,k)=>{if(k in t)return t[k];return null},set:(t,k,v)=>{t[k]=v;return true},deleteProperty:(t,k)=>delete t[k]})})}catch(e){}'
                + 'try{Object.defineProperty(window,"sessionStorage",{value:new Proxy({},{get:(t,k)=>{if(k in t)return t[k];return null},set:(t,k,v)=>{t[k]=v;return true},deleteProperty:(t,k)=>delete t[k]})})}catch(e){}'
                + '<\/script>';
            if (/<\/head>/i.test(partialHtml)) {
                partialHtml = partialHtml.replace(/<\/head>/i, shim + '</head>');
            } else if (/<head[^>]*>/i.test(partialHtml)) {
                partialHtml = partialHtml.replace(/(<head[^>]*>)/i, '$1' + shim);
            }
        }

        // Step 3: 智能闭合标签
        if (/<\/html>/i.test(partialHtml)) return partialHtml;
        if (/<body/i.test(partialHtml) && !/<\/body>/i.test(partialHtml)) {
            partialHtml += '</body></html>';
        } else if (/<head/i.test(partialHtml) && !/<\/head>/i.test(partialHtml)) {
            partialHtml += '</head></html>';
        } else if (/<html/i.test(partialHtml)) {
            partialHtml += '</html>';
        }

        // Step 4: 未完成 HTML 的视觉提示
        if (!/<\/html>/i.test(partialHtml)) {
            const hint = '<style data-preview-hint>'
                + 'body::after{content:"预览渲染中...";position:fixed;bottom:8px;right:12px;'
                + 'font-size:11px;color:rgba(255,255,255,0.6);background:rgba(0,0,0,0.4);'
                + 'padding:2px 8px;border-radius:4px;z-index:9999;pointer-events:none}'
                + '</style>';
            if (/<\/body>/i.test(partialHtml)) {
                partialHtml = partialHtml.replace(/<\/body>/i, hint + '</body>');
            } else if (/<\/head>/i.test(partialHtml)) {
                partialHtml = partialHtml.replace(/<\/head>/i, hint + '</head>');
            }
        }

        return partialHtml;
    }

    /**
     * 节流调度实时预览更新
     * 现在统一走 _scheduleRender（rAF + setTimeout 双保险）
     */
    _scheduleLivePreview(pageName) {
        this._scheduleRender(pageName);
    }

    /**
     * 立即取消待执行的实时预览
     */
    _flushLivePreview() {
        this._cancelScheduledRender();
    }

    /**
     * 将当前累积的流式内容渲染到卡片的 iframe 中
     * 使用 srcdoc 全量写入（稳定可靠），配合 300ms 节流控制刷新频率
     */
    _renderLivePreview(pageName) {
        const partialHtml = this._extractPartialHtml(this.accumulatedContent);
        if (!partialHtml) return;

        const renderable = this._makeRenderableHtml(partialHtml);
        this._updateCardThumb(pageName, renderable);

        if (this.pages[pageName]) {
            this.pages[pageName].html = renderable;
        }
    }

    /**
     * 流式渲染结束清理（生成结束时调用）
     */
    _closeStreamDoc() {
        // srcdoc 模式无需显式关闭文档流
    }

    _updateProgressText(text) {
        // 进度信息已集成到阶段气泡中，不再单独显示
    }

    _updateProgressBar(pct) {
        const fill = document.getElementById('csProgressFill');
        if (fill) fill.style.width = Math.min(100, pct) + '%';

        // 更新进度文本
        const pctText = document.getElementById('csProgressPct');
        if (pctText) pctText.textContent = Math.round(Math.min(100, pct)) + '%';
    }

    /**
     * 统一进度计算：根据当前 round 和 page progress 计算百分比
     * 权重分配: Round 0 (5%) → Round 1 (10%) → Round 2 (75%) → Round 3 (10%)
     */
    _calcUnifiedProgress(round, pageProgress) {
        if (round === 0) return 5;
        if (round === 1) return 10;
        if (round === 2) {
            const total = this._totalPlannedPages || 1;
            const current = (pageProgress && pageProgress.current) || 0;
            // 当前页面算作进行中，进度为 (current-0.5)/total
            return 10 + (Math.max(0, current - 0.5) / total) * 75;
        }
        if (round === 3) return 90;
        return 95;
    }

    _startTimer() {
        this._startTime = Date.now();
        // Timer no longer displays in UI; phase bubbles provide progress info
    }

    _stopTimer() {
        if (this._timerInterval) {
            clearInterval(this._timerInterval);
            this._timerInterval = null;
        }
    }

    _updateProjectStatus(status) {
        if (typeof allProjects === 'undefined') return;
        const idx = allProjects.findIndex(p => p.id === this.projectId);
        if (idx !== -1) {
            allProjects[idx].status = status;
            if (typeof renderProjectList === 'function') renderProjectList();
        }
    }

    _getCardId(pageName) {
        return 'cs-card-' + pageName.replace(/[^a-zA-Z0-9\u4e00-\u9fa5]/g, '_');
    }

    _formatBytes(bytes) {
        if (!bytes || bytes < 1024) return (bytes || 0) + ' B';
        if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
        return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
    }

    _escapeHtml(str) {
        if (!str) return '';
        return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    _escapeAttr(str) {
        if (!str) return '';
        return str.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

    _renderMarkdown(text) {
        if (!text) return '';
        // 简单 Markdown 渲染
        let html = this._escapeHtml(text);
        // 代码块
        html = html.replace(/```(\w*)\n([\s\S]*?)```/g, '<pre><code>$2</code></pre>');
        // 行内代码
        html = html.replace(/`([^`]+)`/g, '<code style="background:rgba(148,163,184,0.15);padding:1px 5px;border-radius:3px;font-size:12px">$1</code>');
        // 粗体
        html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
        // 换行
        html = html.replace(/\n/g, '<br>');
        return html;
    }

    // ==================== 无限画布辅助方法 ====================

    /**
     * 蛇形自动布局：计算下一个卡片位置
     */
    _getNextAutoLayoutPosition() {
        const CARD_W = 420;
        const CARD_H = 420;
        const GAP = 40;
        const PADDING = 50;

        const cursor = this._autoLayoutCursor;
        const x = PADDING + cursor.col * (CARD_W + GAP);
        const y = PADDING + cursor.row * (CARD_H + GAP);

        cursor.col++;
        if (cursor.col >= cursor.maxCols) {
            cursor.col = 0;
            cursor.row++;
        }

        // 防重叠：检查是否与已有卡片位置冲突
        const existingPositions = Object.values(this._cardPositions);
        let pos = { x, y };
        let attempts = 0;
        while (attempts < 50) {
            const conflict = existingPositions.some(p =>
                Math.abs(p.x - pos.x) < CARD_W && Math.abs(p.y - pos.y) < CARD_H
            );
            if (!conflict) break;
            pos.y += CARD_H + GAP;
            attempts++;
        }

        return pos;
    }

    /**
     * 让卡片可拖拽
     */
    _makeCardDraggable(card, pageName) {
        let startX, startY, origLeft, origTop;
        let hasDragged = false;  // 追踪是否实际发生了拖拽

        // 拖拽时的 iframe 遮罩（防止 iframe 捕获鼠标）
        let iframeOverlay = null;

        const onMouseDown = (e) => {
            if (e.button !== 0) return;
            e.stopPropagation(); // 阻止触发画布平移

            startX = e.clientX;
            startY = e.clientY;
            origLeft = parseInt(card.style.left) || 0;
            origTop = parseInt(card.style.top) || 0;
            hasDragged = false;

            document.addEventListener('mousemove', onMouseMove);
            document.addEventListener('mouseup', onMouseUp);
        };

        const onMouseMove = (e) => {
            const scale = this._canvasEngine ? this._canvasEngine._state.scale : 1;
            const dx = (e.clientX - startX) / scale;
            const dy = (e.clientY - startY) / scale;

            // 超过 5px 阈值才视为拖拽，开始拖拽视觉效果
            if (!hasDragged && (Math.abs(dx) > 5 || Math.abs(dy) > 5)) {
                hasDragged = true;
                card.classList.add('cs-dragging-card');
                // 在 iframe 上覆盖遮罩
                const thumbWrap = card.querySelector('.cs-card-thumb-wrap');
                if (thumbWrap) {
                    iframeOverlay = document.createElement('div');
                    iframeOverlay.style.cssText = 'position:absolute;inset:0;z-index:50;cursor:grabbing;';
                    thumbWrap.appendChild(iframeOverlay);
                }
            }

            if (hasDragged) {
                card.style.left = Math.round(origLeft + dx) + 'px';
                card.style.top = Math.round(origTop + dy) + 'px';
            }
        };

        const onMouseUp = (e) => {
            if (hasDragged) {
                card.classList.remove('cs-dragging-card');
                if (iframeOverlay) {
                    iframeOverlay.remove();
                    iframeOverlay = null;
                }
                e.stopPropagation(); // 阻止后续 click 触发预览

                // 更新位置记录
                this._cardPositions[pageName] = {
                    x: parseInt(card.style.left) || 0,
                    y: parseInt(card.style.top) || 0
                };

                this._redrawConnections();
                this._debouncedSaveLayout();
            }
            document.removeEventListener('mousemove', onMouseMove);
            document.removeEventListener('mouseup', onMouseUp);
        };

        card.addEventListener('mousedown', onMouseDown);
    }

    /**
     * 重绘 SVG 连接线
     */
    _redrawConnections() {
        const svgGroup = document.getElementById('csConnectionLines');
        if (!svgGroup) return;

        svgGroup.innerHTML = '';

        const CARD_W = 300;
        const CARD_H = 300;

        for (const conn of this._connections) {
            const fromPos = this._cardPositions[conn.from];
            const toPos = this._cardPositions[conn.to];
            if (!fromPos || !toPos) continue;

            // 源卡片右边缘中心 → 目标卡片左边缘中心
            const x1 = fromPos.x + CARD_W;
            const y1 = fromPos.y + CARD_H / 2;
            const x2 = toPos.x;
            const y2 = toPos.y + CARD_H / 2;

            // 贝塞尔弧线
            const dx = x2 - x1;
            const dy = y2 - y1;
            const dist = Math.sqrt(dx * dx + dy * dy);
            const curvature = Math.max(dist * 0.4, 60);

            const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
            path.setAttribute('class', 'cs-connection-line');
            path.setAttribute('d',
                `M${x1},${y1} C${x1 + curvature},${y1} ${x2 - curvature},${y2} ${x2},${y2}`
            );
            path.setAttribute('marker-end', 'url(#cs-arrow)');
            svgGroup.appendChild(path);

            // 可选标签
            if (conn.label) {
                const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
                text.setAttribute('x', (x1 + x2) / 2);
                text.setAttribute('y', (y1 + y2) / 2 - 8);
                text.setAttribute('text-anchor', 'middle');
                text.setAttribute('fill', '#94a3b8');
                text.setAttribute('font-size', '11');
                text.textContent = conn.label;
                svgGroup.appendChild(text);
            }
        }
    }

    /**
     * 适应全部卡片（供工具栏按钮调用）
     */
    _fitAllCards() {
        if (this._canvasEngine) {
            this._canvasEngine.fitAll(this._cardPositions, 420, 420);
        }
    }

    /**
     * 防抖保存布局
     */
    _debouncedSaveLayout() {
        if (this._layoutSaveTimer) clearTimeout(this._layoutSaveTimer);
        this._layoutSaveTimer = setTimeout(() => this._saveLayout(), 1000);
    }

    /**
     * 保存画布布局到服务器
     */
    async _saveLayout() {
        if (!this.projectId) return;
        try {
            await fetch('/api/canvas-layout', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    projectId: this.projectId,
                    layout: {
                        positions: this._cardPositions,
                        connections: this._connections,
                        viewport: this._canvasEngine ? this._canvasEngine.getState() : {}
                    }
                })
            });
        } catch (e) {
            console.warn('[CanvasStudio] Save layout failed:', e);
        }
    }

    /**
     * 从服务器加载画布布局
     */
    async _loadLayout() {
        if (!this.projectId) return;
        try {
            const resp = await fetch('/api/canvas-layout?projectId=' + encodeURIComponent(this.projectId));
            const data = await resp.json();
            if (data.positions) {
                this._cardPositions = data.positions;
                // 应用到已有卡片
                for (const [name, pos] of Object.entries(data.positions)) {
                    const card = document.getElementById(this._getCardId(name));
                    if (card) {
                        card.style.left = pos.x + 'px';
                        card.style.top = pos.y + 'px';
                    }
                }
            }
            if (data.connections) {
                this._connections = data.connections;
                this._redrawConnections();
            }
            if (data.viewport && this._canvasEngine) {
                this._canvasEngine.restoreState(data.viewport);
            }
        } catch (e) {
            // 无保存的布局是正常的（新项目或首次生成）
        }
    }

    // ==================== 静态方法 ====================

    static getInstance() {
        if (!window._canvasStudio) {
            window._canvasStudio = new CanvasStudio();
        }
        return window._canvasStudio;
    }
}

// 全局实例
const canvasStudio = CanvasStudio.getInstance();

// 全局函数（供 HTML onclick 调用）
function csClose() { canvasStudio.close(); }
function csSendChat() { canvasStudio._onChatSubmit(); }
function csSendPageChat() { canvasStudio._onPageChatSubmit(); }
function csClosePreview() { canvasStudio._closePreviewOverlay(); }
function csOpenInViewer() {
    if (canvasStudio.projectId) {
        window.open('viewer.html?project=' + encodeURIComponent(canvasStudio.projectId), '_blank');
    }
}
