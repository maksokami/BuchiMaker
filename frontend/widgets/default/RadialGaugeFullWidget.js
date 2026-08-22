const { ref, computed, onMounted, onUnmounted, watch } = Vue;

export default {
    name: 'RadialGaugeFullWidget',
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
            if (props.data === null || props.data === undefined) return false;
            return Number.isNaN(parseFloat(props.data));
        });

        const render = () => {
            hasError.value = false;
            // The container div only exists in the v-else branch of the
            // template's v-if — when data goes null, Vue unmounts it out
            // from under the live `chart` instance. Dispose here so a later
            // non-null render re-inits on the fresh element instead of
            // silently updating an instance bound to a detached node.
            if (props.data === null || props.data === undefined || isDataInvalid.value) {
                if (chart) { chart.dispose(); chart = null; }
                return;
            }
            if (!chartRef.value) return;
            const themeName = props.theme === 'dark' ? 'default-dark' : 'default-light';
            if (!chart) chart = echarts.init(chartRef.value, themeName);
            const val = parseFloat(props.data) || 0;
            try {
                chart.setOption({
                    series: [{
                        type: 'gauge', startAngle: 90, endAngle: -270, min: 0, max: 100,
                        splitNumber: 1, radius: '90%', center: ['50%', '50%'],
                        pointer: { show: false },
                        progress: { show: true, overlap: false, roundCap: false, clip: false, itemStyle: { borderWidth: 0 } },
                        axisLine: { lineStyle: { width: 12 } },
                        splitLine: { show: false }, axisTick: { show: false }, axisLabel: { show: false },
                        title: { show: false },
                        detail: { valueAnimation: true, fontSize: 24, fontWeight: 700, formatter: '{value}%', offsetCenter: [0, 0] },
                        data: [{ value: val, name: 'Score' }]
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

        watch(() => props.data, render, { flush: 'post' });
        watch(() => props.theme, () => { chart?.dispose(); chart = null; render(); }, { flush: 'post' });

        return { chartRef, isDataInvalid, hasError };
    },
    template: `
        <div class="gauge-widget">
            <div class="gauge-widget-title">{{ config.label }}</div>
            <div class="widget-body gauge-widget-body">
                <div v-if="data === null || data === undefined" class="widget-unknown">No data</div>
                <div v-else-if="isDataInvalid" class="widget-unknown">Data format invalid</div>
                <div v-else-if="hasError" class="widget-unknown">Chart rendering failed</div>
                <div v-else ref="chartRef" style="width:100%;height:100%;"></div>
            </div>
        </div>
    `
};
