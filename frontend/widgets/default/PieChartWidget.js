const { ref, computed, onMounted, onUnmounted, watch } = Vue;

// Aggregate rows arrive as row-dicts, e.g. [{risk:'High', pct:78.42}, ...].
// Convention: first key = slice name, first numeric key = slice value.
function deriveSlices(rows) {
    const keys = Object.keys(rows[0] || {});
    const nameKey = keys[0];
    const valueKey = keys.slice(1).find(k => typeof rows[0][k] === 'number');
    return rows.map(r => ({ name: r[nameKey], value: r[valueKey] }));
}

export default {
    name: 'PieChartWidget',
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
            const keys = Object.keys(props.data[0] || {});
            const valueKey = keys.slice(1).find(k => typeof props.data[0][k] === 'number');
            return !valueKey;
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
                    tooltip: { trigger: 'item', appendToBody: true },
                    series: [{
                        type: 'pie',
                        radius: ['50%', '70%'],
                        avoidLabelOverlap: false,
                        itemStyle: { borderColor: '#fff', borderWidth: 2 },
                        label: { show: true, formatter: '{b}\n{d}%', fontWeight: 500 },
                        labelLine: { show: true, length: 15, length2: 10 },
                        data: deriveSlices(props.data)
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
            <div class="widget-body">
                <div v-if="!data || data.length === 0" class="widget-unknown">No data</div>
                <div v-else-if="isDataInvalid" class="widget-unknown">Data format invalid</div>
                <div v-else-if="hasError" class="widget-unknown">Chart rendering failed</div>
                <div v-else ref="chartRef" style="width:100%;height:100%;"></div>
            </div>
        </div>
    `
};
