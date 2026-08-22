const { ref, reactive, computed, onMounted, watch } = Vue;

export default {
    props: {
        apiBaseUrl: { type: String, required: true }
    },
    setup(props) {
        const logs = ref([]);
        const total = ref(0);
        const page = ref(1);
        const pageSize = ref(10);
        const searchTerm = ref('');
        const isLoading = ref(false);
        const errorMessage = ref('');

        const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize.value)));
        const showingFrom = computed(() => (total.value === 0 ? 0 : (page.value - 1) * pageSize.value + 1));
        const showingTo = computed(() => Math.min(page.value * pageSize.value, total.value));

        // Up to 5 page-number buttons, centered on the current page and
        // clamped to the valid [1, totalPages] range.
        const visiblePages = computed(() => {
            const tp = totalPages.value;
            let start = Math.max(1, page.value - 2);
            let end = Math.min(tp, start + 4);
            start = Math.max(1, end - 4);
            const pages = [];
            for (let p = start; p <= end; p++) pages.push(p);
            return pages;
        });

        const buildParams = (forExport) => {
            const params = new URLSearchParams();
            if (searchTerm.value.trim()) params.set('search', searchTerm.value.trim());
            if (!forExport) {
                params.set('limit', String(pageSize.value));
                params.set('offset', String((page.value - 1) * pageSize.value));
            }
            return params;
        };

        const fetchLogs = async () => {
            isLoading.value = true;
            errorMessage.value = '';
            try {
                const res = await fetch(`${props.apiBaseUrl}/system/audit-logs?${buildParams(false)}`);
                if (res.ok) {
                    const data = await res.json();
                    logs.value = data.rows;
                    total.value = data.total;
                } else {
                    const err = await res.json().catch(() => ({}));
                    errorMessage.value = err.detail || `Failed to load audit logs (HTTP ${res.status}).`;
                }
            } catch (e) {
                errorMessage.value = 'Network error loading audit logs.';
            } finally {
                isLoading.value = false;
            }
        };

        let searchDebounce = null;
        const onSearchInput = () => {
            clearTimeout(searchDebounce);
            searchDebounce = setTimeout(() => {
                page.value = 1;
                fetchLogs();
            }, 300);
        };

        const onPageSizeChange = () => {
            page.value = 1;
            fetchLogs();
        };

        const goToPage = (n) => {
            if (n < 1 || n > totalPages.value || n === page.value) return;
            page.value = n;
            fetchLogs();
        };

        const exportCsv = () => {
            window.location.href = `${props.apiBaseUrl}/system/audit-logs/export?${buildParams(true)}`;
        };

        const formatResponseTime = (ms) => `${ms} ms`;

        onMounted(fetchLogs);

        return {
            logs, total, page, pageSize, searchTerm, isLoading, errorMessage,
            totalPages, showingFrom, showingTo, visiblePages,
            onSearchInput, onPageSizeChange, goToPage, exportCsv, formatResponseTime,
        };
    },
    template: `
        <div class="settings-page">
            <header class="settings-header">
                <h1>Audit Logs</h1>
            </header>
            <div class="settings-content" style="padding-top: 24px;">
                <div class="audit-controls">
                    <div class="audit-length">
                        <label style="font-weight:600;">Show
                            <select v-model.number="pageSize" @change="onPageSizeChange">
                                <option :value="10">10</option>
                                <option :value="25">25</option>
                                <option :value="50">50</option>
                                <option :value="100">100</option>
                            </select> entries
                        </label>
                    </div>
                    <div class="audit-export">
                        <button class="btn btn-outline" style="padding: 4px 12px; font-weight: 500;" @click="exportCsv">
                            CSV <i class="fa-solid fa-download" style="margin-left: 6px;"></i>
                        </button>
                    </div>
                    <div class="audit-search">
                        <label style="display:flex; align-items:center; font-weight:600;">Search:
                            <div style="position:relative; margin-left:8px;">
                                <input type="text" class="settings-input" style="border-radius: 16px; padding: 4px 28px 4px 12px; width: 160px; height: 32px;"
                                       v-model="searchTerm" @input="onSearchInput" placeholder="path or IP" />
                                <i class="fa-solid fa-magnifying-glass" style="position:absolute; right:10px; top:50%; transform:translateY(-50%); color:var(--text-muted); font-size:0.8rem;"></i>
                            </div>
                        </label>
                    </div>
                </div>

                <div v-if="errorMessage" style="color: var(--danger); padding: 8px 4px; font-weight: 500;">
                    {{ errorMessage }}
                </div>

                <div class="audit-table-wrapper">
                    <table class="audit-table">
                        <thead>
                            <tr>
                                <th>TIME</th>
                                <th>METHOD</th>
                                <th>STATUS</th>
                                <th>REQUEST PATH</th>
                                <th>USER EMAIL</th>
                                <th>IP ADDRESS</th>
                                <th>RESPONSE TIME</th>
                            </tr>
                        </thead>
                        <tbody>
                            <tr v-if="isLoading">
                                <td colspan="7" class="table-empty-state">
                                    <i class="fa-solid fa-circle-notch fa-spin"></i> Loading…
                                </td>
                            </tr>
                            <tr v-else-if="logs.length === 0">
                                <td colspan="7" class="table-empty-state">
                                    <span>No matching audit log entries.</span>
                                </td>
                            </tr>
                            <tr v-for="log in logs" :key="log.id" v-show="!isLoading">
                                <td><span style="font-weight:600; font-size: 0.8rem;">{{ log.ts }}</span></td>
                                <td>{{ log.method }}</td>
                                <td>{{ log.status_code }}</td>
                                <td class="req-path">{{ log.path }}</td>
                                <td style="font-weight:500;">{{ log.user_email || '—' }}</td>
                                <td>{{ log.client_ip || '—' }}</td>
                                <td>{{ formatResponseTime(log.duration_ms) }}</td>
                            </tr>
                        </tbody>
                    </table>
                </div>

                <div class="audit-footer">
                    <div class="audit-info">
                        Showing {{ showingFrom }} to {{ showingTo }} of {{ total }} entries
                    </div>
                    <div class="audit-pagination">
                        <button class="page-btn nav-btn" :disabled="page <= 1" @click="goToPage(page - 1)">Previous</button>
                        <button v-for="p in visiblePages" :key="p" class="page-btn" :class="{ active: p === page }" @click="goToPage(p)">{{ p }}</button>
                        <button class="page-btn nav-btn" :disabled="page >= totalPages" @click="goToPage(page + 1)">Next</button>
                    </div>
                </div>
            </div>
        </div>
    `
};
