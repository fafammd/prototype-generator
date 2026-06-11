/**
 * SpecStudio - 可视化规格编辑器
 *
 * 功能：
 * - 左侧面板：AI 对话气泡（替代原始文本流）
 * - 右侧面板：D3.js 力导向图谱（页面节点 + 导航边）
 * - 双向同步：spec JSON <-> 图谱编辑
 * - 图谱交互：拖拽、选择、添加/删除节点和边、双击编辑详情
 * - SSE 流式集成
 */
class SpecStudio {
    constructor() {
        this.spec = null;
        this.graphData = null;
        this.simulation = null;
        this.svg = null;
        this.selectedNode = null;
        this.selectedEdge = null;
        this.linkMode = false;
        this.linkSource = null;
        this.isStreaming = false;
        this.evtSource = null;

        // 上下文（由 generateSpecForConfirmation 传入）
        this._prompt = '';
        this._formData = {};
        this._allImages = [];
        this._projectName = '';
        this._projectId = null;

        // DOM 引用
        this._container = null;
        this._zoom = null;
        this._width = 800;
        this._height = 600;
        this._resizeObserver = null;
        this._keyHandler = null;
        this._currentDetailNodeId = null;
    }

    // ========================
    // 生命周期
    // ========================

    open(prompt, formData, allImages, projectName) {
        this._prompt = prompt;
        this._formData = formData;
        this._allImages = allImages;
        this._projectName = projectName;
        this.isStreaming = true;

        window.specStudio = this;

        const modal = document.getElementById('specStudioModal');
        modal.classList.remove('hidden');

        // 清空状态
        const chat = document.getElementById('specStudioChat');
        if (chat) chat.innerHTML = '';
        document.getElementById('specStudioActions').classList.add('hidden');
        document.getElementById('specStudioInputArea').classList.add('hidden');
        document.getElementById('specGraphToolbar').classList.add('hidden');
        document.getElementById('specNodeDetail').classList.add('hidden');
        document.getElementById('specGraphPlaceholder').classList.remove('hidden');
        document.getElementById('specStudioStatus').textContent = '正在准备...';
        document.getElementById('specStudioPageCount').textContent = '';

        this.spec = null;
        this._initGraph();

        this._addChatBubble('system', '正在理解你的产品需求，请稍候...');

        // 键盘快捷键
        this._keyHandler = (e) => {
            if (e.key === 'Escape') {
                this.close();
            } else if ((e.key === 'Delete' || e.key === 'Backspace') &&
                       document.activeElement.tagName !== 'INPUT' &&
                       document.activeElement.tagName !== 'TEXTAREA') {
                e.preventDefault();
                this.deleteSelected();
            }
        };
        document.addEventListener('keydown', this._keyHandler);

        // 窗口缩放监听（先断开旧的，避免重复）
        if (this._resizeObserver) {
            this._resizeObserver.disconnect();
        }
        this._resizeObserver = new ResizeObserver(() => {
            const svgEl = document.getElementById('specStudioGraph');
            if (svgEl) {
                const rect = svgEl.getBoundingClientRect();
                this._width = rect.width;
                this._height = rect.height;
                if (this.simulation) {
                    this.simulation.force('center', d3.forceCenter(this._width / 2, this._height / 2));
                    this.simulation.alpha(0.3).restart();
                }
            }
        });
        const graphContainer = document.getElementById('specGraphContainer');
        if (graphContainer) this._resizeObserver.observe(graphContainer);
    }

    close() {
        if (this.evtSource) {
            this.evtSource.close();
            this.evtSource = null;
        }
        if (this._keyHandler) {
            document.removeEventListener('keydown', this._keyHandler);
            this._keyHandler = null;
        }
        if (this._resizeObserver) {
            this._resizeObserver.disconnect();
            this._resizeObserver = null;
        }

        const modal = document.getElementById('specStudioModal');
        modal.classList.add('hidden');

        this.isStreaming = false;
        if (this.simulation) {
            this.simulation.stop();
            this.simulation = null;
        }
    }

    // ========================
    // 对话气泡
    // ========================

    _addChatBubble(role, text) {
        const container = document.getElementById('specStudioChat');
        if (!container) return null;

        const wrapper = document.createElement('div');
        const cssClass = role === 'user' ? 'spec-chat-user' :
                         role === 'ai' ? 'spec-chat-ai' : 'spec-chat-system';
        wrapper.className = cssClass;

        if (role === 'system') {
            wrapper.innerHTML = '<div class="spec-chat-text">' + text + '</div>';
        } else {
            const bubble = document.createElement('div');
            bubble.className = 'spec-chat-bubble';

            if (role === 'ai') {
                const icon = document.createElement('div');
                icon.className = 'flex items-center gap-1.5 mb-1';
                icon.innerHTML = '<span class="w-5 h-5 rounded-full bg-indigo-100 flex items-center justify-center text-[10px] text-indigo-600 font-bold">AI</span>';
                bubble.appendChild(icon);
            }

            const textEl = document.createElement('div');
            textEl.className = 'spec-chat-text';
            textEl.textContent = text;
            bubble.appendChild(textEl);
            wrapper.appendChild(bubble);
        }

        container.appendChild(wrapper);
        this._scrollChat();
        return wrapper;
    }

