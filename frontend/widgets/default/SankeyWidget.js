const { ref, computed, onMounted, onUnmounted, watch } = Vue;

// Data contract: an array of link row-dicts, e.g.
// [{source:'Total Findings', target:'Critical', value:320}, ...] — the same
// row-dict convention every other aggregate-backed widget uses. The YAML
// layout's sankey `data:` field names the aggregate whose query returns
// these rows directly (see dashboards/vuln_metrics.yaml's `sankey_*`
// aggregates for the pattern).
//
// source/target must be coerced to strings: ECharts' sankey series treats a
// *numeric* link.source/target as an INDEX into the `data` array rather than
// a name lookup (only strings are matched by node name) — a query grouping
// by an integer column (e.g. slb_true_risk) would otherwise produce links
// that silently point at the wrong node, or an out-of-range index, and the
// chart fails to render.
function deriveNodeNames(rows) {
    return Array.from(new Set(rows.flatMap(r => [String(r.source), String(r.target)])));
}

export default {
    name: 'SankeyWidget',
    props: {
        id:     { type: String, required: true },
        config: { type: Object, required: true },
        data:   { default: null },
        theme:  { type: String, default: 'light' }
    },
    setup(props) {
        const chartRef = ref(null);
        let chart = null;
        const resizeObserver = new ResizeObserver(() => chart?.resize());
        const hasError = ref(false);

        const isDataInvalid = computed(() => {
            if (!Array.isArray(props.data) || props.data.length === 0) return false;
            const row = props.data[0];
            return !('source' in row && 'target' in row && 'value' in row);
        });

        const render = () => {
            hasError.value = false;
            // The container div only exists in the v-else branch of the
            // template's v-if — when data goes empty, Vue unmounts it out
            // from under the live `chart` instance. Dispose here so a later
            // non-empty render re-inits on the fresh element instead of
            // silently updating an instance bound to a detached node.
            if (!Array.isArray(props.data) || props.data.length === 0 || isDataInvalid.value) {
                if (chart) { chart.dispose(); chart = null; }
                return;
            }
            if (!chartRef.value) return;
            const themeName = props.theme === 'dark' ? 'default-dark' : 'default-light';
            if (!chart) chart = echarts.init(chartRef.value, themeName);
            try {
                chart.setOption({
                    // appendToBody: the card's .widget-frame has overflow:hidden
                    // (theme.css/styles.css), which would otherwise clip the
                    // tooltip at the card edge. Rendering it under <body> as
                    // position:fixed escapes that clipping entirely.
                    tooltip: { trigger: 'item', triggerOn: 'mousemove', appendToBody: true },
                    series: [{
                        type: 'sankey', layout: 'none', emphasis: { focus: 'adjacency' }, nodeAlign: 'justify',
                        data: deriveNodeNames(props.data).map(n => ({ name: n })),
                        links: props.data.map(r => ({ ...r, source: String(r.source), target: String(r.target) })),
                        lineStyle: { color: 'gradient', curveness: 0.5, opacity: 0.5 },
                        itemStyle: { borderWidth: 1, borderColor: '#aaa' },
                        label: { fontWeight: 500 }
                    }]
                });
                resizeObserver.observe(chartRef.value);
            } catch (e) {
                console.error("ECharts rendering failed:", e);
                hasError.value = true;
                if (chart) { chart.dispose(); chart = null; }
            }
        };

        onMounted(render);
        onUnmounted(() => { resizeObserver.disconnect(); chart?.dispose(); });

        watch(() => props.data, render, { deep: true, flush: 'post' });
        watch(() => props.theme, () => { chart?.dispose(); chart = null; render(); }, { flush: 'post' });

        return { chartRef, isDataInvalid, hasError };
    },
    template: `
        <div class="widget-card chart-widget">
            <div class="widget-header"><span class="widget-title">{{ config.title }}</span></div>
            <div class="widget-body sankey-body">
                <div v-if="!data || data.length === 0" class="widget-unknown">No data</div>
                <div v-else-if="isDataInvalid" class="widget-unknown">Data format invalid</div>
                <div v-else-if="hasError" class="widget-unknown">Chart rendering failed</div>
                <div v-else ref="chartRef" style="width:100%;height:100%;"></div>
            </div>
        </div>
    `
};
