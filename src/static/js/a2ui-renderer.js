/**
 * A2UIRenderer — JSONL 组件树 → HTML 实时渲染器
 *
 * AI 输出 JSONL（每行一个 JSON 组件），此渲染器逐行解析并生成 HTML。
 * 每个 JSONL 行是完整独立的 JSON 对象，流式传输时可增量解析。
 *
 * 用法：
 *   const renderer = new A2UIRenderer();
 *   renderer.handleBlock(jsonlLine);
 *   const html = renderer.toHtml();
 */
class A2UIRenderer {
    constructor() {
        this._components = new Map();   // id -> component data
        this._rootId = null;
        this._pageMeta = null;          // page-start marker data
        this._isComplete = false;
        this._designTokens = {};
    }

    /**
     * 处理单个 A2UI 块（一个 JSONL 行）
     * @param {string} line - 单个 JSONL 行
     */
    handleBlock(line) {
        const trimmed = (line || '').trim();
        if (!trimmed) return false;

        let parsed;
        try {
            parsed = JSON.parse(trimmed);
        } catch (e) {
            console.warn('[A2UI] JSON 解析失败:', trimmed.substring(0, 120));
            return false;
        }

        if (parsed.type === 'marker') {
            this._handleMarker(parsed);
        } else if (parsed.type === 'component') {
            this._handleComponent(parsed);
        } else {
            console.warn('[A2UI] 未知行类型:', parsed.type);
            return false;
        }
        return true;
    }

    /**
     * 批量处理多行 JSONL 文本
     * @param {string} text - 多行 JSONL 文本
     */
    handleBlockBatch(text) {
        const lines = text.split('\n');
        let count = 0;
        for (const line of lines) {
            if (this.handleBlock(line)) count++;
        }
        return count;
    }

    /**
     * 重置渲染器状态（新页面开始前调用）
     */
    reset() {
        this._components.clear();
        this._rootId = null;
        this._pageMeta = null;
        this._isComplete = false;
    }

    _handleMarker(marker) {
        if (marker.marker === 'page-start') {
            this.reset();
            this._pageMeta = marker;
            if (marker.designTokens) {
                this._designTokens = marker.designTokens;
            }
        } else if (marker.marker === 'page-end') {
            this._isComplete = true;
        }
    }

    _handleComponent(comp) {
        this._components.set(comp.id, comp);
        if (comp.id === 'root') {
            this._rootId = 'root';
        }
    }

    /**
     * 将组件树转换为完整独立 HTML 文档
     */
    toHtml() {
        if (!this._rootId) return '';

        const bodyContent = this._renderComponent(this._rootId);
        if (!bodyContent) return '';

        const title = this._pageMeta ? this._pageMeta.page : 'Page';
        const bgMode = this._designTokens.bgMode || 'light';
        const bgColor = bgMode === 'dark' ? '#1a1a2e' : '#f5f5f5';
        const primaryColor = this._designTokens.primaryColor || '#004fff';
        const secondaryColor = this._designTokens.secondaryColor || '#10b981';

        return '<!DOCTYPE html>\n' +
            '<html lang="zh-CN">\n<head>\n' +
            '  <meta charset="UTF-8">\n' +
            '  <meta name="viewport" content="width=device-width, initial-scale=1.0">\n' +
            '  <title>' + A2UIRenderer._esc(title) + '</title>\n' +
            '  <script src="https://cdn.tailwindcss.com"><\/script>\n' +
            '  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">\n' +
            '  <style>\n' +
            '    :root { --primary-color: ' + primaryColor + '; --secondary-color: ' + secondaryColor + '; }\n' +
            '    body { margin: 0; background: ' + bgColor + '; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }\n' +
            '    .stripe tbody tr:nth-child(even) { background: #f9fafb; }\n' +
            '  </style>\n' +
            '</head>\n<body>\n' +
            bodyContent + '\n' +
            '</body>\n</html>';
    }

