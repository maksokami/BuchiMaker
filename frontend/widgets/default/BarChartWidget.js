const { ref, computed, onMounted, onUnmounted, watch } = Vue;

// Aggregate rows arrive as row-dicts, e.g. [{region:'North', total:890}, ...].
// Convention: the first key is the category label, remaining numeric keys
// each become their own bar series (supports grouped bars for free).
function deriveSeriesShape(rows) {
    const keys = Object.keys(rows[0] || {});
    const labelKey = keys[0];
    const valueKeys = keys.slice(1).filter(k => typeof rows[0][k] === 'number');
    return {
        categories: rows.map(r => r[labelKey]),
        series: valueKeys.map(vk => ({
            name: vk,
            type: 'bar',
            data: rows.map(r => r[vk]),
            barMaxWidth: 32,
            itemStyle: { borderRadius: [4, 4, 0, 0] }
        }))
    };
}

export default {
    name: 'BarChartWidget',
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
            const valueKeys = keys.slice(1).filter(k => typeof props.data[0][k] === 'number');
            return valueKeys.length === 0;
        });

        // Guarded by v-if in the template, so this only ever runs once the
        // chart container div actually exists in the DOM.
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
            const { categories, series } = deriveSeriesShape(props.data);
            try {
                chart.setOption({
                    // appendToBody: the card's .widget-frame has overflow:hidden
                    // (theme.css/styles.css), which would otherwise clip the
                    // tooltip at the card edge. Rendering it under <body> as
                    // position:fixed escapes that clipping entirely.
                    tooltip: { trigger: 'axis', appendToBody: true },
                    legend: series.length > 1 ? { bottom: 0 } : undefined,
                    grid: { left: '3%', right: '4%', bottom: series.length > 1 ? '15%' : '3%', top: '5%', containLabel: true },
                    xAxis: { type: 'category', data: categories, axisLine: { show: false }, axisTick: { show: false } },
                    yAxis: { type: 'value', splitLine: { lineStyle: { type: 'dashed' } } },
                    series
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

        // flush:'post' — the v-if that swaps in the chart container div must
        // have already patched the DOM before render() runs, otherwise
        // chartRef.value would still be null (Vue's default watch flush is
        // 'pre', which fires before that patch).
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
