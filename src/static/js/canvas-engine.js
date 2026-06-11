/**
 * CanvasEngine - 无限画布引擎
 *
 * 管理画布的平移、缩放、坐标转换。
 * 基于 CSS transform: translate(x,y) scale(s) 实现，
 * 支持鼠标滚轮缩放、拖拽平移、触摸捏合。
 */
class CanvasEngine {
    /**
     * @param {HTMLElement} viewport  视口容器（overflow:hidden）
     * @param {HTMLElement} world     世界容器（承载卡片，绝对定位）
     * @param {SVGElement}  svgGroup  SVG <g> 元素（连接线，共享同一变换）
     */
    constructor(viewport, world, svgGroup) {
        this.viewport = viewport;
        this.world = world;
        this.svgGroup = svgGroup;

        // 变换状态（不可变模式：始终创建新对象）
        this._state = { panX: 0, panY: 0, scale: 1 };

        // 缩放范围
        this.MIN_SCALE = 0.1;
        this.MAX_SCALE = 3;

        // 交互状态
        this._isDragging = false;
        this._lastPointer = { x: 0, y: 0 };
        this._spacePressed = false;

        // 动画
        this._animFrameId = null;

        // 回调
        this._onTransformChange = null;
        // 视口 resize 回调（外部注册，用于触发 fitAll）
        this.onResize = null;

        this._bindEvents();
    }

    // ==================== 公共 API ====================

    /** 注册变换变更回调 */
    onTransformChange(fn) {
        this._onTransformChange = fn;
    }

    /** 获取当前状态快照（用于持久化） */
    getState() {
        return { panX: this._state.panX, panY: this._state.panY, scale: this._state.scale };
    }

    /** 恢复状态（从持久化数据） */
    restoreState(state) {
        if (!state) return;
        this._state = {
            panX: state.panX || 0,
            panY: state.panY || 0,
            scale: Math.max(this.MIN_SCALE, Math.min(this.MAX_SCALE, state.scale || 1))
        };
        this._applyTransform();
    }

    /** 应用变换到 DOM */
    _applyTransform() {
        const { panX, panY, scale } = this._state;
        const transform = `translate(${panX}px, ${panY}px) scale(${scale})`;
        this.world.style.transform = transform;
        if (this.svgGroup) {
            this.svgGroup.setAttribute('transform', `translate(${panX},${panY}) scale(${scale})`);
        }
        if (this._onTransformChange) {
            this._onTransformChange(this._state);
        }
    }

    /** 平移指定偏移量 */
    panBy(dx, dy) {
        this._state = {
            ...this._state,
            panX: this._state.panX + dx,
            panY: this._state.panY + dy
        };
        this._applyTransform();
    }

    /** 以屏幕坐标为中心缩放 */
    zoomAt(screenX, screenY, factor) {
        const oldScale = this._state.scale;
        const newScale = Math.max(this.MIN_SCALE, Math.min(this.MAX_SCALE, oldScale * factor));

        if (newScale === oldScale) return;

        const rect = this.viewport.getBoundingClientRect();
        // 鼠标下的世界坐标
        const worldX = (screenX - rect.left - this._state.panX) / oldScale;
        const worldY = (screenY - rect.top - this._state.panY) / oldScale;

        this._state = {
            panX: screenX - rect.left - worldX * newScale,
            panY: screenY - rect.top - worldY * newScale,
            scale: newScale
        };
        this._applyTransform();
    }

    /** 放大一步 */
    zoomIn() {
        const rect = this.viewport.getBoundingClientRect();
        this.zoomAt(rect.left + rect.width / 2, rect.top + rect.height / 2, 1.25);
    }

    /** 缩小一步 */
    zoomOut() {
        const rect = this.viewport.getBoundingClientRect();
        this.zoomAt(rect.left + rect.width / 2, rect.top + rect.height / 2, 0.8);
    }

    /** 重置视图（居中 + 100%） */
    resetView() {
        this._state = { panX: 0, panY: 0, scale: 1 };
        this._applyTransform();
    }