    /**
     * 递归渲染组件为 HTML 字符串
     */
    _renderComponent(id) {
        const comp = this._components.get(id);
        if (!comp) return '<!-- missing: ' + id + ' -->';

        const renderer = A2UIRenderer.RENDERERS[comp.component];
        if (!renderer) {
            // 未知组件 fallback
            if (comp.component === 'RawHtml') {
                return (comp.props && comp.props.html) || '';
            }
            console.warn('[A2UI] 未知组件类型:', comp.component);
            return '<!-- unknown component: ' + comp.component + ' -->';
        }

        // 渲染子组件
        const childrenHtml = (comp.children || [])
            .map(childId => this._renderComponent(childId))
            .join('\n');

        return renderer(comp.style || '', comp.props || {}, childrenHtml);
    }

    /**
     * 获取当前渲染状态
     */
    getStatus() {
        return {
            componentCount: this._components.size,
            isComplete: this._isComplete,
            pageName: this._pageMeta ? this._pageMeta.page : null,
            hasRoot: this._rootId !== null
        };
    }

    /** HTML 转义 */
    static _esc(str) {
        if (!str) return '';
        return String(str)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    /** 属性值转义 */
    static _escAttr(str) {
        if (!str) return '';
        return String(str)
            .replace(/&/g, '&amp;')
            .replace(/"/g, '&quot;')
            .replace(/</g, '&lt;');
    }
}

// ==================== 组件渲染器 ====================
// 每个渲染器是纯函数: (style, props, childrenHtml) -> htmlString

A2UIRenderer.RENDERERS = {

    // ---- 布局组件 ----

    Page: function(style, props, children) {
        return '<div class="' + style + '">' + children + '</div>';
    },

    FlexRow: function(style, props, children) {
        return '<div class="flex ' + style + '">' + children + '</div>';
    },

    FlexColumn: function(style, props, children) {
        return '<div class="flex flex-col ' + style + '">' + children + '</div>';
    },

    Grid: function(style, props, children) {
        var cols = props.cols || 3;
        var gap = props.gap || 4;
        return '<div class="grid grid-cols-' + cols + ' gap-' + gap + ' ' + style + '">' + children + '</div>';
    },

    // ---- 导航组件 ----

    NavBar: function(style, props, children) {
        var title = props.title || '';
        var logo = props.logo ? '<i class="fas ' + props.logo + ' mr-2" style="color:var(--primary-color)"></i>' : '';
        var breadcrumbs = '';
        if (props.breadcrumbs && props.breadcrumbs.length > 0) {
            var parts = [];
            for (var i = 0; i < props.breadcrumbs.length; i++) {
                if (i < props.breadcrumbs.length - 1) {
                    parts.push('<a href="#" class="text-gray-500 hover:text-gray-700">' + A2UIRenderer._esc(props.breadcrumbs[i].label) + '</a>');
                } else {
                    parts.push('<span class="text-gray-900 font-medium">' + A2UIRenderer._esc(props.breadcrumbs[i].label) + '</span>');
                }
            }
            breadcrumbs = '<nav class="text-sm ml-4">' + parts.join(' <span class="mx-1 text-gray-400">/</span> ') + '</nav>';
        }
        return '<nav class="' + style + '">' + logo + '<span class="font-semibold text-lg">' + A2UIRenderer._esc(title) + '</span>' + breadcrumbs + children + '</nav>';
    },

    SideBar: function(style, props, children) {
        var items = props.items || [];
        var width = props.width || 'w-56';
        var itemsHtml = '';
        for (var i = 0; i < items.length; i++) {
            var item = items[i];
            var activeClass = item.active ? 'bg-blue-50 text-blue-600 font-medium' : 'text-gray-600 hover:bg-gray-100';
            var icon = item.icon ? '<i class="fas ' + item.icon + ' w-5 text-center"></i>' : '';
            itemsHtml += '<a href="#" class="flex items-center px-4 py-2.5 text-sm ' + activeClass + ' transition-colors">' +
                '<span class="mr-3">' + icon + '</span>' + A2UIRenderer._esc(item.label) + '</a>';
        }
        return '<aside class="' + width + ' ' + style + ' flex-shrink-0">' + itemsHtml + children + '</aside>';
    },

    Tabs: function(style, props, children) {
        var items = props.items || [];
        var tabsHtml = '';
        for (var i = 0; i < items.length; i++) {
            var t = items[i];
            var activeClass = t.active ? 'border-b-2 border-blue-500 text-blue-600 font-medium' : 'text-gray-500 hover:text-gray-700';
            tabsHtml += '<button class="px-4 py-2.5 text-sm ' + activeClass + ' transition-colors">' + A2UIRenderer._esc(t.label) + '</button>';
        }
        return '<div class="flex border-b ' + style + '">' + tabsHtml + children + '</div>';
    },

    // ---- 内容组件 ----

    Card: function(style, props, children) {
        var header = '';
        if (props.title) {
            var extra = props.extra || '';
            header = '<div class="flex items-center justify-between mb-4">' +
                '<h3 class="text-lg font-semibold text-gray-800">' + A2UIRenderer._esc(props.title) + '</h3>' +
                extra + '</div>';
        }
        return '<div class="bg-white rounded-xl shadow-sm p-5 ' + style + '">' + header + children + '</div>';
    },

    Table: function(style, props, children) {
        var columns = props.columns || [];
        var data = props.data || [];
        var stripeClass = props.stripe !== false ? 'stripe' : '';

        var thCells = '';
        for (var c = 0; c < columns.length; c++) {
            thCells += '<th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">' +
                A2UIRenderer._esc(columns[c].label) + '</th>';
        }

        var rows = '';
        for (var r = 0; r < data.length; r++) {
            var row = data[r];
            var tdCells = '';
            for (var c2 = 0; c2 < columns.length; c2++) {
                var key = columns[c2].key || '';
                var val = row[key] !== undefined ? row[key] : '';
                // 支持列级 render（如 badge）
                if (columns[c2].render === 'badge') {
                    var badgeColor = { success: 'green', warning: 'yellow', danger: 'red', info: 'blue' }[val] || 'gray';
                    val = '<span class="px-2 py-0.5 rounded-full text-xs font-medium bg-' + badgeColor + '-100 text-' + badgeColor + '-700">' + A2UIRenderer._esc(val) + '</span>';
                } else if (columns[c2].render === 'tag') {
                    val = '<span class="px-2 py-0.5 rounded text-xs border border-gray-300 text-gray-600">' + A2UIRenderer._esc(val) + '</span>';
                } else {
                    val = A2UIRenderer._esc(val);
                }
                tdCells += '<td class="px-4 py-3 text-sm text-gray-900">' + val + '</td>';
            }
            rows += '<tr class="hover:bg-gray-50 border-b border-gray-100">' + tdCells + '</tr>';
        }

        var table = '<table class="min-w-full ' + stripeClass + '">' +
            '<thead class="bg-gray-50"><tr>' + thCells + '</tr></thead>' +
            '<tbody>' + rows + '</tbody></table>';

        var pagination = '';
        if (props.pagination) {
            var total = props.pagination.total || 0;
            var pageSize = props.pagination.pageSize || 10;
            var pages = Math.ceil(total / pageSize);
            pagination = '<div class="flex items-center justify-between mt-4 pt-3 border-t">' +
                '<span class="text-sm text-gray-500">共 ' + total + ' 条</span>' +
                '<div class="flex gap-1">';
            for (var p = 1; p <= Math.min(pages, 5); p++) {
                var active = p === 1 ? 'bg-blue-500 text-white' : 'bg-white text-gray-600 hover:bg-gray-50 border';
                pagination += '<button class="px-3 py-1 rounded text-sm ' + active + '">' + p + '</button>';
            }
            pagination += '</div></div>';
        }

        var scrollWrapper = props.scrollX !== false ? '<div class="overflow-x-auto">' + table + '</div>' : table;
        return '<div class="' + style + '">' + scrollWrapper + pagination + children + '</div>';
    },

    Form: function(style, props, children) {
        var fields = props.fields || [];
        var fieldsHtml = '';
        for (var i = 0; i < fields.length; i++) {
            var f = fields[i];
            var required = f.required ? ' <span class="text-red-500">*</span>' : '';
            var label = '<label class="block text-sm font-medium text-gray-700 mb-1">' + A2UIRenderer._esc(f.label) + required + '</label>';

            if (f.type === 'select' && f.options) {
                var opts = '';
                for (var j = 0; j < f.options.length; j++) {
                    opts += '<option>' + A2UIRenderer._esc(f.options[j]) + '</option>';
                }
                fieldsHtml += '<div class="mb-4">' + label + '<select class="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500">' + opts + '</select></div>';
            } else if (f.type === 'textarea') {
                fieldsHtml += '<div class="mb-4">' + label + '<textarea class="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500" rows="3" placeholder="' + A2UIRenderer._escAttr(f.placeholder || '') + '"></textarea></div>';
            } else {
                fieldsHtml += '<div class="mb-4">' + label + '<input type="' + (f.type || 'text') + '" class="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500" placeholder="' + A2UIRenderer._escAttr(f.placeholder || '') + '"></div>';
            }
        }
        return '<form class="' + style + '">' + fieldsHtml + children + '</form>';
    },

    List: function(style, props, children) {
        var items = props.items || [];
        var itemsHtml = '';
        for (var i = 0; i < items.length; i++) {
            var item = items[i];
            var label = typeof item === 'string' ? item : (item.label || '');
            var desc = item.description ? '<p class="text-sm text-gray-500">' + A2UIRenderer._esc(item.description) + '</p>' : '';
            itemsHtml += '<div class="px-4 py-3 border-b border-gray-100 hover:bg-gray-50 transition-colors">' +
                '<div class="text-sm text-gray-900">' + A2UIRenderer._esc(label) + '</div>' + desc + '</div>';
        }
        return '<div class="' + style + '">' + itemsHtml + children + '</div>';
    },

    StatRow: function(style, props, children) {
        var icon = props.icon ? '<div class="w-12 h-12 rounded-xl bg-blue-50 flex items-center justify-center">' +
            '<i class="fas ' + props.icon + ' text-blue-500 text-lg"></i></div>' : '';
        var trend = '';
        if (props.trend) {
            var trendColor = props.trendUp ? 'text-green-500' : 'text-red-500';
            var trendIcon = props.trendUp ? 'fa-arrow-up' : 'fa-arrow-down';
            trend = '<span class="' + trendColor + ' text-sm font-medium"><i class="fas ' + trendIcon + ' text-xs"></i> ' + A2UIRenderer._esc(props.trend) + '</span>';
        }
        return '<div class="bg-white rounded-xl shadow-sm p-5 ' + style + '">' +
            '<div class="flex items-center justify-between">' + icon +
            '<div class="text-right">' + trend + '</div></div>' +
            '<div class="mt-3"><p class="text-sm text-gray-500">' + A2UIRenderer._esc(props.label) + '</p>' +
            '<p class="text-2xl font-bold text-gray-900 mt-1">' + A2UIRenderer._esc(props.value) + '</p></div></div>';
    },

    Chart: function(style, props, children) {
        var height = props.height || 250;
        var chartType = props.chartType || 'bar';
        var icon = chartType === 'pie' ? 'fa-chart-pie' : chartType === 'line' ? 'fa-chart-line' : 'fa-chart-bar';
        return '<div class="bg-white rounded-xl shadow-sm p-5 ' + style + '" style="min-height:' + height + 'px">' +
            '<div class="flex items-center justify-center h-full text-gray-300">' +
            '<i class="fas ' + icon + ' text-4xl"></i></div>' + children + '</div>';
    },

    // ---- 基础组件 ----

    Text: function(style, props, children) {
        var tag = props.tag || 'p';
        var content = props.content || '';
        return '<' + tag + ' class="' + style + '">' + content + '</' + tag + '>';
    },

    Button: function(style, props, children) {
        var variants = {
            primary: 'bg-blue-500 text-white hover:bg-blue-600 shadow-sm',
            secondary: 'bg-gray-100 text-gray-700 hover:bg-gray-200',
            danger: 'bg-red-500 text-white hover:bg-red-600',
            ghost: 'text-gray-600 hover:bg-gray-100',
            success: 'bg-green-500 text-white hover:bg-green-600'
        };
        var v = variants[props.variant] || variants.primary;
        var icon = props.icon ? '<i class="fas ' + props.icon + ' mr-1"></i>' : '';
        var size = props.size === 'sm' ? 'px-3 py-1.5 text-xs' : props.size === 'lg' ? 'px-6 py-3 text-base' : 'px-4 py-2 text-sm';
        return '<button class="rounded-lg font-medium transition-colors ' + v + ' ' + size + ' ' + style + '">' + icon + A2UIRenderer._esc(props.label || '') + '</button>';
    },

    Input: function(style, props, children) {
        var prepend = props.prepend ? '<div class="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400"><i class="fas ' + props.prepend + '"></i></div>' : '';
        var pl = prepend ? 'pl-10' : '';
        return '<div class="relative">' + prepend +
            '<input type="' + (props.type || 'text') + '" placeholder="' + A2UIRenderer._escAttr(props.placeholder || '') + '" value="' + A2UIRenderer._escAttr(props.value || '') + '" class="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500 ' + pl + ' ' + style + '">' +
            '</div>';
    },

    Badge: function(style, props, children) {
        var colors = {
            success: 'bg-green-100 text-green-700',
            warning: 'bg-yellow-100 text-yellow-700',
            danger: 'bg-red-100 text-red-700',
            info: 'bg-blue-100 text-blue-700',
            default: 'bg-gray-100 text-gray-700'
        };
        var c = colors[props.variant] || colors.default;
        return '<span class="px-2.5 py-0.5 rounded-full text-xs font-medium ' + c + ' ' + style + '">' + A2UIRenderer._esc(props.text || '') + '</span>';
    },

    Tag: function(style, props, children) {
        return '<span class="inline-flex items-center px-2 py-0.5 rounded text-xs border ' +
            (props.color || 'border-gray-300 text-gray-600') + ' ' + style + '">' +
            A2UIRenderer._esc(props.text || '') + '</span>';
    },

    Icon: function(style, props, children) {
        var size = props.size || '16';
        var color = props.color ? 'color:' + props.color + ';' : '';
        return '<i class="fas ' + (props.name || '') + ' ' + style + '" style="font-size:' + size + 'px;' + color + '"></i>';
    },

    Image: function(style, props, children) {
        var fit = props.fit ? 'object-fit:' + props.fit + ';' : '';
        var w = props.width ? 'width:' + props.width + ';' : '';
        var h = props.height ? 'height:' + props.height + ';' : '';
        return '<img src="' + A2UIRenderer._escAttr(props.src || '') + '" alt="' + A2UIRenderer._escAttr(props.alt || '') + '" class="' + style + '" style="' + w + h + fit + '">';
    },

    Avatar: function(style, props, children) {
        if (props.src) {
            return '<img src="' + A2UIRenderer._escAttr(props.src) + '" alt="' + A2UIRenderer._escAttr(props.alt || '') + '" class="rounded-full ' + style + '">';
        }
        return '<div class="rounded-full flex items-center justify-center text-white font-medium ' + style + '">' + A2UIRenderer._esc(props.text || '?') + '</div>';
    },

    Modal: function(style, props, children) {
        if (!props.visible) return '';
        var width = props.width || 'max-w-lg';
        return '<div class="fixed inset-0 bg-black/50 flex items-center justify-center z-50">' +
            '<div class="bg-white rounded-xl shadow-2xl ' + width + ' ' + style + '">' +
            '<div class="px-6 py-4 border-b flex items-center justify-between"><h3 class="font-semibold text-lg">' + A2UIRenderer._esc(props.title || '') + '</h3>' +
            '<button class="text-gray-400 hover:text-gray-600"><i class="fas fa-times"></i></button></div>' +
            '<div class="px-6 py-4">' + children + '</div></div></div>';
    },

    Progress: function(style, props, children) {
        var pct = props.percent || 0;
        var color = props.strokeColor || 'bg-blue-500';
        var statusText = props.status === 'exception' ? 'bg-red-500' : props.status === 'success' ? 'bg-green-500' : color;
        return '<div class="w-full bg-gray-200 rounded-full h-2 ' + style + '">' +
            '<div class="' + statusText + ' h-2 rounded-full transition-all" style="width:' + pct + '%"></div></div>';
    },

    Empty: function(style, props, children) {
        var icon = props.icon || 'fa-inbox';
        return '<div class="flex flex-col items-center justify-center py-12 ' + style + '">' +
            '<i class="fas ' + icon + ' text-4xl text-gray-300 mb-3"></i>' +
            '<p class="text-gray-400 text-sm">' + A2UIRenderer._esc(props.description || '暂无数据') + '</p></div>';
    },

    // ---- 逃生舱口 ----

    RawHtml: function(style, props, children) {
        return '<div class="' + style + '">' + (props.html || '') + '</div>';
    }
};
