/**
 * GenerationPanel — Claude Code 风格的交互式生成面板
 *
 * 特性：
 * - 居中弹出框（和 loadingModal 一致风格）
 * - 支持最小化（右下角浮动条）/ 展开
 * - 支持关闭（后台继续生成）
 * - 单列垂直对话流
 * - 结构化步骤展示 + AI 思考过程 + 代码产物可折叠
 * - 生成完成后支持对话调整
 * - 兼容原有 SSE 流（结构化事件 + 原始文本）
 */

class GenerationPanel {
    constructor() {
        this.projectId = null;
        this.evtSource = null;
        this.chatMode = false;
        this.pages = {};
        this.currentPage = null;
        this.accumulatedContent = '';
        this.thinkingText = '';
        this.roundInfo = {};
        this.isGenerating = false;
        this._chatStreamEl = null;
        this._minimized = false;
        this._startTime = null;
        this._timerInterval = null;
        this.activeConversationId = '';
        this._chatAbortController = null;
        this._chatSending = false;
    }

    // ==================== 生命周期 ====================

    connect(projectId) {
        this.projectId = projectId;
        this._show();
        this._connectSSE();
    }

    connectChat(projectId) {
        /**直接进入聊天模式，加载历史对话 */
        this.projectId = projectId;
        this.chatMode = true;
        this.isGenerating = false;
        this._minimized = false;

        this._show();

        // 重置为聊天模式 UI
        const headerStatus = document.getElementById('gpHeaderStatus');
        if (headerStatus) headerStatus.textContent = '对话调整';
        const spinner = document.getElementById('gpSpinner');
        if (spinner) spinner.style.display = 'none';
        this._stopTimer();

        const content = document.getElementById('gpContent');
        if (content) content.innerHTML = '';

        // 创建聊天内容容器
        let chatContent = document.getElementById('gpChatContent');
        if (!chatContent) {
            chatContent = document.createElement('div');
            chatContent.id = 'gpChatContent';
            chatContent.className = 'gp-chat-content';
            content.appendChild(chatContent);
        }
        chatContent.innerHTML = '';

        // 显示输入区
        const inputArea = document.getElementById('gpInputArea');
        if (inputArea) inputArea.style.display = 'flex';

        // 从后端加载历史消息和对话标签
        this._loadConversationTabs();
        this._loadChatHistory();
    }

    async _loadChatHistory() {
        if (!this.projectId) return;
        try {
            let url = '/api/chat-history?projectId=' + encodeURIComponent(this.projectId);
            if (this.activeConversationId) url += '&conversationId=' + encodeURIComponent(this.activeConversationId);
            const resp = await fetch(url);
            const data = await resp.json();
            if (!data.success || !data.messages || data.messages.length === 0) {
                this._addChatBubble('system', '开始新的对话调整，在下方输入修改指令。');
                return;
            }

            this._addChatBubble('system', '已加载历史对话（' + data.messages.length + ' 条消息），可以继续调整。');

            for (let idx = 0; idx < data.messages.length; idx++) {
                const msg = data.messages[idx];
                if (msg.role === 'user') {
                    this._addChatBubble('user', msg.content);
                } else if (msg.role === 'assistant') {
                    const bubble = this._addChatBubble('assistant', msg.content);
                    // 渲染历史编辑记录（带 diff 的 tool call 卡片）
                    if (msg.html_changes && msg.html_changes.length > 0) {
                        for (let i = 0; i < msg.html_changes.length; i++) {
                            const edit = msg.html_changes[i];
                            this._onToolCallProgress({
                                tool_call_id: 'hist-gp-' + Date.now() + '-' + i,
                                status: edit.applied ? 'applied' : 'failed',
                                page: edit.page || '',
                                old_text: edit.old_text || '',
                                new_text: edit.new_text || '',
                                error: edit.error || null,
                                full_page: edit.search_snippet === '完整页面替换'
                            });
                        }
                        // 撤回按钮
                        if (bubble) {
                            const undoBtn = document.createElement('button');
                            undoBtn.className = 'gp-chat-undo-btn';
                            undoBtn.innerHTML = '<i class="fas fa-undo"></i> 撤回';
                            undoBtn.onclick = () => this._undoChatMessage(idx);
                            bubble.style.position = 'relative';
                            bubble.appendChild(undoBtn);
                        }
                    }
                }
            }

            this._scrollToBottom();
        } catch (e) {
            console.warn('[GenerationPanel] 加载历史失败:', e);
            this._addChatBubble('system', '开始新的对话调整，在下方输入修改指令。');
        }
    }

    destroy() {
        if (this.evtSource) {
            this.evtSource.close();
            this.evtSource = null;
        }
        this._stopTimer();
        this.isGenerating = false;
        this._hide();
        this._hideMiniBar();
    }

    // ==================== UI 显示/隐藏 ====================

    _show() {
        let panel = document.getElementById('generationPanel');
        if (!panel) {
            this._createPanelHTML();
            panel = document.getElementById('generationPanel');
        }
        // 展开模式
        this._minimized = false;
        this._hideMiniBar();
        panel.classList.remove('hidden');
        panel.classList.add('flex');
        this._resetState();
    }

    _hide() {
        const panel = document.getElementById('generationPanel');
        if (panel) {
            panel.classList.add('hidden');
            panel.classList.remove('flex');
        }
    }

    _resetState() {
        this.pages = {};
        this.currentPage = null;
        this.accumulatedContent = '';
        this.thinkingText = '';
        this.chatMode = false;
        this.isGenerating = true;
        this.roundInfo = {};
        this._chatStreamEl = null;

        const content = document.getElementById('gpContent');
        if (content) content.innerHTML = '';

        this._addStep('init', 'preparing', '连接 AI 服务...');

        const inputArea = document.getElementById('gpInputArea');
        if (inputArea) inputArea.style.display = 'none';

        const headerStatus = document.getElementById('gpHeaderStatus');
        if (headerStatus) headerStatus.textContent = '生成中 0:00';

        const spinner = document.getElementById('gpSpinner');
        if (spinner) spinner.style.display = 'inline-block';

        this._startTimer();

        this._scrollToBottom();
    }

    // ==================== 最小化 / 展开 ====================

    _onMinimize() {
        this._minimized = true;
        this._hide();
        this._showMiniBar();
    }

    _onExpand() {
        this._minimized = false;
        this._hideMiniBar();
        const panel = document.getElementById('generationPanel');
        if (panel) {
            panel.classList.remove('hidden');
            panel.classList.add('flex');
        }
    }

    _showMiniBar() {
        let bar = document.getElementById('gpMiniBar');
        if (!bar) {
            bar = document.createElement('div');
            bar.id = 'gpMiniBar';
            bar.className = 'gp-minibar';
            bar.style.display = 'none';
            bar.innerHTML =
                '<span class="gp-minibar-spinner" id="gpMiniSpinner"></span>' +
                '<span class="gp-minibar-title" id="gpMiniTitle">AI 原型生成</span>' +
                '<span class="gp-minibar-status" id="gpMiniStatus">生成中</span>' +
                '<button class="gp-minibar-btn" id="gpMiniExpand" title="展开"><i class="fas fa-chevron-up"></i></button>' +
                '<button class="gp-minibar-btn" id="gpMiniClose" title="关闭"><i class="fas fa-times"></i></button>';
            document.body.appendChild(bar);

            bar.querySelector('#gpMiniExpand').addEventListener('click', () => this._onExpand());
            bar.querySelector('#gpMiniClose').addEventListener('click', () => this.destroy());
        }
        bar.style.display = 'flex';
    }

    _hideMiniBar() {
        const bar = document.getElementById('gpMiniBar');
        if (bar) {
            bar.style.display = 'none';
        }
    }

    _updateMiniBar(status) {
        const title = document.getElementById('gpMiniTitle');
        const miniStatus = document.getElementById('gpMiniStatus');
        const spinner = document.getElementById('gpMiniSpinner');

        if (title) title.textContent = 'AI 原型生成';
        if (miniStatus) miniStatus.textContent = status;

        if (spinner) {
            spinner.style.display = this.isGenerating ? 'inline-block' : 'none';
        }
    }

    // ==================== SSE 连接 ====================