    /** 适应所有卡片 */
    fitAll(cardPositions, cardWidth, cardHeight) {
        const positions = Object.values(cardPositions);
        if (positions.length === 0) {
            this.resetView();
            return;
        }

        let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
        for (const p of positions) {
            minX = Math.min(minX, p.x);
            minY = Math.min(minY, p.y);
            maxX = Math.max(maxX, p.x + (cardWidth || 420));
            maxY = Math.max(maxY, p.y + (cardHeight || 420));
        }

        const rect = this.viewport.getBoundingClientRect();
        const padding = 60;
        const contentW = maxX - minX + padding * 2;
        const contentH = maxY - minY + padding * 2;

        const scaleX = rect.width / contentW;
        const scaleY = rect.height / contentH;
        const scale = Math.max(this.MIN_SCALE, Math.min(this.MAX_SCALE, Math.min(scaleX, scaleY)));

        const centerX = (minX + maxX) / 2;
        const centerY = (minY + maxY) / 2;

        this._state = {
            panX: rect.width / 2 - centerX * scale,
            panY: rect.height / 2 - centerY * scale,
            scale: scale
        };
        this._applyTransform();
    }

    /** 聚焦到指定世界坐标（带动画） */
    centerOn(worldX, worldY, animate) {
        const rect = this.viewport.getBoundingClientRect();
        const targetPanX = rect.width / 2 - worldX * this._state.scale;
        const targetPanY = rect.height / 2 - worldY * this._state.scale;

        if (!animate) {
            this._state = { ...this._state, panX: targetPanX, panY: targetPanY };
            this._applyTransform();
            return;
        }

        // 平滑动画
        const startState = { ...this._state };
        const startTime = performance.now();
        const duration = 300; // ms

        if (this._animFrameId) cancelAnimationFrame(this._animFrameId);

        const animateStep = (now) => {
            const elapsed = now - startTime;
            const t = Math.min(1, elapsed / duration);
            // easeOutCubic
            const ease = 1 - Math.pow(1 - t, 3);

            this._state = {
                panX: startState.panX + (targetPanX - startState.panX) * ease,
                panY: startState.panY + (targetPanY - startState.panY) * ease,
                scale: this._state.scale
            };
            this._applyTransform();

            if (t < 1) {
                this._animFrameId = requestAnimationFrame(animateStep);
            } else {
                this._animFrameId = null;
            }
        };

        this._animFrameId = requestAnimationFrame(animateStep);
    }

    /** 屏幕坐标 → 世界坐标 */
    screenToWorld(sx, sy) {
        const rect = this.viewport.getBoundingClientRect();
        return {
            x: (sx - rect.left - this._state.panX) / this._state.scale,
            y: (sy - rect.top - this._state.panY) / this._state.scale
        };
    }

    /** 世界坐标 → 屏幕坐标 */
    worldToScreen(wx, wy) {
        const rect = this.viewport.getBoundingClientRect();
        return {
            x: wx * this._state.scale + this._state.panX + rect.left,
            y: wy * this._state.scale + this._state.panY + rect.top
        };
    }

    /** 获取缩放百分比文本 */
    getZoomPercent() {
        return Math.round(this._state.scale * 100) + '%';
    }

    /** 重置引擎（清空状态） */
    reset() {
        this._state = { panX: 0, panY: 0, scale: 1 };
        if (this._animFrameId) {
            cancelAnimationFrame(this._animFrameId);
            this._animFrameId = null;
        }
        if (this._resizeObserver) {
            this._resizeObserver.disconnect();
            this._resizeObserver = null;
        }
        this._applyTransform();
    }

    // ==================== 事件绑定 ====================