    _updateStreamingBubble(text) {
        let streamEl = document.getElementById('specStreamBubble');
        if (!streamEl) {
            const bubble = this._addChatBubble('ai', '');
            if (bubble) {
                streamEl = bubble.querySelector('.spec-chat-text');
                if (streamEl) streamEl.id = 'specStreamBubble';
            }
        }
        if (streamEl) {
            streamEl.textContent = text;
            this._scrollChat();
        }
    }

    _scrollChat() {
        const container = document.getElementById('specStudioChat');
        if (container) {
            requestAnimationFrame(() => {
                container.scrollTop = container.scrollHeight;
            });
        }
    }

    _addThinkingBlock(text) {
        const container = document.getElementById('specStudioChat');
        if (!container) return;

        const block = document.createElement('div');
        block.style.cssText = 'margin-bottom: 8px;';
        const truncated = text.length > 800
            ? text.slice(0, 400) + '\n...\n' + text.slice(-400)
            : text;
        block.innerHTML =
            '<div style="display:flex;align-items:center;gap:6px;padding:4px 0;cursor:pointer;font-size:11px;color:#9ca3af;"' +
            ' onclick="this.nextElementSibling.style.display=this.nextElementSibling.style.display===\'none\'?\'block\':\'none\'">' +
            '  <span style="width:16px;height:16px;border-radius:50%;background:#eef2ff;display:inline-flex;align-items:center;justify-content:center;font-size:9px;color:#6366f1;font-weight:700;">?</span>' +
            '  <span>AI 思考笔记</span>' +
            '</div>' +
            '<pre style="display:none;margin:4px 0;padding:8px 12px;background:#f5f3ff;border-radius:6px;font-size:10px;color:#7c3aed;white-space:pre-wrap;max-height:150px;overflow-y:auto;font-family:monospace;line-height:1.4;">' + this._escapeHtml(truncated) + '</pre>';
        container.appendChild(block);
    }

