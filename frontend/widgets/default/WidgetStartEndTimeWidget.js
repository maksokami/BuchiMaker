const { ref, onMounted } = Vue;

// Two datetime-local pickers filtering the same `mapping` column with `gte`
// (start) / `lte` (end). Sends both as one multi-condition payload — see the
// `parse_widget_filters` note in dashboard.py about widgets that emit a list
// of {operator, value} conditions instead of a single one. Per the YAML spec
// comment, `end` defaults to "now" on page load.
function toLocalInputValue(date) {
    const pad = n => String(n).padStart(2, '0');
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export default {
    name: 'WidgetStartEndTimeWidget',
    props: {
        id:     { type: String, required: true },
        config: { type: Object, required: true },
        data:   { default: null },
        theme:  { type: String, default: 'light' }
    },
    emits: ['update:filter'],
    setup(props, { emit }) {
        const start = ref('');
        const end = ref(toLocalInputValue(new Date()));

        const emitUpdate = () => {
            const conditions = [];
            if (start.value) conditions.push({ operator: 'gte', value: start.value });
            if (end.value) conditions.push({ operator: 'lte', value: end.value });
            // operator:null tells DashboardView this is already a finished
            // condition list, not a single value to wrap in {operator,value}.
            emit('update:filter', { widgetId: props.id, value: conditions.length ? conditions : null, operator: null });
        };

        onMounted(emitUpdate);

        const clearStart = () => { start.value = ''; emitUpdate(); };
        const clearEnd = () => { end.value = ''; emitUpdate(); };

        return { start, end, emitUpdate, clearStart, clearEnd };
    },
    template: `
        <div class="start-end-time-widget">
            <label class="input-filter-label" v-if="config.label">{{ config.label }}</label>
            <div class="time-range-row">
                <div class="time-field">
                    <input class="time-input" type="datetime-local" v-model="start" @change="emitUpdate">
                    <button class="input-filter-clear time-field-clear" title="Clear start" @click="clearStart"><i class="fa-solid fa-eraser"></i></button>
                </div>
                <i class="fa-solid fa-arrow-right-long time-range-sep"></i>
                <div class="time-field">
                    <input class="time-input" type="datetime-local" v-model="end" @change="emitUpdate">
                    <button class="input-filter-clear time-field-clear" title="Clear end" @click="clearEnd"><i class="fa-solid fa-eraser"></i></button>
                </div>
            </div>
        </div>
    `
};
