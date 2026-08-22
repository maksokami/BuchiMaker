const { computed } = Vue;

export default {
    name: 'TileWidget',
    props: {
        id:     { type: String, required: true },
        config: { type: Object, required: true },
        data:   { default: null },
        theme:  { type: String, default: 'light' }
    },
    setup(props) {
        const displayValue = computed(() => {
            if (props.data === null || props.data === undefined || props.data === '') return '—';
            if (typeof props.data === 'number') return props.data.toLocaleString();
            return props.data;
        });
        return { displayValue };
    },
    template: `
        <div class="tile-widget">
            <div class="tile-value">{{ displayValue }}</div>
            <div class="tile-label">{{ config.label }}</div>
        </div>
    `
};