    _escapeHtml(str) {
        return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

    // ========================
    // SSE 集成
    // ========================

    connectSSE(projectId) {
        let thinkingText = '';
        let outputText = '';
        let hasOutputStarted = false;

        const statusEl = document.getElementById('specStudioStatus');

        const evtSource = new EventSource(
            '/api/generation-stream?id=' + encodeURIComponent(projectId)
        );
        this.evtSource = evtSource;

        evtSource.onerror = () => {
            if (this.evtSource) {
                this.evtSource.close();
                this.evtSource = null;
            }
            if (!hasOutputStarted) {
                this._addChatBubble('system', '连接中断，请重试');
                if (statusEl) statusEl.textContent = '连接中断';
            }
        };

        evtSource.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);

                if (data.content) {
                    if (data.content.startsWith('[think]')) {
                        thinkingText += data.content.slice(7);
                        if (statusEl) {
                            statusEl.textContent = 'AI 正在思考...';
                        }
                    } else {
                        outputText += data.content;
                        if (!hasOutputStarted) {
                            hasOutputStarted = true;
                            if (thinkingText) {
                                this._addThinkingBlock(thinkingText);
                            }
                            this._addChatBubble('system', '正在生成页面规划...');
                        }
                        this._updateStreamingBubble(outputText);
                        if (statusEl) {
                            statusEl.textContent = 'AI 正在规划你的产品...';
                        }
                    }
                }

                if (data.type && data.data) {
                    this._handleSSEEvent(data.type, data.data);
                }
            } catch (e) {
                console.error('[SpecStudio SSE] Parse error:', e);
            }
        };

        evtSource.addEventListener('status', (event) => {
            const data = JSON.parse(event.data);
            if (data.status === 'completed' || data.status === 'failed') {
                evtSource.close();
                this.evtSource = null;
            }
        });
    }

    _handleSSEEvent(type, d) {
        const statusEl = document.getElementById('specStudioStatus');

        switch (type) {
            case 'phase':
                if (d.status === 'running' && statusEl) {
                    statusEl.textContent = 'AI 正在理解你的产品需求...';
                }
                break;

            case 'spec_complete':
                this.isStreaming = false;
                if (statusEl) statusEl.textContent = '页面规划完成，请确认';
                this._addChatBubble('system', '页面规划完成！右侧地图展示了页面关系，可点击查看详情，拖拽调整布局。');
                this.loadSpec(d);
                break;

            case 'spec_error':
                if (this.evtSource) {
                    this.evtSource.close();
                    this.evtSource = null;
                }
                this._addChatBubble('system', '规划失败: ' + (d.error || '未知错误'));
                if (statusEl) statusEl.textContent = '生成失败';
                break;
        }
    }

    // ========================
    // Spec 处理
    // ========================

    loadSpec(specResult) {
        this.spec = specResult.spec;
        this._projectId = specResult.projectId;

        const graphData = this._specToGraphData(specResult.spec);

        // 空规格处理
        if (graphData.nodes.length === 0) {
            this._addChatBubble('system', '未生成页面规划，将直接制作原型');
            this.graphData = { nodes: [], links: [] };
            return;
        }

        this.graphData = graphData;

        // 显示操作栏和工具栏
        document.getElementById('specStudioActions').classList.remove('hidden');
        document.getElementById('specStudioInputArea').classList.remove('hidden');
        document.getElementById('specGraphToolbar').classList.remove('hidden');
        document.getElementById('specGraphPlaceholder').classList.add('hidden');

        const pageCount = graphData.nodes.length;
        document.getElementById('specStudioPageCount').textContent = pageCount + ' 个页面';
        document.getElementById('specActionPageCount').textContent = pageCount;

        this._renderGraph();
    }

    _specToGraphData(spec) {
        const pages = (spec && spec.pages) || [];
        const navLinks = (spec && spec.navigation && spec.navigation.links) || [];
        const entryPageIds = new Set();

        if (spec && spec.navigation && spec.navigation.pages) {
            spec.navigation.pages.forEach(p => {
                if (p.is_entry) entryPageIds.add(p.id);
            });
        }
        if (spec && spec.navigation && spec.navigation.default_page) {
            entryPageIds.add(spec.navigation.default_page);
        }

        const nodes = pages.map((page, i) => ({
            id: page.id,
            name: page.name || ('Page ' + (i + 1)),
            isEntry: entryPageIds.has(page.id),
            showInNav: page.show_in_nav !== false,
            dataSources: page.data_sources || [],
            componentsNeeded: page.components_needed || [],
            navigatesTo: (page.cross_references && page.cross_references.navigates_to) || [],
            _original: JSON.parse(JSON.stringify(page))
        }));

        const nodeIdSet = new Set(nodes.map(n => n.id));
        const links = navLinks
            .filter(link => nodeIdSet.has(link.from) && nodeIdSet.has(link.to))
            .map((link, i) => ({
                source: link.from,
                target: link.to,
                trigger: link.trigger || '跳转',
                _index: i
            }));

        return { nodes, links };
    }

    _graphDataToSpec() {
        const originalSpec = this.spec;
        if (!originalSpec) return null;

        const nodes = this.graphData.nodes;
        const links = this.graphData.links;

        const pages = nodes.map(node => {
            const base = node._original || {};
            return Object.assign({}, base, {
                id: node.id,
                name: node.name,
                show_in_nav: node.showInNav,
                data_sources: [...(node.dataSources || [])],
                components_needed: [...(node.componentsNeeded || [])],
                cross_references: Object.assign({}, base.cross_references || {}, {
                    navigates_to: [...(node.navigatesTo || [])]
                })
            });
        });

        const navPages = nodes.map(node => ({
            id: node.id,
            name: node.name,
            is_entry: node.isEntry,
            show_in_nav: node.showInNav
        }));

        const defaultPage = (originalSpec.navigation && originalSpec.navigation.default_page)
            ? originalSpec.navigation.default_page
            : (navPages.length > 0 ? navPages[0].id : '');

        const navLinks = links.map(link => ({
            from: typeof link.source === 'object' ? link.source.id : link.source,
            to: typeof link.target === 'object' ? link.target.id : link.target,
            trigger: link.trigger || '跳转'
        }));

        return Object.assign({}, originalSpec, {
            navigation: {
                pages: navPages,
                default_page: defaultPage,
                links: navLinks
            },
            pages: pages
        });
    }

    // ========================
    // D3 图谱
    // ========================

    _initGraph() {
        const svgEl = document.getElementById('specStudioGraph');
        this.svg = d3.select(svgEl);

        this.svg.selectAll('*').remove();

        const rect = svgEl.getBoundingClientRect();
        this._width = rect.width || 800;
        this._height = rect.height || 600;

        this._container = this.svg.append('g').attr('class', 'spec-graph-container');

        // 箭头标记
        const defs = this.svg.append('defs');
        defs.append('marker')
            .attr('id', 'spec-arrow')
            .attr('viewBox', '0 -5 10 10')
            .attr('refX', 28)
            .attr('refY', 0)
            .attr('markerWidth', 8)
            .attr('markerHeight', 8)
            .attr('orient', 'auto')
            .append('path')
            .attr('d', 'M0,-5L10,0L0,5')
            .attr('class', 'spec-edge-arrow');

        defs.append('marker')
            .attr('id', 'spec-arrow-selected')
            .attr('viewBox', '0 -5 10 10')
            .attr('refX', 28)
            .attr('refY', 0)
            .attr('markerWidth', 8)
            .attr('markerHeight', 8)
            .attr('orient', 'auto')
            .append('path')
            .attr('d', 'M0,-5L10,0L0,5')
            .attr('fill', '#4f46e5');

        this._setupZoom();
        this.graphData = { nodes: [], links: [] };
    }

    _renderGraph() {
        if (!this.svg || !this.graphData) return;

        if (this.simulation) this.simulation.stop();

        const container = this._container;
        const nodes = this.graphData.nodes;
        const links = this.graphData.links;

        container.selectAll('.spec-edge-group').remove();
        container.selectAll('.spec-node').remove();

        // 绘制边
        const linkGroups = container.selectAll('.spec-edge-group')
            .data(links)
            .enter()
            .append('g')
            .attr('class', 'spec-edge-group');

        const linkLines = linkGroups.append('path')
            .attr('class', d => 'spec-edge' + (this.selectedEdge === d ? ' selected' : ''))
            .attr('marker-end', d => 'url(#spec-arrow)')
            .on('click', (event, d) => {
                event.stopPropagation();
                this.selectEdge(d);
            });

        // 边标签背景（白色圆角矩形提高可读性）
        linkGroups.append('rect')
            .attr('class', 'spec-edge-label-bg')
            .attr('rx', 4).attr('ry', 4)
            .attr('fill', 'white')
            .attr('fill-opacity', 0.92)
            .attr('stroke', '#e5e7eb')
            .attr('stroke-width', 0.5);

        // 边标签（触发条件）
        const linkLabels = linkGroups.append('text')
            .attr('class', 'spec-edge-label')
            .attr('text-anchor', 'middle')
            .text(d => d.trigger);

        // 根据文本宽度设置背景矩形大小（延迟到渲染后）
        requestAnimationFrame(() => {
            linkLabels.each(function(d, i) {
                const bbox = this.getBBox();
                const bgRect = linkGroups.nodes()[i].querySelector('.spec-edge-label-bg');
                if (bgRect && bbox.width > 0) {
                    d3.select(bgRect)
                        .attr('width', bbox.width + 10)
                        .attr('height', bbox.height + 4)
                        .attr('x', bbox.x - 5)
                        .attr('y', bbox.y - 2);
                }
            });
        });

        // 绘制节点
        const nodeGroups = container.selectAll('.spec-node')
            .data(nodes, d => d.id)
            .enter()
            .append('g')
            .attr('class', d => {
                let cls = 'spec-node';
                if (d.isEntry) cls += ' entry';
                if (!d.showInNav) cls += ' nav-hidden';
                if (this.selectedNode && this.selectedNode.id === d.id) cls += ' selected';
                return cls;
            })
            .on('click', (event, d) => {
                event.stopPropagation();
                if (this.linkMode) {
                    if (!this.linkSource) {
                        this.linkSource = d;
                        this._addChatBubble('system', '已选择页面: ' + d.name + '，请点击目标页面');
                    } else {
                        this._createLink(this.linkSource.id, d.id);
                    }
                } else {
                    this.selectNode(d);
                }
            })
            .on('dblclick', (event, d) => {
                event.stopPropagation();
                event.preventDefault();
                this._showNodeDetail(d);
            })
            .call(this._setupDrag());

        // 节点阴影
        nodeGroups.append('rect')
            .attr('width', 160).attr('height', 56)
            .attr('x', -80).attr('y', -28)
            .attr('rx', 10).attr('ry', 10)
            .attr('fill', 'rgba(0,0,0,0.06)')
            .attr('filter', 'none');

        // 节点背景矩形
        nodeGroups.append('rect')
            .attr('width', 160).attr('height', 52)
            .attr('x', -80).attr('y', -26)
            .attr('rx', 10).attr('ry', 10);

        // 节点左侧色条
        nodeGroups.append('rect')
            .attr('width', 4).attr('height', 36)
            .attr('x', -76).attr('y', -18)
            .attr('rx', 2).attr('ry', 2)
            .attr('fill', d => d.isEntry ? '#059669' : (!d.showInNav ? '#d97706' : '#4f46e5'));

        // 入口页小图标
        nodeGroups.filter(d => d.isEntry)
            .append('text')
            .attr('x', -62).attr('y', -4)
            .attr('font-size', '12px')
            .attr('fill', '#059669')
            .text('\u2302'); // ⌂ house symbol

        // 隐藏导航标记
        nodeGroups.filter(d => !d.showInNav)
            .append('text')
            .attr('x', d => d.isEntry ? -48 : -62)
            .attr('y', -4)
            .attr('font-size', '9px')
            .attr('fill', '#b45309')
            .attr('font-weight', '600')
            .text('\u25CE'); // ◎ hidden symbol

        // 节点名称
        nodeGroups.append('text')
            .attr('text-anchor', 'middle')
            .attr('x', d => (d.isEntry || !d.showInNav) ? 4 : 0)
            .attr('dy', d => {
                if (d.isEntry && !d.showInNav) return '0.4em';
                if (d.isEntry || !d.showInNav) return '0.4em';
                return '0.35em';
            })
            .text(d => this._truncateText(d.name, 8));

        // 连接数 badge
        nodeGroups.filter(d => {
            const linkCount = links.filter(l => {
                const sId = typeof l.source === 'object' ? l.source.id : l.source;
                const tId = typeof l.target === 'object' ? l.target.id : l.target;
                return sId === d.id || tId === d.id;
            }).length;
            return linkCount > 1;
        }).append('text')
            .attr('x', 68).attr('y', -18)
            .attr('font-size', '9px')
            .attr('font-weight', '700')
            .attr('fill', '#6b7280')
            .attr('text-anchor', 'middle')
            .text(d => {
                const lc = links.filter(l => {
                    const sId = typeof l.source === 'object' ? l.source.id : l.source;
                    const tId = typeof l.target === 'object' ? l.target.id : l.target;
                    return sId === d.id || tId === d.id;
                }).length;
                return lc + '';
            });

        // 力仿真 — 分层布局：入口页在上方，导航页在中层，子页面在下方
        // 为节点设置初始 y 偏移来引导分层
        nodes.forEach(n => {
            if (n.isEntry) {
                n._layer = 0;
            } else if (n.showInNav) {
                n._layer = 1;
            } else {
                n._layer = 2;
            }
        });

        this.simulation = d3.forceSimulation(nodes)
            .force('link', d3.forceLink(links).id(d => d.id)
                .distance(d => {
                    // 同层级距离小，跨层级距离大
                    const sourceLayer = d.source._layer || 0;
                    const targetLayer = d.target._layer || 0;
                    return sourceLayer === targetLayer ? 140 : 180;
                }))
            .force('charge', d3.forceManyBody().strength(-600))
            .force('center', d3.forceCenter(this._width / 2, this._height / 2))
            .force('collision', d3.forceCollide().radius(100))
            .force('y', d3.forceY(d => {
                // 按层分布 y 位置
                const layerY = this._height * (0.2 + d._layer * 0.3);
                return layerY;
            }).strength(0.15))
            .force('x', d3.forceX(this._width / 2).strength(0.05))
            .alphaDecay(0.03)
            .on('tick', () => this._tickActions(linkLines, linkGroups, nodeGroups));

        // 点击空白处取消选择
        this.svg.on('click', () => this.deselectAll());
    }

    _tickActions(linkLines, linkGroups, nodeGroups) {
        // 检测双向边：A→B 和 B→A 同时存在时，各自加不同偏移
        const linkPairs = new Map();
        this.graphData.links.forEach((l, i) => {
            const sId = typeof l.source === 'object' ? l.source.id : l.source;
            const tId = typeof l.target === 'object' ? l.target.id : l.target;
            const key = sId < tId ? sId + '-' + tId : tId + '-' + sId;
            if (!linkPairs.has(key)) linkPairs.set(key, []);
            linkPairs.get(key).push(i);
        });

        linkLines.attr('d', (d, i) => {
            const dx = d.target.x - d.source.x;
            const dy = d.target.y - d.source.y;
            const dr = Math.max(Math.sqrt(dx * dx + dy * dy) * 1.2, 80);

            // 检查是否是双向边
            const sId = typeof d.source === 'object' ? d.source.id : d.source;
            const tId = typeof d.target === 'object' ? d.target.id : d.target;
            const key = sId < tId ? sId + '-' + tId : tId + '-' + sId;
            const pair = linkPairs.get(key) || [];
            const isBidirectional = pair.length >= 2;
            const sweepFlag = isBidirectional ? (pair[0] === i ? 1 : 0) : 1;

            return 'M' + d.source.x + ',' + d.source.y +
                   ' A' + dr + ',' + dr + ' 0 0,' + sweepFlag + ' ' +
                   d.target.x + ',' + d.target.y;
        });

        // 边标签定位 + 背景更新
        linkGroups.select('.spec-edge-label')
            .attr('x', d => (d.source.x + d.target.x) / 2)
            .attr('y', d => (d.source.y + d.target.y) / 2 - 6);

        linkGroups.select('.spec-edge-label-bg')
            .attr('x', d => (d.source.x + d.target.x) / 2)
            .attr('y', d => (d.source.y + d.target.y) / 2 - 6);

        nodeGroups.attr('transform', d => 'translate(' + d.x + ',' + d.y + ')');
    }

    _truncateText(text, maxLen) {
        if (!text) return '';
        return text.length > maxLen ? text.slice(0, maxLen - 1) + '...' : text;
    }

    _setupZoom() {
        const zoom = d3.zoom()
            .scaleExtent([0.3, 3])
            .on('zoom', (event) => {
                this._container.attr('transform', event.transform);
            });
        this.svg.call(zoom);
        this._zoom = zoom;
    }

    _setupDrag() {
        return d3.drag()
            .on('start', (event, d) => {
                if (!event.active) this.simulation.alphaTarget(0.3).restart();
                d.fx = d.x;
                d.fy = d.y;
            })
            .on('drag', (event, d) => {
                d.fx = event.x;
                d.fy = event.y;
            })
            .on('end', (event, d) => {
                if (!event.active) this.simulation.alphaTarget(0);
                // 保持拖拽位置
            });
    }

    fitGraph() {
        const svgEl = document.getElementById('specStudioGraph');
        const rect = svgEl.getBoundingClientRect();
        this._width = rect.width;
        this._height = rect.height;

        this.svg.transition().duration(500).call(
            this._zoom.transform,
            d3.zoomIdentity.translate(0, 0).scale(1)
        );
    }

    // ========================
    // 图谱交互
    // ========================

    selectNode(node) {
        this.deselectAll();
        this.selectedNode = node;

        this._container.selectAll('.spec-node')
            .classed('selected', d => d.id === node.id);

        const deleteBtn = document.getElementById('specDeleteBtn');
        if (deleteBtn) {
            deleteBtn.classList.remove('opacity-50', 'pointer-events-none');
        }
    }

    selectEdge(link) {
        this.deselectAll();
        this.selectedEdge = link;

        this._container.selectAll('.spec-edge')
            .classed('selected', d => d === link);

        const deleteBtn = document.getElementById('specDeleteBtn');
        if (deleteBtn) {
            deleteBtn.classList.remove('opacity-50', 'pointer-events-none');
        }
    }

    deselectAll() {
        this.selectedNode = null;
        this.selectedEdge = null;

        this._container.selectAll('.spec-node').classed('selected', false);
        this._container.selectAll('.spec-edge').classed('selected', false);

        const deleteBtn = document.getElementById('specDeleteBtn');
        if (deleteBtn) {
            deleteBtn.classList.add('opacity-50', 'pointer-events-none');
        }

        this._hideNodeDetail();
    }

    addNode() {
        const pageCount = this.graphData.nodes.length;
        const newId = 'page_' + Date.now();
        const newNode = {
            id: newId,
            name: '新页面 ' + (pageCount + 1),
            isEntry: pageCount === 0,
            showInNav: true,
            dataSources: [],
            componentsNeeded: [],
            navigatesTo: [],
            x: this._width / 2 + (Math.random() - 0.5) * 100,
            y: this._height / 2 + (Math.random() - 0.5) * 100,
            _original: {
                id: newId,
                name: '新页面 ' + (pageCount + 1),
                show_in_nav: true,
                data_sources: [],
                components_needed: [],
                cross_references: { navigates_to: [], must_match_style_of: [] }
            }
        };

        this.graphData.nodes.push(newNode);

        this._updatePageCounts();
        this._renderGraph();
        this.selectNode(newNode);
        this._addChatBubble('system', '已添加新页面: ' + newNode.name);
    }

    deleteSelected() {
        if (this.selectedNode) {
            const name = this.selectedNode.name;
            const nodeId = this.selectedNode.id;

            this.graphData.nodes = this.graphData.nodes.filter(n => n.id !== nodeId);
            this.graphData.links = this.graphData.links.filter(l => {
                const sourceId = typeof l.source === 'object' ? l.source.id : l.source;
                const targetId = typeof l.target === 'object' ? l.target.id : l.target;
                return sourceId !== nodeId && targetId !== nodeId;
            });

            // 清除其他节点中对已删除节点的引用
            this.graphData.nodes.forEach(n => {
                n.navigatesTo = n.navigatesTo.filter(id => id !== nodeId);
            });

            this.selectedNode = null;
            this._renderGraph();
            this._updatePageCounts();
            this._addChatBubble('system', '已删除页面: ' + name);
        } else if (this.selectedEdge) {
            const idx = this.graphData.links.indexOf(this.selectedEdge);
            if (idx !== -1) {
                this.graphData.links.splice(idx, 1);
            }
            this.selectedEdge = null;
            this._renderGraph();
            this._addChatBubble('system', '已删除页面跳转');
        }

        this._hideNodeDetail();
    }

    toggleLinkMode() {
        this.linkMode = !this.linkMode;
        this.linkSource = null;

        const btn = document.getElementById('specLinkModeBtn');
        const svgEl = document.getElementById('specStudioGraph');

        if (this.linkMode) {
            btn.classList.add('bg-indigo-50', 'text-indigo-600', 'border-indigo-300');
            svgEl.classList.add('spec-link-mode-active');
            this._addChatBubble('system', '点击第一个页面，再点击第二个页面，即可添加页面跳转');
        } else {
            btn.classList.remove('bg-indigo-50', 'text-indigo-600', 'border-indigo-300');
            svgEl.classList.remove('spec-link-mode-active');
            this._addChatBubble('system', '已退出连接模式');
        }
    }

    _createLink(sourceId, targetId) {
        if (sourceId === targetId) {
            this._addChatBubble('system', '一个页面不能跳转到自己');
            return;
        }

        // 检查是否已存在
        const exists = this.graphData.links.some(l => {
            const sId = typeof l.source === 'object' ? l.source.id : l.source;
            const tId = typeof l.target === 'object' ? l.target.id : l.target;
            return sId === sourceId && tId === targetId;
        });

        if (exists) {
            this._addChatBubble('system', '这个页面跳转已经存在了');
            this.linkSource = null;
            return;
        }

        const sourceNode = this.graphData.nodes.find(n => n.id === sourceId);
        const targetNode = this.graphData.nodes.find(n => n.id === targetId);

        this.graphData.links.push({
            source: sourceId,
            target: targetId,
            trigger: '点击跳转'
        });

        if (sourceNode && !sourceNode.navigatesTo.includes(targetId)) {
            sourceNode.navigatesTo.push(targetId);
        }

        this._renderGraph();
        this._addChatBubble('system',
            '已添加页面跳转: ' + (sourceNode ? sourceNode.name : sourceId) + ' → ' +
            (targetNode ? targetNode.name : targetId));

        // 退出连接模式
        this.linkMode = false;
        this.linkSource = null;
        document.getElementById('specLinkModeBtn').classList.remove('bg-indigo-50', 'text-indigo-600', 'border-indigo-300');
        document.getElementById('specStudioGraph').classList.remove('spec-link-mode-active');
    }

    // ========================
    // 节点详情面板
    // ========================

    _showNodeDetail(node) {
        const panel = document.getElementById('specNodeDetail');
        panel.classList.remove('hidden');

        const spec = this.spec || {};

        // 构建该页面相关数据模型
        const dataModels = (spec.shared_data_models || []).filter(m =>
            node.dataSources.includes(m.name)
        );

        let dataModelsHtml = '';
        dataModels.forEach(model => {
            let fieldsHtml = '';
            if (model.fields && model.fields.length > 0) {
                fieldsHtml = '<div class="space-y-0.5 mt-1">';
                model.fields.forEach(f => {
                    const typeText = f.type || '--';
                    fieldsHtml += '<div class="flex items-center gap-2 text-[11px]">' +
                        '<span class="text-gray-700 font-mono">' + this._escapeHtml(f.name) + '</span>' +
                        '<span class="text-gray-400">' + this._escapeHtml(typeText) + '</span>' +
                        (f.sample ? '<span class="text-gray-400 truncate max-w-[80px]">' + this._escapeHtml(f.sample) + '</span>' : '') +
                        '</div>';
                });
                fieldsHtml += '</div>';
            }
            dataModelsHtml += '<div class="bg-amber-50 rounded-md p-2 mb-1">' +
                '<div class="text-xs font-medium text-amber-800">' + this._escapeHtml(model.name) + ' (' + (model.fields || []).length + ' 字段)</div>' +
                fieldsHtml +
                '</div>';
        });

        // 跳转目标
        let navsHtml = '';
        node.navigatesTo.forEach(id => {
            const target = this.graphData.nodes.find(n => n.id === id);
            navsHtml += '<span class="inline-block px-2 py-0.5 bg-indigo-50 text-indigo-600 rounded text-[11px] mr-1 mb-1">' +
                this._escapeHtml(target ? target.name : id) + '</span>';
        });

        // 构建详情面板 HTML
        panel.innerHTML =
            '<div class="p-4">' +
                '<div class="flex items-center justify-between mb-3">' +
                    '<h3 class="text-sm font-bold text-gray-800">页面详情</h3>' +
                    '<button onclick="specStudio._hideNodeDetail()" class="text-gray-400 hover:text-gray-600">' +
                        '<i class="fas fa-times"></i>' +
                    '</button>' +
                '</div>' +

                // 名称
                '<div class="mb-3">' +
                    '<label class="text-xs text-gray-500 block mb-1">页面名称</label>' +
                    '<input type="text" id="detailNodeName" value="' + this._escapeAttr(node.name) + '"' +
                        ' class="w-full px-2 py-1.5 text-sm border border-gray-200 rounded-md focus:outline-none focus:border-indigo-400">' +
                '</div>' +

                // 标记
                '<div class="flex gap-3 mb-3">' +
                    '<label class="flex items-center gap-1.5 text-xs text-gray-600">' +
                        '<input type="checkbox" id="detailIsEntry" ' + (node.isEntry ? 'checked' : '') + '> 首页</label>' +
                    '<label class="flex items-center gap-1.5 text-xs text-gray-600">' +
                        '<input type="checkbox" id="detailShowInNav" ' + (node.showInNav ? 'checked' : '') + '> 显示在导航栏</label>' +
                '</div>' +

                // 数据源
                '<div class="mb-3">' +
                    '<label class="text-xs text-gray-500 block mb-1">页面数据</label>' +
                    '<div class="text-xs text-gray-700">' + this._escapeHtml(node.dataSources.join(', ') || '无') + '</div>' +
                '</div>' +

                // 组件
                '<div class="mb-3">' +
                    '<label class="text-xs text-gray-500 block mb-1">页面功能</label>' +
                    '<div class="text-xs text-gray-700">' + this._escapeHtml(node.componentsNeeded.join(', ') || '无') + '</div>' +
                '</div>' +

                // 跳转目标
                '<div class="mb-3">' +
                    '<label class="text-xs text-gray-500 block mb-1">可跳转到</label>' +
                    '<div>' + (navsHtml || '<span class="text-xs text-gray-400">无</span>') + '</div>' +
                '</div>' +

                // 保存按钮
                '<button onclick="specStudio._saveNodeDetailEdits(\'' + node.id + '\')"' +
                    ' class="w-full mt-2 px-3 py-1.5 bg-indigo-600 text-white rounded-md text-xs font-medium hover:bg-indigo-700 transition">' +
                    '保存修改</button>' +
            '</div>';

        this._currentDetailNodeId = node.id;
    }

    _hideNodeDetail() {
        const panel = document.getElementById('specNodeDetail');
        if (panel) panel.classList.add('hidden');
        this._currentDetailNodeId = null;
    }

    _saveNodeDetailEdits(nodeId) {
        const node = this.graphData.nodes.find(n => n.id === nodeId);
        if (!node) return;

        const nameInput = document.getElementById('detailNodeName');
        const entryCheckbox = document.getElementById('detailIsEntry');
        const navCheckbox = document.getElementById('detailShowInNav');

        if (nameInput && nameInput.value.trim()) {
            node.name = nameInput.value.trim();
            node._original.name = node.name;
        }
        if (entryCheckbox) {
            node.isEntry = entryCheckbox.checked;
            node._original.is_entry = node.isEntry;
        }
        if (navCheckbox) {
            node.showInNav = navCheckbox.checked;
            node._original.show_in_nav = node.showInNav;
        }

        this._renderGraph();
        this._hideNodeDetail();
        this._addChatBubble('system', '已更新页面: ' + node.name);
    }

    _escapeAttr(str) {
        return str.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    // ========================
    // 操作
    // ========================

    confirm() {
        if (!this.graphData || !this.graphData.nodes || this.graphData.nodes.length === 0) {
            if (typeof showToast === 'function') {
                showToast('没有可确认的页面规划', 'error');
            }
            return;
        }

        const updatedSpec = this._graphDataToSpec();
        const adjustInput = document.getElementById('specStudioAdjustInput');
        const adjustmentNote = adjustInput ? adjustInput.value : '';

        this.close();

        // 调用 script.js 中的 startIncrementalGeneration
        if (typeof startIncrementalGeneration === 'function') {
            startIncrementalGeneration(
                this._prompt,
                this._formData,
                this._allImages,
                this._projectName,
                updatedSpec,
                this._projectId,
                adjustmentNote.trim()
            );
        }
    }

    regenerate() {
        const adjustInput = document.getElementById('specStudioAdjustInput');
        const adjustmentNote = adjustInput ? adjustInput.value : '';
        if (!adjustmentNote.trim()) {
            if (typeof showToast === 'function') {
                showToast('请先输入你想调整的内容', 'info');
            }
            return;
        }

        const currentSpec = this._graphDataToSpec();
        this.close();

        // 调用 script.js 中的 regenerateSpec
        if (typeof regenerateSpec === 'function') {
            regenerateSpec(
                this._prompt,
                this._formData,
                this._allImages,
                this._projectName,
                currentSpec,
                adjustmentNote.trim()
            );
        }
    }

    // ========================
    // 工具方法
    // ========================

    _updatePageCounts() {
        const count = this.graphData.nodes.length;
        const pc = document.getElementById('specStudioPageCount');
        const ac = document.getElementById('specActionPageCount');
        if (pc) pc.textContent = count + ' 个页面';
        if (ac) ac.textContent = count;
    }
}
