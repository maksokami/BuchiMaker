const { ref, reactive, shallowRef, computed, onMounted, onBeforeUnmount, nextTick } = Vue;

// URL query params of the form f[widget_id]=value — see backend
// POST /dashboards/{id}/data docs. These are pinned/locked: they always win
// over a body filter with the same key, on every request, not just the first.
const parsePinnedFiltersFromUrl = () => {
    const params = new URLSearchParams(window.location.search);
    const parsed = {};
    for (const [key, value] of params.entries()) {
        const match = key.match(/^f\[(.+)\]$/);
        if (match) parsed[match[1]] = value;
    }
    return parsed;
};

export default {
    props: {
        dashboardId: { type: String, required: true },
        apiBaseUrl: { type: String, required: true }
    },
    setup(props) {
        const selectedRow = ref(null);
        const isAIChatOpen = ref(false);
        const chatMessages = ref([]);
        const newChatMessage = ref('');
        const chatMessagesContainer = ref(null);

        let grid = null;

        // dashboard-view is v-if'd (not keep-alive'd) in index.html and
        // remounts fresh whenever the sidebar dashboard selection or the
        // Settings-page theme toggle changes — so a one-time read here is
        // sufficient; no MutationObserver/reactivity is needed.
        const currentTheme = document.documentElement.getAttribute('data-theme') || 'light';

        const widgetRegistry = shallowRef({});
        const layout = ref([]);   // [{type, id, grid:{x,y,w,h,min_w,min_h}, config}]
        const gridColumns = ref(12); // from the YAML's optional `grid_columns` (12 or 24)
        const dashboardTitle = ref('Dashboard');
        const dashboardData = ref({});   // widget_id -> resolved value | rows | null
        const dataLoaded = ref(false);
        const loadError = ref(null); // whole-request failure only
        const filters = reactive({}); // widget_id -> value | {operator, value} | [{operator,value}, ...]
        const toggleGroups = reactive({}); // toggle_group name -> currently-active widget_id | null (button_datetime_filter exclusivity)
        const pinnedFilters = reactive({}); // widget_id -> value, from URL f[...] params — always re-sent, never folded into `filters`

        const pinnedFilterEntries = computed(() => Object.entries(pinnedFilters).map(([widgetId, value]) => {
            const widget = layout.value.find(w => w.id === widgetId);
            return { widgetId, value, label: widget?.config?.label || widgetId };
        }));
        const pinnedFilterSummary = computed(() => pinnedFilterEntries.value.map(e => `${e.label}: ${e.value}`).join(', '));

        const loadWidgetPackage = async (pkgName = 'default') => {
            const pkg = await import(`../widgets/${pkgName}/index.js`);
            widgetRegistry.value = pkg.default.components;
            document.querySelector('.dashboard-area')?.setAttribute('data-package', pkgName);
        };

        // The active widget set is a global system setting (Settings → Widgets)
        // rather than per-dashboard, so any widget set change takes effect the
        // next time a dashboard view is (re)mounted.
        const fetchActiveWidgetPackage = async () => {
            try {
                const res = await fetch(`${props.apiBaseUrl}/widgets`);
                if (!res.ok) return 'default';
                const widgetSets = await res.json();
                const active = widgetSets.find(w => w.active);
                return active ? active.id : 'default';
            } catch (e) {
                return 'default';
            }
        };
        const getComponent = (type) => widgetRegistry.value[type] || null;

        // `default_active: true` on a button_filter (grouped or standalone)
        // means its fixed {operator, value} condition should already be
        // applied on first load, before any click — e.g. the "ALL" button in
        // a toggle_group. This has to happen here (not inside the widget
        // component) because `filters`/`toggleGroups` are owned by
        // DashboardView, and the very first fetchData() call needs them
        // populated already; a widget emitting on mount would race the
        // initial fetch and also fire one extra redundant refetch.
        const seedDefaultFilters = () => {
            for (const widget of layout.value) {
                const cfg = widget.config || {};
                if (!cfg.default_active || widget.type !== 'button_filter') continue;
                const decl = {};
                (cfg.filter || []).forEach(entry => Object.assign(decl, entry));
                if (cfg.toggle_group) toggleGroups[cfg.toggle_group] = widget.id;
                filters[widget.id] = decl.operator ? { operator: decl.operator, value: decl.value } : decl.value;
            }
        };

        const fetchLayout = async () => {
            const res = await fetch(`${props.apiBaseUrl}/dashboards/${props.dashboardId}`);
            if (!res.ok) throw new Error(`Failed to load dashboard definition (${res.status})`);
            const def = await res.json();
            layout.value = def.layout || [];
            gridColumns.value = def.grid_columns || 12;
            dashboardTitle.value = def.name || 'Dashboard';
            seedDefaultFilters();

            // Only pin filters that target a widget actually on this dashboard —
            // a stale/bad link shouldn't brick the page, just drop the unknown key.
            Object.keys(pinnedFilters).forEach(k => delete pinnedFilters[k]);
            const layoutIds = new Set(layout.value.map(w => w.id));
            for (const [widgetId, value] of Object.entries(parsePinnedFiltersFromUrl())) {
                if (layoutIds.has(widgetId)) pinnedFilters[widgetId] = value;
                else console.warn(`Ignoring pinned filter "f[${widgetId}]" — no widget with that id on this dashboard.`);
            }
        };

        const fetchData = async () => {
            const qs = new URLSearchParams();
            for (const [widgetId, value] of Object.entries(pinnedFilters)) qs.set(`f[${widgetId}]`, value);
            const qsStr = qs.toString();
            const url = `${props.apiBaseUrl}/dashboards/${props.dashboardId}/data${qsStr ? '?' + qsStr : ''}`;
            const res = await fetch(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ filters: { ...filters } })
            });
            // fetch() decodes Content-Encoding: gzip transparently — no manual handling needed.
            if (!res.ok) throw new Error(`Failed to load dashboard data (${res.status})`);
            const payload = await res.json();
            const map = {};
            for (const entry of payload.widget_map || []) {
                if (entry.data_type === 'total') map[entry.widget_id] = payload.totals?.[entry.data_key] ?? null;
                else if (entry.data_type === 'raw') map[entry.widget_id] = payload.raw?.[entry.data_key] ?? [];
                else map[entry.widget_id] = payload.aggregates?.[entry.data_key] ?? [];
            }
            dashboardData.value = map;
            dataLoaded.value = true;
        };

        let refetchTimer = null;
        const refetchData = () => {
            clearTimeout(refetchTimer);
            refetchTimer = setTimeout(() => {
                fetchData().catch(e => { loadError.value = e.message; });
            }, 300);
        };

        const onWidgetFilterUpdate = ({ widgetId, value, operator, toggleGroup }) => {
            // button_datetime_filter: only one button per toggle_group may be
            // active — deactivate whichever sibling was active before.
            if (toggleGroup) {
                const prevActive = toggleGroups[toggleGroup];
                if (prevActive && prevActive !== widgetId) delete filters[prevActive];
                toggleGroups[toggleGroup] = (value === null || value === undefined) ? null : widgetId;
            }
            if (value === null || value === undefined || value === '') delete filters[widgetId];
            // `operator` falsy (null) means `value` is already a fully-formed
            // condition (or list of conditions) — e.g. widget_start_end_time
            // sends [{operator:'gte',value},{operator:'lte',value}] itself.
            else filters[widgetId] = operator ? { operator, value } : value;
            refetchData();
        };

        const retryLoad = async () => {
            loadError.value = null;
            try {
                if (layout.value.length === 0) {
                    await fetchLayout();
                    await nextTick();
                    if (!grid) grid = GridStack.init({ staticGrid: true, float: true, margin: 5, cellHeight: 80, column: gridColumns.value });
                }
                await fetchData();
            } catch (e) {
                loadError.value = e.message;
            }
        };

        // ── Row selection (right panel) ─────────────────────────────────────
        const selectRow = (widget, row) => {
            const panelCfg = widget.config['on-row-click-panel'];
            const keys = panelCfg?.fields?.length ? panelCfg.fields : Object.keys(row);
            const fields = keys.map(k => ({ key: k, value: row[k] }));
            selectedRow.value = { title: panelCfg?.title, fields, json: JSON.stringify(row, null, 2) };
        };
        const closeRightPanel = () => { selectedRow.value = null; };

        // ── AI Chat (placeholder, unrelated to widgets) ─────────────────────
        const toggleAIChat = () => { isAIChatOpen.value = !isAIChatOpen.value; };
        const scrollToBottom = () => {
            nextTick(() => { if (chatMessagesContainer.value) chatMessagesContainer.value.scrollTop = chatMessagesContainer.value.scrollHeight; });
        };
        const sendChatMessage = () => {
            if (!newChatMessage.value.trim()) return;
            chatMessages.value.push({ role: 'user', content: newChatMessage.value });
            const userMsg = newChatMessage.value;
            newChatMessage.value = '';
            scrollToBottom();
            setTimeout(() => {
                chatMessages.value.push({ role: 'ai', content: `I am a placeholder AI. You said: "${userMsg}". Later I will be linked to the backend API.` });
                scrollToBottom();
            }, 1000);
        };

        const copyToClipboard = async (text) => {
            try { await navigator.clipboard.writeText(text); } catch (e) { console.error('Copy failed', e); }
        };

        // ── Lifecycle ──────────────────────────────────────────────────────
        onMounted(async () => {
            const pkgName = await fetchActiveWidgetPackage();
            await loadWidgetPackage(pkgName);
            try {
                await fetchLayout();
                await nextTick(); // GridStack reads gs-x/gs-y/... attrs from the DOM at init time
                grid = GridStack.init({ staticGrid: true, float: true, margin: 5, cellHeight: 80, column: gridColumns.value });
                await fetchData();
            } catch (e) {
                loadError.value = e.message;
            }
        });

        onBeforeUnmount(() => {
            if (grid) { grid.destroy(false); grid = null; }
            clearTimeout(refetchTimer);
        });

        return {
            layout, dashboardTitle, dashboardData, dataLoaded, loadError, currentTheme,
            pinnedFilterEntries, pinnedFilterSummary,
            getComponent, onWidgetFilterUpdate, retryLoad,
            selectedRow, selectRow, closeRightPanel, copyToClipboard,
            isAIChatOpen, chatMessages, newChatMessage, chatMessagesContainer,
            toggleAIChat, sendChatMessage, toggleGroups
        };
    },
    template: `
        <main class="main-content">
            <header class="topbar">
                <div class="page-title"><h1>{{ dashboardTitle }}</h1></div>
                <div class="topbar-actions">
                    <button class="action-btn primary" @click="toggleAIChat"><i class="fa-solid fa-wand-magic-sparkles"></i> AI Chat</button>
                </div>
            </header>

            <div v-if="pinnedFilterEntries.length" class="pinned-filter-banner">
                <i class="fa-solid fa-lock"></i>
                <span v-if="pinnedFilterEntries.length === 1">This link filters by <strong>{{ pinnedFilterSummary }}</strong>. It was set by the page link and can't be changed using the filters below.</span>
                <span v-else>This link applies fixed filters — <strong>{{ pinnedFilterSummary }}</strong> — set by the page link. They override the filters below.</span>
            </div>

            <div class="dashboard-area">

                <div v-if="loadError" class="dashboard-load-error">
                    <div class="error-icon"><i class="fa-solid fa-triangle-exclamation"></i></div>
                    <div class="error-message">{{ loadError }}</div>
                    <button @click="retryLoad">Retry</button>
                </div>

                <div class="grid-stack" id="dashboard-grid" v-show="!loadError">
                    <div v-for="widget in layout" :key="widget.id" class="grid-stack-item"
                         :gs-x="widget.grid.x" :gs-y="widget.grid.y" :gs-w="widget.grid.w" :gs-h="widget.grid.h"
                         :gs-min-w="widget.grid.min_w" :gs-min-h="widget.grid.min_h" :gs-id="widget.id">
                        <div class="grid-stack-item-content widget-frame" :data-widget-type="widget.type"
                             :style="widget.config.bg ? { '--tile-bg': widget.config.bg } : {}">

                            <!-- 1. Universal loading skeleton -->
                            <div v-if="!dataLoaded" class="widget-loader">
                                <div class="pulse-skeleton-header"></div>
                                <div class="pulse-skeleton-body"></div>
                            </div>

                            <!-- 2. Registered widget component -->
                            <error-boundary v-else-if="getComponent(widget.type)">
                                <component
                                    :is="getComponent(widget.type)"
                                    :id="widget.id"
                                    :config="widget.config"
                                    :data="dashboardData[widget.id] ?? null"
                                    :theme="currentTheme"
                                    :active-group-id="widget.config.toggle_group ? (toggleGroups[widget.config.toggle_group] || null) : null"
                                    :dashboard-id="widget.type === 'basic_table' ? dashboardId : undefined"
                                    :api-base-url="widget.type === 'basic_table' ? apiBaseUrl : undefined"
                                    :filters="widget.type === 'basic_table' ? filters : undefined"
                                    @row-select="row => selectRow(widget, row)"
                                    @update:filter="onWidgetFilterUpdate" />
                            </error-boundary>

                            <!-- 3. Unregistered/deferred widget type -->
                            <div v-else class="widget-unknown">Unsupported widget type: {{ widget.type }}</div>

                        </div>
                    </div>
                </div>
            </div>

            <!-- Right Panel -->
            <aside class="right-panel" v-if="selectedRow">
                <div class="right-panel-header">
                    <h2>{{ selectedRow.title || 'Row Details' }}</h2>
                    <button class="close-btn" @click="closeRightPanel" aria-label="Close Panel"><i class="fa-solid fa-xmark"></i></button>
                </div>
                <div class="right-panel-content">
                    <div class="detail-field" v-for="(field, index) in selectedRow.fields" :key="index">
                        <label>{{ field.key }}</label>
                        <div class="input-group">
                            <input type="text" readonly :value="field.value">
                            <button class="copy-btn" @click="copyToClipboard(field.value)" title="Copy to clipboard"><i class="fa-regular fa-copy"></i></button>
                        </div>
                    </div>
                    <div class="json-box-container" style="margin-top:32px;">
                        <div class="json-header">
                            <span>JSON</span>
                            <button class="copy-btn" @click="copyToClipboard(selectedRow.json)" title="Copy JSON"><i class="fa-regular fa-copy"></i></button>
                        </div>
                        <pre class="json-content">{{ selectedRow.json }}</pre>
                    </div>
                </div>
            </aside>

            <!-- AI Chat Panel -->
            <aside class="ai-chat-panel" :class="{ 'open': isAIChatOpen }">
                <div class="ai-chat-header">
                    <h2><i class="fa-solid fa-robot"></i> AI Assistant</h2>
                    <button class="close-btn" @click="toggleAIChat" aria-label="Close AI Chat"><i class="fa-solid fa-xmark"></i></button>
                </div>
                <div class="ai-chat-content">
                    <div class="ai-chat-messages" ref="chatMessagesContainer">
                        <div class="chat-message ai">
                            <div class="msg-avatar"><i class="fa-solid fa-robot"></i></div>
                            <div class="msg-bubble">Hello! I'm your BuchiMaker AI Assistant. How can I help you today?</div>
                        </div>
                        <div v-for="(msg, index) in chatMessages" :key="index" :class="['chat-message', msg.role]">
                            <div class="msg-avatar"><i :class="msg.role === 'user' ? 'fa-solid fa-user' : 'fa-solid fa-robot'"></i></div>
                            <div class="msg-bubble">{{ msg.content }}</div>
                        </div>
                    </div>
                    <div class="ai-chat-input-area">
                        <input type="text" v-model="newChatMessage" @keyup.enter="sendChatMessage" placeholder="Ask AI something..." class="ai-chat-input">
                        <button class="send-btn" @click="sendChatMessage" aria-label="Send Message"><i class="fa-solid fa-paper-plane"></i></button>
                    </div>
                </div>
            </aside>
        </main>
    `
};
