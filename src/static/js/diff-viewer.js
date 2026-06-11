/**
 * DiffViewer - Claude Code 风格 unified diff 渲染器
 * 零外部依赖（jsdiff 库可选加载，缺失时降级显示）
 *
 * 用法: DiffViewer.render(oldText, newText, { id: 'my-diff' })
 * 返回: HTML 字符串，插入 DOM 后自动绑定 toggle 交互
 */
const DiffViewer = (function () {
    'use strict';

    // ===== CSS 样式（注入一次） =====
    const STYLES = `
.uv-diff-container {
    margin-top: 6px;
    border-radius: 8px;
    overflow: hidden;
    border: 1px solid #e2e8f0;
    font-size: 12px;
    background: #fff;
}
.uv-diff-toggle {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 5px 10px;
    background: #f8fafc;
    cursor: pointer;
    color: #64748b;
    user-select: none;
    transition: background 0.15s;
    font-size: 12px;
}
.uv-diff-toggle:hover { background: #f1f5f9; }
.uv-diff-toggle i { transition: transform 0.2s ease; font-size: 10px; }
.uv-diff-toggle.expanded i { transform: rotate(90deg); }
.uv-diff-badges { display: flex; gap: 4px; margin-left: auto; }
.uv-diff-badge {
    font-size: 10px;
    padding: 1px 6px;
    border-radius: 10px;
    font-weight: 600;
    font-family: 'SF Mono', 'Menlo', 'Consolas', monospace;
    line-height: 1.4;
}
.uv-diff-badge-add { background: #dcfce7; color: #166534; }
.uv-diff-badge-del { background: #fef2f2; color: #991b1b; }
.uv-diff-body {
    max-height: 0;
    overflow: hidden;
    transition: max-height 0.35s cubic-bezier(0.4, 0, 0.2, 1);
}
.uv-diff-body.expanded {
    max-height: 600px;
    overflow-y: auto;
}
.uv-diff-body::-webkit-scrollbar { width: 6px; }
.uv-diff-body::-webkit-scrollbar-thumb { background: #d1d5db; border-radius: 3px; }
.uv-diff-body::-webkit-scrollbar-thumb:hover { background: #9ca3af; }
.uv-diff-table {
    font-family: 'SF Mono', 'Menlo', 'Consolas', 'Courier New', monospace;
    font-size: 11.5px;
    line-height: 1.65;
}
.uv-diff-line {
    display: flex;
    align-items: stretch;
    min-height: 20px;
}
.uv-diff-line:hover { filter: brightness(0.97); }
.uv-diff-line-added { background: rgba(46, 160, 67, 0.12); }
.uv-diff-line-removed { background: rgba(248, 81, 73, 0.12); }
.uv-diff-line-added .uv-diff-prefix,
.uv-diff-line-added .uv-diff-content { color: #1a7f37; }
.uv-diff-line-removed .uv-diff-prefix,
.uv-diff-line-removed .uv-diff-content { color: #cf222e; }
.uv-diff-num {
    min-width: 36px;
    padding: 0 6px;
    text-align: right;
    color: #8b949e;
    background: rgba(0, 0, 0, 0.025);
    user-select: none;
    flex-shrink: 0;
    display: flex;
    align-items: center;
    justify-content: flex-end;
    font-size: 11px;
}
.uv-diff-line-added .uv-diff-num-new,
.uv-diff-line-removed .uv-diff-num-old { color: inherit; font-weight: 600; }
.uv-diff-prefix {
    width: 20px;
    text-align: center;
    flex-shrink: 0;
    opacity: 0.6;
    display: flex;
    align-items: center;
    justify-content: center;
}
.uv-diff-content {
    flex: 1;
    padding: 0 8px;
    white-space: pre-wrap;
    word-break: break-all;
    min-width: 0;
}
.uv-diff-separator {
    display: flex;
    align-items: center;
    background: #f0f4f8;
    color: #64748b;
    font-size: 11px;
    padding: 2px 0;
}
.uv-diff-sep-content {
    padding: 0 8px;
    font-style: italic;
    opacity: 0.7;
}
.uv-diff-sep-num {
    min-width: 36px;
    padding: 0 6px;
    text-align: right;
    flex-shrink: 0;
}
/* 语法高亮 */
.uv-hl-tag { color: #116329; }
.uv-hl-attr { color: #0550ae; }
.uv-hl-string { color: #0a3069; }
.uv-hl-comment { color: #6a737d; }
.uv-hl-doctype { color: #6a737d; }
/* Fallback 样式 */
.uv-diff-fallback-old {
    background: #fef2f2; color: #991b1b; padding: 6px 10px;
    border-bottom: 1px solid #fecaca;
}
.uv-diff-fallback-new {
    background: #f0fdf4; color: #166534; padding: 6px 10px;
}
.uv-diff-fallback-label {
    font-size: 10px; font-weight: 600; margin-bottom: 2px;
    text-transform: uppercase; opacity: 0.7;
}`;

    let _stylesInjected = false;

    function injectStyles() {
        if (_stylesInjected) return;
        _stylesInjected = true;
        var style = document.createElement('style');
        style.textContent = STYLES;
        document.head.appendChild(style);
    }

    // ===== HTML 转义 =====
    function escapeHtml(text) {
        if (!text) return '';
        var div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    // ===== 轻量 HTML 语法高亮 =====
    function highlightHTML(text) {
        var escaped = escapeHtml(text);
        // HTML comments: &lt;!-- ... --&gt;
        escaped = escaped.replace(/(&lt;!--[\s\S]*?--&gt;)/g,
            '<span class="uv-hl-comment">$1</span>');
        // DOCTYPE
        escaped = escaped.replace(/(&lt;!DOCTYPE[^&]*?&gt;)/gi,
            '<span class="uv-hl-doctype">$1</span>');
        // Tags: &lt;/tagname or &lt;tagname
        escaped = escaped.replace(/(&lt;\/?)([\w-]+)/g,
            '$1<span class="uv-hl-tag">$2</span>');
        // Attributes with double-quoted values: attr=&quot;value&quot;
        escaped = escaped.replace(/([\w-]+)(=)(&quot;[^&]*?&quot;)/g,
            '<span class="uv-hl-attr">$1</span>$2<span class="uv-hl-string">$3</span>');
        // Attributes with single-quoted values: attr=&#39;value&#39;
        escaped = escaped.replace(/([\w-]+)(=)(&#39;[^&]*?&#39;)/g,
            '<span class="uv-hl-attr">$1</span>$2<span class="uv-hl-string">$3</span>');
        return escaped;
    }

    // ===== Unified Diff 计算 =====
    function computeUnifiedDiff(oldText, newText, contextLines) {
        contextLines = contextLines || 3;
        if (typeof Diff === 'undefined') return null;

        var patch = Diff.structuredPatch(
            'old', 'new', oldText, newText,
            undefined, undefined, { context: contextLines }
        );

        if (!patch.hunks || patch.hunks.length === 0) return [];

        var lines = [];
        var oldLineNo = 1;
        var newLineNo = 1;

        for (var h = 0; h < patch.hunks.length; h++) {
            var hunk = patch.hunks[h];
            // Gap separator
            if (oldLineNo < hunk.oldStart || newLineNo < hunk.newStart) {
                lines.push({
                    type: 'separator',
                    oldStart: oldLineNo, newStart: newLineNo,
                    oldEnd: hunk.oldStart - 1, newEnd: hunk.newStart - 1
                });
                oldLineNo = hunk.oldStart;
                newLineNo = hunk.newStart;
            }
            for (var i = 0; i < hunk.lines.length; i++) {
                var change = hunk.lines[i];
                if (change.startsWith('+')) {
                    lines.push({ type: 'added', newLine: newLineNo++, content: change.slice(1) });
                } else if (change.startsWith('-')) {
                    lines.push({ type: 'removed', oldLine: oldLineNo++, content: change.slice(1) });
                } else {
                    lines.push({ type: 'context', oldLine: oldLineNo++, newLine: newLineNo++, content: change.slice(1) });
                }
            }
        }
        return lines;
    }

    // ===== 构建 diff HTML =====
    function buildDiffHTML(lines) {
        var html = '<div class="uv-diff-table">';
        for (var i = 0; i < lines.length; i++) {
            var line = lines[i];
            if (line.type === 'separator') {
                html += '<div class="uv-diff-separator">';
                html += '<span class="uv-diff-sep-num"></span>';
                html += '<span class="uv-diff-sep-num"></span>';
                html += '<span class="uv-diff-prefix"></span>';
                html += '<span class="uv-diff-sep-content">@@ ' + (line.oldEnd - line.oldStart + 1) + ' lines skipped @@</span>';
                html += '</div>';
                continue;
            }
            var cls = line.type === 'added' ? 'uv-diff-line-added' :
                      line.type === 'removed' ? 'uv-diff-line-removed' : '';
            var prefix = line.type === 'added' ? '+' : line.type === 'removed' ? '-' : ' ';
            var oldNum = line.oldLine || '';
            var newNum = line.newLine || '';
            var highlighted = highlightHTML(line.content);
            html += '<div class="uv-diff-line ' + cls + '">';
            html += '<span class="uv-diff-num uv-diff-num-old">' + oldNum + '</span>';
            html += '<span class="uv-diff-num uv-diff-num-new">' + newNum + '</span>';
            html += '<span class="uv-diff-prefix">' + prefix + '</span>';
            html += '<span class="uv-diff-content">' + highlighted + '</span>';
            html += '</div>';
        }
        html += '</div>';
        return html;
    }

    // ===== Fallback（jsdiff 未加载时） =====
    function buildFallbackHTML(oldText, newText) {
        var html = '<div class="uv-diff-table">';
        if (oldText) {
            var oldLines = oldText.split('\n');
            html += '<div class="uv-diff-fallback-old"><div class="uv-diff-fallback-label">- Original</div>';
            for (var i = 0; i < Math.min(oldLines.length, 20); i++) {
                html += '<div class="uv-diff-line uv-diff-line-removed">';
                html += '<span class="uv-diff-num">' + (i + 1) + '</span>';
                html += '<span class="uv-diff-num"></span>';
                html += '<span class="uv-diff-prefix">-</span>';
                html += '<span class="uv-diff-content">' + escapeHtml(oldLines[i]) + '</span>';
                html += '</div>';
            }
            if (oldLines.length > 20) {
                html += '<div class="uv-diff-separator"><span class="uv-diff-sep-content">... ' + (oldLines.length - 20) + ' more lines</span></div>';
            }
            html += '</div>';
        }
        if (newText) {
            var newLines = newText.split('\n');
            html += '<div class="uv-diff-fallback-new"><div class="uv-diff-fallback-label">+ Modified</div>';
            for (var j = 0; j < Math.min(newLines.length, 20); j++) {
                html += '<div class="uv-diff-line uv-diff-line-added">';
                html += '<span class="uv-diff-num"></span>';
                html += '<span class="uv-diff-num">' + (j + 1) + '</span>';
                html += '<span class="uv-diff-prefix">+</span>';
                html += '<span class="uv-diff-content">' + escapeHtml(newLines[j]) + '</span>';
                html += '</div>';
            }
            if (newLines.length > 20) {
                html += '<div class="uv-diff-separator"><span class="uv-diff-sep-content">... ' + (newLines.length - 20) + ' more lines</span></div>';
            }
            html += '</div>';
        }
        html += '</div>';
        return html;
    }

    // ===== Toggle 事件委托 =====
    function setupToggle() {
        document.addEventListener('click', function (e) {
            var toggle = e.target.closest('.uv-diff-toggle');
            if (!toggle) return;
            toggle.classList.toggle('expanded');
            var targetId = toggle.getAttribute('data-target');
            var body = document.getElementById(targetId);
            if (!body) return;
            if (body.classList.contains('expanded')) {
                body.style.maxHeight = body.scrollHeight + 'px';
                // Force reflow so the browser registers the current value
                body.offsetHeight; // eslint-disable-line no-unused-expressions
                body.style.maxHeight = '0';
                body.classList.remove('expanded');
            } else {
                body.classList.add('expanded');
                // Set precise max-height for smooth animation
                if (body.scrollHeight > 600) {
                    body.style.maxHeight = '600px';
                } else {
                    body.style.maxHeight = body.scrollHeight + 'px';
                }
            }
        });
    }

    // 初始化 toggle 事件
    var _toggleSetup = false;
    function ensureToggle() {
        if (_toggleSetup) return;
        _toggleSetup = true;
        setupToggle();
    }

    // ===== 主渲染函数 =====
    function render(oldText, newText, options) {
        options = options || {};
        var uid = options.id || ('uv-' + Date.now() + '-' + Math.random().toString(36).slice(2, 6));

        injectStyles();
        ensureToggle();

        var lines = computeUnifiedDiff(oldText || '', newText || '');

        // Count changes for badge
        var added = 0, removed = 0;
        if (lines) {
            for (var i = 0; i < lines.length; i++) {
                if (lines[i].type === 'added') added++;
                if (lines[i].type === 'removed') removed++;
            }
        } else {
            // Fallback: rough count
            var oldLines = (oldText || '').split('\n');
            var newLines = (newText || '').split('\n');
            removed = oldLines.length;
            added = newLines.length;
        }

        // 小改动（≤15行变更）自动展开
        var totalChanges = added + removed;
        var autoExpand = totalChanges > 0 && totalChanges <= 15;

        // Badge HTML
        var badgeHTML = '<div class="uv-diff-badges">';
        if (added > 0) badgeHTML += '<span class="uv-diff-badge uv-diff-badge-add">+' + added + '</span>';
        if (removed > 0) badgeHTML += '<span class="uv-diff-badge uv-diff-badge-del">-' + removed + '</span>';
        badgeHTML += '</div>';

        var html = '<div class="uv-diff-container">';
        html += '<div class="uv-diff-toggle' + (autoExpand ? ' expanded' : '') + '" data-target="' + uid + '">';
        html += '<i class="fas fa-chevron-right"></i>';
        html += ' <span>查看变更</span>';
        html += badgeHTML;
        html += '</div>';
        html += '<div id="' + uid + '" class="uv-diff-body' + (autoExpand ? ' expanded' : '') + '">';

        if (lines) {
            html += buildDiffHTML(lines);
        } else {
            html += buildFallbackHTML(oldText, newText);
        }

        html += '</div></div>';
        return html;
    }

    return { render: render };
})();
