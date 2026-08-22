const { ref, computed, watch } = Vue;

export default {
    name: 'BasicTableWidget',
    props: {
        id:          { type: String, required: true },
        config:      { type: Object, required: true },
        data:        { default: null },
        theme:       { type: String, default: 'light' },
        dashboardId: { type: String, default: '' },
        apiBaseUrl:  { type: String, default: '' },
        filters:     { type: Object, default: () => ({}) }
    },
    emits: ['row-select'],
    setup(props, { emit }) {
        // `rows` is a local, appendable copy of props.data. props.data itself
        // is replaced wholesale (new array reference) whenever the dashboard
        // refetches on a filter change, so pagination state resets by
        // watching for that reference change rather than diffing contents.
        const rows = ref([]);
        const offset = ref(0);
        const totalCount = ref(null);
        const hasMore = ref(false);
        const isLoadingMore = ref(false);
        const loadMoreError = ref(null);

        const resetFromProps = () => {
            rows.value = props.data ? [...props.data] : [];
            offset.value = rows.value.length;
            totalCount.value = null;
            // Unknown until the first /page call; assume there could be more
            // whenever the initial batch is non-empty so the control renders.
            hasMore.value = rows.value.length > 0;
            loadMoreError.value = null;
        };
        watch(() => props.data, resetFromProps, { immediate: true });

        // Column order follows row-dict key order (server preserves query
        // column order); config.columns only ever overrides how specific
        // columns render (link cells), it doesn't define the full column set.
        const columns = computed(() => Object.keys(rows.value[0] || {}));

        const linkColumns = computed(() => {
            const map = {};
            (props.config.columns || []).forEach(c => { if (c.type === 'link') map[c.field] = c; });
            return map;
        });

        const onRowClick = (row) => emit('row-select', row);

        const pageSize = computed(() => Number(props.config.page_size) || 100);

        const loadMore = async () => {
            if (isLoadingMore.value || !hasMore.value) return;
            isLoadingMore.value = true;
            loadMoreError.value = null;
            try {
                const res = await fetch(`${props.apiBaseUrl}/dashboards/${props.dashboardId}/widgets/${props.id}/page`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ filters: { ...props.filters }, limit: pageSize.value, offset: offset.value })
                });
                if (!res.ok) throw new Error(`Failed to load more rows (${res.status})`);
                const payload = await res.json();
                const newRows = payload.rows || [];
                rows.value = rows.value.concat(newRows);
                offset.value += newRows.length;
                totalCount.value = typeof payload.total_count === 'number' ? payload.total_count : null;
                hasMore.value = !!payload.has_more;
            } catch (e) {
                console.error('Load more rows failed', e);
                loadMoreError.value = e.message;
            } finally {
                isLoadingMore.value = false;
            }
        };

        const isExporting = ref(false);
        const exportCsv = async () => {
            if (isExporting.value) return;
            isExporting.value = true;
            try {
                const res = await fetch(`${props.apiBaseUrl}/dashboards/${props.dashboardId}/widgets/${props.id}/export`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ filters: { ...props.filters } })
                });
                if (!res.ok) throw new Error(`Export failed (${res.status})`);
                const blob = await res.blob();
                const cd = res.headers.get('Content-Disposition') || '';
                const match = cd.match(/filename="?([^"]+)"?/);
                const filename = match ? match[1] : `export_${props.dashboardId}_${props.id}.csv`;

                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = filename;
                document.body.appendChild(a);
                a.click();
                a.remove();
                URL.revokeObjectURL(url);
            } catch (e) {
                console.error('Table export failed', e);
            } finally {
                isExporting.value = false;
            }
        };

        return {
            rows, columns, linkColumns, onRowClick, isExporting, exportCsv,
            hasMore, isLoadingMore, loadMore, totalCount, loadMoreError
        };
    },
    template: `
        <div class="widget-card table-widget">
            <div class="widget-header table-widget-header">
                <span class="widget-title">{{ config.title }}</span>
                <button class="table-export-btn" :disabled="isExporting" @click="exportCsv" title="Export as CSV">
                    <i class="fa-solid" :class="isExporting ? 'fa-spinner fa-spin' : 'fa-download'"></i>
                </button>
            </div>
            <div class="widget-body" style="overflow-y:auto;">
                <div v-if="rows.length === 0" class="widget-unknown">No data</div>
                <template v-else>
                    <table class="data-table">
                        <thead>
                            <tr><th v-for="col in columns" :key="col">{{ col }}</th></tr>
                        </thead>
                        <tbody>
                            <tr v-for="(row, ri) in rows" :key="ri" class="clickable-row" @click="onRowClick(row)">
                                <td v-for="col in columns" :key="col">
                                    <a v-if="linkColumns[col]" :href="row[linkColumns[col].url_field]"
                                       :target="linkColumns[col].target || '_blank'" @click.stop>{{ row[col] }}</a>
                                    <span v-else>{{ row[col] }}</span>
                                </td>
                            </tr>
                        </tbody>
                    </table>
                    <div v-if="hasMore" class="table-load-more">
                        <button class="table-load-more-btn" :disabled="isLoadingMore" @click="loadMore">
                            <i v-if="isLoadingMore" class="fa-solid fa-spinner fa-spin"></i>
                            <span v-else>Load more<span v-if="totalCount !== null"> ({{ rows.length }} of {{ totalCount }})</span></span>
                        </button>
                        <div v-if="loadMoreError" class="table-load-more-error">{{ loadMoreError }}</div>
                    </div>
                </template>
            </div>
        </div>
    `
};