    _bindEvents() {
        // 滚轮缩放
        this.viewport.addEventListener('wheel', (e) => {
            e.preventDefault();
            const factor = e.deltaY > 0 ? 0.92 : 1.08;
            this.zoomAt(e.clientX, e.clientY, factor);
        }, { passive: false });

        // 鼠标拖拽平移
        this.viewport.addEventListener('mousedown', (e) => {
            // 只响应左键 + 空白区域（不是卡片）
            if (e.button !== 0) return;
            if (e.target.closest('.cs-page-card')) return;

            this._isDragging = true;
            this._lastPointer = { x: e.clientX, y: e.clientY };
            this.viewport.classList.add('cs-dragging');
            e.preventDefault();
        });

        document.addEventListener('mousemove', (e) => {
            if (!this._isDragging) return;
            const dx = e.clientX - this._lastPointer.x;
            const dy = e.clientY - this._lastPointer.y;
            this._lastPointer = { x: e.clientX, y: e.clientY };
            this.panBy(dx, dy);
        });

        document.addEventListener('mouseup', () => {
            if (this._isDragging) {
                this._isDragging = false;
                this.viewport.classList.remove('cs-dragging');
            }
        });

        // 触摸：单指平移，双指缩放
        let touchState = { touches: [], lastDist: 0, lastCenter: null };

        this.viewport.addEventListener('touchstart', (e) => {
            if (e.target.closest('.cs-page-card')) return;
            touchState.touches = [...e.touches];
            if (e.touches.length === 2) {
                const dx = e.touches[1].clientX - e.touches[0].clientX;
                const dy = e.touches[1].clientY - e.touches[0].clientY;
                touchState.lastDist = Math.sqrt(dx * dx + dy * dy);
                touchState.lastCenter = {
                    x: (e.touches[0].clientX + e.touches[1].clientX) / 2,
                    y: (e.touches[0].clientY + e.touches[1].clientY) / 2
                };
            }
        }, { passive: true });

        this.viewport.addEventListener('touchmove', (e) => {
            if (e.target.closest('.cs-page-card')) return;
            e.preventDefault();

            if (e.touches.length === 2) {
                // 双指缩放
                const dx = e.touches[1].clientX - e.touches[0].clientX;
                const dy = e.touches[1].clientY - e.touches[0].clientY;
                const dist = Math.sqrt(dx * dx + dy * dy);
                const center = {
                    x: (e.touches[0].clientX + e.touches[1].clientX) / 2,
                    y: (e.touches[0].clientY + e.touches[1].clientY) / 2
                };

                if (touchState.lastDist > 0) {
                    const factor = dist / touchState.lastDist;
                    this.zoomAt(center.x, center.y, factor);
                }

                // 双指平移
                if (touchState.lastCenter) {
                    this.panBy(
                        center.x - touchState.lastCenter.x,
                        center.y - touchState.lastCenter.y
                    );
                }

                touchState.lastDist = dist;
                touchState.lastCenter = center;
            } else if (e.touches.length === 1 && touchState.touches.length === 1) {
                // 单指平移
                const dx = e.touches[0].clientX - touchState.touches[0].clientX;
                const dy = e.touches[0].clientY - touchState.touches[0].clientY;
                this.panBy(dx, dy);
                touchState.touches = [...e.touches];
            }
        }, { passive: false });

        this.viewport.addEventListener('touchend', () => {
            touchState = { touches: [], lastDist: 0, lastCenter: null };
        }, { passive: true });

        // 键盘快捷键
        document.addEventListener('keydown', (e) => {
            if (e.code === 'Space' && !e.target.matches('input, textarea')) {
                e.preventDefault();
                this._spacePressed = true;
                this.viewport.style.cursor = 'grab';
            }
        });

        document.addEventListener('keyup', (e) => {
            if (e.code === 'Space') {
                this._spacePressed = false;
                this.viewport.style.cursor = '';
            }
            // Ctrl+0 适应全部（需要外部调用 fitAll）
            // Ctrl+1 重置视图
            if (e.ctrlKey && e.code === 'Digit1') {
                e.preventDefault();
                this.resetView();
            }
        });

        // ResizeObserver：视口尺寸变化时通知外部
        if (typeof ResizeObserver !== 'undefined') {
            let resizeTimer = null;
            this._resizeObserver = new ResizeObserver(() => {
                // 防抖 200ms，避免 resize 期间频繁触发
                if (resizeTimer) clearTimeout(resizeTimer);
                resizeTimer = setTimeout(() => {
                    if (this.onResize) this.onResize();
                }, 200);
            });
            this._resizeObserver.observe(this.viewport);
        }
    }
}

// 全局单例（由 CanvasStudio 初始化时设置）
window.canvasEngine = null;