    _connectSSE() {
        if (!this.projectId) return;

        try {
            this.evtSource = new EventSource(
                '/api/generation-stream?id=' + encodeURIComponent(this.projectId)
            );

            this.evtSource.onmessage = (event) => {
                this._handleSSEMessage(event);
            };

            this.evtSource.addEventListener('status', (event) => {
                this._handleSSEStatus(event);
            });

            this.evtSource.onerror = () => {
                // 如果生成已完成，onerror 是正常的连接关闭，不需要降级
                if (!this.isGenerating) {
                    if (this.evtSource) {
                        this.evtSource.close();
                        this.evtSource = null;
                    }
                    return;
                }
                if (this.evtSource) {
                    this.evtSource.close();
                    this.evtSource = null;
                }
                this._hide();
                this._hideMiniBar();
                if (typeof pollGenerationStatus === 'function') {
                    pollGenerationStatus(this.projectId);
                }
            };
        } catch (e) {
            console.error('[GenerationPanel] SSE 不支持:', e);
            this._hide();
            this._hideMiniBar();
            if (typeof pollGenerationStatus === 'function') {
                pollGenerationStatus(this.projectId);
            }
        }
    }

    // ==================== SSE 消息处理 ====================

    _handleSSEMessage(event) {
        try {
            const data = JSON.parse(event.data);

            // 连接确认事件
            if (data.type === 'connected') {
                console.log('[GenerationPanel] SSE 已连接');
                this._removeStep('init');
                this._addStep('init', 'running', '已连接，等待 AI 响应...');
                return;
            }

            // 结构化事件（来自多轮生成）
            if (data.type && data.data) {
                this._handleStructuredEvent(data);
                return;
            }

            // 原始文本流（来自单次调用）
            if (data.content) {
                if (data.content.startsWith('[think]')) {
                    this.thinkingText += data.content.slice(7);
                    this._showThinking();
                } else {
                    this.accumulatedContent += data.content;
                    this._showRawStream();
                }
                // 更新迷你栏状态
                if (this._minimized) {
                    const charInfo = this.accumulatedContent.length.toLocaleString() + ' 字符';
                    this._updateMiniBar('已生成 ' + charInfo);
                }
            }
        } catch (e) {
            console.error('[GenerationPanel] SSE 解析错误:', e, event.data);
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
                this._addStep('error', 'error', '生成失败: ' + (data.error || '未知错误'));
                this._finishGeneration();
                if (this._minimized) this._updateMiniBar('生成失败');

                const idx = this._findProjectIndex();
                if (idx !== -1) {
                    allProjects[idx].status = 'failed';
                    if (typeof renderProjectList === 'function') renderProjectList();
                    if (typeof showToast === 'function') showToast('"' + allProjects[idx].name + '" 生成失败: ' + (data.error || '未知错误'), 'error');
                }

            } else if (data.status === 'cancelled') {
                if (this.evtSource) {
                    this.evtSource.close();
                    this.evtSource = null;
                }
                this._addStep('cancelled', 'error', '生成已停止');
                this._finishGeneration();
                if (this._minimized) this._updateMiniBar('已停止');

                const idx = this._findProjectIndex();
                if (idx !== -1) {
                    allProjects[idx].status = 'stopped';
                    if (typeof renderProjectList === 'function') renderProjectList();
                    if (typeof showToast === 'function') showToast('"' + allProjects[idx].name + '" 已停止生成');
                }
            }
        } catch (e) {
            console.error('[GenerationPanel] Status 事件解析错误:', e);
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
            case 'artifact':
                this._onArtifactEvent(d);
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
            case 'chat':
                this._onChatEvent(d);
                break;
            case 'preview_update':
                this._onPreviewUpdate(d);
                break;
            case 'diagnostic':
                this._onDiagnosticEvent(d);
                break;
        }
    }

    // ==================== Phase 事件 ====================

    _onPhaseEvent(data) {
        this.roundInfo = {
            round: data.round,
            step: data.step,
            label: data.label || ''
        };

        const stepId = 'gp-phase-' + data.round + '-' + data.step;

        if (data.status === 'running') {
            this._removeStep('init');

            // 新阶段开始时，重置累积内容，避免不同页面复用同一输出
            this.accumulatedContent = '';

            let label = '';
            if (data.round === 1) {
                label = '分析需求，生成设计系统';
            } else if (data.round === 2) {
                label = '生成页面: ' + (data.label || '页面');
                if (data.progress) {
                    label += ' (' + data.progress.current + '/' + data.progress.total + ')';
                }
            } else if (data.round === 3) {
                label = '组装多页导航';
            }
            this._addStep(stepId, 'running', label);

            if (this._minimized) this._updateMiniBar(label);
        } else if (data.status === 'done') {
            this._updateStep(stepId, 'done');
        }
    }

    // ==================== Artifact 事件 ====================

    _onArtifactEvent(data) {
        const stepId = 'gp-phase-' + this.roundInfo.round + '-' + this.roundInfo.step;
        const artifactId = 'gp-artifact-' + data.name.replace(/[^a-zA-Z0-9]/g, '_');

        const stepEl = document.getElementById('step-' + stepId);
        if (!stepEl) return;

        let artifactEl = document.getElementById(artifactId);
        if (!artifactEl) {
            artifactEl = document.createElement('div');
            artifactEl.id = artifactId;
            artifactEl.className = 'gp-artifact';
            stepEl.appendChild(artifactEl);
        }

        const langIcon = data.language === 'css' ? 'fa-palette' : 'fa-file-code';
        const charCount = data.content ? data.content.length : 0;
        const preview = data.content ? data.content.substring(0, 300) : '';

        artifactEl.innerHTML =
            '<div class="gp-artifact-toggle" onclick="this.nextElementSibling.classList.toggle(\'hidden\')">' +
            '  <i class="fas ' + langIcon + '"></i> ' +
            '  <span>' + this._escapeHtml(data.name) + '</span>' +
            '  <span class="gp-artifact-meta">' + charCount.toLocaleString() + ' 字符</span>' +
            '  <i class="fas fa-chevron-right gp-artifact-chevron"></i>' +
            '</div>' +
            '<div class="gp-artifact-code hidden"><pre>' + this._escapeHtml(preview) +
            (charCount > 300 ? '\n...' : '') + '</pre></div>';

        this._scrollToBottom();
    }

    // ==================== Preview 事件 ====================

    _onPagePreview(data) {
        if (!data.page || !data.html_fragment) return;
        this.pages[data.page] = data.html_fragment;

        const stepId = 'gp-phase-' + this.roundInfo.round + '-' + this.roundInfo.step;

        const stepEl = document.getElementById('step-' + stepId);
        if (stepEl) {
            let previewContainer = stepEl.querySelector('.gp-page-previews');
            if (!previewContainer) {
                previewContainer = document.createElement('div');
                previewContainer.className = 'gp-page-previews';
                stepEl.appendChild(previewContainer);
            }

            const cardId = 'gp-preview-card-' + data.page.replace(/[^a-zA-Z0-9]/g, '_');
            let card = document.getElementById(cardId);
            if (!card) {
                card = document.createElement('div');
                card.id = cardId;
                card.className = 'gp-preview-card';
                previewContainer.appendChild(card);
            }

            card.innerHTML =
                '<div class="gp-preview-label">' + this._escapeHtml(data.page) + '</div>' +
                '<div class="gp-preview-thumb-wrap">' +
                '<iframe class="gp-preview-thumb" srcdoc="' + this._escapeAttr(data.html_fragment) + '"></iframe>' +
                '</div>';

            this._scrollToBottom();
        }
    }

    _onPreviewUpdate(data) {
        // 对话调整后的预览更新 — 通知 viewer 窗口刷新
        if (!data) return;
        try {
            localStorage.setItem('prototype-update-' + this.projectId, Date.now().toString());
            localStorage.removeItem('prototype-update-signal');
            localStorage.setItem('prototype-update-signal', this.projectId);
        } catch (e) {
            console.warn('[GenerationPanel] 无法通过 localStorage 发送更新:', e);
        }
    }

    _onEditResult(data) {
        // 显示编辑结果摘要
        const chatContent = document.getElementById('gpChatContent');
        if (!chatContent || !data) return;

        const summary = document.createElement('div');
        summary.className = 'gp-chat-bubble gp-chat-system';

        let html = '<div class="gp-edit-summary">';
        if (data.mode === 'edit' || data.mode === 'tool_call') {
            html += '<div class="gp-edit-header">';
            html += '<i class="fas fa-code"></i> ';
            html += data.applied + '/' + (data.applied + data.failed) + ' 处修改已应用';
            html += '</div>';
        } else if (data.mode === 'full_html') {
            html += '<div class="gp-edit-header">';
            html += '<i class="fas fa-file-code"></i> ';
            html += '完整页面已重新生成';
            html += '</div>';
        } else if (data.message) {
            html += '<div class="gp-edit-header">';
            html += '<i class="fas fa-info-circle"></i> ';
            html += this._escapeHtml(data.message);
            html += '</div>';
        } else {
            html += '<div class="gp-edit-header">';
            html += '<i class="fas fa-file-code"></i> ';
            html += '完整页面已重新生成';
            html += '</div>';
        }

        if (data.edit_results) {
            for (const edit of data.edit_results) {
                const cls = edit.applied ? 'gp-edit-applied' : 'gp-edit-failed';
                const icon = edit.applied ? 'fa-check' : 'fa-times';
                html += '<div class="gp-edit-item ' + cls + '">';
                html += '<i class="fas ' + icon + '"></i> ';
                html += '[' + this._escapeHtml(edit.page || '') + '] ';
                html += this._escapeHtml((edit.search_snippet || '').substring(0, 80));
                if (!edit.applied && edit.error) {
                    const errorMap = {
                        'not_found': '未找到匹配',
                        'multiple_matches': '匹配多处，需更多上下文',
                        'page_not_found': '页面不存在'
                    };
                    html += ' <span class="gp-edit-error">(' +
                            (errorMap[edit.error] || edit.error) + ')</span>';
                }
                html += '</div>';
            }
        }

        // JS 诊断警告（结构化显示）
        if (data.js_warnings && data.js_warnings.length > 0) {
            html += '<div class="gp-diagnostic-panel">';
            html += '<div class="gp-diagnostic-header">';
            html += '<i class="fas fa-stethoscope"></i> ';
            html += '诊断结果 (' + data.js_warnings.length + ')';
            html += '</div>';
            for (const diag of data.js_warnings) {
                const msg = typeof diag === 'string' ? diag : (diag.message || diag);
                const sev = (typeof diag === 'object' && diag.severity) ? diag.severity : 'error';
                const rule = (typeof diag === 'object' && diag.rule) ? diag.rule : '';
                const line = (typeof diag === 'object' && diag.line) ? diag.line : '';
                const sevClass = sev === 'error' ? 'gp-diag-error' : 'gp-diag-warning';
                const sevIcon = sev === 'error' ? 'fa-times-circle' : 'fa-exclamation-triangle';
                html += '<div class="gp-diagnostic-item ' + sevClass + '">';
                html += '<i class="fas ' + sevIcon + '"></i> ';
                if (line) html += '<span class="gp-diag-line">L' + line + '</span> ';
                html += '<span class="gp-diag-msg">' + this._escapeHtml(msg) + '</span>';
                if (rule) html += ' <span class="gp-diag-rule">' + this._escapeHtml(rule) + '</span>';
                html += '</div>';
            }
            html += '</div>';
        }

        // 回滚按钮
        if (data.can_rollback && this.projectId) {
            const pid = this.projectId;
            html += '<button onclick="window._gp._rollbackEdit()" ' +
                    'style="margin-top:6px;padding:2px 10px;font-size:11px;' +
                    'border:1px solid #d1d5db;border-radius:4px;cursor:pointer;' +
                    'background:#f9fafb;color:#374151;" ' +
                    'title="恢复到本次编辑前的版本">' +
                    '<i class="fas fa-undo" style="margin-right:4px;"></i>回滚</button>';
        }

        html += '</div>';
        summary.innerHTML = '<div class="gp-chat-text">' + html + '</div>';
        chatContent.appendChild(summary);
        this._scrollToBottom();
    }

    // ==================== 回滚操作 ====================

    _rollbackEdit() {
        if (!this.projectId) return;
        fetch('/api/chat-rollback', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ projectId: this.projectId })
        }).then(r => r.json()).then(result => {
            if (result.success) {
                this._addChatBubble('system', '已回滚到编辑前版本');
                // 通知预览器刷新
                try {
                    localStorage.setItem('prototype-update-' + this.projectId, Date.now().toString());
                    localStorage.setItem('prototype-update-signal', this.projectId);
                } catch (e) { /* ignore */ }
            } else {
                this._addChatBubble('system', '回滚失败: ' + (result.error || '未知错误'));
            }
        }).catch(err => {
            this._addChatBubble('system', '回滚请求失败: ' + err.message);
        });
    }

    // ==================== Tool Call 实时显示 ====================

    _onToolCallProgress(data) {
        /**实时显示 AI 的 tool call 操作过程（类似 Claude Code） */
        const chatContent = document.getElementById('gpChatContent');
        if (!chatContent) return;

        const tcId = data.tool_call_id || ('tc-' + Date.now());
        let el = document.getElementById(tcId);

        if (!el) {
            // 新建 tool call 显示元素
            el = document.createElement('div');
            el.id = tcId;
            el.className = 'gp-chat-bubble gp-chat-tool';
            chatContent.appendChild(el);
        }

        // 根据状态渲染
        const status = data.status || 'pending'; // pending | running | applied | failed
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
                statusText = rPage ? `读取 ${rPage}${rLine ? ' ' + rLine : ''}...` : '读取页面中...';
            } else {
                statusText = isFullPage ? '整页替换中...' : '编辑中...';
            }
            statusColor = '#6366f1';
        } else if (status === 'applied') {
            iconClass = 'fas fa-check-circle';
            statusText = isReadPage ? '已读取' : '已应用';
            statusColor = '#22c55e';
        } else if (status === 'failed') {
            iconClass = 'fas fa-times-circle';
            statusText = data.error || '失败';
            statusColor = '#ef4444';
        }

        let html = '<div class="gp-tool-call">';
        html += '<div class="gp-tool-header">';
        html += '<i class="fas ' + (isReadPage ? 'fa-eye' : 'fa-file-code') + '" style="color:#818cf8;margin-right:4px;"></i> ';
        if (isReadPage) {
            // read_page: 显示 "读取文件 XXX 第 xx-xx 行"
            const lineDesc = data.line_desc || '';
            const fileName = page || data.page || '';
            let readLabel = '读取文件';
            if (fileName) readLabel += ' ' + this._escapeHtml(fileName);
            if (lineDesc) readLabel += ' ' + this._escapeHtml(lineDesc);
            html += '<span class="gp-tool-name" style="color:#c4b5fd;">' + readLabel + '</span>';
        } else {
            html += '<span class="gp-tool-name">edit_file</span>';
        }
        html += ' <span style="color:' + statusColor + ';font-size:11px;">';
        html += '<i class="' + iconClass + '"></i> ' + statusText;
        html += '</span>';
        html += '</div>';

        if (isReadPage) {
            // read_page 详情行
            const contentLen = data.content_length || 0;
            const lineDesc = data.line_desc || '';
            if (page || lineDesc) {
                html += '<div class="gp-tool-detail" style="color:#9ca3af;font-size:11px;">';
                if (page && !lineDesc) html += this._escapeHtml(page);
                if (contentLen > 0) html += (page ? ' · ' : '') + contentLen + ' 字符';
                html += '</div>';
            }
        } else if (page) {
            html += '<div class="gp-tool-detail">';
            html += '<span class="gp-tool-page">' + this._escapeHtml(page) + '</span>';
            if (!isFullPage && snippet) {
                html += ' <span class="gp-tool-snippet">' + this._escapeHtml(snippet) + '</span>';
            } else if (isFullPage) {
                html += ' <span class="gp-tool-snippet">完整页面替换</span>';
            }
            html += '</div>';
        }

        // diff 展示：unified diff 渲染
        const oldText = data.old_text || data.old_snippet || '';
        const newText = data.new_text || data.new_snippet || '';
        if (oldText || newText) {
            const uid = 'uvd-gp-' + tcId.replace(/[^a-zA-Z0-9]/g, '');
            html += (typeof DiffViewer !== 'undefined')
                ? DiffViewer.render(oldText, newText, { id: uid })
                : '<div class="gp-diff"><div class="gp-diff-body show gp-diff-old">'
                  + '<div class="gp-diff-label">- Original</div>'
                  + this._escapeHtml(oldText) + '</div>'
                  + '<div class="gp-diff-new"><div class="gp-diff-label">+ Modified</div>'
                  + this._escapeHtml(newText) + '</div></div>';
        }

        html += '</div>';
        el.innerHTML = '<div class="gp-chat-text">' + html + '</div>';

        this._scrollToBottom();
    }

    // ==================== Complete / Error ====================

    _onPhaseComplete(data) {
        if (data.totalPages) {
            this._addStep('gp-phase-complete', 'done', '全部 ' + data.totalPages + ' 页生成完成');
        }
    }

    _onErrorEvent(data) {
        this._addStep('gp-error-' + Date.now(), 'error', '错误' +
            (data.page ? ' (' + data.page + ')' : '') + ': ' + (data.message || '未知'));
    }

    _onChatEvent(data) {
        this._appendChatStream(data.content || '');
    }

    // ==================== 校验事件 ====================

    _onDiagnosticEvent(data) {
        const content = document.getElementById('gpContent');
        if (!content) return;

        // 辅助：确保审查步骤和子动作容器存在
        const ensureReviewUI = () => {
            let stepEl = document.getElementById('step-gp-diagnostic');
            if (!stepEl) {
                this._addStep('gp-diagnostic', 'running', '智能审查: 分析页面...');
                stepEl = document.getElementById('step-gp-diagnostic');
            }
            // 子动作容器作为 step 的兄弟元素（不受 _updateStep innerHTML 替换影响）
            let actionsEl = document.getElementById('gp-diag-actions');
            if (!actionsEl && stepEl) {
                actionsEl = document.createElement('div');
                actionsEl.id = 'gp-diag-actions';
                actionsEl.className = 'gp-review-actions';
                stepEl.appendChild(actionsEl);
            }
            return { stepEl, actionsEl };
        };

        // 辅助：添加一行子动作
        const addAction = (icon, text, color) => {
            const { actionsEl } = ensureReviewUI();
            if (!actionsEl) return;
            const row = document.createElement('div');
            row.className = 'gp-review-action-row';
            row.innerHTML = `<span class="gp-review-action-icon" style="color:${color}">${icon}</span>` +
                            `<span class="gp-review-action-text" style="color:${color}">${this._escapeHtml(text)}</span>`;
            actionsEl.appendChild(row);
            // 保留最多 30 条子动作，防止 DOM 过大
            while (actionsEl.children.length > 30) {
                actionsEl.removeChild(actionsEl.firstChild);
            }
            this._scrollToBottom();
        };

        if (data.status === 'reviewing') {
            const { stepEl } = ensureReviewUI();

            if (data.action === 'analysis') {
                // AI 分析文本 — 截取关键行展示
                const text = data.message || '';
                // 只显示非空、有意义的行（去掉 markdown 标记）
                const lines = text.split('\n').filter(l => l.trim().length > 5).slice(0, 5);
                const display = lines.join('\n');
                if (display.length > 10) {
                    addAction('💭', display.length > 200 ? display.substring(0, 200) + '...' : display, '#8b5cf6');
                }

            } else if (data.action === 'read') {
                // 读取文件动作
                addAction('📖', data.message || '读取代码', '#6366f1');

            } else if (data.action === 'edit_fail') {
                // 编辑失败
                addAction('⚠️', data.message || '编辑匹配失败', '#f59e0b');

            } else {
                // 默认 reviewing 状态 — 更新步骤标签
                if (stepEl) {
                    const labelEl = stepEl.querySelector('.gp-step-label');
                    if (labelEl) labelEl.textContent = '智能审查: ' + data.message;
                }
                // 显示检查清单（首次）
                if (data.checks && !document.getElementById('gp-diag-checklist')) {
                    const { actionsEl } = ensureReviewUI();
                    if (actionsEl) {
                        const checklist = document.createElement('div');
                        checklist.id = 'gp-diag-checklist';
                        checklist.className = 'gp-review-checklist';
                        checklist.innerHTML = data.checks.map(c =>
                            `<div class="gp-review-check-item">○ ${this._escapeHtml(c)}</div>`
                        ).join('');
                        actionsEl.appendChild(checklist);
                    }
                }
            }
            this._scrollToBottom();

        } else if (data.status === 'fixing') {
            if (data.action === 'edit') {
                // 修复成功
                addAction('✏️', data.message || '修复完成', '#f59e0b');
            } else {
                // 更新步骤标签
                const { stepEl } = ensureReviewUI();
                if (stepEl) {
                    const labelEl = stepEl.querySelector('.gp-step-label');
                    if (labelEl) labelEl.textContent = '智能审查: ' + data.message;
                }
            }
            this._scrollToBottom();

        } else if (data.status === 'errors_found') {
            // 发现错误，显示修复中步骤
            this._addStep('gp-diagnostic', 'running', '校验: ' + data.message);

            // 显示错误详情
            if (data.errors && data.errors.length > 0) {
                const { actionsEl } = ensureReviewUI();
                if (actionsEl) {
                    const errorItems = data.errors.slice(0, 5).map(e =>
                        `<div style="font-size:11px;color:#ef4444;padding:1px 0;">● ${this._escapeHtml(e.message)}</div>`
                    ).join('');
                    const detailEl = document.createElement('div');
                    detailEl.className = 'gp-artifact';
                    detailEl.innerHTML = errorItems +
                        (data.errors.length > 5 ? `<div style="font-size:10px;color:#9ca3af;">...还有 ${data.errors.length - 5} 个问题</div>` : '');
                    actionsEl.appendChild(detailEl);
                }
            }
            this._scrollToBottom();

        } else if (data.status === 'fixed') {
            // 修复完成
            this._updateStep('gp-diagnostic', 'done');
            const stepEl = document.getElementById('step-gp-diagnostic');
            if (stepEl) {
                const labelEl = stepEl.querySelector('.gp-step-label');
                if (labelEl) labelEl.textContent = '校验: ' + data.message;
            }
            addAction('✓', '已自动修复 ' + data.before_errors + ' 个错误' +
                (data.after_errors > 0 ? '，剩余 ' + data.after_errors + ' 个警告' : ''), '#22c55e');
            this._scrollToBottom();

        } else if (data.status === 'passed') {
            // 审查/校验通过
            let stepEl = document.getElementById('step-gp-diagnostic');
            if (stepEl) {
                this._updateStep('gp-diagnostic', 'done');
                const labelEl = stepEl.querySelector('.gp-step-label');
                if (labelEl) labelEl.textContent = '智能审查: ' + data.message;
            } else {
                this._addStep('gp-diagnostic', 'done', '智能审查: ' + data.message);
            }
            // 最终结果行
            addAction('✓', data.message || '审查通过', '#22c55e');
            this._scrollToBottom();
        }
    }

    _escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    // ==================== 思考过程显示 ====================

    _showThinking() {
        let thinkBlock = document.getElementById('gp-thinking');
        if (!thinkBlock) {
            this._removeStep('init');

            const content = document.getElementById('gpContent');
            if (!content) return;

            thinkBlock = document.createElement('div');
            thinkBlock.id = 'gp-thinking';
            thinkBlock.className = 'gp-thinking-block';
            content.appendChild(thinkBlock);
        }

        const display = this.thinkingText.length > 800
            ? this.thinkingText.slice(0, 400) + '\n...\n' + this.thinkingText.slice(-400)
            : this.thinkingText;

        thinkBlock.innerHTML =
            '<div class="gp-thinking-toggle" onclick="this.nextElementSibling.classList.toggle(\'hidden\')">' +
            '  <span class="gp-thinking-icon"></span>' +
            '  <span>AI 思考过程</span>' +
            '  <span class="gp-thinking-meta">' + this.thinkingText.length.toLocaleString() + ' 字符</span>' +
            '  <i class="fas fa-chevron-right gp-artifact-chevron"></i>' +
            '</div>' +
            '<div class="gp-thinking-content">' +
            '<pre>' + this._escapeHtml(display) + '</pre></div>';

        this._scrollToBottom();
    }

    // ==================== 原始流显示 ====================

    _showRawStream() {
        this._removeStep('init');

        // 多轮生成时：每个 phase 步骤有自己的流区域
        // 单页生成时：使用全局流区域
        const isMultiRound = this.roundInfo.round && this.roundInfo.round > 0;
        const streamId = isMultiRound
            ? 'gp-raw-' + this.roundInfo.round + '-' + this.roundInfo.step
            : 'gp-raw-stream';

        let streamBlock = document.getElementById(streamId);
        if (!streamBlock) {
            const content = isMultiRound
                ? document.getElementById('step-gp-phase-' + this.roundInfo.round + '-' + this.roundInfo.step)
                : document.getElementById('gpContent');
            if (!content) return;

            streamBlock = document.createElement('div');
            streamBlock.id = streamId;
            streamBlock.className = 'gp-raw-stream-block';
            content.appendChild(streamBlock);
        }

        const len = this.accumulatedContent.length;
        const display = len > 1500
            ? '...\n' + this.accumulatedContent.slice(-1200)
            : this.accumulatedContent;

        // 优化：只更新 pre 内容，避免重建整个 DOM 导致滚动重置
        const preEl = streamBlock.querySelector('pre');
        const metaEl = streamBlock.querySelector('.gp-artifact-meta');

        if (preEl && metaEl) {
            preEl.textContent = display;
            metaEl.textContent = len.toLocaleString() + ' 字符';
        } else {
            // 首次创建
            const label = isMultiRound ? 'AI 输出' : 'AI 输出';
            streamBlock.innerHTML =
                '<div class="gp-raw-toggle" onclick="this.nextElementSibling.classList.toggle(\'hidden\')">' +
                '  <i class="fas fa-code"></i>' +
                '  <span>' + label + '</span>' +
                '  <span class="gp-artifact-meta">' + len.toLocaleString() + ' 字符</span>' +
                '  <i class="fas fa-chevron-right gp-artifact-chevron"></i>' +
                '</div>' +
                '<div class="gp-artifact-code"><pre></pre></div>';
            streamBlock.querySelector('pre').textContent = display;
        }

        this._scrollToBottom();
    }

    // ==================== 完成处理 ====================

    _onGenerationComplete(statusData) {
        this._finishGeneration();

        const content = document.getElementById('gpContent');
        if (content) {
            content.querySelectorAll('.gp-status-running').forEach(el => {
                el.classList.remove('gp-status-running');
                el.classList.add('gp-status-done');
            });
        }

        this._addStep('gp-done', 'done', '生成完成');

        this.chatMode = true;
        const inputArea = document.getElementById('gpInputArea');
        if (inputArea) inputArea.style.display = 'flex';

        this._addChatBubble('system', '生成完成！可以在下方输入调整指令来修改原型，如「把表格改成卡片布局」。');

        // 如果处于最小化状态，更新迷你栏
        if (this._minimized) {
            this._updateMiniBar('生成完成');
        }

        const idx = this._findProjectIndex();
        if (idx !== -1) {
            allProjects[idx].status = null;
            if (typeof renderProjectList === 'function') renderProjectList();
            if (typeof showToast === 'function') showToast('"' + allProjects[idx].name + '" 生成完成！');
        }
    }

    _finishGeneration() {
        this.isGenerating = false;
        this._stopTimer();

        const elapsed = this._getElapsedText();
        const headerStatus = document.getElementById('gpHeaderStatus');
        if (headerStatus) headerStatus.textContent = '已完成，用时 ' + elapsed;
        const spinner = document.getElementById('gpSpinner');
        if (spinner) spinner.style.display = 'none';
    }

    // ==================== 步骤管理 ====================

    _addStep(id, status, label) {
        const content = document.getElementById('gpContent');
        if (!content) return;

        let el = document.getElementById('step-' + id);
        if (el) {
            this._updateStep(id, status);
            return;
        }

        el = document.createElement('div');
        el.id = 'step-' + id;
        el.className = 'gp-step';

        el.innerHTML = this._buildStepHTML(id, status, label);
        content.appendChild(el);
        this._scrollToBottom();
    }

    _updateStep(id, status) {
        const el = document.getElementById('step-' + id);
        if (!el) return;

        const labelEl = el.querySelector('.gp-step-label');
        const label = labelEl ? labelEl.textContent : '';

        // 保存子元素（如审查动作流、产物详情等），避免 innerHTML 替换时丢失
        const savedChildren = [];
        while (el.firstChild) {
            const child = el.firstChild;
            if (child.classList && (
                child.classList.contains('gp-review-actions') ||
                child.classList.contains('gp-artifact') ||
                child.classList.contains('gp-page-previews') ||
                child.classList.contains('gp-review-checklist')
            )) {
                savedChildren.push(el.removeChild(child));
            } else {
                el.removeChild(child);
            }
        }

        el.innerHTML = this._buildStepHTML(id, status, label);

        // 恢复子元素
        for (const child of savedChildren) {
            el.appendChild(child);
        }
    }

    _removeStep(id) {
        const el = document.getElementById('step-' + id);
        if (el) el.remove();
    }

    _buildStepHTML(id, status, label) {
        let iconClass = '';
        let statusClass = '';

        if (status === 'done') {
            iconClass = 'gp-icon-done';
            statusClass = 'gp-status-done';
        } else if (status === 'running') {
            iconClass = 'gp-icon-running';
            statusClass = 'gp-status-running';
        } else if (status === 'error') {
            iconClass = 'gp-icon-error';
            statusClass = 'gp-status-error';
        } else {
            iconClass = 'gp-icon-pending';
            statusClass = 'gp-status-pending';
        }

        return '<div class="gp-step-row ' + statusClass + '">' +
               '  <span class="gp-step-icon ' + iconClass + '"></span>' +
               '  <span class="gp-step-label">' + this._escapeHtml(label) + '</span>' +
               '</div>';
    }

    // ==================== 对话功能 ====================

    sendChatMessage(text) {
        if (!text.trim() || !this.projectId) return;
        if (this._chatSending) return;

        this._addChatBubble('user', text);

        const input = document.getElementById('gpChatInput');
        if (input) input.value = '';

        // 中止之前的请求
        if (this._chatAbortController) {
            this._chatAbortController.abort();
            this._chatAbortController = null;
        }
        this._chatAbortController = new AbortController();
        this._chatSending = true;
        this._setSendBtnStop(true);

        fetch('/api/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                projectId: this.projectId,
                message: text,
                targetPage: this.currentPage || '',
                conversationId: this.activeConversationId
            }),
            signal: this._chatAbortController.signal
        }).then(response => {
            if (!response.ok) throw new Error('请求失败');

            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';

            this._chatStreamEl = null;

            const readChunk = () => {
                reader.read().then(({ done, value }) => {
                    if (done) {
                        this._chatStreamEl = null;
                        this._finishChat();
                        return;
                    }

                    buffer += decoder.decode(value, { stream: true });
                    const lines = buffer.split('\n');
                    buffer = lines.pop() || '';

                    for (const line of lines) {
                        if (line.startsWith('data: ')) {
                            const payload = line.slice(6).trim();
                            if (payload === '[DONE]') {
                                this._chatStreamEl = null;
                                this._finishChat();
                                return;
                            }

                            try {
                                const evt = JSON.parse(payload);
                                if (evt.type === 'chat' && evt.data) {
                                    this._appendChatStream(evt.data.content || '');
                                } else if (evt.type === 'tool_call_progress' && evt.data) {
                                    this._onToolCallProgress(evt.data);
                                } else if (evt.type === 'edit_result' && evt.data) {
                                    this._onEditResult(evt.data);
                                } else if (evt.type === 'preview_update' && evt.data) {
                                    this._onPreviewUpdate(evt.data);
                                }
                            } catch (e) {
                                // 非JSON行忽略
                            }
                        }
                    }

                    readChunk();
                });
            };

            readChunk();
        }).catch(err => {
            if (err.name === 'AbortError') {
                this._addChatBubble('system', '已中断');
            } else {
                this._addChatBubble('system', '发送失败: ' + err.message);
            }
            this._finishChat();
        });
    }

    _finishChat() {
        this._chatSending = false;
        this._chatAbortController = null;
        this._setSendBtnStop(false);
    }

    _setSendBtnStop(isStop) {
        const sendBtn = document.getElementById('gpSendBtn');
        if (!sendBtn) return;
        if (isStop) {
            sendBtn.textContent = '停止';
            sendBtn.style.background = '#ef4444';
            sendBtn.onclick = () => {
                if (this._chatAbortController) {
                    this._chatAbortController.abort();
                    this._chatAbortController = null;
                }
                this._finishChat();
            };
        } else {
            sendBtn.textContent = '发送';
            sendBtn.style.background = '';
            sendBtn.onclick = () => {
                const input = document.getElementById('gpChatInput');
                if (input) this.sendChatMessage(input.value);
            };
        }
    }

    _appendChatStream(content) {
        const chatContent = document.getElementById('gpChatContent');
        if (!chatContent) return;

        if (!this._chatStreamEl) {
            this._chatStreamEl = document.createElement('div');
            this._chatStreamEl.className = 'gp-chat-bubble gp-chat-ai';
            this._chatStreamEl.innerHTML = '<div class="gp-chat-text"></div>';
            chatContent.appendChild(this._chatStreamEl);
        }

        const textEl = this._chatStreamEl.querySelector('.gp-chat-text');
        if (textEl) textEl.textContent += content;

        this._scrollToBottom();
    }

    _addChatBubble(role, text) {
        const chatContent = document.getElementById('gpChatContent');
        if (!chatContent) return;

        const bubble = document.createElement('div');
        bubble.className = 'gp-chat-bubble gp-chat-' + role;
        bubble.innerHTML = '<div class="gp-chat-text">' + this._escapeHtml(text) + '</div>';
        chatContent.appendChild(bubble);

        this._scrollToBottom();
    }

    // ==================== 操作按钮 ====================

    _onClose() {
        // 关闭 = 最小化到迷你栏（继续后台生成）
        if (this.isGenerating) {
            this._onMinimize();
        } else {
            this.destroy();
        }
    }

    async _onStop() {
        if (this.projectId) {
            try {
                const res = await fetch('/api/stop-generation', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ id: this.projectId })
                });
                const data = await res.json();
                if (!data.success) {
                    this._addStep('gp-cancelled', 'error', data.message || '停止失败');
                    return;
                }
            } catch (e) {
                this._addStep('gp-cancelled', 'error', '停止请求失败');
                return;
            }
        }
        this._addStep('gp-cancelled', 'error', '正在停止...');
    }

    _onOpenPreview() {
        if (this.projectId) {
            window.open('/projects/' + this.projectId + '/index.html', '_blank');
        }
    }

    // ==================== HTML 模板 ====================

    _createPanelHTML() {
        const div = document.createElement('div');
        div.id = 'generationPanel';
        div.className = 'fixed inset-0 hidden items-center justify-center z-50';
        div.innerHTML = this._getPanelTemplate();
        document.body.appendChild(div);

        // 绑定事件
        const closeBtn = div.querySelector('#gpCloseBtn');
        if (closeBtn) closeBtn.addEventListener('click', () => this._onClose());

        const minimizeBtn = div.querySelector('#gpMinimizeBtn');
        if (minimizeBtn) minimizeBtn.addEventListener('click', () => this._onMinimize());

        const stopBtn = div.querySelector('#gpStopBtn');
        if (stopBtn) stopBtn.addEventListener('click', () => this._onStop());

        const openBtn = div.querySelector('#gpOpenBtn');
        if (openBtn) openBtn.addEventListener('click', () => this._onOpenPreview());

        const sendBtn = div.querySelector('#gpSendBtn');
        if (sendBtn) sendBtn.addEventListener('click', () => {
            const input = document.getElementById('gpChatInput');
            if (input) this.sendChatMessage(input.value);
        });

        const chatInput = div.querySelector('#gpChatInput');
        if (chatInput) chatInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                this.sendChatMessage(chatInput.value);
            }
        });
    }

    _getPanelTemplate() {
        return `
<style>
/* ---- 遮罩 + 居中弹出框 ---- */
#generationPanel { background: rgba(0,0,0,0.45); backdrop-filter: blur(4px); }
.gp-container { width: 680px; max-width: 95vw; height: 75vh; max-height: 745px; background: #fff; border-radius: 16px; display: flex; flex-direction: column; box-shadow: 0 25px 50px -12px rgba(0,0,0,0.25); overflow: hidden; animation: gp-slide-up 0.2s ease-out; }
@keyframes gp-slide-up { from { opacity: 0; transform: translateY(20px); } to { opacity: 1; transform: translateY(0); } }

/* ---- 头部 ---- */
.gp-header { display: flex; align-items: center; justify-content: space-between; padding: 14px 20px; border-bottom: 1px solid #f0f0f0; flex-shrink: 0; }
.gp-header-left { display: flex; align-items: center; gap: 10px; }
.gp-header-title { font-size: 14px; font-weight: 600; color: #1f2937; }
.gp-header-status { font-size: 12px; color: #9ca3af; margin-left: 8px; }
.gp-spinner { width: 16px; height: 16px; border: 2px solid #e5e7eb; border-top-color: #6366f1; border-radius: 50%; animation: gp-spin 0.8s linear infinite; display: inline-block; }
@keyframes gp-spin { to { transform: rotate(360deg); } }
.gp-header-actions { display: flex; gap: 2px; }
.gp-header-btn { background: none; border: none; color: #9ca3af; cursor: pointer; padding: 6px 8px; border-radius: 6px; font-size: 13px; transition: all 0.15s; }
.gp-header-btn:hover { background: #f3f4f6; color: #4b5563; }

/* ---- 内容区 ---- */
.gp-content { flex: 1; min-height: 0; overflow-y: auto; padding: 16px 20px; display: flex; flex-direction: column; justify-content: flex-start; }
.gp-content-spacer { flex-shrink: 0; }

/* ---- 步骤行 ---- */
.gp-step { margin-bottom: 4px; }
.gp-step-row { display: flex; align-items: center; gap: 10px; padding: 6px 0; min-height: 32px; }
.gp-step-label { font-size: 13px; color: #374151; line-height: 1.4; }

/* 步骤图标 */
.gp-step-icon { width: 20px; height: 20px; border-radius: 50%; display: flex; align-items: center; justify-content: center; flex-shrink: 0; position: relative; }
.gp-icon-done { background: #22c55e; }
.gp-icon-done::after { content: ''; width: 6px; height: 10px; border: 2px solid #fff; border-top: none; border-left: none; transform: rotate(45deg) translateY(-1px); }
.gp-icon-running { background: transparent; border: 2px solid #e5e7eb; border-top-color: #6366f1; animation: gp-spin 0.8s linear infinite; }
.gp-icon-error { background: #ef4444; }
.gp-icon-error::after { content: ''; width: 8px; height: 2px; background: #fff; border-radius: 1px; }
.gp-icon-pending { background: transparent; border: 2px solid #e5e7eb; }

/* 状态色 */
.gp-status-done .gp-step-label { color: #6b7280; }
.gp-status-running .gp-step-label { color: #1f2937; font-weight: 500; }
.gp-status-error .gp-step-label { color: #ef4444; }

/* ---- 产物（可折叠代码块） ---- */
.gp-artifact { margin: 2px 0 6px 30px; }
.gp-artifact-toggle { display: flex; align-items: center; gap: 6px; padding: 4px 8px; border-radius: 6px; cursor: pointer; font-size: 12px; color: #6b7280; transition: background 0.15s; }
.gp-artifact-toggle:hover { background: #f9fafb; }
.gp-artifact-toggle i:first-child { color: #818cf8; font-size: 11px; }
.gp-artifact-meta { font-size: 11px; color: #d1d5db; margin-left: 4px; }
.gp-artifact-chevron { font-size: 10px; margin-left: auto; transition: transform 0.15s; color: #d1d5db; }
.gp-artifact-code { margin: 4px 0 8px 30px; background: #1e293b; border-radius: 8px; overflow: hidden; }
.gp-artifact-code pre { padding: 10px 14px; color: #e2e8f0; font-size: 11px; font-family: 'Menlo','Consolas','Courier New',monospace; white-space: pre-wrap; word-break: break-all; max-height: 240px; overflow-y: auto; margin: 0; line-height: 1.5; }

/* ---- 页面预览缩略图 ---- */
.gp-page-previews { display: flex; gap: 10px; margin: 4px 0 8px 30px; flex-wrap: wrap; }
.gp-preview-card { width: 160px; border: 1px solid #e5e7eb; border-radius: 8px; overflow: hidden; background: #fff; }
.gp-preview-label { padding: 4px 8px; font-size: 11px; color: #6b7280; background: #f9fafb; border-bottom: 1px solid #e5e7eb; }
.gp-preview-thumb-wrap { position: relative; width: 160px; height: 120px; overflow: hidden; }
.gp-preview-thumb { position: absolute; top: 0; left: 0; width: 390px; height: 300px; border: none; transform: scale(0.4103); transform-origin: top left; pointer-events: none; }

/* ---- 思考过程 ---- */
.gp-thinking-block { margin: 8px 0; }
.gp-thinking-toggle { display: flex; align-items: center; gap: 6px; padding: 6px 0; cursor: pointer; font-size: 13px; color: #9ca3af; }
.gp-thinking-toggle:hover { color: #6b7280; }
.gp-thinking-icon { width: 16px; height: 16px; border-radius: 50%; background: #f3e8ff; display: inline-flex; align-items: center; justify-content: center; }
.gp-thinking-icon::after { content: '?'; font-size: 10px; color: #8b5cf6; font-weight: 700; }
.gp-thinking-meta { font-size: 11px; color: #d1d5db; margin-left: 4px; }
.gp-thinking-content { margin: 4px 0 8px 22px; }
.gp-thinking-content pre { padding: 8px 12px; background: #faf5ff; border-radius: 6px; font-size: 11px; color: #7c3aed; white-space: pre-wrap; word-break: break-all; max-height: 200px; overflow-y: auto; margin: 0; font-family: 'Menlo','Consolas','Courier New',monospace; line-height: 1.5; }

/* ---- 原始流（单页） ---- */
.gp-raw-stream-block { margin: 8px 0; }
.gp-raw-toggle { display: flex; align-items: center; gap: 6px; padding: 6px 0; cursor: pointer; font-size: 13px; color: #9ca3af; }
.gp-raw-toggle:hover { color: #6b7280; }
.gp-raw-toggle i { color: #6366f1; font-size: 12px; }

/* ---- 对话区 ---- */
.gp-chat-area { flex-shrink: 0; border-top: 1px solid #f0f0f0; display: flex; flex-direction: column; max-height: 35%; min-height: 0; }
.gp-chat-content { flex: 1; min-height: 0; overflow-y: auto; padding: 12px 20px; }
.gp-chat-bubble { margin-bottom: 10px; }
.gp-chat-text { font-size: 13px; line-height: 1.6; white-space: pre-wrap; word-break: break-word; }
.gp-chat-user .gp-chat-text { color: #1f2937; font-weight: 500; }
.gp-chat-ai .gp-chat-text { color: #374151; }
.gp-chat-system .gp-chat-text { color: #6b7280; font-size: 12px; font-style: italic; }
.gp-edit-summary { background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 8px; padding: 8px 12px; margin: 4px 0; font-style: normal; }
.gp-edit-header { font-size: 12px; font-weight: 600; color: #15803d; margin-bottom: 6px; }
.gp-edit-header i { margin-right: 4px; }
.gp-edit-item { font-size: 11px; color: #374151; padding: 2px 0; font-family: monospace; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.gp-edit-applied i { color: #22c55e; margin-right: 4px; }
.gp-edit-failed i { color: #ef4444; margin-right: 4px; }
.gp-edit-error { color: #ef4444; font-size: 10px; }

/* Tool call 实时显示 */
.gp-chat-tool .gp-chat-text { font-style: normal; }
.gp-tool-call { background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 6px 10px; font-size: 12px; }
.gp-tool-header { display: flex; align-items: center; gap: 6px; }
.gp-tool-name { font-family: 'Menlo','Consolas',monospace; font-size: 11px; color: #4f46e5; background: #eef2ff; padding: 1px 6px; border-radius: 4px; }
.gp-tool-detail { margin-top: 3px; padding-left: 18px; color: #6b7280; }
.gp-tool-page { font-weight: 600; color: #374151; }
.gp-tool-snippet { font-family: 'Menlo','Consolas',monospace; font-size: 10px; color: #9ca3af; max-width: 400px; display: inline-block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; vertical-align: middle; }
/* gp-diff 样式已迁移到 DiffViewer (uv-diff-*) */

/* 诊断面板 */
.gp-diagnostic-panel { background: #fef2f2; border: 1px solid #fecaca; border-radius: 6px; padding: 6px 10px; margin-top: 6px; }
.gp-diagnostic-header { font-size: 12px; font-weight: 600; color: #991b1b; margin-bottom: 4px; }
.gp-diagnostic-header i { margin-right: 4px; }
.gp-diagnostic-item { font-size: 11px; padding: 2px 0; display: flex; align-items: baseline; gap: 4px; }
.gp-diag-error { color: #dc2626; }
.gp-diag-error i { color: #dc2626; }
.gp-diag-warning { color: #d97706; }
.gp-diag-warning i { color: #d97706; }
.gp-diag-line { font-family: 'Menlo','Consolas',monospace; font-size: 10px; background: #fee2e2; color: #991b1b; padding: 0 4px; border-radius: 2px; flex-shrink: 0; }

/* 审查动作流 */
.gp-review-actions { margin: 2px 0 4px 30px; display: flex; flex-direction: column; gap: 2px; }
.gp-review-action-row { display: flex; align-items: flex-start; gap: 6px; padding: 2px 0; }
.gp-review-action-icon { font-size: 12px; flex-shrink: 0; width: 16px; text-align: center; line-height: 1.4; }
.gp-review-action-text { font-size: 11px; line-height: 1.4; word-break: break-all; }
.gp-review-checklist { margin: 2px 0 6px 16px; }
.gp-review-check-item { font-size: 11px; color: #6b7280; padding: 1px 0; }
.gp-diag-msg { flex: 1; }
.gp-diag-rule { font-family: 'Menlo','Consolas',monospace; font-size: 9px; color: #9ca3af; background: #f3f4f6; padding: 0 3px; border-radius: 2px; flex-shrink: 0; }

/* 输入区 */
.gp-input-area { display: none; align-items: center; gap: 8px; padding: 12px 20px; border-top: 1px solid #f0f0f0; background: #fafafa; }
.gp-input { flex: 1; border: 1px solid #e5e7eb; border-radius: 8px; padding: 8px 14px; font-size: 13px; outline: none; transition: border-color 0.15s, box-shadow 0.15s; }
.gp-input:focus { border-color: #6366f1; box-shadow: 0 0 0 2px rgba(99,102,241,0.1); }
.gp-send-btn { background: #6366f1; color: #fff; border: none; border-radius: 8px; padding: 8px 16px; font-size: 13px; cursor: pointer; transition: background 0.15s; white-space: nowrap; }
.gp-send-btn:hover { background: #4f46e5; }

/* ---- 最小化浮动条 ---- */
.gp-minibar { position: fixed; bottom: 24px; right: 24px; height: 44px; background: #fff; border-radius: 12px; box-shadow: 0 4px 24px rgba(0,0,0,0.12); display: flex; align-items: center; gap: 10px; padding: 0 16px; z-index: 50; cursor: default; animation: gp-fade-in 0.2s ease-out; }
@keyframes gp-fade-in { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }
.gp-minibar-spinner { width: 14px; height: 14px; border: 2px solid #e5e7eb; border-top-color: #6366f1; border-radius: 50%; animation: gp-spin 0.8s linear infinite; flex-shrink: 0; }
.gp-minibar-title { font-size: 13px; font-weight: 600; color: #1f2937; white-space: nowrap; }
.gp-minibar-status { font-size: 12px; color: #9ca3af; white-space: nowrap; }
.gp-minibar-btn { background: none; border: none; color: #9ca3af; cursor: pointer; padding: 4px 6px; border-radius: 4px; font-size: 12px; transition: all 0.15s; }
.gp-minibar-btn:hover { background: #f3f4f6; color: #4b5563; }

/* ---- 对话标签 ---- */
.gp-conv-tabs { display: flex; align-items: center; gap: 4px; padding: 6px 12px; border-bottom: 1px solid #f0f0f0; overflow-x: auto; flex-shrink: 0; background: #fafafa; }
.gp-conv-tabs::-webkit-scrollbar { height: 3px; }
.gp-conv-tabs::-webkit-scrollbar-thumb { background: #d1d5db; border-radius: 2px; }
.gp-conv-tab { padding: 4px 12px; border-radius: 6px; font-size: 12px; cursor: pointer; white-space: nowrap; transition: all 0.15s; background: transparent; border: 1px solid transparent; color: #6b7280; }
.gp-conv-tab:hover { background: #f3f4f6; color: #374151; }
.gp-conv-tab.active { background: #6366f1; color: #fff; border-color: #6366f1; }
.gp-conv-tab-close { margin-left: 4px; font-size: 10px; opacity: 0; transition: opacity 0.15s; padding: 0 2px; }
.gp-conv-tab:hover .gp-conv-tab-close { opacity: 0.6; }
.gp-conv-tab-close:hover { opacity: 1; }
.gp-conv-tab-new { background: none; border: 1px dashed #d1d5db; border-radius: 6px; padding: 4px 10px; font-size: 12px; cursor: pointer; color: #9ca3af; transition: all 0.15s; flex-shrink: 0; }
.gp-conv-tab-new:hover { border-color: #6366f1; color: #6366f1; }
.gp-conv-tab-close { margin-left: 4px; font-size: 14px; line-height: 1; opacity: 0; transition: opacity 0.15s; padding: 0 2px; pointer-events: auto; }
.gp-conv-tab:hover .gp-conv-tab-close { opacity: 0.5; }
.gp-conv-tab-close:hover { opacity: 1 !important; color: #ef4444; }

/* 撤回按钮 */
.gp-chat-undo-btn { position: absolute; top: 4px; right: 4px; background: none; border: none; color: #9ca3af; cursor: pointer; font-size: 11px; opacity: 0; transition: opacity 0.15s; padding: 2px 4px; }
.gp-chat-bubble:hover .gp-chat-undo-btn { opacity: 0.7; }
.gp-chat-undo-btn:hover { opacity: 1 !important; color: #ef4444; }
</style>

<div class="gp-container">
    <!-- 头部 -->
    <div class="gp-header">
        <div class="gp-header-left">
            <span class="gp-spinner" id="gpSpinner"></span>
            <span class="gp-header-title">AI 原型生成</span>
            <span class="gp-header-status" id="gpHeaderStatus">生成中</span>
        </div>
        <div class="gp-header-actions">
            <button class="gp-header-btn" id="gpStopBtn" title="停止生成"><i class="fas fa-stop"></i></button>
            <button class="gp-header-btn" id="gpOpenBtn" title="打开预览"><i class="fas fa-external-link-alt"></i></button>
            <button class="gp-header-btn" id="gpMinimizeBtn" title="最小化"><i class="fas fa-minus"></i></button>
            <button class="gp-header-btn" id="gpCloseBtn" title="关闭"><i class="fas fa-times"></i></button>
        </div>
    </div>

    <!-- 内容区 -->
    <div class="gp-content" id="gpContent">
    </div>

    <!-- 对话区 -->
    <div class="gp-chat-area">
        <div id="gpConvTabs" class="gp-conv-tabs" style="display:none;">
            <!-- JS 动态生成标签 -->
            <button class="gp-conv-tab-new" id="gpConvTabNew" title="新对话"><i class="fas fa-plus"></i></button>
        </div>
        <div class="gp-chat-content" id="gpChatContent"></div>
        <div class="gp-input-area" id="gpInputArea">
            <input type="text" id="gpChatInput" class="gp-input"
                   placeholder="输入调整指令，如：表格改成卡片布局..."
                   autocomplete="off">
            <button id="gpSendBtn" class="gp-send-btn">发送</button>
        </div>
    </div>
</div>`;
    }

    // ==================== 计时器 ====================

    _startTimer() {
        this._stopTimer();
        this._startTime = Date.now();
        this._timerInterval = setInterval(() => {
            if (!this.isGenerating) return;
            const elapsed = this._getElapsedText();
            const headerStatus = document.getElementById('gpHeaderStatus');
            if (headerStatus) headerStatus.textContent = '生成中 ' + elapsed;
            // 最小化状态也更新迷你栏
            if (this._minimized) {
                this._updateMiniBar('生成中 ' + elapsed);
            }
            // 定时器驱动滚动到底部（最可靠的兜底）
            this._scrollToBottom();
        }, 1000);
    }

    _stopTimer() {
        if (this._timerInterval) {
            clearInterval(this._timerInterval);
            this._timerInterval = null;
        }
    }

    _getElapsedText() {
        if (!this._startTime) return '0:00';
        const diff = Math.floor((Date.now() - this._startTime) / 1000);
        const min = Math.floor(diff / 60);
        const sec = diff % 60;
        if (min > 0) {
            return min + ' 分 ' + sec + ' 秒';
        }
        return sec + ' 秒';
    }

    // ==================== 工具方法 ====================

    _scrollToBottom() {
        const content = document.getElementById('gpContent');
        if (content) {
            // 直接设置，如果浏览器还没布局则用 requestAnimationFrame 兜底
            content.scrollTop = content.scrollHeight;
            requestAnimationFrame(() => {
                content.scrollTop = content.scrollHeight;
            });
        }
        const chat = document.getElementById('gpChatContent');
        if (chat) {
            chat.scrollTop = chat.scrollHeight;
            requestAnimationFrame(() => {
                chat.scrollTop = chat.scrollHeight;
            });
        }
    }

    _findProjectIndex() {
        if (typeof allProjects === 'undefined') return -1;
        return allProjects.findIndex(p => p.id === this.projectId);
    }

    _escapeHtml(text) {
        if (!text) return '';
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    _escapeAttr(text) {
        if (!text) return '';
        return text
            .replace(/&/g, '&amp;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;');
    }

    // ==================== 多对话管理 ====================

    async _loadConversationTabs() {
        if (!this.projectId) return;
        try {
            const res = await fetch('/api/conversations?projectId=' + encodeURIComponent(this.projectId));
            const data = await res.json();
            if (!data.success) return;
            this.activeConversationId = data.active_conversation_id || '';
            // 渲染标签栏（如果面板中有标签容器）
            const tabsEl = document.getElementById('gpConvTabs');
            if (!tabsEl) return;
            const convs = (data.conversations || []).slice().sort((a, b) => {
                if (a.pinned !== b.pinned) return b.pinned ? 1 : -1;
                return (b.updated_at || 0) - (a.updated_at || 0);
            });
            let html = '';
            const canClose = convs.length > 1;
            for (const c of convs) {
                const isActive = c.id === this.activeConversationId;
                const pin = c.pinned ? '<i class="fas fa-thumbtack" style="font-size:9px;color:#f59e0b;"></i>' : '';
                const title = (c.title || '新对话').substring(0, 12);
                const closeBtn = canClose ? `<span class="gp-conv-tab-close" onclick="event.stopPropagation();window._gp._deleteConversationTab('${c.id}','${(c.title || '新对话').replace(/'/g, "\\'")}')">&times;</span>` : '';
                html += `<button class="gp-conv-tab${isActive ? ' active' : ''}" onclick="window._gp._switchConversation('${c.id}')">${pin}<span>${title}</span>${closeBtn}</button>`;
            }
            html += '<button class="gp-conv-tab-new" onclick="window._gp._createNewConversation()" title="新对话"><i class="fas fa-plus"></i></button>';
            tabsEl.innerHTML = html;
            // 有对话时显示标签栏
            tabsEl.style.display = (convs.length > 0) ? 'flex' : 'none';
        } catch (e) {
            console.warn('[GenerationPanel] 加载对话标签失败:', e);
        }
    }

    async _switchConversation(convId) {
        if (convId === this.activeConversationId) return;
        // 关闭正在进行的连接
        if (this.evtSource) {
            this.evtSource.close();
            this.evtSource = null;
        }
        this.isGenerating = false;
        try {
            const res = await fetch('/api/conversations/switch', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ projectId: this.projectId, conversationId: convId })
            });
            const data = await res.json();
            if (data.success) {
                this.activeConversationId = convId;
                await this._loadConversationTabs();
                await this._loadChatHistory();
            }
        } catch (e) {
            console.warn('[GenerationPanel] 切换对话失败:', e);
        }
    }

    async _createNewConversation() {
        // 关闭正在进行的连接
        if (this.evtSource) {
            this.evtSource.close();
            this.evtSource = null;
        }
        this.isGenerating = false;
        try {
            const res = await fetch('/api/conversations/create', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    projectId: this.projectId,
                    title: '新对话',
                    cloneFrom: this.activeConversationId || ''
                })
            });
            const data = await res.json();
            if (data.success && data.conversation) {
                this.activeConversationId = data.conversation.id;
                await this._loadConversationTabs();
                await this._loadChatHistory();
            }
        } catch (e) {
            console.warn('[GenerationPanel] 创建对话失败:', e);
        }
    }

    async _undoChatMessage(msgIndex) {
        if (!confirm('撤回将还原到该消息之前的状态，且该消息之后的所有消息也会被移除。确定吗？')) return;
        try {
            const res = await fetch('/api/conversations/undo', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    projectId: this.projectId,
                    conversationId: this.activeConversationId,
                    messageIndex: msgIndex
                })
            });
            const data = await res.json();
            if (data.success) {
                await this._loadChatHistory();
            } else {
                alert(data.error || '撤回失败');
            }
        } catch (e) {
            console.warn('[GenerationPanel] 撤回失败:', e);
            alert('撤回失败');
        }
    }

    async _deleteConversationTab(convId, title) {
        if (!confirm(`确定要删除对话「${title || '新对话'}」吗？此操作不可撤销。`)) return;
        try {
            const res = await fetch('/api/conversations/delete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ projectId: this.projectId, conversationId: convId })
            });
            const data = await res.json();
            if (data.success) {
                // 删除的是当前对话，后端已自动切换
                if (convId === this.activeConversationId) {
                    const listRes = await fetch('/api/conversations?projectId=' + encodeURIComponent(this.projectId));
                    const listData = await listRes.json();
                    if (listData.success) {
                        this.activeConversationId = listData.active_conversation_id || '';
                    }
                }
                await this._loadConversationTabs();
                await this._loadChatHistory();
            } else {
                alert(data.error || '删除失败');
            }
        } catch (e) {
            console.warn('[GenerationPanel] 删除对话失败:', e);
        }
    }
}

// 全局实例
window._gp = null;
